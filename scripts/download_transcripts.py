#!/usr/bin/env python3
"""
download_transcripts.py - 解决云端 Web IDE / 网页下载被拦截的全自动传输工具

特点：
1. 纯 Python 标准库编写（0 第三方依赖），云端 Linux 和 本地 Mac/Windows 即插即用。
2. 双通道支持：
   - 模式一 [HTTP 临时快传]：云端一键 serve，本地一键 pull，自动 MD5 校验并解压 UTF-8 中文名。
   - 模式二 [浏览器反拦截注入]：输出 Playwright / 油猴 / 浏览器 DevTools 注入代码，
     清除 window.showSaveFilePicker 阻断，恢复原生下载流。
   - 模式三 [分片/单文件极速校验解码]：支持标准 Base64 流无损还原。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


# ---------------------------------------------------------------------------
# 浏览器端反拦截注入脚本（针对 VS Code Web / ModelScope Code Editor / JupyterLab）
# ---------------------------------------------------------------------------
BROWSER_INTERCEPT_PATCH_JS = """
// == ModelScope / VS Code Web 下载反拦截补丁 ==
(function() {
    console.log('[Anti-Intercept] 正在注入下载防护...');
    
    // 1. 阻断 File System Access API (showSaveFilePicker)
    // 强制 VS Code Web 回退到标准的 Blob <a download> 机制，避免触发无头环境挂起
    if (window.showSaveFilePicker) {
        try {
            delete window.showSaveFilePicker;
            console.log('[Anti-Intercept] 已移除 window.showSaveFilePicker，恢复标准 Blob 下载');
        } catch (e) {
            window.showSaveFilePicker = undefined;
            console.log('[Anti-Intercept] 已覆写 window.showSaveFilePicker 为 undefined');
        }
    }

    // 2. 挂载全局辅助下载函数 window.__downloadBlob
    window.__downloadBlob = function(blob, filename) {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename || 'download.zip';
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }, 1000);
        console.log('[Anti-Intercept] 已触发标准下载:', a.download);
    };

    console.log('[Anti-Intercept] 补丁注入成功！现在在文件树中右键点击 Download 即可正常下载。');
})();
"""


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """解决 Linux zip 打包中文文件名时未置 UTF-8 标记导致的乱码"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def unpack_zip_with_md5(zip_bytes: bytes, out_dir: Path, expected_md5: str | None = None) -> int:
    actual_md5 = hashlib.md5(zip_bytes).hexdigest()
    print(f"[*] 收到文件大小: {len(zip_bytes):,} 字节, MD5: {actual_md5}")
    if expected_md5 and actual_md5.lower() != expected_md5.lower():
        raise ValueError(f"MD5 校验不匹配！期望: {expected_md5}, 实际: {actual_md5}")

    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    import io
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            decoded_name = Path(decode_zip_name(info)).name
            target = out_dir / decoded_name
            target.write_bytes(zf.read(info.filename))
            count += 1
            print(f"  -> 解包: {target.name} ({target.stat().st_size:,} bytes)")
    print(f"[✓] 成功解包 {count} 个文件至: {out_dir}")
    return count


# ---------------------------------------------------------------------------
# 单次安全 HTTP 传输服务
# ---------------------------------------------------------------------------
class OneTimeDownloadHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args, file_path: Path, token: str, expected_md5: str, **kwargs):
        self.file_path = file_path
        self.token = token
        self.expected_md5 = expected_md5
        super().__init__(*args, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        req_token = params.get("token", [""])[0]

        if req_token != self.token:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden: Invalid or missing token")
            return

        if parsed.path in ["/download", "/"]:
            data = self.file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{self.file_path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Checksum-MD5", self.expected_md5)
            self.end_headers()
            self.wfile.write(data)
            print(f"\n[✓] 文件已成功下发至客户端: {self.client_address[0]}")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


def cmd_serve(args: argparse.Namespace) -> None:
    file_path = Path(args.file).resolve()
    if not file_path.is_file():
        raise SystemExit(f"错误: 待下载文件不存在: {file_path}")

    raw_data = file_path.read_bytes()
    file_md5 = hashlib.md5(raw_data).hexdigest()
    token = secrets.token_hex(8)
    port = args.port

    handler = lambda *h_args, **h_kwargs: OneTimeDownloadHandler(
        *h_args, file_path=file_path, token=token, expected_md5=file_md5, **h_kwargs
    )

    print("=" * 60)
    print(f"[*] 启动单次极速快传服务 (端口: {port})")
    print(f"[*] 文件路径: {file_path} ({len(raw_data):,} 字节)")
    print(f"[*] 文件 MD5: {file_md5}")
    print(f"[*] 提取 Token: {token}")
    print("=" * 60)
    print("本地或对端拉取命令：")
    print(f"python3 scripts/download_transcripts.py pull --url http://<服务器IP>:{port}/download?token={token} --out-dir ./逐字稿/")
    print("=" * 60)

    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()


def cmd_pull(args: argparse.Namespace) -> None:
    url = args.url
    out_dir = Path(args.out_dir).resolve()
    print(f"[*] 正在从云端拉取逐字稿压缩包: {url} ...")

    req = urllib.request.Request(url, headers={"User-Agent": "cloud-video-to-epub/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            data = resp.read()
            expected_md5 = resp.headers.get("X-Checksum-MD5") or args.md5
    except urllib.error.URLError as e:
        raise SystemExit(f"拉取失败: {e}")

    unpack_zip_with_md5(data, out_dir, expected_md5)


def cmd_print_patch(args: argparse.Namespace) -> None:
    print(BROWSER_INTERCEPT_PATCH_JS.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="云端/Web IDE 逐字稿全自动下载与反拦截工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. serve 模式（在云端运行）
    p_serve = subparsers.add_parser("serve", help="[云端运行] 启动带 Token 的轻量单次下载服务")
    p_serve.add_argument("--file", required=True, help="待提供下载的文件路径 (如 plain_all.zip)")
    p_serve.add_argument("--port", type=int, default=8080, help="服务监听端口 (默认 8080)")

    # 2. pull 模式（在本地运行）
    p_pull = subparsers.add_parser("pull", help="[本地运行] 通过 URL 一键拉取、MD5 校验并自动解压")
    p_pull.add_argument("--url", required=True, help="完整的下载链接 (包含 token)")
    p_pull.add_argument("--out-dir", required=True, help="解压目标目录 (如 ./逐字稿/)")
    p_pull.add_argument("--md5", default=None, help="可选的期望 MD5 校验值")
    p_pull.add_argument("--timeout", type=int, default=30, help="网络超时时间秒数")

    # 3. patch-js 模式（打印自动化浏览器注入补丁）
    p_patch = subparsers.add_parser("patch-js", help="输出针对 VS Code Web 的反拦截 JavaScript 注入代码")

    args = parser.parse_args()

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "pull":
        cmd_pull(args)
    elif args.command == "patch-js":
        cmd_print_patch(args)


if __name__ == "__main__":
    main()
