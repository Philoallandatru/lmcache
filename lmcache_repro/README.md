# LMCache 0.4.6 Reproducibility Benchmark

Reproduces the LMCache portion of the validation report
(`~/llm/storage/kv_cache_benchmark/vllm_lmcache_validate/validation_results.md`)
on our hardware (Qwen3-4B-Instruct-2507, 2× RTX 5080/5060 Ti).

**📊 Headline result: see [RESULTS.md](RESULTS.md)**
- LMCache CPU backend adds **+0.09×** over vllm prefix caching baseline
- LMCache GPU backend is essentially a no-op (prefix cache already does the job)
- API 0.3.12 → 0.4.6 fully compatible

## Quick Start

```bash
# 1. Activate venv (vllm 0.22.1 + lmcache 0.4.6)
source ~/llm/.venv/bin/activate

# 2. Run full matrix (3 trials × 3 backends = 9 runs, ~5 min)
cd ~/llm/infer/ai_ssd_prestudy/lmcache_repro
./scripts/run_full_benchmark.sh 3

# 3. View summary
python3 scripts/analyze_results.py
```

## What it measures

| Backend | What it does |
|---|---|
| `disabled` | Baseline, no LMCache. Pass2 ~ 1.29× speedup from vllm prefix caching |
| `cpu` | LMCache offloads KV to host CPU RAM. Pass2 ~ 1.38× (prefix cache + LMCache) |
| `gpu` | LMCache keeps KV on GPU. Pass2 ~ 1.32× (prefix cache dominates) |

For each backend, the script sends 8 KILLER prompts twice:
- **Pass 1 (cold)**: KV cache empty, all prompts get full prefill
- **Pass 2 (hot)**: Same prompts, expect LMCache hit
- **Speedup** = mean(Pass1 TTFT) / mean(Pass2 TTFT)

See [PLAN.md](PLAN.md) for protocol details and historical blockers.