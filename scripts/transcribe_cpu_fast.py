#!/usr/bin/env python3
"""Optimized multi-worker batched CPU transcription for faster-whisper on 8-core / 32GB cloud nodes."""

from __future__ import annotations

import argparse
import concurrent.futures
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HOME", "/mnt/workspace/models/hf_cache")

from faster_whisper import BatchedInferencePipeline, WhisperModel  # noqa: E402

MEDIA_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".webm", ".mp4", ".mov", ".mkv"}

DEFAULT_LOCAL_MODEL_DIR = Path("/mnt/workspace/models/faster-whisper/large-v3-turbo")
DEFAULT_MODEL_CACHE_DIR = Path("/mnt/workspace/models/faster-whisper")

_WORKER_MODEL: WhisperModel | None = None
_WORKER_BATCHED: BatchedInferencePipeline | None = None
_WORKER_CFG: dict = {}


def probe_duration(path: Path) -> float:
    """Return media duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(res.stdout.strip())


def convert_to_16k_wav(src: Path, dst: Path) -> None:
    """Convert audio/video to 16kHz mono 16-bit PCM WAV via ffmpeg."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def write_text_atomic(path: Path, content: str) -> None:
    """Write text safely via temp file + rename so interrupted runs stay resume-safe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def init_worker(
    model_ref: str,
    model_cache_dir: str,
    threads_per_worker: int,
    language: str,
    beam_size: int,
    batch_size: int,
    tail_gap_warn: float,
    work_dir: str,
) -> None:
    """Load WhisperModel + BatchedInferencePipeline once per worker process."""
    global _WORKER_MODEL, _WORKER_BATCHED, _WORKER_CFG
    os.environ["OMP_NUM_THREADS"] = str(threads_per_worker)
    os.environ["MKL_NUM_THREADS"] = str(threads_per_worker)

    pid = os.getpid()
    print(f"[Worker {pid}] 加载模型 {model_ref} (int8, threads={threads_per_worker})...", flush=True)
    _WORKER_MODEL = WhisperModel(
        model_ref,
        device="cpu",
        compute_type="int8",
        cpu_threads=threads_per_worker,
        download_root=model_cache_dir,
    )
    try:
        _WORKER_BATCHED = BatchedInferencePipeline(model=_WORKER_MODEL)
        print(f"[Worker {pid}] BatchedInferencePipeline 就绪 (batch_size={batch_size}, beam_size={beam_size})", flush=True)
    except Exception as exc:
        _WORKER_BATCHED = None
        print(f"[Worker {pid}] BatchedInferencePipeline 不可用，回退标准模式: {exc}", flush=True)

    _WORKER_CFG = {
        "language": None if language == "auto" else language,
        "beam_size": beam_size,
        "batch_size": batch_size,
        "tail_gap_warn": tail_gap_warn,
        "work_dir": Path(work_dir),
    }


def transcribe_one(task: tuple[int, int, str, str, str]) -> tuple[str, str | None]:
    """Process one audio file inside a worker process."""
    idx, total, src_str, txt_str, ts_str = task
    src = Path(src_str)
    txt_path = Path(txt_str)
    ts_path = Path(ts_str)
    work_dir: Path = _WORKER_CFG["work_dir"]
    wav_path = work_dir / f"{os.getpid()}_{src.stem}.wav"
    pid = os.getpid()
    t0 = time.time()

    print(f"[{idx}/{total}] [Worker {pid}] 开始: {src.name}", flush=True)
    try:
        dur = probe_duration(src)
        convert_to_16k_wav(src, wav_path)

        lang = _WORKER_CFG["language"]
        beam = _WORKER_CFG["beam_size"]
        batch = _WORKER_CFG["batch_size"]

        if _WORKER_BATCHED is not None:
            try:
                segments, info = _WORKER_BATCHED.transcribe(
                    str(wav_path),
                    language=lang,
                    beam_size=beam,
                    batch_size=batch,
                )
                seg_list = list(segments)
            except Exception as exc:
                print(f"[{idx}/{total}] [Worker {pid}] 批处理异常，切换标准推理: {exc}", flush=True)
                segments, info = _WORKER_MODEL.transcribe(
                    str(wav_path),
                    language=lang,
                    beam_size=beam,
                    vad_filter=True,
                )
                seg_list = list(segments)
        else:
            segments, info = _WORKER_MODEL.transcribe(
                str(wav_path),
                language=lang,
                beam_size=beam,
                vad_filter=True,
            )
            seg_list = list(segments)

        plain_lines: list[str] = []
        ts_lines: list[str] = []
        last_end = 0.0
        for seg in seg_list:
            text = seg.text.strip()
            if not text:
                continue
            plain_lines.append(text)
            ts_lines.append(f"[{seg.start:.2f} -> {seg.end:.2f}] {text}")
            if seg.end > last_end:
                last_end = seg.end

        write_text_atomic(txt_path, "\n".join(plain_lines) + "\n")
        write_text_atomic(ts_path, "\n".join(ts_lines) + "\n")

        elapsed = time.time() - t0
        speed = (dur / elapsed) if elapsed > 0 else 0.0
        tail_gap = max(0.0, dur - last_end)
        det_lang = getattr(info, "language", "unknown")
        print(
            f"[{idx}/{total}] [Worker {pid}] 完成: {src.name} | 语言 {det_lang} | "
            f"时长 {dur:.1f}s | 末段 {last_end:.1f}s | 尾部差 {tail_gap:.1f}s | "
            f"耗时 {elapsed:.1f}s ({speed:.2f}x 实时)",
            flush=True,
        )

        warn_msg = None
        if not plain_lines or tail_gap > _WORKER_CFG["tail_gap_warn"]:
            warn_msg = (
                f"[WARN] {src.name}: duration={dur:.1f}s, last_end={last_end:.1f}s, "
                f"tail_gap={tail_gap:.1f}s, segments={len(plain_lines)}"
            )
            print(warn_msg, flush=True)
        return src.name, warn_msg
    finally:
        if wav_path.exists():
            wav_path.unlink()


def parse_args() -> argparse.Namespace:
    default_project = Path(__file__).resolve().parent
    default_model = (
        str(DEFAULT_LOCAL_MODEL_DIR) if DEFAULT_LOCAL_MODEL_DIR.is_dir() else "large-v3-turbo"
    )
    parser = argparse.ArgumentParser(description="魔搭 8核 CPU 多路批处理极速语音转写脚本")
    parser.add_argument("--project-dir", type=str, default=str(default_project), help="项目根目录（含 audio/ 与 逐字稿/）")
    parser.add_argument("--input-dir", type=str, default=None, help="音频输入目录（默认 <project-dir>/audio）")
    parser.add_argument("--output-dir", type=str, default=None, help="逐字稿输出目录（默认 <project-dir>/逐字稿）")
    parser.add_argument("--work-dir", type=str, default=None, help="临时 wav 目录（默认 <project-dir>/.work，勿用 /tmp）")
    parser.add_argument("--model-cache-dir", type=str, default=str(DEFAULT_MODEL_CACHE_DIR))
    parser.add_argument("--model-size", type=str, default=default_model)
    parser.add_argument("--workers", type=int, default=2, help="并发进程数（8核推荐 2）")
    parser.add_argument("--threads-per-worker", type=int, default=4, help="每进程 CPU 线程数")
    parser.add_argument("--batch-size", type=int, default=4, help="BatchedInferencePipeline 批大小")
    parser.add_argument("--beam-size", type=int, default=1, help="Beam 宽度（1 最快）")
    parser.add_argument("--language", type=str, default="zh", help="语言代码（默认 zh，可设为 auto 自动检测）")
    parser.add_argument("--tail-gap-warn", type=float, default=8.0)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_dir = Path(args.project_dir).resolve()
    input_dir = Path(args.input_dir).resolve() if args.input_dir else (project_dir / "audio")
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (project_dir / "逐字稿")
    work_dir = Path(args.work_dir).resolve() if args.work_dir else (project_dir / ".work")
    model_cache_dir = Path(args.model_cache_dir).resolve()

    if not input_dir.is_dir():
        raise SystemExit(f"错误: 输入目录不存在: {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    model_cache_dir.mkdir(parents=True, exist_ok=True)

    for stale in list(work_dir.glob("*.wav")) + list(output_dir.glob("*.part")):
        stale.unlink(missing_ok=True)

    media_files = sorted(
        p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS
    )
    if not media_files:
        raise SystemExit(f"错误: {input_dir} 下未找到音视频文件。")

    pending: list[tuple[int, int, str, str, str]] = []
    total = len(media_files)
    for idx, src in enumerate(media_files, start=1):
        txt_path = output_dir / f"{src.stem}.txt"
        ts_path = output_dir / f"{src.stem}.timestamps.txt"
        if (
            not args.overwrite
            and txt_path.is_file()
            and txt_path.stat().st_size > 0
            and ts_path.is_file()
            and ts_path.stat().st_size > 0
        ):
            print(f"[{idx}/{total}] 已存在，跳过: {src.name}", flush=True)
            continue
        pending.append((idx, total, str(src), str(txt_path), str(ts_path)))

    if not pending:
        print("所有文件均已转写完成！")
        return

    print(
        f"共 {total} 个文件，待转写 {len(pending)} 个 | "
        f"workers={args.workers}, threads={args.threads_per_worker}, "
        f"batch_size={args.batch_size}, beam_size={args.beam_size}, lang={args.language}",
        flush=True,
    )

    t_start = time.time()
    warn_lines: list[str] = []
    failures: list[tuple[str, str]] = []

    mp_ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=mp_ctx,
        initializer=init_worker,
        initargs=(
            args.model_size,
            str(model_cache_dir),
            args.threads_per_worker,
            args.language,
            args.beam_size,
            args.batch_size,
            args.tail_gap_warn,
            str(work_dir),
        ),
    ) as executor:
        future_map = {executor.submit(transcribe_one, task): Path(task[2]).name for task in pending}
        for future in concurrent.futures.as_completed(future_map):
            name = future_map[future]
            try:
                _, warn_msg = future.result()
                if warn_msg:
                    warn_lines.append(warn_msg)
            except Exception as exc:
                print(f"[ERROR] {name}: {exc}", file=sys.stderr, flush=True)
                failures.append((name, str(exc)))

    if warn_lines:
        warnings_log = output_dir / "_warnings.log"
        prev = warnings_log.read_text(encoding="utf-8") if warnings_log.is_file() else ""
        write_text_atomic(warnings_log, prev + "\n".join(warn_lines) + "\n")

    print(
        f"\n全部结束！总耗时 {time.time() - t_start:.1f}s | "
        f"成功 {len(pending) - len(failures)}/{len(pending)} | 告警 {len(warn_lines)} 条",
        flush=True,
    )
    if failures:
        for name, err in failures:
            print(f"  - {name}: {err}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
