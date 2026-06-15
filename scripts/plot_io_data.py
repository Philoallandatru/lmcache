#!/usr/bin/env python3
"""
scripts/plot_io_data.py
从 ai_ssd_prestudy/results 画 10 张 IO profiling 图,存到 results/plots/

数据源:
  - results/l3_fio/*/seq1t,rand4k_1t,seq4t (4 盘 × 3 = 12 个 fio 文件)
  - results/hicache_v3/*/load_test.log (Phase2 v3, 4 盘)
  - results/hicache_14b_awq_v3/*/load_test.log (Phase4 v3, 4 盘)
  - results/hicache_multiclient_v3/*/load_test.log (Phase5 v3, 4 盘)
  - results/hicache_writeback_v3/*/load_test.log (Phase3 v3, 4 盘)
  - results/hicache_multiprompt/*/load_test.log (Phase7 v2, 4 盘)
  - results/hicache_32k/*/load_test.log (Phase8 32K, 4 盘)
  - iostat_*.log (4 盘时间序列)
  - cache_file_list.txt (4 盘 L3 file 计数)
"""
import os
import re
import json
import glob
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# 设置中文字体 (系统 fallback, 失败也不影响)
plt.rcParams['font.sans-serif'] = ['Noto Sans CJK SC', 'Noto Sans CJK HK', 'DejaVu Sans', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False

# 4 盘统一颜色
DISK_COLORS = {
    'WDC':     '#1f77b4',  # blue
    'BIWIN':   '#ff7f0e',  # orange
    'Seagate': '#2ca02c',  # green
    'ZHITAI':  '#d62728',  # red
}
DISK_ORDER = ['BIWIN', 'WDC', 'Seagate', 'ZHITAI']

ROOT = Path(__file__).parent.parent
RESULTS = ROOT / "results"
PLOTS = RESULTS / "plots"
PLOTS.mkdir(exist_ok=True)

DISK_DRIVE_MAP = {
    'BIWIN':   'baseline_biwin_ext4',
    'WDC':     'ai_ssd0_wdc_ntfs',
    'Seagate': 'ai_ssd1_seagate_ntfs',
    'ZHITAI':  'ai_ssd2_zhitai_ntfs',
}
DISK_NVME = {
    'BIWIN':   'nvme1n1',
    'WDC':     'nvme0n1',
    'Seagate': 'nvme2n1',
    'ZHITAI':  'nvme3n1',
}

def find_load_test(results_subdir, disk):
    """找 load_test.log,容忍 _wb / _drop 等后缀"""
    base = RESULTS / results_subdir
    candidates = [
        base / DISK_DRIVE_MAP[disk] / 'load_test.log',
        base / f"{DISK_DRIVE_MAP[disk]}_wb" / 'load_test.log',
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]  # 默认返回 path (不存在的)

# ----------------------------------------------------------------------
# 解析 fio 文件
# ----------------------------------------------------------------------
def parse_fio(path):
    """解析 fio 输出文件,返回 dict of metrics"""
    txt = path.read_text()
    metrics = {}

    # 主 IOPS / BW 行: "  read: IOPS=2632, BW=2632MiB/s ..."
    m = re.search(r'read:\s*IOPS=([\d.kKM]+),\s*BW=([\d.kKM]+)MiB/s', txt)
    if m:
        def parse_num(s):
            s = s.strip()
            if s.endswith('k'): return float(s[:-1]) * 1e3
            if s.endswith('K'): return float(s[:-1]) * 1e3
            if s.endswith('M'): return float(s[:-1]) * 1e6
            if s.endswith('G'): return float(s[:-1]) * 1e9
            return float(s)
        metrics['iops'] = parse_num(m.group(1))
        metrics['bw_MiBs'] = parse_num(m.group(2))

    # clat percentiles
    percentiles = {}
    for line in txt.split('\n'):
        m = re.match(r'\s*\|\s*([\d.]+)th=\[\s*(\d+)\]', line)
        if m:
            percentiles[f'p{m.group(1)}'] = int(m.group(2))  # usec
    if percentiles:
        metrics['clat'] = percentiles

    # 模式 (seq1t / rand4k_1t / seq4t)
    fname = path.stem
    if 'seq1t' in fname: metrics['mode'] = 'seq1t'
    elif 'rand4k' in fname: metrics['mode'] = 'rand4k_1t'
    elif 'seq4t' in fname: metrics['mode'] = 'seq4t'
    else: metrics['mode'] = 'unknown'

    return metrics

# ----------------------------------------------------------------------
# 解析 load_test.log (sglang hicache cold/warm)
# ----------------------------------------------------------------------
def parse_load_test(path):
    """解析 hicache_load_test.py log,返回 list of (label, latency_s)
    支持格式:
      [cold] latency=1.438s          (single client)
      [cold cid=0] latency=1.728s    (4 client, 取第一个)
    """
    entries = []
    seen_labels = set()
    for line in path.read_text().split('\n'):
        # 标准格式: [label] latency=...s
        # 4-client 格式: [label cid=N] latency=...s
        m = re.match(r'\[([\w_]+)(?:\s+cid=\d+)?\]\s+latency=([\d.]+)s', line)
        if m:
            label = m.group(1)
            # 4-client 时只取第一次 (cid=0)
            if label not in seen_labels:
                entries.append((label, float(m.group(2))))
                seen_labels.add(label)
    return entries

# ----------------------------------------------------------------------
# 解析 iostat 时间序列
# ----------------------------------------------------------------------
def parse_iostat(path):
    """解析 iostat -dxm 1 N 输出"""
    samples = []
    for line in path.read_text().split('\n'):
        # nvme0n1  0.12  0.01  ...
        m = re.match(r'(nvme\d+n\d+p?\d*)\s+([\d.]+)\s+([\d.]+)\s+', line)
        if m:
            samples.append({
                'dev': m.group(1),
                'rps': float(m.group(2)),
                'rMBs': float(m.group(3)),
            })
    return samples

# ----------------------------------------------------------------------
# 图 1: 4 盘 seq1t 带宽对比
# ----------------------------------------------------------------------
def plot_fio_bw():
    fig, ax = plt.subplots(figsize=(10, 6))
    modes = ['seq1t', 'seq4t']
    x = np.arange(len(modes))
    width = 0.2
    for i, disk in enumerate(DISK_ORDER):
        iops_list, bw_list = [], []
        for mode in modes:
            pattern = f"{RESULTS}/l3_fio/{disk}_*_{mode}.txt"
            files = glob.glob(pattern)
            if not files:
                # BIWIN special case
                files = glob.glob(f"{RESULTS}/l3_fio/{disk}_ext4_{mode}.txt")
            if files:
                m = parse_fio(Path(files[0]))
                bw_list.append(m.get('bw_MiBs', 0) / 1024)  # 转 GB/s
            else:
                bw_list.append(0)
        ax.bar(x + (i - 1.5) * width, bw_list, width,
               label=disk, color=DISK_COLORS[disk])
    ax.set_xlabel('Test mode')
    ax.set_ylabel('Sequential Read BW (GB/s)')
    ax.set_title('4 盘 Sequential Read BW (1024 KiB, libaio) - fio')
    ax.set_xticks(x)
    ax.set_xticklabels([m for m in modes])
    ax.legend(title='Drive', loc='upper left')
    ax.grid(axis='y', alpha=0.3)
    for i, disk in enumerate(DISK_ORDER):
        for j, mode in enumerate(modes):
            pattern = f"{RESULTS}/l3_fio/{disk}_*_{mode}.txt"
            files = glob.glob(pattern) or glob.glob(f"{RESULTS}/l3_fio/{disk}_ext4_{mode}.txt")
            if files:
                m = parse_fio(Path(files[0]))
                bw_gb = m.get('bw_MiBs', 0) / 1024
                ax.text(j + (i - 1.5) * width, bw_gb + 0.1, f'{bw_gb:.1f}',
                        ha='center', fontsize=8)
    plt.tight_layout()
    plt.savefig(PLOTS / "01_fio_bw.png", dpi=120)
    plt.close()
    print("✓ 01_fio_bw.png")

# ----------------------------------------------------------------------
# 图 2: 4 盘 random 4K IOPS 对比
# ----------------------------------------------------------------------
def plot_fio_iops():
    fig, ax = plt.subplots(figsize=(8, 6))
    iops_list = []
    for disk in DISK_ORDER:
        files = glob.glob(f"{RESULTS}/l3_fio/{disk}_*_rand4k_1t.txt")
        if not files:
            files = glob.glob(f"{RESULTS}/l3_fio/{disk}_ext4_rand4k_1t.txt")
        if files:
            m = parse_fio(Path(files[0]))
            iops_list.append(m.get('iops', 0) / 1000)  # K IOPS
        else:
            iops_list.append(0)
    bars = ax.bar(DISK_ORDER, iops_list,
                  color=[DISK_COLORS[d] for d in DISK_ORDER])
    ax.set_ylabel('Random 4K Read IOPS (K)')
    ax.set_title('4 盘 Random 4K Read IOPS (1 thread, iodepth=1) - fio')
    ax.grid(axis='y', alpha=0.3)
    for bar, val in zip(bars, iops_list):
        ax.text(bar.get_x() + bar.get_width()/2, val + 0.3, f'{val:.1f}K',
                ha='center', fontsize=10, fontweight='bold')
    plt.tight_layout()
    plt.savefig(PLOTS / "02_fio_rand4k_iops.png", dpi=120)
    plt.close()
    print("✓ 02_fio_rand4k_iops.png")

# ----------------------------------------------------------------------
# 图 3: 4 盘 latency percentile (p50/p99/p99.9)
# ----------------------------------------------------------------------
def plot_fio_latency_percentiles():
    fig, ax = plt.subplots(figsize=(10, 6))
    percentiles = ['p50.00', 'p90.00', 'p99.00', 'p99.90']
    x = np.arange(len(percentiles))
    width = 0.2
    for i, disk in enumerate(DISK_ORDER):
        lat_list = []
        # 用 seq4t (4 thread, 更接近 hicache 实际场景)
        files = glob.glob(f"{RESULTS}/l3_fio/{disk}_*_seq4t.txt")
        if not files:
            files = glob.glob(f"{RESULTS}/l3_fio/{disk}_ext4_seq4t.txt")
        if files:
            m = parse_fio(Path(files[0]))
            clat = m.get('clat', {})
            for p in percentiles:
                lat_list.append(clat.get(p, 0) / 1000)  # us -> ms
        else:
            lat_list = [0] * len(percentiles)
        ax.bar(x + (i - 1.5) * width, lat_list, width,
               label=disk, color=DISK_COLORS[disk])
    ax.set_xlabel('Latency percentile')
    ax.set_ylabel('Latency (ms)')
    ax.set_title('4 盘 Sequential Read Latency Percentiles (1024 KiB, 4 thread) - fio')
    ax.set_xticks(x)
    ax.set_xticklabels([p.replace('p', 'p') for p in percentiles])
    ax.set_yscale('log')
    ax.legend(title='Drive', loc='upper left')
    ax.grid(axis='y', alpha=0.3, which='both')
    plt.tight_layout()
    plt.savefig(PLOTS / "03_fio_latency_percentiles.png", dpi=120)
    plt.close()
    print("✓ 03_fio_latency_percentiles.png")

# ----------------------------------------------------------------------
# 图 4: 4 盘 cold/warm latency 加速比 (Phase2 v3)
# ----------------------------------------------------------------------
def plot_hicache_cold_warm():
    fig, ax = plt.subplots(figsize=(10, 6))
    cold_means, warm_means, accel = [], [], []
    for disk in DISK_ORDER:
        log_path = find_load_test('hicache_v3', disk)
        if not log_path.exists():
            cold_means.append(0); warm_means.append(0); accel.append(0); continue
        entries = parse_load_test(log_path)
        cold = next((l for lbl, l in entries if lbl == 'cold'), 0)
        warms = [l for lbl, l in entries if lbl.startswith('warm_')]
        warm_mean = np.mean(warms) if warms else 0
        cold_means.append(cold)
        warm_means.append(warm_mean)
        accel.append(cold / warm_mean if warm_mean else 0)

    x = np.arange(len(DISK_ORDER))
    width = 0.35
    ax.bar(x - width/2, cold_means, width, label='cold (1st req)',
           color='#d62728', alpha=0.8)
    ax.bar(x + width/2, warm_means, width, label='warm (mean of 5)',
           color='#2ca02c', alpha=0.8)
    for i, (c, w, a) in enumerate(zip(cold_means, warm_means, accel)):
        ax.text(i - width/2, c + 0.02, f'{c:.2f}s', ha='center', fontsize=9)
        ax.text(i + width/2, w + 0.02, f'{w:.2f}s', ha='center', fontsize=9)
        ax.text(i, max(c, w) + 0.15, f'accel={a:.2f}×', ha='center', fontsize=10,
                fontweight='bold', color='blue')
    ax.set_xticks(x)
    ax.set_xticklabels(DISK_ORDER)
    ax.set_ylabel('Latency (s)')
    ax.set_title('HiCache Cold vs Warm Latency (Phase2 v3, 4 盘)\n7K prompt × 6 rounds, single client', y=1.02)
    ax.legend(loc='upper right')
    ax.grid(axis='y', alpha=0.3)
    # accel 标注放在 warm bar 上方 (避免与 title 重叠)
    for i, a in enumerate(accel):
        ax.text(i + width/2, warm_means[i] + 0.05, f'accel={a:.2f}×', ha='center',
                fontsize=9, fontweight='bold', color='blue')
    plt.tight_layout()
    plt.savefig(PLOTS / "04_hicache_cold_warm.png", dpi=120)
    plt.close()
    print("✓ 04_hicache_cold_warm.png")

# ----------------------------------------------------------------------
# 图 5: Phase2-5 v3 4 盘 spread 横向对比
# ----------------------------------------------------------------------
def plot_phase_spread():
    phases = [
        ('Phase2 v3\n(4B 7K, write_through)', 'hicache_v3'),
        ('Phase3 v3\n(4B 7K, write_back)',    'hicache_writeback_v3'),
        ('Phase4 v3\n(14B-AWQ 7K)',           'hicache_14b_awq_v3'),
        ('Phase5 v3\n(4-client N=4 drop)',    'hicache_multiclient_v3'),
        ('Phase7 v2\n(multiprompt L2 evict)', 'hicache_multiprompt'),
        ('Phase8 32K\n(no data, OOM)',        'hicache_32k'),
    ]
    fig, ax = plt.subplots(figsize=(13, 6))
    x = np.arange(len(phases))
    width = 0.18
    for i, disk in enumerate(DISK_ORDER):
        cold_means = []
        for label, subdir in phases:
            log_path = find_load_test(subdir, disk)
            if not log_path.exists():
                cold_means.append(0); continue
            entries = parse_load_test(log_path)
            # Phase7/8 用 p0 cold (multiprompt), 其他用 cold
            cold = next((l for lbl, l in entries if lbl in ('cold', 'p0')), 0)
            cold_means.append(cold)
        ax.bar(x + (i - 1.5) * width, cold_means, width,
               label=disk, color=DISK_COLORS[disk])
    ax.set_xticks(x)
    ax.set_xticklabels([p[0] for p in phases], rotation=20, ha='right', fontsize=9)
    ax.set_ylabel('Cold Latency (s)')
    ax.set_title('Cold Latency 4 盘对比 (Phase2-8 v3)\nPhase7/8 含 multiprompt L2 evict')
    ax.legend(title='Drive', loc='upper left', ncol=4)
    ax.grid(axis='y', alpha=0.3)
    plt.tight_layout()
    plt.savefig(PLOTS / "05_phase_spread.png", dpi=120)
    plt.close()
    print("✓ 05_phase_spread.png")

# ----------------------------------------------------------------------
# 图 6: Phase7 v2 vs v3 (cache hit vs cold-from-device)
# ----------------------------------------------------------------------
def plot_cache_hit_vs_device():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    # v2: multiprompt cold-from-device
    v2_cold, v2_replay = [], []
    # v3: standard cold (cache hit warm)
    v3_cold, v3_warm = [], []
    for disk in DISK_ORDER:
        subdir = DISK_DRIVE_MAP[disk]
        # v2 multiprompt
        v2_path = RESULTS / "hicache_multiprompt" / subdir / "load_test.log"
        if v2_path.exists():
            entries = parse_load_test(v2_path)
            v2_cold.append(next((l for lbl, l in entries if lbl == 'p0'), 0))
            v2_replay.append(next((l for lbl, l in entries if lbl == 'replay_p0'), 0))
        else:
            v2_cold.append(0); v2_replay.append(0)
        # v3 standard
        v3_path = RESULTS / "hicache_v3" / subdir / "load_test.log"
        if v3_path.exists():
            entries = parse_load_test(v3_path)
            v3_cold.append(next((l for lbl, l in entries if lbl == 'cold'), 0))
            warms = [l for lbl, l in entries if lbl.startswith('warm_')]
            v3_warm.append(np.mean(warms) if warms else 0)
        else:
            v3_cold.append(0); v3_warm.append(0)

    x = np.arange(len(DISK_ORDER))
    width = 0.2

    # Left: v2 multiprompt
    ax1.bar(x - width/2, v2_cold, width, label='p0 (cold fill)', color='#1f77b4')
    ax1.bar(x + width/2, v2_replay, width, label='replay_p0 (L3 read)', color='#ff7f0e')
    for i, (c, r) in enumerate(zip(v2_cold, v2_replay)):
        ax1.text(i - width/2, c + 0.1, f'{c:.2f}s', ha='center', fontsize=8)
        ax1.text(i + width/2, r + 0.1, f'{r:.2f}s', ha='center', fontsize=8)
    ax1.set_xticks(x); ax1.set_xticklabels(DISK_ORDER)
    ax1.set_ylabel('Latency (s)')
    ax1.set_title('Phase7 v2 multiprompt (L3 真读盘)\n19.8 GB L3 file > page cache')
    ax1.legend()
    ax1.grid(axis='y', alpha=0.3)

    # Right: v3 standard
    ax2.bar(x - width/2, v3_cold, width, label='cold (1st)', color='#1f77b4')
    ax2.bar(x + width/2, v3_warm, width, label='warm (mean)', color='#2ca02c')
    for i, (c, w) in enumerate(zip(v3_cold, v3_warm)):
        ax2.text(i - width/2, c + 0.02, f'{c:.2f}s', ha='center', fontsize=8)
        ax2.text(i + width/2, w + 0.02, f'{w:.2f}s', ha='center', fontsize=8)
    ax2.set_xticks(x); ax2.set_xticklabels(DISK_ORDER)
    ax2.set_ylabel('Latency (s)')
    ax2.set_title('Phase2 v3 standard (L3 page cache hit)\n1.2 GB L3 file < page cache')
    ax2.legend()
    ax2.grid(axis='y', alpha=0.3)
    plt.tight_layout()
    plt.savefig(PLOTS / "06_cache_hit_vs_device.png", dpi=120)
    plt.close()
    print("✓ 06_cache_hit_vs_device.png")

# ----------------------------------------------------------------------
# 图 7: iostat time series (sglang 跑时)
# ----------------------------------------------------------------------
def plot_iostat_timeseries():
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), sharex=True)
    for ax, disk in zip(axes.flatten(), DISK_ORDER):
        subdir = DISK_DRIVE_MAP[disk]
        nvme = DISK_NVME[disk]
        iostat_path = RESULTS / "hicache_v3" / subdir / f"iostat_{nvme}.log"
        if not iostat_path.exists():
            ax.set_title(f"{disk} - no iostat data")
            continue
        samples = parse_iostat(iostat_path)
        if not samples:
            continue
        rMBs = [s['rMBs'] for s in samples]
        # 找非零段 (其他全 0 是空闲)
        nonzero_idx = [i for i, v in enumerate(rMBs) if v > 0.5]
        if not nonzero_idx:
            ax.set_title(f"{disk} - no IO activity")
            continue
        ax.plot(rMBs, color=DISK_COLORS[disk], linewidth=1.5)
        ax.fill_between(range(len(rMBs)), rMBs, alpha=0.3, color=DISK_COLORS[disk])
        ax.set_title(f"{disk} ({nvme}) - HiCache 跑时 read bandwidth")
        ax.set_ylabel('Read MB/s')
        ax.grid(alpha=0.3)
        max_v = max(rMBs)
        ax.text(0.02, 0.95, f'peak: {max_v:.1f} MB/s\nmean: {np.mean([v for v in rMBs if v > 0.5]):.1f} MB/s',
                transform=ax.transAxes, fontsize=9, verticalalignment='top',
                bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
    axes[-1, 0].set_xlabel('Sample index (1s interval)')
    axes[-1, 1].set_xlabel('Sample index (1s interval)')
    plt.suptitle('iostat time series during HiCache test (Phase2 v3, 4 盘)\nNVMe sequential read activity', y=1.02)
    plt.tight_layout()
    plt.savefig(PLOTS / "07_iostat_timeseries.png", dpi=120)
    plt.close()
    print("✓ 07_iostat_timeseries.png")

# ----------------------------------------------------------------------
# 图 8: L3 file count growth (cache_file_list.txt 最后大小)
# ----------------------------------------------------------------------
def plot_l3_file_count():
    fig, ax = plt.subplots(figsize=(9, 6))
    counts, sizes_gb = [], []
    for disk in DISK_ORDER:
        subdir = DISK_DRIVE_MAP[disk]
        # 数 cache_hicache 目录文件
        cache_dirs = [
            RESULTS / "hicache_v3" / subdir / "cache_hicache",
            RESULTS / "hicache_v3" / subdir / "cache_dir",
        ]
        cnt = 0
        for cd in cache_dirs:
            if cd.exists():
                cnt = len(list(cd.glob("*")))
                break
        counts.append(cnt)
        # file list
        fpath = RESULTS / "hicache_v3" / subdir / "cache_file_list.txt"
        sz = 0
        if fpath.exists():
            for line in fpath.read_text().split('\n'):
                m = re.match(r'\s*\S+\s+\S+\s+(\d+)', line)
                if m: sz += int(m.group(1))
        sizes_gb.append(sz / 1024**3)

    x = np.arange(len(DISK_ORDER))
    ax2 = ax.twinx()
    bars1 = ax.bar(x - 0.2, counts, 0.4, label='L3 file count', color='#1f77b4', alpha=0.8)
    bars2 = ax2.bar(x + 0.2, sizes_gb, 0.4, label='L3 total size (GB)', color='#ff7f0e', alpha=0.8)
    ax.set_xticks(x); ax.set_xticklabels(DISK_ORDER)
    ax.set_ylabel('File count', color='#1f77b4')
    ax2.set_ylabel('Total size (GB)', color='#ff7f0e')
    ax.set_title('L3 Cache File Count + Size after Phase2 v3 (4 盘)\n6 round × write_through, sglang 0.5.13')
    for i, (c, s) in enumerate(zip(counts, sizes_gb)):
        ax.text(i - 0.2, c + 1, f'{c}', ha='center', fontsize=10)
        ax2.text(i + 0.2, s + 0.01, f'{s:.2f}G', ha='center', fontsize=10)
    plt.tight_layout()
    plt.savefig(PLOTS / "08_l3_file_count.png", dpi=120)
    plt.close()
    print("✓ 08_l3_file_count.png")

# ----------------------------------------------------------------------
# 图 9: 决策雷达图 (4 盘综合评分)
# ----------------------------------------------------------------------
def plot_decision_radar():
    # 5 个维度: seq BW / rand IOPS / p99 latency (反) / cold hicache latency (反) / price (假设)
    # 评分 0-10, 越高越好
    metrics_by_disk = {}
    for disk in DISK_ORDER:
        # 找每个盘的 metrics
        f = glob.glob(f"{RESULTS}/l3_fio/{disk}_*_seq1t.txt")
        if not f: f = glob.glob(f"{RESULTS}/l3_fio/{disk}_ext4_seq1t.txt")
        seq_bw = parse_fio(Path(f[0]))['bw_MiBs'] / 1024 if f else 0  # GB/s
        # rand4k
        f = glob.glob(f"{RESULTS}/l3_fio/{disk}_*_rand4k_1t.txt")
        if not f: f = glob.glob(f"{RESULTS}/l3_fio/{disk}_ext4_rand4k_1t.txt")
        rand_iops = parse_fio(Path(f[0]))['iops'] / 1000 if f else 0  # K
        # seq4t p99 latency
        f = glob.glob(f"{RESULTS}/l3_fio/{disk}_*_seq4t.txt")
        if not f: f = glob.glob(f"{RESULTS}/l3_fio/{disk}_ext4_seq4t.txt")
        p99_us = parse_fio(Path(f[0]))['clat'].get('p99.00', 99999) if f else 99999
        p99_ms = p99_us / 1000
        # hicache cold-from-device (Phase7 v2 replay, 4 盘差异最大)
        v2_log = RESULTS / "hicache_multiprompt" / DISK_DRIVE_MAP[disk] / "load_test.log"
        cold = 0
        if v2_log.exists():
            entries = parse_load_test(v2_log)
            cold = next((l for lbl, l in entries if lbl == 'replay_p0'), 0)
        # 价格 (相对估值, WDC 4TB ~$300, Seagate 2TB ~$150, BIWIN 2TB ~$200, ZHITAI 2TB ~$180)
        price_score = {'WDC': 8, 'BIWIN': 7, 'Seagate': 9, 'ZHITAI': 6}.get(disk, 5)
        metrics_by_disk[disk] = {
            'seq_bw': seq_bw,
            'rand_iops': rand_iops,
            'p99_lat_ms': p99_ms,
            'cold_s': cold,
            'price': price_score,
        }

    # 归一化到 0-10
    def norm(direction, vals):
        vmin, vmax = min(vals), max(vals)
        if vmax == vmin: return [5] * len(vals)
        out = []
        for v in vals:
            if direction == 'higher_better':
                out.append((v - vmin) / (vmax - vmin) * 10)
            else:  # lower_better
                out.append((vmax - v) / (vmax - vmin) * 10)
        return out

    labels = ['seq BW\n(GB/s)', 'rand 4K\n(K IOPS)', 'p99 lat\n(ms, inv)',
              'hicache cold\n(s, inv)', 'price\n(higher=cheap)']
    n = len(labels)
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(9, 9), subplot_kw=dict(projection='polar'))
    # 4 盘值收集 (跨盘归一化)
    all_seq_bw = [metrics_by_disk[d]['seq_bw'] for d in DISK_ORDER]
    all_rand_iops = [metrics_by_disk[d]['rand_iops'] for d in DISK_ORDER]
    all_p99 = [metrics_by_disk[d]['p99_lat_ms'] for d in DISK_ORDER]
    all_cold = [metrics_by_disk[d]['cold_s'] for d in DISK_ORDER]
    all_price = [metrics_by_disk[d]['price'] for d in DISK_ORDER]

    for disk in DISK_ORDER:
        m = metrics_by_disk[disk]
        vals = [
            norm('higher_better', all_seq_bw)[DISK_ORDER.index(disk)],
            norm('higher_better', all_rand_iops)[DISK_ORDER.index(disk)],
            norm('lower_better',  all_p99)[DISK_ORDER.index(disk)],
            norm('lower_better',  all_cold)[DISK_ORDER.index(disk)],
            norm('higher_better', all_price)[DISK_ORDER.index(disk)],
        ]
        vals += vals[:1]
        ax.plot(angles, vals, 'o-', linewidth=2, label=disk, color=DISK_COLORS[disk])
        ax.fill(angles, vals, alpha=0.15, color=DISK_COLORS[disk])
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylim(0, 10)
    ax.set_yticks([2, 4, 6, 8, 10])
    ax.set_yticklabels(['2', '4', '6', '8', '10'], fontsize=8)
    ax.set_title('4 盘综合评分雷达图\n(seq BW, rand IOPS, p99 lat, hicache L3 cold-from-device, price)', y=1.10, fontsize=12)
    ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1))
    ax.grid(True)
    plt.tight_layout()
    plt.savefig(PLOTS / "09_decision_radar.png", dpi=120, bbox_inches='tight')
    plt.close()
    print("✓ 09_decision_radar.png")
    return metrics_by_disk

# ----------------------------------------------------------------------
# 图 10: 4 盘加速比 vs IO 模式 (multiprompt cold/warm/replay)
# ----------------------------------------------------------------------
def plot_speedup_by_mode():
    fig, ax = plt.subplots(figsize=(12, 6))
    # 3 种模式: cold, warm, replay (Phase7)
    modes = []
    cold_means, warm_means, replay_means = [], [], []
    for disk in DISK_ORDER:
        subdir = DISK_DRIVE_MAP[disk]
        v2_log = RESULTS / "hicache_multiprompt" / subdir / "load_test.log"
        if v2_log.exists():
            entries = parse_load_test(v2_log)
            cold_means.append(next((l for lbl, l in entries if lbl == 'p0'), 0))
            warm_means.append(np.mean([l for lbl, l in entries if lbl.startswith('p') and lbl != 'p0']) if any(lbl.startswith('p') and lbl != 'p0' for lbl, _ in entries) else 0)
            replay_means.append(next((l for lbl, l in entries if lbl == 'replay_p0'), 0))
        else:
            cold_means.append(0); warm_means.append(0); replay_means.append(0)
    x = np.arange(len(DISK_ORDER))
    width = 0.25
    ax.bar(x - width, cold_means, width, label='p0 (cold, L2 fill)', color='#1f77b4')
    ax.bar(x, warm_means, width, label='p1-p19 (warm, L2 hit)', color='#2ca02c')
    ax.bar(x + width, replay_means, width, label='replay_p0 (L3 read)', color='#d62728')
    for i, (c, w, r) in enumerate(zip(cold_means, warm_means, replay_means)):
        ax.text(i - width, c + 0.1, f'{c:.2f}', ha='center', fontsize=8)
        ax.text(i, w + 0.05, f'{w:.2f}', ha='center', fontsize=8)
        ax.text(i + width, r + 0.1, f'{r:.2f}', ha='center', fontsize=8)
    ax.set_xticks(x); ax.set_xticklabels(DISK_ORDER)
    ax.set_ylabel('Latency (s)')
    ax.set_title('HiCache Multiprompt Latency 分模式 (Phase7 v2, 4 盘)\np0 cold → L2 fill, p1-p19 L2 hit, replay_p0 L3 read')
    ax.legend()
    ax.grid(axis='y', alpha=0.3)
    plt.tight_layout()
    plt.savefig(PLOTS / "10_multiprompt_modes.png", dpi=120)
    plt.close()
    print("✓ 10_multiprompt_modes.png")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
if __name__ == '__main__':
    print(f"Plotting to {PLOTS}/")
    plot_fio_bw()
    plot_fio_iops()
    plot_fio_latency_percentiles()
    plot_hicache_cold_warm()
    plot_phase_spread()
    plot_cache_hit_vs_device()
    plot_iostat_timeseries()
    plot_l3_file_count()
    metrics = plot_decision_radar()
    plot_speedup_by_mode()

    # 也打印 metrics 给 doc 用
    print("\n=== 4 盘核心指标 (供 doc 引用) ===")
    for d, m in metrics.items():
        print(f"{d:10s}: seq_bw={m['seq_bw']:.2f} GB/s, rand_iops={m['rand_iops']:.1f}K, "
              f"p99_lat={m['p99_lat_ms']:.1f}ms, hicache_cold={m['cold_s']:.2f}s")
    print(f"\n✓ 10 张图全部完成 in {PLOTS}/")
