#!/usr/bin/env python3
"""Download media (or scan local media), convert to 16kHz mono Opus, and pack <45 MiB upload zips."""

from __future__ import annotations

import argparse
import concurrent.futures
import subprocess
import zipfile
from pathlib import Path

MEDIA_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".webm", ".mp4", ".mov", ".mkv"}
SCRIPTS_DIR = Path(__file__).resolve().parent


def download_from_url(url: str, audio_dir: Path, browser: str | None, lang_prefix: str) -> None:
    """Download audio-only tracks and metadata JSONs via yt-dlp."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    fmt = (
        f"bestaudio[language^={lang_prefix}][ext=m4a]/"
        f"bestaudio[language^={lang_prefix}]/"
        "bestaudio[ext=m4a]/bestaudio"
    )
    cmd = [
        "yt-dlp",
        "-f",
        fmt,
        "--write-info-json",
        "--no-overwrites",
        "--download-archive",
        str(audio_dir.parent / "downloaded.txt"),
        "-o",
        str(audio_dir / "%(upload_date)s_%(id)s_%(title).60s.%(ext)s"),
    ]
    if browser:
        cmd.extend(["--cookies-from-browser", browser])
    cmd.append(url)

    print("开始拉取纯音频:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def convert_to_16k_opus(src: Path, dst: Path) -> tuple[str, int]:
    """Convert one media file to 16kHz mono 32k Opus .ogg."""
    if not (dst.is_file() and dst.stat().st_size > 0):
        subprocess.run(
            [
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
                "libopus",
                "-b:a",
                "32k",
                str(dst),
            ],
            check=True,
        )
    return dst.name, dst.stat().st_size


def pack_under_limit(
    ogg_files: list[Path],
    info_source_dir: Path,
    bundles_dir: Path,
    max_bytes: int,
) -> list[Path]:
    """Pack 16kHz Opus files (+ cloud scripts in part1) into zips strictly under max_bytes."""
    bundles_dir.mkdir(parents=True, exist_ok=True)
    for old in bundles_dir.glob("speech_part*.zip"):
        old.unlink()

    created: list[Path] = []
    part_idx = 1
    current_files: list[tuple[Path, str, int]] = []
    current_size = 0

    def add_cloud_scripts(zf: zipfile.ZipFile) -> None:
        for script_name in ("setup_asr.sh", "transcribe_cpu_fast.py"):
            sp = SCRIPTS_DIR / script_name
            if sp.is_file():
                zf.write(sp, arcname=script_name, compress_type=zipfile.ZIP_DEFLATED)

    # Reserve ~50 KB for cloud scripts in part 1
    script_reserve = 50 * 1024

    def flush_part(idx: int, items: list[tuple[Path, str, int]]) -> Path:
        zpath = bundles_dir / f"speech_part{idx}.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            if idx == 1:
                add_cloud_scripts(zf)
            for path, arcname, comp_type in items:
                zf.write(path, arcname=arcname, compress_type=comp_type)
        actual = zpath.stat().st_size
        assert actual <= max_bytes, f"{zpath.name} ({actual} B) exceeds limit ({max_bytes} B)"
        print(f"已打包 {zpath.name}: {actual / (1024 * 1024):.1f} MB ({len(items)} 项)")
        return zpath

    for ogg in ogg_files:
        sz = ogg.stat().st_size
        if sz + (script_reserve if part_idx == 1 else 0) > max_bytes:
            raise ValueError(f"单条压缩音频过大 ({ogg.name}: {sz} B)，无法装入 {max_bytes} B 限制内。")

        limit = max_bytes - (script_reserve if part_idx == 1 else 0) - 65536
        if current_files and current_size + sz > limit:
            created.append(flush_part(part_idx, current_files))
            part_idx += 1
            current_files = []
            current_size = 0

        current_files.append((ogg, f"audio/{ogg.name}", zipfile.ZIP_STORED))
        current_size += sz

        info_json = info_source_dir / f"{ogg.stem}.info.json"
        if info_json.is_file() and info_json.stat().st_size < 1024 * 1024:
            current_files.append((info_json, f"audio/{info_json.name}", zipfile.ZIP_DEFLATED))
            current_size += info_json.stat().st_size // 2

    if current_files:
        created.append(flush_part(part_idx, current_files))

    return created


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拉取/转换音视频为 16kHz Opus 并自动分卷打包 (<45 MiB)")
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--url", type=str, help="YouTube 频道或播放列表链接")
    src_group.add_argument("--input-dir", type=str, help="本地音视频目录路径")
    parser.add_argument("--out-dir", type=str, required=True, help="项目输出根目录")
    parser.add_argument("--cookies-from-browser", type=str, default=None, help="yt-dlp 读取登录态的浏览器 (edge/chrome/safari)")
    parser.add_argument("--lang-prefix", type=str, default="zh", help="优先选择的原声音轨语言前缀")
    parser.add_argument("--workers", type=int, default=4, help="ffmpeg 并行转换数")
    parser.add_argument("--max-part-mb", type=int, default=44, help="每个分卷包最大体积 (MB)，默认 44 MB 以避开 50 MiB 限制")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir).resolve()
    raw_dir = Path(args.input_dir).resolve() if args.input_dir else (out_dir / "audio")
    ogg_dir = out_dir / "audio_16k"
    bundles_dir = out_dir / "bundles"
    ogg_dir.mkdir(parents=True, exist_ok=True)

    if args.url:
        download_from_url(args.url, raw_dir, args.cookies_from_browser, args.lang_prefix)

    media_files = sorted(
        p for p in raw_dir.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS
    )
    if not media_files:
        raise SystemExit(f"错误: {raw_dir} 中未找到音视频文件。")

    print(f"发现 {len(media_files)} 个媒体文件，开始并行转 16kHz 单声道 Opus...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [
            ex.submit(convert_to_16k_opus, src, ogg_dir / f"{src.stem}.ogg")
            for src in media_files
        ]
        total_bytes = sum(f.result()[1] for f in futures)

    ogg_files = sorted(ogg_dir.glob("*.ogg"))
    print(f"转换完成: {len(ogg_files)} 个文件，总计 {total_bytes / (1024 * 1024):.1f} MB")

    bundles = pack_under_limit(
        ogg_files=ogg_files,
        info_source_dir=raw_dir,
        bundles_dir=bundles_dir,
        max_bytes=args.max_part_mb * 1024 * 1024,
    )
    print(f"\n全部完成！请将 {bundles_dir} 下的 {len(bundles)} 个分卷上传到魔搭 /mnt/workspace/<项目>/")


if __name__ == "__main__":
    main()
