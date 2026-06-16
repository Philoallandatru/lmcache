#!/usr/bin/env python3
"""
scripts/analyze_p5.py

P5 分析: 3 write_policy × 4 盘 = 12 数据点
- replay_p0 latency 跨 policy × 盘 对比 (mean / stdev / min / max)
- p0..p19 冷 fill latency 跨 policy 对比
- iostat 模式跨 policy 对比 (read MB peak / r_await / write IO)
- sglang metrics 跨 policy 对比 (gen_throughput / cache_hit_rate / prompt_tokens_total)

数据源: results/hicache_multiprompt_p5_{p1_wt,p2_wb,p3_wts}/<disk_round>/

用法:
  source ~/llm/.venv/bin/activate
  python scripts/analyze_p5.py
"""
import os
import re
import json
import statistics
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
RESULTS = ROOT / "results"

DISK_ORDER = ['BIWIN', 'WDC', 'Seagate', 'ZHITAI']
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

# P5 三个 policy + 对应 SUBDIR
POLICY_SUBDIR = {
    'write_through':           'hicache_multiprompt_p5_p1_wt',
    'write_back':              'hicache_multiprompt_p5_p2_wb',
    'write_through_selective': 'hicache_multiprompt_p5_p3_wts',
}
POLICY_LABEL = {
    'write_through':           'WT',
    'write_back':              'WB',
    'write_through_selective': 'WTS',
}
POLICY_ORDER = ['write_through', 'write_back', 'write_through_selective']

IOSTAT_COLS = [
    'rps', 'rMBs', 'rrqmps', 'pct_rrqm', 'r_await', 'rareq_sz',
    'wps',  'wMBs', 'wrqmps', 'pct_wrqm', 'w_await', 'wareq_sz',
    'dps',  'dMBs', 'drqmps', 'pct_drqm', 'd_await', 'dareq_sz',
    'fps',  'f_await', 'aqu_sz', 'pct_util',
]


def parse_iostat(path):
    """从 iostat -dx -m 1 N 输出里抽 nvmeXn1 (物理盘, 跳过 nvmeXpY 逻辑盘)."""
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


def parse_load_test(jsonl_path):
    """解析 hicache_load_test.py 输出 jsonl, 返回 list of dict."""
    out = []
    if not jsonl_path.exists():
        return out
    for line in jsonl_path.read_text().split('\n'):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            out.append(e)
        except Exception:
            pass
    return out


def parse_sglang_metrics(path):
    """从 sglang /metrics 抓关键指标 (Prometheus 文本格式)."""
    if not path.exists():
        return {}
    text = path.read_text()
    metrics = {}

    # 关键指标列表
    keys = [
        'vllm:prompt_tokens_total',
        'vllm:generation_tokens_total',
        'vllm:num_requests_total',
        'vllm:time_to_first_token_seconds_sum',
        'vllm:time_to_first_token_seconds_count',
        'vllm:time_to_first_token_seconds_bucket',
        'vllm:e2e_request_latency_seconds_sum',
        'vllm:e2e_request_latency_seconds_count',
        'vllm:cache_hit_rate',
        'vllm:cpu_cache_usage_perc',
        'sglang:hicache_storage_write_seconds_sum',
        'sglang:hicache_storage_read_seconds_sum',
    ]

    for line in text.split('\n'):
        for key in keys:
            # 匹配 <key>{labels?} <value>
            if line.startswith(key + ' ') or line.startswith(key + '{'):
                parts = line.rsplit(' ', 1)
                if len(parts) == 2:
                    try:
                        val = float(parts[1])
                        # 累加同 key 不同 label 的值
                        metrics[key] = metrics.get(key, 0) + val
                    except ValueError:
                        pass
    return metrics


def analyze_iostat(samples):
    """一组 iostat 样本 → 关键指标 dict."""
    if not samples:
        return None
    read_mb = [s['rMBs'] for s in samples]
    write_mb = [s['wMBs'] for s in samples]
    active = [s for s in samples if s['rMBs'] > 0.5 or s['wMBs'] > 0.5]
    if not active:
        active = samples
    return {
        'samples': len(samples),
        'active_samples': len(active),
        'read_peak_mbs': max(read_mb),
        'read_mean_active_mbs': statistics.mean([s['rMBs'] for s in active]) if active else 0,
        'read_p99_active_mbs': float(np.percentile([s['rMBs'] for s in active], 99)) if active else 0,
        'write_peak_mbs': max(write_mb),
        'write_mean_active_mbs': statistics.mean([s['wMBs'] for s in active]) if active else 0,
        'total_read_mb': sum(read_mb),
        'total_write_mb': sum(write_mb),
        'r_await_mean_active_ms': statistics.mean([s['r_await'] for s in active]) if active else 0,
        'r_await_p99_active_ms': float(np.percentile([s['r_await'] for s in active], 99)) if active else 0,
        'rareq_sz_mean_active_kb': statistics.mean([s['rareq_sz'] for s in active]) if active else 0,
        'aqu_sz_peak': max(s['aqu_sz'] for s in samples),
        'aqu_sz_mean_active': statistics.mean([s['aqu_sz'] for s in active]) if active else 0,
        'util_peak_pct': max(s['pct_util'] for s in samples),
        'util_mean_active_pct': statistics.mean([s['pct_util'] for s in active]) if active else 0,
        'pct_rrqm_mean_active': statistics.mean([s['pct_rrqm'] for s in active]) if active else 0,
    }


def main():
    print("=" * 90)
    print("P5 分析: 3 write_policy × 4 盘 = 12 数据点")
    print("=" * 90)

    # ===== 1. 数据完整性检查 =====
    print("\n[1/4] 数据完整性检查")
    missing = []
    for policy in POLICY_ORDER:
        sub = POLICY_SUBDIR[policy]
        for disk in DISK_ORDER:
            round_name = DISK_DRIVE_MAP[disk]
            jsonl = RESULTS / sub / round_name / 'load_test.jsonl'
            iostat = RESULTS / sub / round_name / f"iostat_{DISK_NVME[disk]}.log"
            metrics = RESULTS / sub / round_name / 'metrics_after.json'
            if not jsonl.exists():
                missing.append(f"  MISSING jsonl: {sub}/{round_name}")
            if not iostat.exists():
                missing.append(f"  MISSING iostat: {sub}/{round_name}")
            if not metrics.exists():
                missing.append(f"  MISSING metrics: {sub}/{round_name}")
    if missing:
        print(f"  ⚠️  {len(missing)} missing files:")
        for m in missing[:5]:
            print(m)
        if len(missing) > 5:
            print(f"  ... ({len(missing)-5} more)")
    else:
        print("  ✅ 12/12 数据点全部完整")

    # ===== 2. replay_p0 latency 跨 policy × 盘 =====
    print("\n[2/4] replay_p0 latency 跨 policy × 盘 (s)")
    print(f"  {'Disk':10s} | " + " | ".join([f"{POLICY_LABEL[p]:>16s}" for p in POLICY_ORDER]))
    print("  " + "-" * 75)

    replay_matrix = {}
    for disk in DISK_ORDER:
        row = [f"  {disk:10s}"]
        replay_matrix[disk] = {}
        for policy in POLICY_ORDER:
            sub = POLICY_SUBDIR[policy]
            round_name = DISK_DRIVE_MAP[disk]
            jsonl = RESULTS / sub / round_name / 'load_test.jsonl'
            entries = parse_load_test(jsonl)
            replay_lats = [e['latency_s'] for e in entries if str(e.get('label', '')).startswith('replay_')]
            if replay_lats:
                mean = statistics.mean(replay_lats)
                row.append(f"  {mean:6.3f}±{statistics.stdev(replay_lats):.3f}    " if len(replay_lats) > 1 else f"  {mean:6.3f}±0.000    ")
                replay_matrix[disk][policy] = {
                    'mean': mean,
                    'stdev': statistics.stdev(replay_lats) if len(replay_lats) > 1 else 0,
                    'n': len(replay_lats),
                    'min': min(replay_lats),
                    'max': max(replay_lats),
                }
            else:
                row.append(f"  {'N/A':>16s}")
                replay_matrix[disk][policy] = None
        print(" | ".join(row))

    # 跨 policy 极差
    print(f"\n  cross-policy spread (max - min):")
    for disk in DISK_ORDER:
        lats = [replay_matrix[disk][p]['mean'] for p in POLICY_ORDER if replay_matrix[disk][p]]
        if lats:
            spread = max(lats) - min(lats)
            print(f"    {disk:10s}: {min(lats):.3f} - {max(lats):.3f} s (spread={spread:.3f} s, {spread/min(lats)*100:.1f}%)")

    # ===== 3. iostat 模式跨 policy × 盘 =====
    print("\n[3/4] iostat 模式跨 policy × 盘")
    print(f"  {'Policy':20s} {'Disk':10s} {'read_peak':>10s} {'read_mean':>10s} {'write_peak':>10s} {'r_await_p99':>11s} {'aqu_peak':>9s} {'util_peak':>10s}")
    print("  " + "-" * 100)

    iostat_matrix = {}
    for policy in POLICY_ORDER:
        sub = POLICY_SUBDIR[policy]
        iostat_matrix[policy] = {}
        for disk in DISK_ORDER:
            round_name = DISK_DRIVE_MAP[disk]
            log = RESULTS / sub / round_name / f"iostat_{DISK_NVME[disk]}.log"
            samples = parse_iostat(log)
            m = analyze_iostat(samples)
            iostat_matrix[policy][disk] = m
            if m:
                print(f"  {POLICY_LABEL[policy]:20s} {disk:10s} {m['read_peak_mbs']:8.1f}MB {m['read_mean_active_mbs']:8.1f}MB "
                      f"{m['write_peak_mbs']:8.1f}MB {m['r_await_p99_active_ms']:9.2f}ms {m['aqu_sz_peak']:7.2f}  {m['util_peak_pct']:8.1f}%")
            else:
                print(f"  {POLICY_LABEL[policy]:20s} {disk:10s} NO DATA")

    # ===== 4. sglang metrics 跨 policy =====
    print("\n[4/4] sglang metrics 跨 policy (gen_throughput, cache_hit_rate, prompt_tokens)")
    print(f"  {'Policy':20s} {'Disk':10s} {'prompt_tok':>11s} {'gen_tok':>11s} {'ttft_sum':>10s} {'e2e_sum':>10s} {'cache_hit':>10s}")
    print("  " + "-" * 95)

    metrics_matrix = {}
    for policy in POLICY_ORDER:
        sub = POLICY_SUBDIR[policy]
        metrics_matrix[policy] = {}
        for disk in DISK_ORDER:
            round_name = DISK_DRIVE_MAP[disk]
            m_path = RESULTS / sub / round_name / 'metrics_after.json'
            m = parse_sglang_metrics(m_path)
            metrics_matrix[policy][disk] = m
            if m:
                pt = m.get('vllm:prompt_tokens_total', 0)
                gt = m.get('vllm:generation_tokens_total', 0)
                ttft = m.get('vllm:time_to_first_token_seconds_sum', 0)
                e2e = m.get('vllm:e2e_request_latency_seconds_sum', 0)
                ch = m.get('vllm:cache_hit_rate', 0)
                print(f"  {POLICY_LABEL[policy]:20s} {disk:10s} {pt:11.0f} {gt:11.0f} {ttft:10.2f} {e2e:10.2f} {ch:10.4f}")
            else:
                print(f"  {POLICY_LABEL[policy]:20s} {disk:10s} NO DATA")

    # ===== 5. 写盘总量对比 (L3 fill) =====
    print("\n[5/5] L3 写盘总量 (write_back 行为差异)")
    for policy in POLICY_ORDER:
        total_w = sum(iostat_matrix[policy][d]['total_write_mb'] for d in DISK_ORDER if iostat_matrix[policy][d])
        total_r = sum(iostat_matrix[policy][d]['total_read_mb'] for d in DISK_ORDER if iostat_matrix[policy][d])
        print(f"  {POLICY_LABEL[policy]:20s}: total_write={total_w/1024:.1f} GB, total_read={total_r/1024:.1f} GB")

    # ===== 保存 JSON + CSV =====
    summary = {
        'replay_p0_latency': replay_matrix,
        'iostat_pattern': iostat_matrix,
        'sglang_metrics': metrics_matrix,
    }
    out_json = RESULTS / 'p5_policy_matrix_summary.json'
    out_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(f"\n✓ JSON saved to {out_json}")

    # CSV for replay latency
    csv_rows = []
    for disk in DISK_ORDER:
        for policy in POLICY_ORDER:
            r = replay_matrix[disk][policy]
            if r:
                csv_rows.append({
                    'disk': disk,
                    'policy': policy,
                    'policy_label': POLICY_LABEL[policy],
                    'replay_p0_mean_s': r['mean'],
                    'replay_p0_stdev_s': r['stdev'],
                    'replay_p0_min_s': r['min'],
                    'replay_p0_max_s': r['max'],
                    'n_runs': r['n'],
                })
    if csv_rows:
        df = pd.DataFrame(csv_rows)
        csv_path = RESULTS / 'p5_replay_latency.csv'
        df.to_csv(csv_path, index=False)
        print(f"✓ CSV saved to {csv_path}")


if __name__ == '__main__':
    main()
