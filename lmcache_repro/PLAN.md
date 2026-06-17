# LMCache 0.4.6 Reproducibility Benchmark (Plan)

Reproduces the methodology from `~/llm/storage/kv_cache_benchmark/vllm_lmcache_validate/validation_results.md`
for the **LMCache portion only** (not the kv-cache.py section).

## Status (2026-06-17)

✅ **Smoke test PASSED on Qwen3-4B-Instruct-2507** — all 3 backends run end-to-end.
🚧 **Need to run full 3-trial matrix next.**

### Hardware reality (resolved)
- 2× RTX 5080 / RTX 5060 Ti, each 16 GB
- Original plan: Qwen2.5-7B-Instruct (14.29 GiB bf16) → **OOM** in 16 GB GPU
- **Switched to Qwen3-4B-Instruct-2507** (~8 GB bf16) per user direction. Fits with 2.45 GiB KV headroom.

### Smoke test results (1 trial, 8 KILLER prompts × 2 passes)

| Backend | Pass1 mean TTFT | Pass2 mean TTFT | Speedup |
|---|---|---|---|
| `disabled` (no LMCache) | 317.8 ms | 206.6 ms | **1.54×** |
| `cpu` (LMCache CPU offload) | 368.1 ms | 214.2 ms | **1.72×** |
| `gpu` (LMCache GPU only) | 348.0 ms | 222.6 ms | **1.56×** |

Observations:
- **All backends show Pass2 > Pass1** — that's vllm's built-in **prefix caching** kicking in
  (the same 8 prompts in Pass2 share token prefixes with Pass1's KV cache)
- **LMCache CPU adds another ~0.18×** on top of prefix caching (368 → 214 ms vs 318 → 207 ms)
- LMCache hit confirmed in CPU backend log: `LMCache hit tokens: 256`
- Model load time: ~10 s on RTX 5080

### API compatibility check (LMCache 0.3.12 → 0.4.6) ✅

| Setting | 0.3.12 | 0.4.6 | Compatible? |
|---|---|---|---|
| `KVTransferConfig(kv_connector="LMCacheConnectorV1")` | Yes | Yes | ✅ |
| `kv_role="kv_both"` | Yes | Yes | ✅ |
| `LMCACHE_CHUNK_SIZE=256` | Yes | Yes (default) | ✅ |
| `LMCACHE_LOCAL_CPU=True/False` | Yes | Yes (default True) | ✅ |
| `LMCACHE_MAX_LOCAL_CPU_SIZE=32` | Yes | Yes (default 5.0) | ✅ |

vllm 0.22.1 still ships `LMCacheConnectorV1` at `vllm/distributed/kv_transfer/kv_connector/v1/lmcache_connector.py`.

### vllm API gotchas hit during smoke test
1. `first_token_time` → use `first_token_latency` (pre-computed, units: seconds)
2. `metrics` may be None unless `disable_log_stats=False`
3. TTFT is wall-clock from request arrival to first token

## Protocol

```
For each backend ∈ {disabled, cpu, gpu}:
  For trial in 0..num_trials:
    Pass 1: send 8 KILLER prompts → measure TTFT each
    Pass 2: re-send same 8 KILLER prompts → measure TTFT each
    Speedup = mean(Pass1_TTFT) / mean(Pass2_TTFT)
```

3 backends × 3 trials = **9 runs**, each ~1 min (10s load + 30s Pass1 + 30s Pass2 + cooldowns).

## Layout

```
lmcache_repro/
├── configs/
│   ├── lmcache_cpu_config.yaml     # LMCACHE_LOCAL_CPU=True, max=32GB
│   └── lmcache_gpu_config.yaml     # LMCACHE_LOCAL_CPU=False
├── scripts/
│   ├── lmcache_bench.py            # Main driver (pass1/pass2 protocol)
│   └── run_full_benchmark.sh       # Orchestrator (3 trials × 3 backends)
├── results/                         # Per-trial JSON output
└── logs/                            # Per-trial vllm logs
```

## Next steps

1. Run full 3-trial matrix (~10 min total)
2. Build summary table comparing 3 backends across 3 trials
3. Add a one-page write-up comparing against validation_results.md expectations