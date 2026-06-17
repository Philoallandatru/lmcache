#!/usr/bin/env python3
"""
500-prompt LMCache throughput benchmark.
Adapted from run-lmcache-validation.py template for Qwen2.5-14B-AWQ.

Runs 3 configs (baseline / LMCache CPU / LMCache GPU) × 1-3 trials each.
Measures throughput (tok/s) from offline inference.

Usage:
  cd ~/llm/infer/ai_ssd_prestudy/lmcache_repro
  source ~/llm/.venv/bin/activate
  python scripts/bench_500_prompts.py [--only baseline|lmcache_gpu|lmcache_cpu|all]
"""

import json
import time
import os
import sys
import argparse

# ---- User-configurable knobs ----
MODEL_PATH = "/home/ficus/llm/models/Qwen/Qwen2___5-14B-Instruct-AWQ"
DATASET_PATH = "/home/ficus/llm/storage/datasets/sharegpt/ShareGPT_V3_unfiltered_cleaned_split_no_imsorry.json"
NUM_PROMPTS = 500
MAX_TOKENS = 128
TEMPERATURE = 0.7
NUM_TRIALS = 1
SEED = 42

OUT_DIR = "results/lmcache_500prompts"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LMCACHE_GPU_YAML = os.path.join(SCRIPT_DIR, "..", "configs", "lmcache_gpu_config.yaml")
LMCACHE_CPU_YAML = os.path.join(SCRIPT_DIR, "..", "configs", "lmcache_cpu_config.yaml")


def load_prompts(dataset_path: str, n: int) -> list[str]:
    """Load first N human prompts from ShareGPT-format dataset."""
    with open(dataset_path) as f:
        data = json.load(f)
    prompts = []
    for conv in data:
        if len(prompts) >= n:
            break
        if 'conversations' in conv:
            for msg in conv['conversations']:
                if msg.get('from') == 'human':
                    prompts.append(msg.get('value', '')[:2048])
                    break
    print(f"  Loaded {len(prompts)} prompts from dataset")
    return prompts


def run_benchmark(name: str, prompts: list[str],
                  use_lmcache: bool = False,
                  lmcache_config_file: str | None = None,
                  trial: int = 1) -> dict:
    """Run a single benchmark trial. Replicates report methodology exactly."""
    os.environ.pop("LMCACHE_CONFIG_FILE", None)
    if lmcache_config_file:
        os.environ["LMCACHE_CONFIG_FILE"] = lmcache_config_file

    print(f"\n{'='*60}")
    print(f"  {name} - Trial {trial}")
    print(f"{'='*60}")

    from vllm import LLM, SamplingParams
    from vllm.config import KVTransferConfig

    ktc = None
    if use_lmcache:
        ktc = KVTransferConfig(
            kv_connector="LMCacheConnectorV1",
            kv_role="kv_both",
        )

    t0 = time.time()
    llm = LLM(
        model=MODEL_PATH,
        gpu_memory_utilization=0.75,
        trust_remote_code=True,
        dtype="bfloat16",
        max_model_len=4096,
        max_num_seqs=8,
        enforce_eager=True,
        enable_prefix_caching=False,  # KEY: match report's vLLM 0.13 behavior
        kv_transfer_config=ktc,
        seed=SEED + trial,
    )
    t_load = time.time() - t0
    print(f"  Model loaded in {t_load:.1f}s")

    sampling_params = SamplingParams(
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
    )

    t1 = time.time()
    outputs = llm.generate(prompts, sampling_params)
    elapsed = time.time() - t1

    total_prompt_tokens = sum(len(o.prompt_token_ids) for o in outputs)
    total_output_tokens = sum(len(o.outputs[0].token_ids) for o in outputs)
    total_tokens = total_prompt_tokens + total_output_tokens

    # Per-request TTFT
    ttfts = []
    for o in outputs:
        m = o.metrics
        if m is not None:
            ftl = getattr(m, "first_token_latency", None)
            if ftl is not None and ftl > 0:
                ttfts.append(ftl * 1000)  # convert to ms

    avg_ttft_ms = sum(ttfts) / max(len(ttfts), 1) if ttfts else 0

    result = {
        "trial": trial,
        "name": name,
        "model_load_time_s": round(t_load, 1),
        "elapsed_s": round(elapsed, 2),
        "total_tokens": total_tokens,
        "prompt_tokens": total_prompt_tokens,
        "output_tokens": total_output_tokens,
        "throughput_tok_per_s": round(total_tokens / elapsed, 1),
        "output_throughput_tok_per_s": round(total_output_tokens / elapsed, 1),
        "avg_ttft_ms": round(avg_ttft_ms, 1),
        "requests_completed": len(outputs),
        "config": {
            "model": MODEL_PATH,
            "use_lmcache": use_lmcache,
            "gpu_memory_utilization": 0.75,
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
            "num_prompts": NUM_PROMPTS,
        },
    }

    print(f"  Elapsed: {elapsed:.2f}s")
    print(f"  Prompt tokens: {total_prompt_tokens}")
    print(f"  Output tokens: {total_output_tokens}")
    print(f"  Throughput (total): {result['throughput_tok_per_s']:.0f} tok/s")
    print(f"  Throughput (output): {result['output_throughput_tok_per_s']:.0f} tok/s")
    print(f"  Avg TTFT: {avg_ttft_ms:.1f} ms")

    del llm
    import torch
    torch.cuda.empty_cache()
    import gc
    gc.collect()

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only",
                        choices=["baseline", "lmcache_gpu", "lmcache_cpu", "all"],
                        default="all")
    args = parser.parse_args()

    print("=" * 60)
    print("  500-Prompt Throughput Benchmark: vLLM + LMCache")
    print("=" * 60)
    print(f"  Model: {MODEL_PATH}")
    print(f"  Prompts: {NUM_PROMPTS}, Max tokens: {MAX_TOKENS}")
    print(f"  Trials per config: {NUM_TRIALS}")

    os.makedirs(OUT_DIR, exist_ok=True)

    prompts = load_prompts(DATASET_PATH, NUM_PROMPTS)
    print(f"  Prompt samples: min_len={min(len(p) for p in prompts)}, "
          f"max_len={max(len(p) for p in prompts)}")

    configs = []
    if args.only in ("baseline", "all"):
        configs.append(("vLLM Baseline", False, None))
    if args.only in ("lmcache_gpu", "all"):
        configs.append(("LMCache GPU", True, LMCACHE_GPU_YAML))
    if args.only in ("lmcache_cpu", "all"):
        configs.append(("LMCache CPU", True, LMCACHE_CPU_YAML))

    all_results = {}
    for name, use_lmcache, lmcache_yaml in configs:
        trial_results = []
        for t in range(1, NUM_TRIALS + 1):
            result = run_benchmark(name, prompts, use_lmcache, lmcache_yaml, trial=t)
            trial_results.append(result)

            fname = name.lower().replace(" ", "_").replace("+", "_")
            outpath = os.path.join(OUT_DIR, f"{fname}_trial{t}.json")
            with open(outpath, "w") as f:
                json.dump(result, f, indent=2)
            print(f"  Saved: {outpath}")

        avg_elapsed = sum(r["elapsed_s"] for r in trial_results) / NUM_TRIALS
        avg_total_tok = sum(r["total_tokens"] for r in trial_results) / NUM_TRIALS
        avg_output_tok = sum(r["output_tokens"] for r in trial_results) / NUM_TRIALS
        avg_throughput = sum(r["throughput_tok_per_s"] for r in trial_results) / NUM_TRIALS
        avg_output_throughput = sum(r["output_throughput_tok_per_s"] for r in trial_results) / NUM_TRIALS
        avg_ttft = sum(r["avg_ttft_ms"] for r in trial_results) / NUM_TRIALS

        totals = {
            "name": name,
            "trials": trial_results,
            "average": {
                "elapsed_s": round(avg_elapsed, 2),
                "total_tokens": round(avg_total_tok, 0),
                "output_tokens": round(avg_output_tok, 0),
                "throughput_tok_per_s": round(avg_throughput, 1),
                "output_throughput_tok_per_s": round(avg_output_throughput, 1),
                "avg_ttft_ms": round(avg_ttft, 1),
            }
        }
        all_results[name] = totals

        rkey = name.lower().replace(" ", "_").replace("+", "_")
        outpath = os.path.join(OUT_DIR, f"{rkey}_summary.json")
        with open(outpath, "w") as f:
            json.dump(totals, f, indent=2)
        print(f"  Summary saved: {outpath}")

    # Print comparison table
    print("\n" + "=" * 70)
    print("  RESULTS SUMMARY (500 Prompts)")
    print("=" * 70)
    header = f"  {'Config':<25} {'Throughput':>12} {'Out Thru':>12} {'TTFT':>10} {'Elapsed':>10}"
    print(header)
    print(f"  {'-'*25} {'-'*12} {'-'*12} {'-'*10} {'-'*10}")
    for name, data in all_results.items():
        a = data["average"]
        print(f"  {name:<25} {a['throughput_tok_per_s']:>8.0f} tok/s  "
              f"{a['output_throughput_tok_per_s']:>8.0f} tok/s  "
              f"{a['avg_ttft_ms']:>6.1f} ms  {a['elapsed_s']:>8.1f}s")
    print("=" * 70)

    all_out = os.path.join(OUT_DIR, "all_results.json")
    with open(all_out, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n  Consolidated: {all_out}")


if __name__ == "__main__":
    main()
