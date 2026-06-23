#!/usr/bin/env python3
"""
Complete Analysis and Report Generation Script
Summarizes all experimental data, generates IO analysis plots, and creates a comprehensive report
"""

import json
import os
import sys
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')
import numpy as np
from datetime import datetime

# Project paths
BASE_DIR = Path("/home/ficus/llm/infer/ai_ssd_prestudy")
RESULTS_DIR = BASE_DIR / "results"
REPORTS_DIR = BASE_DIR / "reports"
PLOTS_DIR = RESULTS_DIR / "plots"

PLOTS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)

def load_json(path):
    """Load JSON file"""
    with open(path) as f:
        return json.load(f)

def load_all_data():
    """Load all experimental data"""
    data = {}
    
    # LMCache validation results
    lmcache_path = RESULTS_DIR / "lmcache_validation" / "all_results.json"
    if lmcache_path.exists():
        data['lmcache'] = load_json(lmcache_path)
    
    # SGlang metrics
    sglang_path = RESULTS_DIR / "sglang_metrics_summary.json"
    if sglang_path.exists():
        data['sglang'] = load_json(sglang_path)
    
    # IO pattern analysis
    io_path = RESULTS_DIR / "io_pattern_analysis.json"
    if io_path.exists():
        data['io_patterns'] = load_json(io_path)
    
    # FastLLM results
    fastllm_dir = RESULTS_DIR / "fastllm-2026-06-23"
    if fastllm_dir.exists():
        data['fastllm_comparison'] = load_json(fastllm_dir / "full_comparison_2026-06-23.json")
        data['fastllm_io'] = load_json(fastllm_dir / "io_analysis_summary_2026-06-23.json")
    
    return data

def plot_lmcache_comparison(data):
    """Plot LMCache comparison chart"""
    if 'lmcache' not in data:
        return None
    
    lm_data = data['lmcache']
    configs = []
    throughputs = []
    output_throughputs = []
    elapsed_times = []
    
    for name, result in lm_data.items():
        configs.append(name)
        avg = result['average']
        throughputs.append(avg['throughput_tok_per_s'])
        output_throughputs.append(avg['output_throughput_tok_per_s'])
        elapsed_times.append(avg['elapsed_s'])
    
    # Create figure with subplots
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    
    # Throughput comparison
    x = np.arange(len(configs))
    width = 0.35
    ax1.bar(x - width/2, throughputs, width, label='Total Throughput', alpha=0.8)
    ax1.bar(x + width/2, output_throughputs, width, label='Output Throughput', alpha=0.8)
    ax1.set_ylabel('Throughput (tok/s)')
    ax1.set_title('LMCache Throughput Comparison')
    ax1.set_xticks(x)
    ax1.set_xticklabels(configs, rotation=15, ha='right')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # Latency comparison
    ax2.bar(configs, elapsed_times, alpha=0.8, color='coral')
    ax2.set_ylabel('Elapsed Time (s)')
    ax2.set_title('LMCache Latency Comparison (500 prompts)')
    ax2.set_xticklabels(configs, rotation=15, ha='right')
    ax2.grid(True, alpha=0.3)
    
    plt.tight_layout()
    output_path = PLOTS_DIR / "lmcache_comparison.png"
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    return output_path

def plot_io_patterns(data):
    """Plot IO pattern analysis"""
    if 'io_patterns' not in data:
        return None
    
    io_data = data['io_patterns']
    
    # Group by disk
    disks = {}
    for entry in io_data:
        disk = entry['disk']
        if disk not in disks:
            disks[disk] = []
        disks[disk].append(entry)
    
    # Create comprehensive IO analysis
    fig = plt.figure(figsize=(16, 12))
    gs = fig.add_gridspec(3, 2, hspace=0.3, wspace=0.3)
    
    # 1. Read throughput peak by disk
    ax1 = fig.add_subplot(gs[0, 0])
    disk_names = list(disks.keys())
    read_peaks = [np.mean([e['read_mb_peak'] for e in disks[d]]) for d in disk_names]
    ax1.bar(disk_names, read_peaks, alpha=0.8, color='steelblue')
    ax1.set_ylabel('Peak Read MB/s')
    ax1.set_title('Average Peak Read Throughput by Disk')
    ax1.grid(True, alpha=0.3)
    
    # 2. Active vs total samples
    ax2 = fig.add_subplot(gs[0, 1])
    active_ratios = [np.mean([e['active_samples']/e['total_samples'] for e in disks[d]]) * 100 
                     for d in disk_names]
    ax2.bar(disk_names, active_ratios, alpha=0.8, color='orange')
    ax2.set_ylabel('Active Ratio (%)')
    ax2.set_title('Disk Activity Ratio (Active / Total Samples)')
    ax2.grid(True, alpha=0.3)
    
    # 3. Total data read/written
    ax3 = fig.add_subplot(gs[1, 0])
    total_reads = [np.sum([e['total_read_mb'] for e in disks[d]]) for d in disk_names]
    total_writes = [np.sum([e['total_write_mb'] for e in disks[d]]) for d in disk_names]
    x = np.arange(len(disk_names))
    width = 0.35
    ax3.bar(x - width/2, total_reads, width, label='Total Read', alpha=0.8)
    ax3.bar(x + width/2, total_writes, width, label='Total Write', alpha=0.8)
    ax3.set_ylabel('Total MB')
    ax3.set_title('Total Data Transfer by Disk')
    ax3.set_xticks(x)
    ax3.set_xticklabels(disk_names)
    ax3.legend()
    ax3.grid(True, alpha=0.3)
    
    # 4. Average latency
    ax4 = fig.add_subplot(gs[1, 1])
    latencies = [np.mean([e['r_await_mean_active'] for e in disks[d] if e['active_samples'] > 0]) 
                 for d in disk_names]
    ax4.bar(disk_names, latencies, alpha=0.8, color='crimson')
    ax4.set_ylabel('Read Latency (ms)')
    ax4.set_title('Average Read Latency (Active Samples)')
    ax4.grid(True, alpha=0.3)
    
    # 5. Burst count comparison
    ax5 = fig.add_subplot(gs[2, 0])
    burst_counts = [np.sum([e['n_bursts'] for e in disks[d]]) for d in disk_names]
    ax5.bar(disk_names, burst_counts, alpha=0.8, color='green')
    ax5.set_ylabel('Number of Bursts')
    ax5.set_title('Total IO Bursts by Disk')
    ax5.grid(True, alpha=0.3)
    
    # 6. Utilization
    ax6 = fig.add_subplot(gs[2, 1])
    util_peaks = [np.mean([e['pct_util_peak'] for e in disks[d]]) for d in disk_names]
    util_means = [np.mean([e['pct_util_mean_active'] for e in disks[d] if e['active_samples'] > 0]) 
                  for d in disk_names]
    x = np.arange(len(disk_names))
    width = 0.35
    ax6.bar(x - width/2, util_peaks, width, label='Peak', alpha=0.8)
    ax6.bar(x + width/2, util_means, width, label='Mean (Active)', alpha=0.8)
    ax6.set_ylabel('Utilization (%)')
    ax6.set_title('Disk Utilization')
    ax6.set_xticks(x)
    ax6.set_xticklabels(disk_names)
    ax6.legend()
    ax6.grid(True, alpha=0.3)
    
    plt.suptitle('IO Pattern Analysis Summary', fontsize=16, y=0.995)
    
    output_path = PLOTS_DIR / "io_patterns_complete.png"
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    return output_path

def plot_fastllm_comparison(data):
    """Plot FastLLM comparison"""
    if 'fastllm_comparison' not in data:
        return None
    
    ftllm = data['fastllm_comparison']
    tests = ftllm['tests']
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    
    # Extract data
    modes = []
    tps_vals = []
    latency_vals = []
    
    for test in tests:
        mode = test.get('mode', 'Unknown')
        if 'disk' in test:
            mode = f"{mode}\n{test['disk']}"
        modes.append(mode)
        tps_vals.append(test.get('avg_tps', 0))
        latency_vals.append(test.get('avg_latency_s', 0))
    
    # TPS comparison
    colors = ['green' if 'GPU' in m else 'orange' if 'CPU' in m else 'coral' for m in modes]
    ax1.barh(modes, tps_vals, color=colors, alpha=0.8)
    ax1.set_xlabel('Throughput (tok/s)')
    ax1.set_title('Qwen3-30B-A3B Throughput Comparison')
    ax1.grid(True, alpha=0.3, axis='x')
    
    # Latency comparison
    ax2.barh(modes, latency_vals, color=colors, alpha=0.8)
    ax2.set_xlabel('Latency (s)')
    ax2.set_title('Qwen3-30B-A3B Latency Comparison')
    ax2.grid(True, alpha=0.3, axis='x')
    
    plt.tight_layout()
    output_path = PLOTS_DIR / "fastllm_comparison.png"
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    return output_path

def plot_sglang_metrics(data):
    """Plot SGlang metrics"""
    if 'sglang' not in data:
        return None
    
    sg_data = data['sglang']
    
    # Extract data for one representative run (v3)
    if 'v3' not in sg_data:
        return None
    
    v3_data = sg_data['v3']
    disks = list(v3_data.keys())
    gen_throughputs = [v3_data[d]['gen_throughput'] for d in disks]
    ttfts = [v3_data[d]['ttft_avg'] for d in disks]
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    
    # Generation throughput
    ax1.bar(disks, gen_throughputs, alpha=0.8, color='teal')
    ax1.set_ylabel('Generation Throughput (tok/s)')
    ax1.set_title('SGlang Generation Throughput by Disk (v3)')
    ax1.grid(True, alpha=0.3)
    
    # TTFT
    ax2.bar(disks, ttfts, alpha=0.8, color='purple')
    ax2.set_ylabel('TTFT (s)')
    ax2.set_title('SGlang Time to First Token by Disk (v3)')
    ax2.grid(True, alpha=0.3)
    
    plt.tight_layout()
    output_path = PLOTS_DIR / "sglang_metrics.png"
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    return output_path

def generate_markdown_report(data, plots):
    """Generate comprehensive markdown report"""
    
    report_lines = [
        "# AI SSD 预研：完整实验报告",
        "",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 执行摘要",
        "",
        "本报告汇总了 AI SSD 预研项目的所有实验数据，包括：",
        "- LMCache KV 缓存验证实验（GPU/CPU offload）",
        "- SGlang 多磁盘性能对比",
        "- FastLLM Qwen3-30B-A3B 端到端推理性能",
        "- IO 模式深度分析",
        "",
        "---",
        "",
    ]
    
    # LMCache Results
    if 'lmcache' in data:
        report_lines.extend([
            "## 1. LMCache 验证实验",
            "",
            "### 实验配置",
            "- **模型**: Qwen3-4B-Instruct",
            "- **GPU**: RTX 5080 (16GB)",
            "- **Prompts**: 500",
            "- **Max tokens**: 128",
            "- **Trials**: 3",
            "",
            "### 结果汇总",
            "",
            "| 配置 | 总吞吐量 (tok/s) | 输出吞吐量 (tok/s) | 耗时 (s) |",
            "|------|-----------------|-------------------|---------|",
        ])
        
        for name, result in data['lmcache'].items():
            avg = result['average']
            report_lines.append(
                f"| {name} | {avg['throughput_tok_per_s']:.1f} | "
                f"{avg['output_throughput_tok_per_s']:.1f} | {avg['elapsed_s']:.2f} |"
            )
        
        report_lines.extend([
            "",
            "### 关键发现",
            "- LMCache CPU offload 模式保持了较高的吞吐量（~6400 tok/s）",
            "- CPU 内存作为 KV cache 存储介质的可行性得到验证",
            "",
        ])
        
        if plots.get('lmcache'):
            report_lines.extend([
                f"![LMCache Comparison]({plots['lmcache'].relative_to(BASE_DIR)})",
                "",
            ])
    
    # FastLLM Results
    if 'fastllm_comparison' in data:
        ftllm = data['fastllm_comparison']
        report_lines.extend([
            "## 2. FastLLM Qwen3-30B-A3B 端到端性能",
            "",
            "### 实验配置",
            f"- **模型**: {ftllm['model']}",
            f"- **量化**: {ftllm['quant']}",
            f"- **GPU**: {ftllm['gpu']}",
            "",
            "### 性能对比",
            "",
            "| 模式 | 引擎 | 吞吐量 (tok/s) | 延迟 (s) | 备注 |",
            "|------|------|---------------|---------|------|",
        ])
        
        for test in ftllm['tests']:
            mode = test.get('mode', '')
            engine = test.get('engine', '')
            tps = test.get('avg_tps', 'N/A')
            lat = test.get('avg_latency_s', 'N/A')
            disk = test.get('disk', '')
            
            note = disk if disk else test.get('gpu_config', '')[:30]
            report_lines.append(f"| {mode} | {engine} | {tps} | {lat} | {note} |")
        
        report_lines.extend([
            "",
            "### 关键发现",
        ])
        
        for finding in ftllm.get('key_findings', []):
            report_lines.append(f"- {finding}")
        
        report_lines.append("")
        
        if plots.get('fastllm'):
            report_lines.extend([
                f"![FastLLM Comparison]({plots['fastllm'].relative_to(BASE_DIR)})",
                "",
            ])
    
    # IO Analysis
    if 'io_patterns' in data:
        io_data = data['io_patterns']
        
        # Calculate summary statistics
        disks = {}
        for entry in io_data:
            disk = entry['disk']
            if disk not in disks:
                disks[disk] = {'reads': [], 'writes': [], 'bursts': [], 'latencies': []}
            disks[disk]['reads'].append(entry['total_read_mb'])
            disks[disk]['writes'].append(entry['total_write_mb'])
            disks[disk]['bursts'].append(entry['n_bursts'])
            if entry['active_samples'] > 0:
                disks[disk]['latencies'].append(entry['r_await_mean_active'])
        
        report_lines.extend([
            "## 3. IO 模式分析",
            "",
            "### 磁盘 IO 统计",
            "",
            "| 磁盘 | 总读取 (GB) | 总写入 (GB) | Burst 次数 | 平均延迟 (ms) |",
            "|------|------------|------------|-----------|--------------|",
        ])
        
        for disk in sorted(disks.keys()):
            total_read = sum(disks[disk]['reads']) / 1024
            total_write = sum(disks[disk]['writes']) / 1024
            total_bursts = sum(disks[disk]['bursts'])
            avg_lat = np.mean(disks[disk]['latencies']) if disks[disk]['latencies'] else 0
            
            report_lines.append(
                f"| {disk} | {total_read:.2f} | {total_write:.2f} | "
                f"{total_bursts} | {avg_lat:.3f} |"
            )
        
        report_lines.extend([
            "",
            "### 关键观察",
            "- BIWIN (系统盘) 主要承担小量读取，几乎无 burst",
            "- WDC 出现显著的读取 burst，峰值达 1.5 GB/s",
            "- Seagate 和 ZHITAI 有大量写入操作（~19 GB）",
            "- 读取延迟普遍较低（<1ms），磁盘性能未饱和",
            "",
        ])
        
        if plots.get('io'):
            report_lines.extend([
                f"![IO Patterns]({plots['io'].relative_to(BASE_DIR)})",
                "",
            ])
    
    # SGlang Results
    if 'sglang' in data:
        report_lines.extend([
            "## 4. SGlang 多磁盘性能对比",
            "",
            "### v3 配置结果",
            "",
            "| 磁盘 | 生成吞吐量 (tok/s) | TTFT (s) | Cache Hit Rate |",
            "|------|-------------------|----------|----------------|",
        ])
        
        if 'v3' in data['sglang']:
            for disk, metrics in data['sglang']['v3'].items():
                if metrics:
                    report_lines.append(
                        f"| {disk} | {metrics['gen_throughput']:.2f} | "
                        f"{metrics['ttft_avg']:.3f} | {metrics['cache_hit_rate']:.1%} |"
                    )
        
        report_lines.extend([
            "",
            "### 观察",
            "- BIWIN 表现最佳（~8-9 tok/s generation throughput）",
            "- WDC 性能相对较低（~5.6-6.7 tok/s）",
            "- TTFT 差异不大（1.2-1.3s），主要差异在生成阶段",
            "",
        ])
        
        if plots.get('sglang'):
            report_lines.extend([
                f"![SGlang Metrics]({plots['sglang'].relative_to(BASE_DIR)})",
                "",
            ])
    
    # FastLLM IO Analysis
    if 'fastllm_io' in data:
        ftllm_io = data['fastllm_io']
        report_lines.extend([
            "## 5. FastLLM IO 深度分析",
            "",
            "### FIO 基准测试",
            "",
        ])
        
        if 'experiments' in ftllm_io and 'C_fio_benchmark' in ftllm_io['experiments']:
            fio = ftllm_io['experiments']['C_fio_benchmark']
            report_lines.extend([
                "| 磁盘 | 4K IOPS | 4K BW (MB/s) | 4K Latency (μs) | 128K BW (MB/s) |",
                "|------|---------|--------------|----------------|----------------|",
            ])
            
            for disk, metrics in fio['disks'].items():
                report_lines.append(
                    f"| {disk} | {metrics['4k_iops']:,} | {metrics['4k_bw_mbs']} | "
                    f"{metrics['4k_lat_us']:.1f} | {metrics['128k_bw_mbs']} |"
                )
            
            report_lines.append("")
        
        report_lines.extend([
            "### 关键发现",
        ])
        
        for finding in ftllm_io.get('key_findings', []):
            report_lines.append(f"- {finding}")
        
        report_lines.append("")
    
    # Conclusions
    report_lines.extend([
        "---",
        "",
        "## 总体结论",
        "",
        "### 1. SSD 性能未充分利用",
        "- FIO 测得 290K IOPS，但实际推理仅用 7-8 tok/s",
        "- 瓶颈在应用层 MoE expert 加载策略，非磁盘硬件",
        "",
        "### 2. 纯 GPU 方案性能最优",
        "- 双卡 TP 达到 ~184 tok/s，远超 SSD offload (7-8 tok/s)",
        "- CPU RAM offload 达 33 tok/s，是 SSD 的 4-5 倍",
        "",
        "### 3. 磁盘间差异有限",
        "- 不同磁盘推理性能相差 <20%",
        "- 主要受文件系统（ext4 vs NTFS）和 page cache 影响",
        "",
        "### 4. LMCache 可行性验证",
        "- CPU offload 模式保持高吞吐（6400 tok/s）",
        "- KV cache 离线存储方案技术可行",
        "",
        "---",
        "",
        "## 附录",
        "",
        "### 实验环境",
        "- **CPU**: AMD/Intel x86_64",
        "- **GPU**: RTX 5080 (16GB) + RTX 5060 Ti (16GB)",
        "- **RAM**: 系统内存充足",
        "- **OS**: Linux",
        "- **磁盘**:",
        "  - BIWIN X570 (NVMe, ext4, 系统盘)",
        "  - WDC (NVMe, NTFS)",
        "  - Seagate (NVMe, NTFS)",
        "  - ZHITAI Ti600 (NVMe, NTFS)",
        "",
        "### 数据来源",
        "- LMCache 验证: `results/lmcache_validation/`",
        "- SGlang 测试: `results/sglang_metrics_summary.json`",
        "- FastLLM 对比: `results/fastllm-2026-06-23/`",
        "- IO 分析: `results/io_pattern_analysis.json`",
        "",
    ])
    
    # Write report
    report_path = REPORTS_DIR / f"complete_analysis_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))
    
    return report_path

def main():
    print("=" * 70)
    print("  AI SSD 预研：完整数据分析与报告生成")
    print("=" * 70)
    print()
    
    # Load all data
    print("📊 加载实验数据...")
    data = load_all_data()
    print(f"   ✓ 已加载 {len(data)} 个数据集")
    print()
    
    # Generate plots
    print("📈 生成图表...")
    plots = {}
    
    if 'lmcache' in data:
        print("   - LMCache 对比图...")
        plots['lmcache'] = plot_lmcache_comparison(data)
    
    if 'io_patterns' in data:
        print("   - IO 模式分析图...")
        plots['io'] = plot_io_patterns(data)
    
    if 'fastllm_comparison' in data:
        print("   - FastLLM 对比图...")
        plots['fastllm'] = plot_fastllm_comparison(data)
    
    if 'sglang' in data:
        print("   - SGlang 指标图...")
        plots['sglang'] = plot_sglang_metrics(data)
    
    print(f"   ✓ 已生成 {len(plots)} 个图表")
    print()
    
    # Generate report
    print("📝 生成完整报告...")
    report_path = generate_markdown_report(data, plots)
    print(f"   ✓ 报告已保存: {report_path}")
    print()
    
    print("=" * 70)
    print("✅ 完成！")
    print("=" * 70)
    print()
    print(f"报告位置: {report_path}")
    print(f"图表目录: {PLOTS_DIR}")
    print()

if __name__ == "__main__":
    main()
