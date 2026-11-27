#!/usr/bin/env python3
"""
playwright_pull.py - 基于 Playwright 的全自动防拦截下载脚本 (用于 ModelScope / VS Code Web)

功能：
1. 自动连接已打开的 Chrome 调试端口（CDP）或启动 Playwright 实例；
2. 注入针对 VS Code Web 的反拦截补丁，彻底消除 window.showSaveFilePicker 弹窗挂起问题；
3. 捕获 download 事件并将云端压缩包自动落盘至本地目录，全程无需人工干预。
"""

import argparse
import asyncio
from pathlib import Path

# 反拦截核心注入逻辑：
# 当 VS Code Web 检测到 window.showSaveFilePicker 不存在时，
# 会自动优雅降级为原生的 <a download="plain_all.zip" href="blob:..."> 点击，
# 从而被 Playwright 的 page.expect_download() 100% 成功捕获。
ANTI_INTERCEPT_SCRIPT = """
(() => {
    try {
        delete window.showSaveFilePicker;
        console.log('[Anti-Intercept] Hook installed: showSaveFilePicker deleted.');
    } catch (e) {
        window.showSaveFilePicker = undefined;
        console.log('[Anti-Intercept] Hook installed: showSaveFilePicker set to undefined.');
    }
})();
"""


async def run_auto_download(target_file: str, out_path: Path, cdp_url: str | None = None):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit("提示: 本脚本依赖 playwright，请先执行 `pip install playwright` 并安装驱动。")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        if cdp_url:
            print(f"[*] 正在连接到现有浏览器 CDP: {cdp_url} ...")
            browser = await p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
        else:
            print("[*] 正在启动 Chromium 实例 (配置 accept_downloads=True) ...")
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(accept_downloads=True)

        # 全局注入防拦截补丁（每个新建页面和已存在页面）
        await context.add_init_script(ANTI_INTERCEPT_SCRIPT)

        pages = context.pages
        page = pages[0] if pages else await context.new_page()

        # 确保当前页面也执行了补丁
        await page.evaluate(ANTI_INTERCEPT_SCRIPT)

        print(f"[*] 准备下载文件: {target_file}")
        print("[*] 监听 download 事件中...")

        async with page.expect_download(timeout=60000) as download_info:
            # 在 VS Code Web 文件树中定位目标文件并触发右键下载
            # 支持传入选择器或执行 VS Code 内部命令
            download_trigger = await page.evaluate(f"""
                async (filename) => {{{{
                    // 尝试定位 VS Code 文件树项
                    const items = Array.from(document.querySelectorAll('.monaco-list-row'));
                    const target = items.find(el => el.textContent.includes(filename));
                    if (target) {{{{
                        target.dispatchEvent(new MouseEvent('contextmenu', {{{{ bubbles: true, clientX: 200, clientY: 200 }}}}));
                        return true;
                    }}}}
                    return false;
                }}}}
            """, target_file)

            if download_trigger:
                await asyncio.sleep(0.5)
                await page.locator("text=Download").or_(page.locator("text=下载")).first.click()
            else:
                print(f"[!] 未能在 DOM 中直接定位 {target_file}，等待用户或扩展触发下载...")

        download = await download_info.value
        print(f"[✓] 捕获到下载流: {download.suggested_filename}")
        await download.save_as(str(out_path))
        print(f"[✓] 文件已成功自动保存到本地: {out_path} ({out_path.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(description="Playwright 全自动防拦截下载工具")
    parser.add_argument("--file", default="plain_all.zip", help="云端待下载文件名")
    parser.add_argument("--out", default="./plain_all.zip", help="本地保存目标路径")
    parser.add_argument("--cdp", default=None, help="可选的远程 Chrome 调试接口 (例如 http://localhost:9222)")

    args = parser.parse_args()
    asyncio.run(run_auto_download(args.file, Path(args.out).resolve(), args.cdp))


if __name__ == "__main__":
    main()
