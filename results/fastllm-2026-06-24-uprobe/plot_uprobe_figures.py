"""Generate experiment H uprobe + IO behavior figures

Data source:
- logs/uprobe_h2_disk.log (13 probes, 5060 DiskMergeMOE, 5059 CpuMergeMOE, 0 CUDA MoE)
- logs/biosnoop_b_test.log (2943 IO events)
- results/full_comparison_2026-06-23.json (7 configs × 10 requests)
"""
import json
import re
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from pathlib import Path

OUT_DIR = Path("/home/ficus/llm/infer/ai_ssd_prestudy/results/fastllm-2026-06-24-uprobe/figures")
OUT_DIR.mkdir(parents=True, exist_ok=True)

plt.rcParams['font.size'] = 11
plt.rcParams['figure.dpi'] = 100

# === Data 1: 7-config TPS comparison ===
# From REPORT.md (10 prompts averaged)
configs = [
    ("GPU (Dual-card TP)", 184.0, "llama.cpp", "#2ca02c"),
    ("CPU RAM\n(--moe_device numa)", 33.4, "ftllm", "#ff9f1c"),
    ("BIWIN SSD\next4 (hot)", 7.96, "ftllm", "#5b8bd0"),
    ("BIWIN SSD\next4 (cold)", 7.41, "ftllm", "#5b8bd0"),
    ("nvme2n1\nNTFS (hot)", 8.24, "ftllm", "#5b8bd0"),
    ("nvme2n1\nNTFS (cold)", 4.57, "ftllm", "#5b8bd0"),
    ("nvme1n1\nNTFS (hot)", 8.14, "ftllm", "#5b8bd0"),
    ("nvme1n1\nNTFS (cold)", 5.00, "ftllm", "#5b8bd0"),
    ("nvme3n1\nNTFS (hot)", 7.45, "ftllm", "#5b8bd0"),
    ("nvme3n1\nNTFS (cold)", 5.39, "ftllm", "#5b8bd0"),
]

# === Fig 1: TPS comparison + 25x gap annotation ===
fig, ax = plt.subplots(figsize=(13, 6))
labels = [c[0] for c in configs]
tps = [c[1] for c in configs]
colors = [c[3] for c in configs]
y_pos = list(range(len(configs)))
bars = ax.barh(y_pos, tps, color=colors, edgecolor='black', linewidth=0.6)
ax.set_yticks(y_pos)
ax.set_yticklabels(labels, fontsize=9)
ax.set_xlabel("Throughput (tokens/s)", fontsize=12, fontweight='bold')
ax.set_title("Qwen3-30B-A3B — 7 Configurations TPS Comparison (2026-06-23)", fontsize=13, fontweight='bold')
ax.invert_yaxis()
ax.grid(axis='x', alpha=0.3, linestyle='--')

# Value annotation
for i, (bar, t) in enumerate(zip(bars, tps)):
    ax.text(t + 2, bar.get_y() + bar.get_height()/2,
            f"{t:.1f}", va='center', fontsize=9, fontweight='bold')

# 25x annotation
ax.annotate('', xy=(0, 0), xytext=(0, 4),
            arrowprops=dict(arrowstyle='<->', color='red', lw=2))
ax.text(20, 2, '25.2x gap\n(llama.cpp fused CUDA MoE\n vs ftllm CpuMergeMOE + SSD IO)',
        fontsize=10, color='red', fontweight='bold',
        bbox=dict(boxstyle='round,pad=0.4', facecolor='lightyellow', edgecolor='red'))

# Legend
from matplotlib.patches import Patch
legend_elements = [
    Patch(facecolor='#2ca02c', label='llama.cpp (GPU fused MoE)'),
    Patch(facecolor='#ff9f1c', label='ftllm NUMA (MoE in CPU)'),
    Patch(facecolor='#5b8bd0', label='ftllm disk (MoE in CPU + SSD)'),
]
ax.legend(handles=legend_elements, loc='lower right', fontsize=10)
ax.set_xlim(0, 220)
plt.tight_layout()
plt.savefig(OUT_DIR / "fig01_tps_comparison_25x_gap.png", dpi=120, bbox_inches='tight')
plt.close()
print(f"[OK] {OUT_DIR}/fig01_tps_comparison_25x_gap.png")

# === Fig 2: Experiment H uprobe call counts (key evidence) ===
fig, ax = plt.subplots(figsize=(10, 6))
moe_funcs = [
    "DiskMergeMOE::Run\n(ftllm disk mode)",
    "CpuMergeMOE::Run\n(ftllm disk mode)",
    "NumasFusedMOE::Run\n(ftllm numa mode)",
    "CudaHalfMergeMOEGGUF\n(ftllm disk mode)",
    "CudaBFloat16MergeMOEGGUF\n(ftllm disk mode)",
    "CudaFloatMergeMOEGGUF\n(ftllm disk mode)",
]
moe_calls = [5060, 5059, 0, 0, 0, 0]
moe_colors = ['#d62728', '#ff9f1c', '#7f7f7f', '#2ca02c', '#2ca02c', '#2ca02c']
y_pos = list(range(len(moe_funcs)))
bars = ax.barh(y_pos, moe_calls, color=moe_colors, edgecolor='black', linewidth=0.6)
ax.set_yticks(y_pos)
ax.set_yticklabels(moe_funcs, fontsize=10)
ax.set_xlabel("Call Count (60s window, 5 requests × 80 tokens)", fontsize=11, fontweight='bold')
ax.set_title("Experiment H — uprobe: ftllm MoE Function Call Distribution\n(2026-06-24, bpftrace on libfastllm.so)",
             fontsize=12, fontweight='bold')
ax.invert_yaxis()
ax.grid(axis='x', alpha=0.3, linestyle='--')

# Value + label
for i, (bar, c) in enumerate(zip(bars, moe_calls)):
    if c > 0:
        ax.text(c + 100, bar.get_y() + bar.get_height()/2,
                f"{c}", va='center', fontsize=11, fontweight='bold', color='red')
    else:
        ax.text(50, bar.get_y() + bar.get_height()/2,
                f"0  [none]", va='center', fontsize=11, fontweight='bold', color='gray')

# Key annotation
ax.text(3500, 0.5, 'KEY FINDING:\nGPU does NOT run MoE (CUDA 0 calls)\nMoE runs on CPU (CpuMergeMOE 5059 calls)\nDiskMergeMOE 5060 ~= CpuMergeMOE 5059 (1:1)',
        fontsize=10, color='black',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='lightyellow', edgecolor='red', linewidth=1.5))

ax.set_xlim(0, 6500)
plt.tight_layout()
plt.savefig(OUT_DIR / "fig02_uprobe_moe_call_distribution.png", dpi=120, bbox_inches='tight')
plt.close()
print(f"[OK] {OUT_DIR}/fig02_uprobe_moe_call_distribution.png")

# === Fig 3: MoE function latency distribution (dual histogram) ===
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

# DiskMergeMOE latency
disk_moe_buckets = [
    ("[1K, 2K)", 3286), ("[2K, 4K)", 194), ("[4K, 8K)", 481),
    ("[8K, 16K)", 317), ("[16K, 32K)", 385), ("[32K, 64K)", 345),
    ("[64K, 128K)", 3), ("[128K, 256K)", 11), ("[256K, 512K)", 22),
    ("[512K, 1M)", 17),
]
disk_labels = [b[0] for b in disk_moe_buckets]
disk_counts = [b[1] for b in disk_moe_buckets]
ax1.bar(range(len(disk_labels)), disk_counts, color='#d62728', edgecolor='black', linewidth=0.5)
ax1.set_xticks(range(len(disk_labels)))
ax1.set_xticklabels(disk_labels, rotation=45, ha='right', fontsize=8)
ax1.set_ylabel("Count", fontsize=11, fontweight='bold')
ax1.set_xlabel("Latency bucket (us)", fontsize=11, fontweight='bold')
ax1.set_title("DiskMergeMOE::Run Latency\n( 1-2ms, IO )", fontsize=11, fontweight='bold')
ax1.grid(axis='y', alpha=0.3, linestyle='--')
for i, c in enumerate(disk_counts):
    ax1.text(i, c + 50, str(c), ha='center', fontsize=8, fontweight='bold')
ax1.set_ylim(0, max(disk_counts) * 1.15)

# CpuMergeMOE latency
cpu_moe_buckets = [
    ("[512, 1K)", 3918), ("[1K, 2K)", 163), ("[2K, 4K)", 143),
    ("[4K, 8K)", 79), ("[8K, 16K)", 105), ("[16K, 32K)", 445),
    ("[32K, 64K)", 186), ("[64K, 128K)", 3), ("[128K, 256K)", 0),
    ("[256K, 512K)", 19),
]
cpu_labels = [b[0] for b in cpu_moe_buckets]
cpu_counts = [b[1] for b in cpu_moe_buckets]
ax2.bar(range(len(cpu_labels)), cpu_counts, color='#ff9f1c', edgecolor='black', linewidth=0.5)
ax2.set_xticks(range(len(cpu_labels)))
ax2.set_xticklabels(cpu_labels, rotation=45, ha='right', fontsize=8)
ax2.set_ylabel("Count", fontsize=11, fontweight='bold')
ax2.set_xlabel("Latency bucket (us)", fontsize=11, fontweight='bold')
ax2.set_title("CpuMergeMOE::Run Latency\n( 512μs-1ms, CPU MoE forward )", fontsize=11, fontweight='bold')
ax2.grid(axis='y', alpha=0.3, linestyle='--')
for i, c in enumerate(cpu_counts):
    ax2.text(i, c + 50, str(c), ha='center', fontsize=8, fontweight='bold')
ax2.set_ylim(0, max(cpu_counts) * 1.15)

fig.suptitle("Experiment H — MoE  (bpftrace uprobe)\n DiskMergeMOE (1-2ms) + CpuMergeMOE (0.5-1ms) = 1.5-3ms ",
             fontsize=12, fontweight='bold', y=1.02)
plt.tight_layout()
plt.savefig(OUT_DIR / "fig03_moe_latency_histogram.png", dpi=120, bbox_inches='tight')
plt.close()
print(f"[OK] {OUT_DIR}/fig03_moe_latency_histogram.png")

# === Fig 4: IO size distribution (biosnoop) ===
fig, ax = plt.subplots(figsize=(12, 5))
# Parsed from biosnoop log
io_size = {
    4096: 662, 8192: 22, 12288: 28, 16384: 46, 24576: 22, 32768: 131,
    49152: 32, 61440: 36, 65536: 366, 81920: 106, 98304: 95, 114688: 90,
    122880: 191, 126976: 435, 131072: 2614,
}
sizes = sorted(io_size.keys())
counts = [io_size[s] for s in sizes]
size_kb = [s / 1024 for s in sizes]
ax.bar(range(len(sizes)), counts, color='#5b8bd0', edgecolor='black', linewidth=0.5)
ax.set_xticks(range(len(sizes)))
ax.set_xticklabels([f"{kb:.0f}K" for kb in size_kb], rotation=45, ha='right', fontsize=9)
ax.set_ylabel("IO Count (total 5049)", fontsize=11, fontweight='bold')
ax.set_xlabel("IO Block Size", fontsize=11, fontweight='bold')
ax.set_title("bpftrace biosnoop: ftllm  IO  ( 4K  →  128KB  52%)\nBIWIN ext4 cold, 5 requests × 80 tokens",
             fontsize=12, fontweight='bold')
ax.grid(axis='y', alpha=0.3, linestyle='--')
for i, (s, c) in enumerate(zip(sizes, counts)):
    if c > 50:
        ax.text(i, c + 30, str(c), ha='center', fontsize=8, fontweight='bold')

# 128KB annotation
peak_idx = sizes.index(131072)
ax.annotate(': 128KB\n51.7% of all IO', xy=(peak_idx, 2614), xytext=(peak_idx - 5, 2800),
            arrowprops=dict(arrowstyle='->', color='red', lw=1.5),
            fontsize=10, color='red', fontweight='bold',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='lightyellow', edgecolor='red'))

plt.tight_layout()
plt.savefig(OUT_DIR / "fig04_io_size_histogram_128kb.png", dpi=120, bbox_inches='tight')
plt.close()
print(f"[OK] {OUT_DIR}/fig04_io_size_histogram_128kb.png")

# === Fig 5: 25x root cause -- data flow comparison ===
fig, ax = plt.subplots(figsize=(14, 5))
ax.set_xlim(0, 14)
ax.set_ylim(0, 6)
ax.axis('off')

# 3 stacks
# llama.cpp (green)
ax.add_patch(mpatches.FancyBboxPatch((0.3, 4.0), 2.5, 1.2, boxstyle="round,pad=0.05",
                                      facecolor='#2ca02c', edgecolor='black', linewidth=1.5))
ax.text(1.55, 4.6, "VRAM\n17.3 GB", ha='center', va='center', fontsize=11, fontweight='bold', color='white')
ax.add_patch(mpatches.FancyArrowPatch((1.55, 4.0), (1.55, 2.8), arrowstyle='->', mutation_scale=20, color='black', linewidth=1.5))
ax.add_patch(mpatches.FancyBboxPatch((0.3, 1.4), 2.5, 1.2, boxstyle="round,pad=0.05",
                                      facecolor='#2ca02c', edgecolor='black', linewidth=1.5))
ax.text(1.55, 2.0, "GPU fused\nCUDA MoE", ha='center', va='center', fontsize=11, fontweight='bold', color='white')
ax.text(1.55, 0.5, "184 tps", ha='center', va='center', fontsize=20, fontweight='bold', color='#2ca02c')
ax.text(1.55, 5.5, "llama.cpp dual-card TP", ha='center', va='center', fontsize=12, fontweight='bold')

# ftllm disk (blue) - 4 boxes
ax.add_patch(mpatches.FancyBboxPatch((5.5, 4.0), 1.6, 1.2, boxstyle="round,pad=0.05",
                                      facecolor='#1f77b4', edgecolor='black', linewidth=1.5))
ax.text(6.3, 4.6, "SSD\n128KB", ha='center', va='center', fontsize=10, fontweight='bold', color='white')
ax.add_patch(mpatches.FancyArrowPatch((6.3, 4.0), (6.3, 3.4), arrowstyle='->', mutation_scale=18, color='black', linewidth=1.5))

ax.add_patch(mpatches.FancyBboxPatch((5.5, 2.2), 1.6, 1.2, boxstyle="round,pad=0.05",
                                      facecolor='#d62728', edgecolor='black', linewidth=1.5))
ax.text(6.3, 2.8, "CPU\nCpuMergeMOE", ha='center', va='center', fontsize=8, fontweight='bold', color='white')
ax.add_patch(mpatches.FancyArrowPatch((6.3, 2.2), (6.3, 1.6), arrowstyle='->', mutation_scale=18, color='black', linewidth=1.5))

ax.add_patch(mpatches.FancyBboxPatch((5.5, 0.4), 1.6, 1.2, boxstyle="round,pad=0.05",
                                      facecolor='#ff9f1c', edgecolor='black', linewidth=1.5))
ax.text(6.3, 1.0, "GPU non-MoE\n()", ha='center', va='center', fontsize=8, fontweight='bold', color='white')
ax.text(6.3, -0.3, "7 tps", ha='center', va='center', fontsize=20, fontweight='bold', color='#d62728')
ax.text(6.3, 5.5, "ftllm disk", ha='center', va='center', fontsize=12, fontweight='bold')

# ftllm numa (orange)
ax.add_patch(mpatches.FancyBboxPatch((10.3, 4.0), 2.5, 1.2, boxstyle="round,pad=0.05",
                                      facecolor='#ff9f1c', edgecolor='black', linewidth=1.5))
ax.text(11.55, 4.6, "RAM\n(mmap SSD)", ha='center', va='center', fontsize=10, fontweight='bold', color='white')
ax.add_patch(mpatches.FancyArrowPatch((11.55, 4.0), (11.55, 2.8), arrowstyle='->', mutation_scale=20, color='black', linewidth=1.5))
ax.add_patch(mpatches.FancyBboxPatch((10.3, 1.4), 2.5, 1.2, boxstyle="round,pad=0.05",
                                      facecolor='#d62728', edgecolor='black', linewidth=1.5))
ax.text(11.55, 2.0, "CPU\n(numa  MoE)", ha='center', va='center', fontsize=10, fontweight='bold', color='white')
ax.text(11.55, 0.5, "33 tps", ha='center', va='center', fontsize=20, fontweight='bold', color='#ff9f1c')
ax.text(11.55, 5.5, "ftllm numa", ha='center', va='center', fontsize=12, fontweight='bold')

# Annotations
ax.text(7, 4.6, "1-2ms", fontsize=9, color='black', fontweight='bold')
ax.text(7, 2.8, "0.5-1ms", fontsize=9, color='black', fontweight='bold')
ax.text(7.5, 0.85, "GPU  CPU", fontsize=8, color='red', style='italic')

ax.set_title("Why 25x Gap? -- Data Flow Comparison (Experiment H uprobe)\n",
             fontsize=13, fontweight='bold', pad=15)
ax.text(7.5, 0.85, "GPU waits CPU", fontsize=8, color='red', style='italic')
plt.savefig(OUT_DIR / "fig05_dataflow_25x_root_cause.png", dpi=120, bbox_inches='tight')
plt.close()
print(f"[OK] {OUT_DIR}/fig05_dataflow_25x_root_cause.png")

print(f"\n=== Generated 5 figures to {OUT_DIR} ===")
import os
for f in sorted(os.listdir(OUT_DIR)):
    path = OUT_DIR / f
    print(f"  {f}  ({path.stat().st_size // 1024} KB)")
