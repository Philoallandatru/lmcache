#!/usr/bin/env bash
# 通用 vllm 启动脚本: LMCache local storage 真实 offload
# 用法: ./serve_lmcache.sh <cache_dir> <lmcache_yaml> <log_tag>
set -euo pipefail

CACHE_DIR="${1:?usage: $0 <cache_dir> <lmcache_yaml> <log_tag>}"
LMCACHE_YAML="${2:?missing lmcache_yaml}"
LOG_TAG="${3:?missing log_tag}"

MODEL_DIR="/home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507"
VLLM_LOG="/home/ficus/llm/infer/ai_ssd_prestudy/logs/vllm_${LOG_TAG}.log"

# 清空旧 cache, 避免上一轮 warm 数据污染本轮 cold 测量
rm -rf "$CACHE_DIR"/*
mkdir -p "$CACHE_DIR"

echo "[serve] cache_dir=$CACHE_DIR  log=$VLLM_LOG  yaml=$LMCACHE_YAML"

# 环境变量: LMCache 配置 + 双 GPU 顺序
export LMCACHE_CONFIG_FILE="$LMCACHE_YAML"
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=0   # 用 RTX 5060 Ti 跑 4B 模型(轻)
export TOKENIZERS_PARALLELISM=false

cd /home/ficus/llm
source .venv/bin/activate

exec vllm serve "$MODEL_DIR" \
    --max-model-len 8192 \
    --max-num-seqs 32 \
    --gpu-memory-utilization 0.7 \
    --port 8000 \
    --host 0.0.0.0 \
    --served-model-name Qwen3-4B-Instruct-2507 \
    --dtype bfloat16 \
    --enforce-eager \
    --no-enable-log-requests \
    --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
    > "$VLLM_LOG" 2>&1
