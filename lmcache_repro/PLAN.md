# LMCache 0.4.6 Reproducibility Benchmark (Plan)

Reproduces the methodology from `~/llm/storage/kv_cache_benchmark/vllm_lmcache_validate/validation_results.md`
for the **LMCache portion only** (not the kv-cache.py section).

## Status (2026-06-17)

🚧 **Blocker: GPU memory insufficient for Qwen2.5-7B-Instruct bf16.**

### Hardware reality
- 2× RTX 5080 / RTX 5060 Ti, each 16 GB
- Qwen2.5-7B-Instruct bf16 weights = **14.29 GiB**
- vllm `gpu_memory_utilization=0.7` × 16 GB = 11.2 GB quota
- 14.29 > 11.2 → No room for KV cache → `ValueError: No available memory for the cache blocks`

### Tried options (all blocked)
1. `max_model_len=2048` → OOM at profile_run
2. `max_model_len=1024, max_num_seqs=4, gpu_mem_util=0.7` → KV cache budget -4.15 GiB
3. `cpu_offload_gb=4` (only effective for CPU backend) → not active for disabled baseline

### Decision options (need user input)

| Option | Trade-off |
|---|---|
| **A) Switch to Qwen3-4B-Instruct-2507** (~8 GB bf16) | Fits on 16 GB card with room for KV. Diverges from report's Mistral-7B → Qwen2.5-7B comparison but matches ai_ssd_prestudy baseline |
| **B) Use AWQ/GPTQ quantized 7B** (~4-6 GB) | Best fidelity to original 7B model. Need to download/quantize. AWQ for Qwen2.5-7B-Instruct exists on HF |
| **C) Force cpu_offload_gb even for disabled backend** | Slow. Weights swap to CPU RAM. Keeps Qwen2.5-7B-Instruct. TTFT will be unreliable |
| **D) Use a smaller KILLER prompt set** | Doesn't help; issue is model size, not prompt size |

## Protocol (independent of which model we pick)

```
For each backend ∈ {disabled, cpu, gpu}:
  For trial in 0..num_trials:
    Pass 1: send 8 KILLER prompts (long, 270-410 tokens each) → measure TTFT each
    Pass 2: re-send same 8 KILLER prompts → measure TTFT each
    Speedup = mean(Pass1_TTFT) / mean(Pass2_TTFT)
```

Expected:
- `disabled`: speedup ≈ 1.0 (no caching)
- `cpu`: speedup > 1 (LMCache hit on Pass 2)
- `gpu`: speedup similar to cpu or higher

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
├── logs/                            # Per-trial vllm logs
└── prompts/
```

## API compatibility check (LMCache 0.3.12 → 0.4.6) ✅

| Setting | 0.3.12 | 0.4.6 | Compatible? |
|---|---|---|---|
| `KVTransferConfig(kv_connector="LMCacheConnectorV1")` | Yes | Yes | ✅ |
| `kv_role="kv_both"` | Yes | Yes | ✅ |
| `LMCACHE_CHUNK_SIZE=256` | Yes | Yes (default) | ✅ |
| `LMCACHE_LOCAL_CPU=True/False` | Yes | Yes (default True) | ✅ |
| `LMCACHE_MAX_LOCAL_CPU_SIZE=32` | Yes | Yes (default 5.0) | ✅ |

vllm 0.22.1 still ships `LMCacheConnectorV1` at `vllm/distributed/kv_transfer/kv_connector/v1/lmcache_connector.py`.