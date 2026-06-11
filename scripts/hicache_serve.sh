#!/bin/bash
# scripts/hicache_serve.sh
# 按官方 docs/advanced_features/hicache_best_practices.md 例子启动 SGLang HiCache
# (基于 §"Deployment with HF3FS" L80-98, 去 PD 部分)
#
# 用法: hicache_serve.sh <cache_dir> [write_policy] [extra_args...]
#
# 关键:
#   - 必须用 SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR 环境变量设 L3 目录
#     (sglang 0.5.13 --file-storage-path CLI 参数不生效,源码 hicache_storage.py::HiCacheFile L344 仍读环境变量)
#   - 参数严格按 best_practices.md L11-19 + L80-98

set -e

CACHE_DIR=${1:?"cache_dir required (e.g. /mnt/ai_ssd0/cache_hicache)"}
WRITE_POLICY=${2:-write_through}
shift 2 || true
EXTRA_ARGS="$@"

source ~/llm/.venv/bin/activate

# 用环境变量设 L3 目录 (绕开 0.5.13 --file-storage-path CLI bug)
export SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR="$CACHE_DIR"
mkdir -p "$CACHE_DIR"
rm -rf "$CACHE_DIR"/*  # 每轮清空 L3 避免污染

echo "==== Starting SGLang HiCache ===="
echo "L3 cache dir: $CACHE_DIR"
echo "Write policy: $WRITE_POLICY"
echo "Model: Qwen3-4B-Instruct-2507"
echo "Extra args: $EXTRA_ARGS"
echo "================================"

python -m sglang.launch_server \
    --model-path /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --port 30000 \
    --context-length 8192 \
    --mem-fraction-static 0.7 \
    --page-size 64 \
    --enable-metrics \
    --enable-cache-report \
    --enable-hierarchical-cache \
    --hicache-ratio 2 \
    --hicache-size 0 \
    --hicache-mem-layout page_first_direct \
    --hicache-io-backend direct \
    --hicache-write-policy "$WRITE_POLICY" \
    --hicache-storage-backend file \
    --hicache-storage-prefetch-policy timeout \
    $EXTRA_ARGS