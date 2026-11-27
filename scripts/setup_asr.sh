#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
WORKSPACE_DIR="/mnt/workspace"
ENV_DIR="${WORKSPACE_DIR}/asr_env"
MODEL_DIR="${WORKSPACE_DIR}/models/faster-whisper/large-v3-turbo"
MS_CACHE_DIR="/tmp/ms_cache"

if [ ! -d "${WORKSPACE_DIR}" ]; then
  echo "错误: 未找到 ${WORKSPACE_DIR}，请在魔搭 Notebook 等带持久化盘的云端环境中运行。"
  exit 1
fi

echo "=== 1. 检查持久化磁盘与 ffmpeg ==="
df -h "${WORKSPACE_DIR}"
command -v ffmpeg ffprobe >/dev/null 2>&1 || {
  echo "错误: 未找到 ffmpeg / ffprobe，请先安装 ffmpeg。"
  exit 1
}

PYTHON_BIN="$(command -v python || command -v python3 || true)"
if [ -z "${PYTHON_BIN}" ]; then
  echo "错误: 未找到 python 或 python3。"
  exit 1
fi

echo ""
echo "=== 2. 创建持久化虚拟环境 (${ENV_DIR}) ==="
if [ ! -x "${ENV_DIR}/bin/python" ]; then
  "${PYTHON_BIN}" -m venv --system-site-packages "${ENV_DIR}"
  echo "已创建持久化虚拟环境: ${ENV_DIR}"
else
  echo "检测到已有虚拟环境: ${ENV_DIR}，跳过重建。"
fi

# 不把 torch 列入安装列表，避免覆盖预装版本
"${ENV_DIR}/bin/pip" install --upgrade-strategy only-if-needed \
  faster-whisper opencc-python-reimplemented modelscope

echo ""
echo "=== 3. 准备持久化目录与校验导入 ==="
mkdir -p "${MODEL_DIR}" "${PROJECT_DIR}/.work" "${PROJECT_DIR}/逐字稿" "${PROJECT_DIR}/audio"
"${ENV_DIR}/bin/python" -c "import faster_whisper, opencc, modelscope; print('依赖导入成功')"

echo ""
echo "=== 4. 从魔搭 Hub 下载 faster-whisper large-v3-turbo 到持久化目录 ==="
if [ -f "${MODEL_DIR}/model.bin" ] && [ -f "${MODEL_DIR}/config.json" ]; then
  echo "检测到模型文件已存在于 ${MODEL_DIR}，跳过下载。"
else
  mkdir -p "${MS_CACHE_DIR}"
  MODELSCOPE_CACHE="${MS_CACHE_DIR}" "${ENV_DIR}/bin/python" -c "
from modelscope import snapshot_download
snapshot_download('pengzhendong/faster-whisper-large-v3-turbo', local_dir='${MODEL_DIR}')
"
  rm -rf "${MS_CACHE_DIR}"
fi

echo ""
echo "=== 5. 校验模型可在 8 核 CPU int8 下正常加载 ==="
"${ENV_DIR}/bin/python" -c "
from faster_whisper import WhisperModel
m = WhisperModel('${MODEL_DIR}', device='cpu', compute_type='int8', cpu_threads=8)
print('WhisperModel 加载成功:', '${MODEL_DIR}')
"

echo ""
echo "=== 安装完成！当前磁盘状态 ==="
df -h "${WORKSPACE_DIR}"
ls -lh "${MODEL_DIR}"
echo ""
echo "下一步后台运行极速转写："
echo "  cd ${PROJECT_DIR} && OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 nohup ${ENV_DIR}/bin/python -u transcribe_cpu_fast.py --project-dir ${PROJECT_DIR} > transcribe_fast.log 2>&1 < /dev/null &"
