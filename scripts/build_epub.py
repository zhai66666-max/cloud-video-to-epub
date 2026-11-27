#!/usr/bin/env python3
"""Generic EPUB 3.0 assembler, cover renderer, OpenCC/quote checker, and 5-point verifier."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DEFAULT_SONGTI_FONT = "/System/Library/Fonts/Supplemental/Songti.ttc"
MEDIA_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".webm", ".mp4", ".mov", ".mkv"}


def load_opencc_t2s():
    """Optionally load OpenCC t2s converter if installed."""
    try:
        import opencc  # type: ignore

        return opencc.OpenCC("t2s")
    except ImportError:
        return None


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    y: int,
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    canvas_w: int = 1200,
) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (canvas_w - w) // 2
    draw.text((x, y), text, font=font, fill=fill)
    return y + h


def generate_cover(
    out_path: Path,
    title: str,
    subtitle: str,
    author: str,
    stats_line1: str,
    stats_line2: str,
    font_path: str = DEFAULT_SONGTI_FONT,
) -> None:
    """Generate a 1200x1800 classic Chinese serif book cover."""
    w, h = 1200, 1800
    bg = (24, 34, 46)
    gold = (214, 182, 118)
    ivory = (242, 237, 224)
    muted = (170, 178, 186)

    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(64, 64), (w - 64, h - 64)], outline=gold, width=3)
    draw.rectangle([(82, 82), (w - 82, h - 82)], outline=(78, 96, 114), width=1)

    if Path(font_path).is_file():
        font_kicker = ImageFont.truetype(font_path, 38, index=6)
        title_size = 112 if len(title) <= 7 else 84
        font_title = ImageFont.truetype(font_path, title_size, index=1)
        font_sub = ImageFont.truetype(font_path, 44, index=6)
        font_meta = ImageFont.truetype(font_path, 36, index=6)
        font_author = ImageFont.truetype(font_path, 44, index=1)
    else:
        default_font = ImageFont.load_default()
        font_kicker = font_title = font_sub = font_meta = font_author = default_font

    draw_centered_text(draw, 250, "AUDIO / VIDEO TRANSCRIPT COLLECTION", font_kicker, gold, w)
    draw.line([(420, 330), (w - 420, 330)], fill=gold, width=2)

    y = draw_centered_text(draw, 480, title, font_title, ivory, w)
    if subtitle:
        draw_centered_text(draw, y + 60, subtitle, font_sub, (218, 222, 226), w)

    draw.line([(490, 930), (w - 490, 930)], fill=(96, 114, 132), width=2)
    draw_centered_text(draw, 990, stats_line1, font_meta, muted, w)
    if stats_line2:
        draw_centered_text(draw, 1065, stats_line2, font_meta, muted, w)

    draw_centered_text(draw, 1490, author, font_author, ivory, w)
    draw_centered_text(draw, 1575, "语音转写精校版", font_meta, gold, w)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    print(f"封面已生成: {out_path} ({out_path.stat().st_size // 1024} KB)")


def compute_exact_stats(all_mds: list[Path], audio_dir: Path | None) -> tuple[float | None, int]:
    """Compute exact total audio minutes via ffprobe (if audio_dir given) and body chars."""
    total_minutes: float | None = None
    if audio_dir and audio_dir.is_dir():
        media = sorted(p for p in audio_dir.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS)
        if media:
            total_sec = 0.0
            for m in media:
                out = subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(m),
                    ],
                    text=True,
                ).strip()
                total_sec += float(out)
            total_minutes = total_sec / 60.0

    body_chars = 0
    for md in all_mds:
        lines = md.read_text(encoding="utf-8").splitlines()
        body = "\n".join(
            line for line in lines if not line.startswith("## ") and not line.startswith("> 来源：")
        )
        clean = re.sub(r"\s", "", re.sub(r"[*`>|#-]", "", body))
        body_chars += len(clean)

    return total_minutes, body_chars


def resolve_volumes(polished_dir: Path, volumes_json: Path | None) -> list[tuple[str, list[str]]]:
    """Load custom volume grouping from JSON or default to single chronological volume."""
    disk_files = sorted(p.name for p in polished_dir.glob("*.md"))
    if not disk_files:
        raise SystemExit(f"错误: {polished_dir} 下未找到任何 .md 润色稿。")

    if not volumes_json:
        return [("正文全集", disk_files)]

    raw = json.loads(volumes_json.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        volumes = [(k, list(v)) for k, v in raw.items()]
    elif isinstance(raw, list):
        volumes = [(item["title"], list(item["files"])) for item in raw]
    else:
        raise ValueError("volumes.json 格式需为对象 {卷名: [文件名]} 或数组 [{title, files}]。")
    return volumes


def build_merged_markdown(
    polished_dir: Path,
    merged_md: Path,
    volumes: list[tuple[str, list[str]]],
    total_minutes: float | None,
    body_chars: int,
    allow_ascii_quotes: bool,
) -> list[tuple[str, int]]:
    """Validate all chapters and assemble 合并.md."""
    disk_mds = {p.name: p for p in polished_dir.glob("*.md")}
    assigned = [fname for _, files in volumes for fname in files]

    assert len(assigned) == len(set(assigned)), "存在重复收录的章节！"
    missing = set(assigned) - set(disk_mds.keys())
    unassigned = set(disk_mds.keys()) - set(assigned)
    assert not missing, f"缺失润色文件: {missing}"
    assert not unassigned, f"未纳入任何卷的文件: {unassigned}"

    cc = load_opencc_t2s()
    toc_rows = []
    for vol_title, files in volumes:
        toc_rows.append(f"**{vol_title}**（{len(files)} 篇）")
        for fname in files:
            text = disk_mds[fname].read_text(encoding="utf-8")
            if not allow_ascii_quotes:
                assert '"' not in text, f"发现未替换的 ASCII 双引号: {fname}"
                assert "“" not in text and "”" not in text, f"发现残留弯引号: {fname}"
            if cc is not None:
                converted = cc.convert(text)
                if converted != text:
                    diffs = sum(1 for a, b in zip(text, converted) if a != b)
                    print(f"[OpenCC 提示] {fname} 有 {diffs} 处简繁差异（若为专有名词如「昇腾」请忽略）")
            first_line = text.splitlines()[0].lstrip("#").strip()
            toc_rows.append(f"- 《{first_line}》")
        toc_rows.append("")

    duration_line = (
        f"- **音频时长**：{total_minutes:.1f} 分钟（约 {total_minutes / 60:.2f} 小时）"
        if total_minutes is not None
        else "- **音频时长**：（未传入音频目录，略过统计）"
    )
    preface = "\n".join(
        [
            "# 前言",
            "",
            f"本书共收录 **{len(assigned)} 篇** 精选讲稿，经语音转写与全文精校汇编而成。",
            "",
            "### 统计口径",
            "",
            duration_line,
            f"- **正文净字数**：约 **{body_chars / 10000:.2f} 万字**（精确统计 **{body_chars:,} 字**，不含篇目标题、来源标注、前言与附记）",
            "",
            "### 卷目总览",
            "",
            *toc_rows,
        ]
    )

    parts = [preface]
    vol_expected: list[tuple[str, int]] = []
    for vol_title, files in volumes:
        vol_parts = [f"# {vol_title}", ""]
        for fname in files:
            content = disk_mds[fname].read_text(encoding="utf-8").strip()
            if content.startswith("# ") and not content.startswith("## "):
                content = "#" + content
            assert content.startswith("## "), f"{fname} 首行需为 ## 标题"
            vol_parts.append(content)
            vol_parts.append("")
        parts.append("\n".join(vol_parts))
        vol_expected.append((vol_title, len(files)))

    postface = "\n".join(
        [
            "# 附记",
            "",
            "本书由「云服务器版 video-to-epub」流水线生成：云端 `faster-whisper large-v3-turbo` 语音转写，并行 AI 全文精校排版，经 `pandoc` 编译为 EPUB 3.0 电子书。由于口播涉及专有名词与数字，如有存疑处建议对照原音视频核实。",
            "",
        ]
    )
    parts.append(postface)

    merged_text = "\n\n".join(parts).strip() + "\n"
    merged_md.write_text(merged_text, encoding="utf-8")
    print(f"已生成合并文稿: {merged_md} ({len(merged_text):,} 字符)")
    return vol_expected


def verify_epub(epub_path: Path, vol_expected: list[tuple[str, int]], expected_total: int, title: str, author: str) -> None:
    """Run all 5 EPUB internal checks + nested TOC DOM tree verification."""
    with zipfile.ZipFile(epub_path) as zf:
        names = set(zf.namelist())

        xml_files = sorted(n for n in names if n.endswith((".xhtml", ".opf", ".ncx")))
        assert xml_files, "EPUB 中缺少 .xhtml/.opf/.ncx 文件"
        parsed_docs = {name: ET.fromstring(zf.read(name)) for name in xml_files}
        print(f"[内检1] XML 良构校验通过: 共 {len(xml_files)} 个文件")

        opf_name = next(n for n in names if n.endswith("content.opf"))
        opf_dir = str(Path(opf_name).parent)
        opf_root = parsed_docs[opf_name]
        assert opf_root.attrib.get("version") == "3.0", f"非 EPUB 3.0: {opf_root.attrib.get('version')}"
        opf_text = zf.read(opf_name).decode("utf-8")
        assert title in opf_text and author in opf_text, "content.opf 元数据不完整"

        manifest_hrefs = []
        for elem in opf_root.iter():
            if elem.tag.endswith("item") and "href" in elem.attrib:
                href = urllib.parse.unquote(elem.attrib["href"])
                full = str(Path(f"{opf_dir}/{href}" if opf_dir != "." else href))
                assert full in names, f"manifest 缺失文件: {full}"
                manifest_hrefs.append(full)
        print(f"[内检2&5] OPF manifest ({len(manifest_hrefs)} 项) 与 EPUB 3.0 元数据校验通过")

        ids_by_file: dict[str, set[str]] = {}
        h2_empty = 0
        for xname in (n for n in names if n.endswith(".xhtml")):
            xroot = parsed_docs[xname]
            ids_by_file[xname] = {el.attrib["id"] for el in xroot.iter() if "id" in el.attrib}
            for el in xroot.iter():
                if el.tag.endswith("}h2") or el.tag == "h2":
                    if not "".join(el.itertext()).strip():
                        h2_empty += 1
        assert h2_empty == 0, f"发现 {h2_empty} 个空 h2 标题！"
        print("[内检4] 空 h2 标题数为 0")

        nav_name = next(n for n in names if n.endswith("nav.xhtml"))
        nav_dir = str(Path(nav_name).parent)
        nav_root = parsed_docs[nav_name]
        epub_type_attr = "{http://www.idpf.org/2007/ops}type"
        toc_nav = next(
            (el for el in nav_root.iter() if el.tag.endswith("nav") and el.attrib.get(epub_type_attr) == "toc"),
            None,
        )
        assert toc_nav is not None, "nav.xhtml 中未找到 epub:type='toc'"

        hrefs = [el.attrib["href"] for el in toc_nav.iter() if el.tag.endswith("a") and "href" in el.attrib]
        for raw_href in hrefs:
            decoded = urllib.parse.unquote(raw_href)
            fpath, frag = decoded.split("#", 1) if "#" in decoded else (decoded, None)
            full = f"{nav_dir}/{fpath}" if nav_dir != "." else fpath
            assert full in ids_by_file, f"TOC 目标文件不存在: {full}"
            if frag:
                assert frag in ids_by_file[full], f"TOC 锚点不存在: {full}#{frag}"
        print(f"[内检3] nav.xhtml 共 {len(hrefs)} 条目录锚点全部解析成功")

        top_ol = next((child for child in toc_nav if child.tag.endswith("ol")), None)
        assert top_ol is not None, "TOC 中未找到顶层 <ol>"
        vol_map: dict[str, int] = {}
        for li in top_ol:
            if not li.tag.endswith("li"):
                continue
            a_el = next((c for c in li if c.tag.endswith("a")), None)
            sub_ol = next((c for c in li if c.tag.endswith("ol")), None)
            title_txt = "".join(a_el.itertext()).strip() if a_el is not None else ""
            sub_links = [c for c in sub_ol.iter() if c.tag.endswith("a")] if sub_ol is not None else []
            vol_map[title_txt] = len(sub_links)

        total_in_toc = 0
        for expected_vol, expected_count in vol_expected:
            actual = vol_map.get(expected_vol, -1)
            print(f"  - {expected_vol}: {actual} 篇 (预期 {expected_count} 篇)")
            assert actual == expected_count, f"{expected_vol} 篇数不匹配: {actual} != {expected_count}"
            total_in_toc += actual
        assert total_in_toc == expected_total, f"总篇数不匹配: {total_in_toc} != {expected_total}"
        assert any("cover" in n.lower() for n in names), "EPUB 未包含封面文件"
        print(f"EPUB 全部内检通过！总篇数={total_in_toc}，文件大小={epub_path.stat().st_size / (1024 * 1024):.2f} MB")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成封面、校验文字并打包验证 EPUB 3.0 电子书")
    parser.add_argument("--polished-dir", type=str, required=True, help="润色稿 .md 目录")
    parser.add_argument("--audio-dir", type=str, default=None, help="音频目录（用于 ffprobe 精确计算总时长）")
    parser.add_argument("--out-dir", type=str, required=True, help="输出目录（存放 封面.png、合并.md 和 .epub）")
    parser.add_argument("--title", type=str, required=True, help="电子书书名")
    parser.add_argument("--subtitle", type=str, default="", help="副标题")
    parser.add_argument("--author", type=str, default="合集编校", help="作者名")
    parser.add_argument("--volumes-json", type=str, default=None, help="可选分卷配置 JSON")
    parser.add_argument("--font-path", type=str, default=DEFAULT_SONGTI_FONT, help="封面字体路径")
    parser.add_argument("--allow-ascii-quotes", action="store_true", help="允许保留 ASCII 双引号")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    polished_dir = Path(args.polished_dir).resolve()
    audio_dir = Path(args.audio_dir).resolve() if args.audio_dir else None
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    volumes = resolve_volumes(polished_dir, Path(args.volumes_json).resolve() if args.volumes_json else None)
    ordered_paths = [polished_dir / fname for _, files in volumes for fname in files]
    total_minutes, body_chars = compute_exact_stats(ordered_paths, audio_dir)

    cover_path = out_dir / "封面.png"
    merged_md = out_dir / "合并.md"
    epub_path = out_dir / f"{args.title}.epub"

    stats_line1 = f"全 {len(volumes)} 卷 · 收录 {len(ordered_paths)} 篇精校讲稿"
    stats_line2 = (
        f"音频时长 {total_minutes:.0f} 分钟 · 约 {body_chars / 10000:.1f} 万字"
        if total_minutes is not None
        else f"正文净计约 {body_chars / 10000:.1f} 万字"
    )
    generate_cover(cover_path, args.title, args.subtitle, args.author, stats_line1, stats_line2, args.font_path)

    vol_expected = build_merged_markdown(
        polished_dir, merged_md, volumes, total_minutes, body_chars, args.allow_ascii_quotes
    )

    cmd = [
        "pandoc",
        str(merged_md),
        "-o",
        str(epub_path),
        "--metadata",
        f"title={args.title}",
        "--metadata",
        f"author={args.author}",
        "--metadata",
        "lang=zh-CN",
        "--toc",
        "--toc-depth=2",
        "--split-level=1",
        f"--epub-cover-image={cover_path}",
    ]
    if args.subtitle:
        cmd.extend(["--metadata", f"subtitle={args.subtitle}"])
    subprocess.run(cmd, check=True)

    verify_epub(epub_path, vol_expected, len(ordered_paths), args.title, args.author)


if __name__ == "__main__":
    main()
