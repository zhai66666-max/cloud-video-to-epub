---
name: cloud-video-to-epub
description: 云服务器版「视频/音频 → 语音转写 → 并行AI润色 → EPUB电子书」完整工作流。当用户希望把 YouTube 频道或本地音视频批量转成电子书，且不想让本地电脑高负载发热、要求用魔搭（ModelScope）等免费云端 8核 CPU 实例持久化部署 Whisper 模型进行加速转写并回传润色出书时使用。
---

# 云服务器版 video-to-epub

将长视频/音频的**重算力语音转写**放到云服务器（魔搭长期免费 8 核 32 GB CPU 实例）的**持久化存储目录**中跑，本机只做轻量音频压缩与最终的并行 AI 润色和 `pandoc` EPUB 3.0 打包。

## 整体流水线

```text
[本机] yt-dlp (--cookies-from-browser) 拉取纯音频 / 读取本地音视频
   │   ffmpeg 4线程并行转 16kHz 单声道 32kbps Opus (.ogg)，体积缩至 1/4
   │   自动打包成 <45 MiB 分卷 (speech_part*.zip)，附带云端脚本
   ▼
[魔搭 CPU 8核32G] 上传到持久化目录 /mnt/workspace/<项目名>/ 并解压
   │   setup_asr.sh → /mnt/workspace/asr_env (不覆盖系统包)
   │                → 从魔搭 Hub 拉取 large-v3-turbo 到 /mnt/workspace/models/faster-whisper
   │   transcribe_cpu_fast.py → 2并发 × 4线程 + BatchedInferencePipeline(batch=4, beam=1)
   │                          → 约 11x 实时，原子落盘 逐字稿/*.txt + 尾部时长告警核查
   ▼
[传回本机] zip 纯文本逐字稿 (排除 timestamps) + extract_utf8_zip.py (修复 CP437→UTF-8 + MD5)
   ▼
[本机 / Swarm 并行润色] 统一简体中文 · 修正同音字(非词必改，是词从原文) · 引号统一「」 · 划分 ### 小节
   ▼
[本机 build_epub.py] OpenCC 残留检查 · ffprobe 精确回算时长/净字数 · 宋体封面 · pandoc EPUB 3.0 + 5项内检
```

---

## 七条实战关键经验（避坑必读）

1. **YouTube 登录态与原声轨优先**
   - 代理环境下 `yt-dlp` 常触发 `Sign in to confirm you're not a bot`，需显式询问用户日常登录 YouTube 的浏览器并传入 `--cookies-from-browser <edge|chrome|safari>`。
   - 格式选择器写 `-f "bestaudio[language^=zh][ext=m4a]/bestaudio[language^=zh]/bestaudio[ext=m4a]/bestaudio"`，防止多音轨视频误下成外语配音轨。
   - **提示**：无 Cookie 拉频道列表时 YouTube 可能自动翻译标题；带上浏览器 Cookie 后能拿到准确中文原标题和发布日期。

2. **先压缩成 16kHz 单声道 Opus，再分卷突破浏览器上传限制**
   - 浏览器自动化上传或 WebIDE 常见单文件 **50 MiB** 上限。
   - Whisper 内部本就要降采样到 16kHz 单声道。直接在本机用 `ffmpeg -ac 1 -ar 16000 -c:a libopus -b:a 32k` 转换，9.6 小时音频能从 **536 MB（m4a）压到 133.5 MB（ogg）**，拆 3–4 个 `< 45 MiB` 的 zip 即可秒传。

3. **魔搭持久化目录 + 避开 HuggingFace `hf-mirror` 卡死**
   - 魔搭系统盘关机即重置，**虚拟环境、模型权重、临时 `.work` 目录、逐字稿与日志必须全部放在 `/mnt/workspace`**。
   - 不要放 `/tmp`（系统定期清理且关机清空）。
   - `hf-mirror.com` 的 xet 通道大文件容易卡在 4 MB 超时。直接用魔搭官方 SDK 拉国内镜像仓库：
     `modelscope download pengzhendong/faster-whisper-large-v3-turbo --local_dir /mnt/workspace/models/faster-whisper/large-v3-turbo`
     实测 1.6 GB 仅需 **18 秒**，且关机重启后秒级复用。

4. **8 核 32G CPU 实例的并发上限与监控假象**
   - 容器里监控栏显示 CPU `11.9%` 横线是因为魔搭按宿主机总核数（如 64 核）作分母：`8 / 64 = 12.5%`。拉成水平线说明 **8 核配额已经 100% 吃满**，再开第 3 个进程只会互相抢占。
   - 黄金配比：**`workers=2` × `threads_per_worker=4` + `BatchedInferencePipeline(batch_size=4)` + `beam_size=1` + 指定主语言（如 `--language zh`）**，总吞吐可达 **~11x 实时**，内存仅占 ~5.5 GB / 28 GB。
   - 注意：`transcribe_cpu_fast.py` 使用 `multiprocessing.get_context("spawn")` 并通过环境变量 `OMP_NUM_THREADS=4` 限制每进程线程数。

5. **WebIDE 无法直接触发浏览器下载时的无损回传方案**
   - 当浏览器沙箱拦截下载或 `dl.path()` 返回 `null` 时，纯文本 `.txt` 体积很小（10–20 篇打包后仅 100–200 KB），可在云端执行：
     ```bash
     zip -j plain.zip 逐字稿/*.txt -x "*timestamps*"
     md5sum plain.zip
     base64 -w 0 plain.zip | fold -w 9800 > plain_folded.b64
     ```
     *（用 `fold -w 9800` 避开部分编辑器单行 10,000 字符截断。）*
   - 在本地用 `scripts/extract_utf8_zip.py` 解压时，务必处理 Linux `zip` 未置 UTF-8 标志位（`flag_bits & 0x800 == 0`）导致的 CP437 乱码：`name.encode("cp437").decode("utf-8")`。

6. **并行 AI 润色铁律（`video-to-epub` 精校标准）**
   - **完整保留原文**：严禁总结、缩水或略写论据，润色后字符数应略高于原逐字稿（因补全了标点、空格与 `###` 小节标题）。
   - **简繁统一与核验**：用 `opencc.OpenCC("t2s")` 全文扫描；注意按官方专名保留极少数合理字形（如「昇腾」）。
   - **同音词原则**：「非词必改，是词从原文」；警惕长文本年份漂移（如 `2026` 听成 `2016`）。
   - **引号清零**：正文禁止残留 ASCII `"` 或弯引号 `“”`，统一改为中文直角引号 `「」`（书名用 `《》`）。
   - **章节层级约定**：单篇文件首行必须为 `## 篇名`，第二/三行为 `> 来源：...`，正文内小节用 `###`，这样在组装 `# 卷名` 时配合 `pandoc --toc-depth=2 --split-level=1` 才能得到「卷 → 篇」两级目录。

7. **前言回算、封面与 EPUB 3.0 五项内检**
   - 前言里的时长（`ffprobe` 求和）和净字数（剔除标题与标记后统计）必须由脚本精确回算，绝不拍脑袋。
   - 生成 `1200×1800` 宋体封面（macOS 优先 `/System/Library/Fonts/Supplemental/Songti.ttc`），否则阅读器里会显示灰块。
   - `pandoc` 打包后必须执行 5 项 XML/DOM 内检：XML 良构、`content.opf` manifest 资源完整、`<nav epub:type="toc">` 每条锚点在目标文件存在 `id`（注意避开 `landmarks` 第二个 `<nav>`）、空 `<h2>` 为 0、卷篇计数与预期 100% 一致。

---

## 执行步骤（Agent SOP）

1. **前置确认**：询问用户媒体源（YouTube URL 或本地目录）、登录 YouTube 的浏览器、以及是否有分卷/书名/作者偏好。
2. **本地拉取与打包**：运行 `scripts/fetch_and_pack.py` 生成 `< 45 MiB` 的 `speech_part*.zip`。
3. **云端部署与启动**：
   - 如用户尚未开机，协助启动魔搭 `CPU环境`（遇滑块验证需暂停交由用户手动拖动）。
   - 上传 `speech_part*.zip` 到 `/mnt/workspace/<项目>/` 并解压，跑 `bash setup_asr.sh`（持久安装），再 `nohup` 后台启动 `transcribe_cpu_fast.py`。
   - 建议每 10 分钟定时巡检一次进度；若全片已转完再拉取，或者边转边拉，注意**全部取回并校验 MD5 后及时停止云端实例**。
4. **本地解包与并行润色**：
   - 用 `scripts/extract_utf8_zip.py` 还原正确中文文件名。
   - 多路并行派出润色子代理/Swarm 工人写入 `润色稿/<stem>.md`（推荐 `Gemini 4 Hydrogen (Medium)` 或同等长上下文高性价比模型）。
   - 每篇写完后核查字符数、0 ASCII 引号、首行 `##` 与来源行。
5. **打包与交付**：
   - 运行 `scripts/build_epub.py` 完成 OpenCC 校验、宋体封面绘制、`合并.md` 组装、`pandoc` 出书及 5 项内检。
   - 交付 `.epub`、`合并.md`、`封面.png`，并提醒用户原始逐字稿与精校版的区别。
