#!/usr/bin/env python3
"""Aggregate LMCache benchmark results into a summary table."""
import json
from pathlib import Path
import statistics

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def load_trial(backend: str, trial: int) -> dict:
    p = RESULTS_DIR / f"{backend}_trial{trial}.json"
    return json.loads(p.read_text())


def summarize(backend: str) -> dict:
    p1_means = []
    p2_means = []
    speedups = []
    for t in range(3):
        r = load_trial(backend, t)
        p1_means.append(r["pass1_ttft_mean_s"])
        p2_means.append(r["pass2_ttft_mean_s"])
        speedups.append(r["speedup"])
    return {
        "pass1_mean_ms": statistics.mean(p1_means) * 1000,
        "pass1_std_ms": statistics.stdev(p1_means) * 1000,
        "pass2_mean_ms": statistics.mean(p2_means) * 1000,
        "pass2_std_ms": statistics.stdev(p2_means) * 1000,
        "speedup_mean": statistics.mean(speedups),
        "speedup_std": statistics.stdev(speedups),
        "pass1_p50_ms": statistics.median(p1_means) * 1000,
        "pass2_p50_ms": statistics.median(p2_means) * 1000,
        "trials": [
            {"trial": t, "p1_ms": r["pass1_ttft_mean_s"] * 1000,
             "p2_ms": r["pass2_ttft_mean_s"] * 1000, "speedup": r["speedup"]}
            for t, r in enumerate(
                load_trial(backend, t) for t in range(3)
            )
        ],
    }


def main():
    rows = []
    for backend in ["disabled", "cpu", "gpu"]:
        s = summarize(backend)
        rows.append((backend, s))

    # Markdown table
    print("\n## LMCache Benchmark Summary (Qwen3-4B-Instruct-2507, 8 KILLER prompts × 2 passes, 3 trials)\n")
    print("| Backend | Pass1 mean TTFT (ms) | Pass2 mean TTFT (ms) | Speedup (Pass1/Pass2) |")
    print("|---|---|---|---|")
    for backend, s in rows:
        print(
            f"| `{backend}` | {s['pass1_mean_ms']:.1f} ± {s['pass1_std_ms']:.1f} "
            f"| {s['pass2_mean_ms']:.1f} ± {s['pass2_std_ms']:.1f} "
            f"| **{s['speedup_mean']:.3f}x** ± {s['speedup_std']:.3f} |"
        )

    print("\n### Per-trial breakdown\n")
    for backend, s in rows:
        print(f"**{backend}**:")
        print(f"| Trial | Pass1 (ms) | Pass2 (ms) | Speedup |")
        print(f"|---|---|---|---|")
        for t in s["trials"]:
            print(f"| {t['trial']} | {t['p1_ms']:.1f} | {t['p2_ms']:.1f} | {t['speedup']:.3f}x |")
        print()

    # LMCache vs baseline delta
    if len(rows) >= 3:
        baseline_su = rows[0][1]["speedup_mean"]
        cpu_su = rows[1][1]["speedup_mean"]
        gpu_su = rows[2][1]["speedup_mean"]
        print(f"### LMCache contribution over vllm prefix caching baseline")
        print(f"- Baseline (no LMCache): {baseline_su:.3f}x speedup = vllm prefix caching alone")
        print(f"- LMCache CPU:           {cpu_su:.3f}x speedup ({cpu_su - baseline_su:+.3f}x over baseline)")
        print(f"- LMCache GPU:           {gpu_su:.3f}x speedup ({gpu_su - baseline_su:+.3f}x over baseline)")


if __name__ == "__main__":
    main()