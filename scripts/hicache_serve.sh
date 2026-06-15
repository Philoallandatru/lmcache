#!/bin/bash
# scripts/hicache_serve.sh
# 按官方 docs/advanced_features/hicache_best_practices.md 例子启动 SGLang HiCache
# (基于 §"Deployment with HF3FS" L80-98, 去 PD 部分)
#
# 用法: hicache_serve.sh <cache_dir> [write_policy] [extra_args...]
#
# 环境变量 (env vars, 都可选, 都有 4B 默认值):
#   MODEL_PATH        - 模型路径 (默认: Qwen3-4B-Instruct-2507)
#   TP_SIZE           - tensor parallel size (默认: 1, 14B-AWQ 用 2)
#   PORT              - server port (默认: 30000, 14B 用 30001 避免冲突)
#   CTX_LEN           - context length (默认: 8192, 14B 用 12288)
#   MEM_STATIC        - mem_fraction_static (默认: 0.7, 14B-AWQ 用 0.85)
#   WATCHDOG_TIMEOUT  - sglang watchdog (默认: 不设, 14B 设 1800 让长测不被杀)
#   LOG_FILE          - server stdout/stderr 落盘位置 (默认: 不落盘, 透到 caller)
#
# 关键:
#   - 必须用 SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR 环境变量设 L3 目录
#     (sglang 0.5.13 --file-storage-path CLI 参数不生效,源码 hicache_storage.py::HiCacheFile L344 仍读环境变量)

set -e

CACHE_DIR=${1:?"cache_dir required (e.g. /mnt/ai_ssd0/cache_hicache)"}
WRITE_POLICY=${2:-write_through}
shift 2 || true
EXTRA_ARGS="$@"

# 模型 + 部署配置 (env 覆盖, 默认 4B Phase2 配)
MODEL_PATH=${MODEL_PATH:-/home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507}
TP_SIZE=${TP_SIZE:-1}
PORT=${PORT:-30000}
CTX_LEN=${CTX_LEN:-8192}
MEM_STATIC=${MEM_STATIC:-0.7}
# hicache_ratio 决定 L2 host RAM 容量 (= device_pool_size × hicache_ratio)
#   - 默认 2: device pool 20K × 2 = 41K tokens (8K prompt 装得下, L2 hit 100%)
#   - Phase6 暴露盘差: 0.1 → 2K tokens < 8K prompt → 必 L2 miss → L3 真读盘
HICACHE_RATIO=${HICACHE_RATIO:-2}

source ~/llm/.venv/bin/activate

# 用环境变量设 L3 目录 (绕开 0.5.13 --file-storage-path CLI bug)
export SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR="$CACHE_DIR"
mkdir -p "$CACHE_DIR"
rm -rf "$CACHE_DIR"/*  # 每轮清空 L3 避免污染

echo "==== Starting SGLang HiCache ===="
echo "Model:       $MODEL_PATH"
echo "TP size:     $TP_SIZE"
echo "Port:        $PORT"
echo "Context:     $CTX_LEN"
echo "Mem static:  $MEM_STATIC"
echo "L3 cache:    $CACHE_DIR"
echo "Write policy:$WRITE_POLICY"
echo "Extra args:  $EXTRA_ARGS"
echo "================================"

# 拼 watchdog 参数
WATCHDOG_ARG=""
if [ -n "$WATCHDOG_TIMEOUT" ]; then
    WATCHDOG_ARG="--watchdog-timeout $WATCHDOG_TIMEOUT"
fi

python -m sglang.launch_server \
    --model-path "$MODEL_PATH" \
    --port "$PORT" \
    --tp-size "$TP_SIZE" \
    --context-length "$CTX_LEN" \
    --mem-fraction-static "$MEM_STATIC" \
    --max-total-tokens "$((CTX_LEN + 512))" \
    --page-size 64 \
    --enable-metrics \
    --enable-cache-report \
    --enable-hierarchical-cache \
    --hicache-ratio "$HICACHE_RATIO" \
    --hicache-size 0 \
    --hicache-mem-layout page_first_direct \
    --hicache-io-backend direct \
    --hicache-write-policy "$WRITE_POLICY" \
    --hicache-storage-backend file \
    --hicache-storage-prefetch-policy timeout \
    $WATCHDOG_ARG \
    $EXTRA_ARGS