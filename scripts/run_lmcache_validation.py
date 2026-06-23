#!/usr/bin/env python3
"""
Benchmark: vLLM Baseline vs LMCache GPU vs LMCache CPU Offload
Adapted from the reference validation doc, using Qwen3-4B on RTX 5080.
"""

import json
import time
import os
import sys
import argparse

# ---- Config ----
MODEL_PATH = "/home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507"
DATASET_PATH = "/home/ficus/llm/storage/datasets/sharegpt/ShareGPT_V3_unfiltered_cleaned_split_no_imsorry.json"
NUM_PROMPTS = 500
MAX_TOKENS = 128
TEMPERATURE = 0.7
NUM_TRIALS = 3
SEED = 42

OUT_DIR = "/home/ficus/llm/infer/ai_ssd_prestudy/results/lmcache_validation"
os.makedirs(OUT_DIR, exist_ok=True)


def load_prompts(dataset_path: str, n: int) -> list[str]:
    """Load first N human prompts from ShareGPT dataset."""
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
    """Run a single benchmark trial."""
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

    llm = LLM(
        model=MODEL_PATH,
        gpu_memory_utilization=0.7,
        trust_remote_code=True,
        dtype='bfloat16',
        max_model_len=8192,
        enforce_eager=True,
        kv_transfer_config=ktc,
        seed=SEED + trial,
    )

    sampling_params = SamplingParams(
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
    )

    t0 = time.time()
    outputs = llm.generate(prompts, sampling_params)
    elapsed = time.time() - t0

    # Token counting
    total_prompt_tokens = sum(len(o.prompt_token_ids) for o in outputs)
    total_output_tokens = sum(len(o.outputs[0].token_ids) for o in outputs)
    total_tokens = total_prompt_tokens + total_output_tokens

    result = {
        "trial": trial,
        "name": name,
        "elapsed_s": round(elapsed, 2),
        "total_tokens": total_tokens,
        "prompt_tokens": total_prompt_tokens,
        "output_tokens": total_output_tokens,
        "throughput_tok_per_s": round(total_tokens / elapsed, 1),
        "output_throughput_tok_per_s": round(total_output_tokens / elapsed, 1),
        "requests_completed": len(outputs),
        "config": {
            "model": MODEL_PATH,
            "use_lmcache": use_lmcache,
            "lmcache_config": lmcache_config_file,
            "gpu_memory_utilization": 0.7,
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
            "num_prompts": NUM_PROMPTS,
        },
    }

    print(f"  Elapsed: {elapsed:.2f}s")
    print(f"  Prompt tokens: {total_prompt_tokens}")
    print(f"  Output tokens: {total_output_tokens}")
    print(f"  Total tokens: {total_tokens}")
    print(f"  Throughput (total): {result['throughput_tok_per_s']:.0f} tok/s")
    print(f"  Throughput (output): {result['output_throughput_tok_per_s']:.0f} tok/s")

    del llm
    import torch
    torch.cuda.empty_cache()
    import gc
    gc.collect()

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["baseline", "lmcache_gpu", "lmcache_cpu", "all"],
                       default="all")
    args = parser.parse_args()

    print("=" * 60)
    print("  KV Cache Benchmark: vLLM + LMCache Validation")
    print("=" * 60)
    print(f"  Model: Qwen3-4B-Instruct")
    print(f"  GPU: RTX 5080 (16GB)")
    print(f"  Prompts: {NUM_PROMPTS}")
    print(f"  Max tokens: {MAX_TOKENS}")
    print(f"  Trials per config: {NUM_TRIALS}")

    prompts = load_prompts(DATASET_PATH, NUM_PROMPTS)

    configs = []
    if args.only in ("baseline", "all"):
        configs.append(("vLLM Baseline", False, None))
    if args.only in ("lmcache_gpu", "all"):
        configs.append(("LMCache GPU", True,
                        "/home/ficus/llm/infer/ai_ssd_prestudy/scripts/lmcache_gpu_config.yaml"))
    if args.only in ("lmcache_cpu", "all"):
        configs.append(("LMCache CPU", True,
                        "/home/ficus/llm/infer/ai_ssd_prestudy/scripts/lmcache_cpu_config.yaml"))

    all_results = {}
    for name, use_lmcache, lmcache_yaml in configs:
        trial_results = []
        for t in range(1, NUM_TRIALS + 1):
            result = run_benchmark(name, prompts, use_lmcache, lmcache_yaml, trial=t)
            trial_results.append(result)

            # Save individual trial
            fname = name.lower().replace(" ", "_").replace("+", "_")
            outpath = os.path.join(OUT_DIR, f"{fname}_trial{t}.json")
            with open(outpath, "w") as f:
                json.dump(result, f, indent=2)
            print(f"  Saved: {outpath}")

        # Compute averages
        avg_elapsed = sum(r["elapsed_s"] for r in trial_results) / NUM_TRIALS
        avg_total_tok = sum(r["total_tokens"] for r in trial_results) / NUM_TRIALS
        avg_output_tok = sum(r["output_tokens"] for r in trial_results) / NUM_TRIALS
        avg_throughput = sum(r["throughput_tok_per_s"] for r in trial_results) / NUM_TRIALS
        avg_output_throughput = sum(r["output_throughput_tok_per_s"] for r in trial_results) / NUM_TRIALS

        totals = {
            "name": name,
            "trials": trial_results,
            "average": {
                "elapsed_s": round(avg_elapsed, 2),
                "total_tokens": round(avg_total_tok, 0),
                "output_tokens": round(avg_output_tok, 0),
                "throughput_tok_per_s": round(avg_throughput, 1),
                "output_throughput_tok_per_s": round(avg_output_throughput, 1),
            }
        }
        all_results[name] = totals

        # Save summary
        rkey = name.lower().replace(" ", "_").replace("+", "_")
        outpath = os.path.join(OUT_DIR, f"{rkey}_summary.json")
        with open(outpath, "w") as f:
            json.dump(totals, f, indent=2)
        print(f"  Summary saved: {outpath}")

    # Print comparison table
    print("\n")
    print("=" * 70)
    print("  RESULTS SUMMARY")
    print("=" * 70)
    print(f"  {'Config':<25} {'Throughput':>15} {'Output Throughput':>18} {'Elapsed':>10}")
    print(f"  {'-'*25} {'-'*15} {'-'*18} {'-'*10}")
    for name, data in all_results.items():
        a = data["average"]
        print(f"  {name:<25} {a['throughput_tok_per_s']:>10.0f} tok/s  {a['output_throughput_tok_per_s']:>10.0f} tok/s  {a['elapsed_s']:>8.1f}s")
    print("=" * 70)

    # Save consolidated results
    all_out = os.path.join(OUT_DIR, "all_results.json")
    with open(all_out, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n  Consolidated results: {all_out}")


if __name__ == "__main__":
    main()
