#!/usr/bin/env python3
"""Safely unpack Ubuntu-created zips with UTF-8 Chinese filenames and optional MD5 verification."""

from __future__ import annotations

import argparse
import base64
import hashlib
import zipfile
from pathlib import Path


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """Restore UTF-8 filename when Linux zip omitted UTF-8 flag bit 0x800."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="无损校验并解包云端传回的 UTF-8 中文逐字稿 zip")
    parser.add_argument("input_file", type=str, help="输入 .zip 或 base64 .b64 文件路径")
    parser.add_argument("--out-dir", type=str, required=True, help="解压目标目录")
    parser.add_argument("--md5", type=str, default=None, help="期望的 zip MD5 校验值")
    parser.add_argument("--from-b64", action="store_true", help="输入文件为 base64 文本而非二进制 zip")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    in_path = Path(args.input_file).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.from_b64:
        b64_text = "".join(in_path.read_text(encoding="utf-8").split())
        raw = base64.b64decode(b64_text)
        zip_path = in_path.with_suffix(".zip")
        zip_path.write_bytes(raw)
    else:
        raw = in_path.read_bytes()
        zip_path = in_path

    actual_md5 = hashlib.md5(raw).hexdigest()
    print(f"ZIP MD5: {actual_md5} ({len(raw):,} bytes)")
    if args.md5 and actual_md5.lower() != args.md5.lower():
        raise SystemExit(f"错误: MD5 不一致！期望 {args.md5}，实际 {actual_md5}")

    extracted = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            decoded_name = Path(decode_zip_name(info)).name
            dest = out_dir / decoded_name
            dest.write_bytes(zf.read(info.filename))
            chars = len(dest.read_text(encoding="utf-8", errors="replace"))
            print(f"  {dest.stat().st_size:7d} bytes | {chars:6d} chars | {dest.name}")
            extracted += 1

    print(f"解包完成: 共 {extracted} 个文件 -> {out_dir}")


if __name__ == "__main__":
    main()
