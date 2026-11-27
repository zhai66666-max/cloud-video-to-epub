# 云服务器版 video-to-epub

把**本地或 YouTube 频道/列表里的长视频或音频**转成「**16kHz 极速压缩包 → 魔搭长期免费 8 核 32G CPU 云端持久化语音转写 → 本地/Agent 并行精校润色 → 带宋体封面与两级目录的 EPUB 3.0 电子书**」的完整自动化 Agent Skill 与工具链。

灵感来源于本地版的 [`video-to-epub`](https://github.com/zhai66666-max/video-to-epub)，针对**不希望本机笔记本长时间高负载发热**、同时希望把**语音转写外包给免费云端服务器（且开关机不丢模型与环境）**的场景全面重构。

---

## 核心优势：本机凉爽 + 云端白嫖持久化

1. **本机零发热**：Mac/PC 只负责用 `yt-dlp` 拉取纯音频并极速转成 Whisper 专用的 16kHz 单声道 Opus（体积缩小 **4 倍**），最重、最耗时的 Whisper 语音识别全部丢给魔搭（ModelScope）长期免费的 **8 核 32 GB CPU 实例**在云端后台跑。
2. **开关机不丢环境与模型**：虚拟环境（`/mnt/workspace/asr_env`）和模型权重（`/mnt/workspace/models/faster-whisper/large-v3-turbo`）全部落在魔搭持久化存储目录 `/mnt/workspace` 下，**不覆盖官方环境**，下次开机 0 秒恢复开跑。
3. **国内内网拉模型**：直接从魔搭 Hub 下载 `pengzhendong/faster-whisper-large-v3-turbo`（1.6 GB 实测 18 秒拉完），彻底绕开 HuggingFace xet/镜像卡死问题。
4. **8 核 CPU 极限压榨（速度翻倍）**：通过 **2 进程并发 × 每进程 4 线程 + `BatchedInferencePipeline(batch_size=4)` + `beam_size=1`**，实测把单路 `5.95x` 提升到 **~11.0x 实时吞吐**，32 GB 内存仅占用约 5.5 GB。
5. **流水线可并行 + 全自动 EPUB 内检**：支持断点续跑、CP437/UTF-8 压缩包文件名防乱码解包、OpenCC 简繁残留校验、宋体封面生成以及 5 项 EPUB 3.0 XML/DOM 结构校验。

---

## 实测基准（17 期中文长节目 · 9.62 小时 · 20.5 万字）

| 阶段 / 指标 | 实测数据 | 说明 |
| :--- | :--- | :--- |
| 原始音频 (`140` m4a, 129k) | 17 期，577.3 分钟，**536 MB** | `yt-dlp --cookies-from-browser` 优先抓原声轨 |
| 16kHz 单声道 Opus (32k `.ogg`) | **133.5 MB**（缩容 **75%**） | 4 线程并行转换仅约 10 秒；自动拆成 `< 45 MiB` 上传包 |
| 云端拉取 `large-v3-turbo` | 1.6 GB / **18 秒** | ModelScope Hub `pengzhendong/faster-whisper-large-v3-turbo` |
| 单进程 `beam_size=5` (8 线程) | **5.95x 实时** | 33.8 分钟音频耗时 340.4s |
| **2 路并发 × 4 线程 `batch=4, beam=1`** | **~11.0x 实时**（单路 5.43–5.63x） | 剩余 16 期（9 小时）总计约 **49.8 分钟**，内存占用 5.5 GB / 28 GB |
| 17 篇润色 + EPUB 打包 + 5 项内检 | 并行润色 ~2 分钟，打包 **3 秒** | 成品 0.37 MB，约 20.5 万字，0 残留英文引号与繁体错误 |

---

## 目录结构

```text
cloud-video-to-epub/
├── SKILL.md                   # Agent Skill 完整操作指南与全部踩坑记录
├── README.md                  # 本说明文档
├── LICENSE                    # MIT
└── scripts/
    ├── fetch_and_pack.py      # [本地] 拉取音视频、转 16kHz Opus 并自动分卷打包 (<45 MiB)
    ├── setup_asr.sh           # [云端] 魔搭 /mnt/workspace 持久化环境与模型一键安装
    ├── transcribe_cpu_fast.py # [云端] 2路并发 + BatchedInferencePipeline + 断点续跑转写
    ├── extract_utf8_zip.py    # [本地] 修复 Linux zip 中文文件名编码并校验 MD5 解包
    └── build_epub.py          # [本地] 生成宋体封面、OpenCC/引号检查、组装合并.md 并打包与内检 EPUB
```

---

## 安装为 Agent Skill

```bash
# Antigravity / Gemini 配置目录
git clone https://github.com/zhai66666-max/cloud-video-to-epub.git \
  ~/.gemini/config/skills/cloud-video-to-epub

# 或 Claude Code / WorkBuddy skills 目录
git clone https://github.com/zhai66666-max/cloud-video-to-epub.git \
  ~/.workbuddy/skills/cloud-video-to-epub
```

---

## 快速上手（5 步走完）

### 第 1 步：本地环境依赖（macOS）

```bash
brew install ffmpeg yt-dlp pandoc
python3 -m venv .epub_venv
.epub_venv/bin/pip install pillow opencc-python-reimplemented -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 第 2 步：本地拉取/转换并分卷打包（< 45 MiB 每包）

```bash
# 方式 A：从 YouTube 频道或播放列表抓纯音频并自动打包
python3 scripts/fetch_and_pack.py \
  --url "https://www.youtube.com/@channel/videos" \
  --cookies-from-browser edge \
  --out-dir ./my_book_project

# 方式 B：直接处理本地已有的视频/音频文件夹
python3 scripts/fetch_and_pack.py \
  --input-dir ./raw_media \
  --out-dir ./my_book_project
```

会在 `./my_book_project/bundles/` 生成若干个不超过 45 MiB 的 `speech_part*.zip`（已内置 `setup_asr.sh` 和 `transcribe_cpu_fast.py`）。

### 第 3 步：上传到魔搭长期免费 CPU 实例并后台开跑

在魔搭启动 **CPU 环境（8 核 32 GB，长期有效）**，把 `speech_part*.zip` 上传到 `/mnt/workspace/work_project`，打开终端执行：

```bash
cd /mnt/workspace/work_project
for z in speech_part*.zip; do unzip -o "$z"; done && rm -f speech_part*.zip

# 首次安装：建持久化 venv 并从魔搭内网拉模型（约 1-2 分钟；以后重启免重装）
bash setup_asr.sh

# 后台极速开跑（输出全部原子落盘到 /mnt/workspace/work_project/逐字稿）
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 nohup /mnt/workspace/asr_env/bin/python -u \
  transcribe_cpu_fast.py \
  --project-dir /mnt/workspace/work_project \
  > transcribe_fast.log 2>&1 < /dev/null &

# 查看实时进度
tail -f transcribe_fast.log
```

### 第 4 步：无损拉回逐字稿并并行 AI 润色

如果 WebIDE 直接下载受浏览器限制，可在魔搭终端打包并按 9800 字符折行输出（或通过文件下载）：

```bash
cd /mnt/workspace/work_project
zip -j plain_transcripts.zip 逐字稿/*.txt -x "*timestamps*"
md5sum plain_transcripts.zip
```

拉回本地后用 `scripts/extract_utf8_zip.py` 解包（自动修复 Linux `zip` 中文文件名 CP437 乱码）：

```bash
python3 scripts/extract_utf8_zip.py plain_transcripts.zip --out-dir ./my_book_project/逐字稿 --md5 <MD5>
```

然后按 `SKILL.md` 中的规则并行派出 LLM 润色到 `./my_book_project/润色稿/`。

### 第 5 步：生成宋体封面、打包 EPUB 并完成 5 项内检

```bash
.epub_venv/bin/python scripts/build_epub.py \
  --polished-dir ./my_book_project/润色稿 \
  --audio-dir ./my_book_project/audio \
  --out-dir ./my_book_project \
  --title "我的电子书" \
  --subtitle "精选讲稿合集" \
  --author "作者名"
```

*(支持传入 `--volumes-json volumes.json` 自定义多卷目录分组；不传时默认按文件名日期正序编为一卷。)*

---

## License

MIT
