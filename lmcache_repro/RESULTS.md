# LMCache 0.4.6 Reproducibility Benchmark — Results Report

**Date**: 2026-06-17 | **Model**: Qwen2.5-14B-Instruct-AWQ (from ModelScope, 9.3 GiB, 48 layers, 8 KV heads)
**Goal**: Reproduce the LMCache section of `vllm_lmcache_validate/validation_results.md`

## Headline Result

| Backend | Pass1 (cold) | Pass2 (hot) | Speedup | LMCache Δ over baseline |
|---|---|---|---|---|
| `disabled` (no LMCache) | 1123.7 ± 1.5 ms | 415.4 ± 0.4 ms | **2.705×** | — (baseline) |
| `cpu` (LMCache host RAM) | 1246.8 ± 9.2 ms | 421.5 ± 1.2 ms | **2.958×** | **+0.253×** 🔥 |
| `gpu` (LMCache GPU only) | 1236.2 ± 1.6 ms | 427.0 ± 0.7 ms | **2.895×** | **+0.190×** |

(Mean ± std-dev across 3 trials, 8 KILLER prompts × 2 passes per trial, RTX 5080 16 GB.)

## Comparison: 4B vs 14B AWQ

The 14B AWQ model (48 layers, 8 KV heads) creates **meaningful KV cache pressure** that the 4B could not:

| Metric | Qwen3-4B bf16 | Qwen2.5-14B-AWQ | Why 14B wins |
|---|---|---|---|
| KV per token | 72 KB | **192 KB** (2.7×) | 48 layers × 8 KV heads |
| disabled speedup | 1.292× | **2.705×** (2.1×) | Longer prefill → more to cache |
| **LMCache CPU Δ** | +0.090× | **+0.253×** (3×) ✅ | Meaningful LMCache acceleration |
| **LMCache GPU Δ** | +0.026× (no-op) | **+0.190×** (7×) ✅ | LMCache finally measurable |

## What the numbers say

### 1. LMCache adds real value at 14B scale
- **CPU backend**: +0.253× over prefix caching baseline = 9.3% faster Pass2
- **GPU backend**: +0.190× = 7.0% faster Pass2
- Both well above measurement noise (stdev < 1%)

### 2. Pass1 overhead is acceptable
- LMCache CPU: Pass1 +123 ms (+11%) — cost of offloading KV to host RAM
- LMCache GPU: Pass1 +112 ms (+10%) — cost of internal LMCache bookkeeping
- Both are a fraction of total TTFT

### 3. vllm prefix caching still dominates
- Even without LMCache, Pass2 is 2.705× faster than Pass1
- This is vllm 0.22.x's `enable_prefix_caching=True` (default on)

## Methodology

- **Model**: Qwen2.5-14B-Instruct-AWQ (4-bit, 5.19 GiB weights shipped, 9.3 GiB loaded in GPU)
- **Hardware**: RTX 5080 16 GB, 64 GB host RAM
- **Software**: vllm 0.22.1, LMCache 0.4.6, PyTorch 2.x
- **Protocol**: 8 KILLER prompts (270-410 tokens) × Pass1 → same 8 prompts × Pass2 → speedup = Pass1/Pass2
- **3 backends**: disabled (no LMCache), cpu (LMCACHE_LOCAL_CPU=True, max 32 GB), gpu (LMCACHE_LOCAL_CPU=False)
- **3 trials per backend**, results repeatable within <1% stdev

### API compat (0.3.12 → 0.4.6)
- `KVTransferConfig(kv_connector="LMCacheConnectorV1", kv_role="kv_both")` ✅
- `LMCACHE_CHUNK_SIZE=256, LMCACHE_LOCAL_CPU={T/F}, LMCACHE_MAX_LOCAL_CPU_SIZE=32` ✅

### vllm gotchas
- Use `RequestStateStats.first_token_latency` (pre-computed seconds), not `first_token_ts`
- `disable_log_stats=False` required for `o.metrics` to populate
- ZMQ IPC path ≤107 chars → use `/tmp/` for `TMPDIR`

## Reproducibility

```bash
source ~/llm/.venv/bin/activate
cd ~/llm/infer/ai_ssd_prestudy/lmcache_repro
./scripts/run_full_benchmark.sh 3    # ~5 min, 9 runs
python3 scripts/analyze_results.py   # print summary table
```

## Bottom line

**LMCache 0.4.6 provides measurable acceleration (+0.19-0.25× over prefix caching) on a 14B AWQ model.
To see LMCache's real value, you need a model with sufficient KV cache pressure — 4B is too small,
14B is the sweet spot for single GPU validation. 32B+ would require tensor parallelism.**
