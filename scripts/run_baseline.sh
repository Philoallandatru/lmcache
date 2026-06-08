#!/usr/bin/env bash
set -uo pipefail
cd /home/ficus/llm
export LMCACHE_CONFIG_FILE=/home/ficus/llm/infer/ai_ssd_prestudy/scripts/lmcache_baseline.yaml
export LMCACHE_USE_DYNAMIC_VBUCKET=1
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=0
export TOKENIZERS_PARALLELISM=***
source .venv/bin/activate
exec vllm serve /home/ficus/llm/models/Qwen/Qwen2.5-7B-Instruct \
  --max-model-len 8192 \
  --max-num-seqs 32 \
  --gpu-memory-utilization 0.7 \
  --port 8000 \
  --host 0.0.0.0 \
  --served-model-name Qwen2.5-7B-Instruct \
  --dtype bfloat16 \
  --enforce-eager \
  --no-enable-log-requests \
  --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
  >> /home/ficus/llm/infer/ai_ssd_prestudy/logs/vllm_baseline.log 2>&1
