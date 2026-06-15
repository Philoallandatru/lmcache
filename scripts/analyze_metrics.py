#!/usr/bin/env python3
"""
scripts/analyze_metrics.py

跨盘 × 跨 run 解析 sglang Prometheus metrics_after.json

关注:
  - cache_hit_rate: prefix cache hit rate
  - prompt_tokens_total: 总 prefill token 数
  - num_used_tokens: KV cache 占用
  - gen_throughput: token/s
  - time_to_first_token histogram (TTFT)
  - uncached_prompt_tokens histogram

用法:
  source ~/llm/.venv/bin/activate
  python scripts/analyze_metrics.py
"""
import re
import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).parent.parent
RESULTS = ROOT / "results"

DISK_ORDER = ['BIWIN', 'WDC', 'Seagate', 'ZHITAI']
DISK_DRIVE_MAP = {
    'BIWIN':   'baseline_biwin_ext4',
    'WDC':     'ai_ssd0_wdc_ntfs',
    'Seagate': 'ai_ssd1_seagate_ntfs',
    'ZHITAI':  'ai_ssd2_zhitai_ntfs',
}
G_RUNS = ['v3', 'g1', 'g2', 'g3', 'g4', 'g5', 'P3v2_A1', 'P3v2_A2', 'P3v3_A1']
G_SUBDIR = {
    'v3':      'hicache_multiprompt',
    'g1':      'hicache_multiprompt_g1',
    'g2':      'hicache_multiprompt_g2',
    'g3':      'hicache_multiprompt_g3',
    'g4':      'hicache_multiprompt_g4',
    'g5':      'hicache_multiprompt_g5',
    'P3v2_A1': 'hicache_multiprompt_p3v2_A1',
    'P3v2_A2': 'hicache_multiprompt_p3v2_A2',
    'P3v3_A1': 'hicache_multiprompt_p3v3_A1',
}


def parse_prom_metrics(path):
    """解析 Prometheus 文本格式,保留 label 以便 histogram bucket 估算 p99."""
    metrics = {}
    if not path.exists():
        return metrics
    for line in path.read_text().split('\n'):
        if not line or line.startswith('#'):
            continue
        # 格式: metric_name{labels} value [timestamp]
        m = re.match(r'^([\w:]+)(\{[^}]*\})?\s+([\d\.eE\-\+]+|NaN)\s*(\d+)?$', line)
        if not m:
            continue
        name, labels, val, _ = m.groups()
        try:
            v = float(val)
        except ValueError:
            continue
        if labels:
            label_items = tuple(sorted(re.findall(r'(\w+)="([^"]*)"', labels)))
            key = (name, label_items)
        else:
            key = name
        # 同名 metric 累加 (sglang 多 tp_rank/moe_ep_rank 时累加)
        if key in metrics:
            if isinstance(metrics[key], list):
                metrics[key].append(v)
            else:
                metrics[key] = [metrics[key], v]
        else:
            metrics[key] = v
    return metrics


def metric_name(key):
    if isinstance(key, tuple):
        return key[0]
    return key


def metric_labels(key):
    if isinstance(key, tuple):
        return dict(key[1])
    return {}


def collect_metrics(run='v3'):
    """收集一盘四组的 metrics 摘要: cache_hit_rate, prompt_tokens, num_requests, gen_throughput, num_used_tokens, TTFT avg/p99."""
    out = {}
    for disk in DISK_ORDER:
        subdir = DISK_DRIVE_MAP[disk]
        path = RESULTS / G_SUBDIR[run] / subdir / 'metrics_after.json'
        m = parse_prom_metrics(path)
        if not m:
            out[disk] = None
            continue
        # 找关键 metric
        def get(key):
            values = [
                v for metric_key, v in m.items()
                if metric_name(metric_key) == key
            ]
            if not values:
                return 0
            flat = []
            for v in values:
                if isinstance(v, list):
                    flat.extend(v)
                else:
                    flat.append(v)
            # counter/sum/count 类指标按 labels 累加; gauge 取均值。
            if key.endswith('_total') or key.endswith('_sum') or key.endswith('_count'):
                return sum(flat)
            return statistics.mean(flat)
        # TTFT histogram: 看 sum + count 推算平均
        ttft_sum = get('sglang:time_to_first_token_seconds_sum')
        ttft_count = get('sglang:time_to_first_token_seconds_count')
        ttft_avg = ttft_sum / ttft_count if ttft_count else 0
        # p99 从 histogram bucket 估算
        ttft_buckets = []
        for k, v in m.items():
            if metric_name(k) != 'sglang:time_to_first_token_seconds_bucket':
                continue
            labels = metric_labels(k)
            le_raw = labels.get('le')
            if le_raw in (None, '+Inf', 'Inf'):
                continue
            if isinstance(v, list):
                v = sum(v)
            try:
                ttft_buckets.append((float(le_raw), v))
            except ValueError:
                pass
        ttft_buckets.sort()
        # p99 = 第一个 > 0.99 * count 的 bucket
        ttft_p99 = 0
        if ttft_buckets and ttft_count:
            threshold = 0.99 * ttft_count
            for le, v in ttft_buckets:
                if v >= threshold:
                    ttft_p99 = le
                    break

        out[disk] = {
            'cache_hit_rate': get('sglang:cache_hit_rate'),
            'prompt_tokens_total': get('sglang:prompt_tokens_total'),
            'num_requests_total': get('sglang:num_requests_total'),
            'gen_throughput': get('sglang:gen_throughput'),
            'num_used_tokens': get('sglang:num_used_tokens'),
            'ttft_avg': ttft_avg,
            'ttft_p99': ttft_p99,
            'uncached_prompt_total': get('sglang:uncached_prompt_tokens_histogram_sum'),
        }
    return out


def main():
    print("=" * 90)
    print("SGLANG METRICS 跨盘 × 跨 run 对比 (cache_hit_rate, prompt_tokens, gen_throughput, TTFT)")
    print("=" * 90)

    summary = {}
    for run in G_RUNS:
        if not (RESULTS / G_SUBDIR[run]).exists():
            continue
        print(f"\n[run={run}]")
        print(f"  {'盘':10s} {'hit_rate':>9s} {'reqs':>5s} {'prompt_tok':>10s} {'gen_tps':>7s} "
              f"{'used_tok':>9s} {'ttft_avg':>9s} {'ttft_p99':>9s} {'uncached':>10s}")
        m = collect_metrics(run)
        for disk in DISK_ORDER:
            d = m.get(disk)
            if d:
                print(f"  {disk:10s} {d['cache_hit_rate']*100:>8.2f}% {d['num_requests_total']:>5.0f} "
                      f"{d['prompt_tokens_total']:>10.0f} {d['gen_throughput']:>7.2f} "
                      f"{d['num_used_tokens']:>9.0f} {d['ttft_avg']:>9.3f} {d['ttft_p99']:>9.3f} "
                      f"{d['uncached_prompt_total']:>10.0f}")
        summary[run] = m

    # 输出 JSON 给 doc 用
    out_path = RESULTS / 'sglang_metrics_summary.json'
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\n✓ Saved to {out_path}")

    # CSV 方便对比,避免依赖 pandas。
    rows = []
    for run, m in summary.items():
        for disk, d in m.items():
            if d:
                rows.append({
                    'run': run, 'disk': disk, **d
                })
    csv_path = RESULTS / 'sglang_metrics_summary.csv'
    if rows:
        with csv_path.open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    print(f"✓ CSV saved to {csv_path}")

    # 关键洞察总结
    print("\n=== 关键洞察 ===")
    # 1. cache_hit_rate 跨盘跨 run
    print("1. cache_hit_rate (应该 0%,因为 20 unique prompts + 1 replay):")
    for run in G_RUNS:
        if run in summary:
            rates = [d['cache_hit_rate'] for disk, d in summary[run].items() if d]
            if rates:
                print(f"   {run}: rates = {[f'{r*100:.1f}%' for r in rates]}")

    # 2. uncached_prompt_total 跨 run (反映 L3 write 量)
    print("\n2. uncached_prompt_tokens_total (反映 sglang 实际 prefill token, 越高 → 越少 L2 hit):")
    for run in G_RUNS:
        if run in summary:
            uncs = [d['uncached_prompt_total'] for disk, d in summary[run].items() if d]
            if uncs:
                print(f"   {run}: {uncs} mean={statistics.mean(uncs):.0f}")

    # 3. ZHITAI 跨 run 趋势 (prompt_tokens, gen_throughput)
    print("\n3. ZHITAI 跨 run ttft_avg / gen_throughput:")
    for run in G_RUNS:
        if run in summary and 'ZHITAI' in summary[run] and summary[run]['ZHITAI']:
            d = summary[run]['ZHITAI']
            print(f"   {run}: ttft_avg={d['ttft_avg']:.3f}s gen_tps={d['gen_throughput']:.2f} prompt={d['prompt_tokens_total']:.0f}")


if __name__ == '__main__':
    main()
