# LMCache 0.4.6 Reproducibility Benchmark

Reproduces the LMCache portion of the validation report
(`~/llm/storage/kv_cache_benchmark/vllm_lmcache_validate/validation_results.md`)
using **Qwen2.5-7B-Instruct** (locally cached) instead of Mistral-7B-Instruct-v0.2.

## Quick Start

```bash
# 1. Activate venv (vllm 0.22.1 + lmcache 0.4.6)
source ~/llm/.venv/bin/activate

# 2. Run full matrix (3 trials × 3 backends = 9 runs)
cd ~/llm/infer/ai_ssd_prestudy/lmcache_repro
./scripts/run_full_benchmark.sh 3

# Or run a single backend/trial
HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES=0 python3 scripts/lmcache_bench.py \
    --model /home/ficus/llm/models/Qwen/Qwen2.5-7B-Instruct \
    --backend cpu \
    --trial-id 0 \
    --output results/cpu_trial0.json
```

## What it measures

| Backend | What it does |
|---|---|
| `disabled` | Baseline, no LMCache. Pass2 TTFT ≈ Pass1 TTFT (sanity) |
| `cpu` | LMCache offloads KV to host CPU RAM. Pass2 should hit cache |
| `gpu` | LMCache keeps KV on GPU. Pass2 should hit cache |

For each backend, the script sends 8 KILLER prompts twice:
- **Pass 1 (cold)**: KV cache empty, all prompts get full prefill
- **Pass 2 (hot)**: Same prompts, expect LMCache hit
- **Speedup** = mean(Pass1 TTFT) / mean(Pass2 TTFT)

See [PLAN.md](PLAN.md) for the full protocol and current blockers.