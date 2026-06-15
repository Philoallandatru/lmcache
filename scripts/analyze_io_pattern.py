#!/usr/bin/env python3
"""
scripts/analyze_io_pattern.py

对每个 disk 的 iostat log 做细 IO 模式分析,输出:
- IO 模式核心指标 CSV (per-disk × run mean/peak/p50/p99)
- burst detection (连续非零段统计)
- 跨盘 5-run 对比表 (replay_p0 latency mean/std/min/max/cv)

用法:
  source ~/llm/.venv/bin/activate
  python scripts/analyze_io_pattern.py
"""
import os
import re
import json
import glob
import statistics
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
RESULTS = ROOT / "results"
PLOTS = RESULTS / "plots"
PLOTS.mkdir(exist_ok=True)

# 4 盘统一颜色 + 映射 (commit 936dc65 修正确认)
DISK_ORDER = ['BIWIN', 'WDC', 'Seagate', 'ZHITAI']
DISK_COLORS = {
    'BIWIN':   '#ff7f0e',
    'WDC':     '#1f77b4',
    'Seagate': '#2ca02c',
    'ZHITAI':  '#d62728',
}
DISK_DRIVE_MAP = {
    'BIWIN':   'baseline_biwin_ext4',
    'WDC':     'ai_ssd0_wdc_ntfs',
    'Seagate': 'ai_ssd1_seagate_ntfs',
    'ZHITAI':  'ai_ssd2_zhitai_ntfs',
}
DISK_NVME = {
    'BIWIN':   'nvme0n1',
    'WDC':     'nvme1n1',
    'Seagate': 'nvme2n1',
    'ZHITAI':  'nvme3n1',
}

# 数据源: v3 (基础) + g1..g5 (多 run)
G_RUNS = ['v3', 'g1', 'g2', 'g3', 'g4', 'g5']
G_SUBDIR = {
    'v3': 'hicache_multiprompt',
    'g1': 'hicache_multiprompt_g1',
    'g2': 'hicache_multiprompt_g2',
    'g3': 'hicache_multiprompt_g3',
    'g4': 'hicache_multiprompt_g4',
    'g5': 'hicache_multiprompt_g5',
}

# IOSTAT 列名映射 (sysstat 12.x -dx -m 22 列)
IOSTAT_COLS = [
    'rps', 'rMBs', 'rrqmps', 'pct_rrqm', 'r_await', 'rareq_sz',
    'wps',  'wMBs', 'wrqmps', 'pct_wrqm', 'w_await', 'wareq_sz',
    'dps',  'dMBs', 'drqmps', 'pct_drqm', 'd_await', 'dareq_sz',
    'fps',  'f_await', 'aqu_sz', 'pct_util',
]


def parse_iostat(path):
    """解析 iostat -dx -m 1 N 输出,返回 list of dict (每个样本一个 dict).
    跳过 nvmeXpY (逻辑盘),只保留 nvmeXnY 物理盘。
    """
    samples = []
    if not path.exists():
        return samples
    for line in path.read_text().split('\n'):
        m = re.match(r'(nvme\d+n\d+)\s+([\-\d.]+)\s+', line)
        if not m:
            continue
        dev = m.group(1)
        if 'p' in dev:
            continue
        vals = line.split()
        if len(vals) < len(IOSTAT_COLS) + 1:
            continue
        try:
            s = {'dev': dev}
            for i, col in enumerate(IOSTAT_COLS):
                v = vals[i + 1]
                s[col] = float(v) if v not in ('-', '') else 0.0
            samples.append(s)
        except (ValueError, IndexError):
            continue
    return samples


def detect_bursts(samples, col='rMBs', threshold=0.5):
    """检测连续非零段 (burst)。返回 list of burst dict."""
    bursts = []
    in_burst = False
    start = 0
    vals = []
    for i, s in enumerate(samples):
        v = s.get(col, 0)
        if v > threshold:
            if not in_burst:
                in_burst = True
                start = i
                vals = []
            vals.append(v)
        else:
            if in_burst:
                end = i - 1
                duration = end - start + 1
                if duration >= 1:
                    bursts.append({
                        'start_idx': start,
                        'end_idx': end,
                        'duration_samples': duration,
                        'peak': max(vals),
                        'mean': statistics.mean(vals),
                        'sum': sum(vals),
                    })
                in_burst = False
                vals = []
    if in_burst:
        end = len(samples) - 1
        duration = end - start + 1
        if duration >= 1:
            bursts.append({
                'start_idx': start,
                'end_idx': end,
                'duration_samples': duration,
                'peak': max(vals),
                'mean': statistics.mean(vals),
                'sum': sum(vals),
            })
    return bursts


def analyze_disk_iostat(disk, run='v3'):
    """分析一盘的 iostat log,返回 dict of metrics。"""
    subdir = DISK_DRIVE_MAP[disk]
    nvme = DISK_NVME[disk]
    log_path = RESULTS / G_SUBDIR[run] / subdir / f"iostat_{nvme}.log"
    samples = parse_iostat(log_path)
    if not samples:
        return None

    all_read_mb = [s['rMBs'] for s in samples]
    all_write_mb = [s['wMBs'] for s in samples]
    all_aqu_sz = [s['aqu_sz'] for s in samples]
    all_util = [s['pct_util'] for s in samples]
    all_r_await = [s['r_await'] for s in samples]
    all_rareq_sz = [s['rareq_sz'] for s in samples]
    all_rrqm = [s['pct_rrqm'] for s in samples]

    active = [s for s in samples if s['rMBs'] > 0.5]
    if not active:
        active = samples

    active_read_mb = [s['rMBs'] for s in active]
    active_r_await = [s['r_await'] for s in active]
    active_rareq_sz = [s['rareq_sz'] for s in active]
    active_aqu_sz = [s['aqu_sz'] for s in active]
    active_rrqm = [s['pct_rrqm'] for s in active]

    bursts = detect_bursts(samples, 'rMBs', threshold=0.5)
    big_bursts = sorted(bursts, key=lambda b: -b['duration_samples'])[:5]

    return {
        'disk': disk,
        'run': run,
        'nvme': nvme,
        'total_samples': len(samples),
        'active_samples': len(active),
        'read_mb_peak': max(all_read_mb) if all_read_mb else 0,
        'read_mb_mean_all': statistics.mean(all_read_mb) if all_read_mb else 0,
        'read_mb_mean_active': statistics.mean(active_read_mb) if active_read_mb else 0,
        'read_mb_p50_active': statistics.median(active_read_mb) if active_read_mb else 0,
        'read_mb_p99_active': float(np.percentile(active_read_mb, 99)) if active_read_mb else 0,
        'write_mb_peak': max(all_write_mb) if all_write_mb else 0,
        'write_mb_mean_active': statistics.mean([s['wMBs'] for s in active]) if active else 0,
        'total_write_mb': sum(all_write_mb),
        'total_read_mb': sum(all_read_mb),
        'r_await_mean_active': statistics.mean(active_r_await) if active_r_await else 0,
        'r_await_p99_active': float(np.percentile(active_r_await, 99)) if active_r_await else 0,
        'rareq_sz_mean_active': statistics.mean(active_rareq_sz) if active_rareq_sz else 0,
        'rareq_sz_p50_active': statistics.median(active_rareq_sz) if active_rareq_sz else 0,
        'aqu_sz_mean_active': statistics.mean(active_aqu_sz) if active_aqu_sz else 0,
        'aqu_sz_peak': max(all_aqu_sz) if all_aqu_sz else 0,
        'pct_util_mean_active': statistics.mean([s['pct_util'] for s in active]) if active else 0,
        'pct_util_peak': max(all_util) if all_util else 0,
        'pct_rrqm_mean_active': statistics.mean(active_rrqm) if active_rrqm else 0,
        'n_bursts': len(bursts),
        'bursts_top5': big_bursts,
    }


def main():
    print("IO 模式分析 (v3 + g1..g5 多 run)")
    print("=" * 80)

    all_results = []
    for run in G_RUNS:
        print(f"\n[run={run}]")
        for disk in DISK_ORDER:
            r = analyze_disk_iostat(disk, run)
            if r:
                all_results.append(r)
                print(f"  {disk:10s} samples={r['total_samples']:4d} active={r['active_samples']:3d} "
                      f"read_peak={r['read_mb_peak']:6.1f} MB/s read_mean_act={r['read_mb_mean_active']:5.1f} "
                      f"r_await_act={r['r_await_mean_active']:5.2f}ms rareq_sz_act={r['rareq_sz_mean_active']:5.1f}KB "
                      f"aqu_act={r['aqu_sz_mean_active']:4.2f} util_act={r['pct_util_mean_active']:5.1f}% "
                      f"bursts={r['n_bursts']}")
            else:
                print(f"  {disk:10s} NO DATA")

    # CSV
    df = pd.DataFrame([{
        'disk': r['disk'],
        'run': r['run'],
        'nvme': r['nvme'],
        'total_samples': r['total_samples'],
        'active_samples': r['active_samples'],
        'read_mb_peak': r['read_mb_peak'],
        'read_mb_mean_active': r['read_mb_mean_active'],
        'read_mb_p50_active': r['read_mb_p50_active'],
        'read_mb_p99_active': r['read_mb_p99_active'],
        'write_mb_peak': r['write_mb_peak'],
        'write_mb_mean_active': r['write_mb_mean_active'],
        'total_read_mb': r['total_read_mb'],
        'total_write_mb': r['total_write_mb'],
        'r_await_mean_active': r['r_await_mean_active'],
        'r_await_p99_active': r['r_await_p99_active'],
        'rareq_sz_mean_active': r['rareq_sz_mean_active'],
        'rareq_sz_p50_active': r['rareq_sz_p50_active'],
        'aqu_sz_mean_active': r['aqu_sz_mean_active'],
        'aqu_sz_peak': r['aqu_sz_peak'],
        'pct_util_mean_active': r['pct_util_mean_active'],
        'pct_util_peak': r['pct_util_peak'],
        'pct_rrqm_mean_active': r['pct_rrqm_mean_active'],
        'n_bursts': r['n_bursts'],
    } for r in all_results])
    csv_path = RESULTS / 'io_pattern_analysis.csv'
    df.to_csv(csv_path, index=False)
    print(f"\n✓ CSV saved to {csv_path}")

    json_path = RESULTS / 'io_pattern_analysis.json'
    json_path.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"✓ JSON saved to {json_path}")

    # replay latency 跨盘跨 run 汇总
    print("\n=== 4 盘 × 多 run replay_p0 latency 汇总 ===")
    lat_summary = []
    for disk in DISK_ORDER:
        subdir = DISK_DRIVE_MAP[disk]
        runs_data = []
        for run in G_RUNS:
            jsonl = RESULTS / G_SUBDIR[run] / subdir / 'load_test.jsonl'
            if jsonl.exists():
                for line in jsonl.read_text().split('\n'):
                    try:
                        e = json.loads(line)
                        if e.get('label', '').startswith('replay_'):
                            runs_data.append((run, e['latency_s']))
                    except Exception:
                        pass
        if runs_data:
            lats = [x[1] for x in runs_data]
            print(f"  {disk:10s}: n={len(lats):2d} runs={['{}={:.3f}'.format(r, v) for r, v in runs_data]}")
            mean = statistics.mean(lats)
            stdev = statistics.stdev(lats) if len(lats) > 1 else 0
            cv = stdev / mean * 100 if mean else 0
            lat_summary.append({
                'disk': disk,
                'n_runs': len(lats),
                'runs': runs_data,
                'mean': mean,
                'stdev': stdev,
                'min': min(lats),
                'max': max(lats),
                'cv_pct': cv,
                'spread_s': max(lats) - min(lats),
            })
    json_path2 = RESULTS / 'multiprompt_g_summary.json'
    json_path2.write_text(json.dumps(lat_summary, indent=2, ensure_ascii=False))
    print(f"\n✓ replay latency summary saved to {json_path2}")


if __name__ == '__main__':
    main()
