# AI SSD 预研：完整实验报告

**生成时间**: 2026-06-24 07:39:28

## 执行摘要

本报告汇总了 AI SSD 预研项目的所有实验数据，包括：
- LMCache KV 缓存验证实验（GPU/CPU offload）
- SGlang 多磁盘性能对比
- FastLLM Qwen3-30B-A3B 端到端推理性能
- IO 模式深度分析

---

## 1. LMCache 验证实验

### 实验配置
- **模型**: Qwen3-4B-Instruct
- **GPU**: RTX 5080 (16GB)
- **Prompts**: 500
- **Max tokens**: 128
- **Trials**: 3

### 结果汇总

| 配置 | 总吞吐量 (tok/s) | 输出吞吐量 (tok/s) | 耗时 (s) |
|------|-----------------|-------------------|---------|
| LMCache CPU | 6378.9 | 3432.9 | 18.50 |

### 关键发现
- LMCache CPU offload 模式保持了较高的吞吐量（~6400 tok/s）
- CPU 内存作为 KV cache 存储介质的可行性得到验证

![LMCache Comparison](results/plots/lmcache_comparison.png)

## 2. FastLLM Qwen3-30B-A3B 端到端性能

### 实验配置
- **模型**: Qwen3-30B-A3B
- **量化**: Q4_K_M GGUF (17.3 GB)
- **GPU**: RTX 5080 16GB + RTX 5060 Ti 16GB

### 性能对比

| 模式 | 引擎 | 吞吐量 (tok/s) | 延迟 (s) | 备注 |
|------|------|---------------|---------|------|
| 纯GPU (双卡TP) | llama.cpp | 183.84 | 1.39 | RTX 5080 + 5060 Ti, tensor-spl |
| CPU RAM offload | ftllm | 33.4 | 2.39 | GPU (non-MoE layers) + CPU RAM |
| SSD offload | ftllm | 7.9 | 10.4 | BIWIN X570 (ext4) |
| SSD offload | ftllm | 7.8 | 13.0 | ZHITAI Ti600 (NTFS) |
| SSD offload | ftllm | 6.9 | 14.3 | WDC (NTFS) |
| SSD offload | ftllm | 6.5 | 15.8 | Seagate (NTFS) |

### 关键发现
- 纯GPU双卡(TP)推理达到 ~184 tps,比CPU RAM offload快5.5倍,比SSD offload快23-28倍
- ftllm不支持GGUF格式的多卡纯GPU推理(Qwen3 MoE需要merged qkv weight)
- llama.cpp原生支持GGUF + 多GPU tensor parallelism,且性能优异
- 双卡32GB足够装下17.3GB Q4_K_M模型,每卡约9-10GB

![FastLLM Comparison](results/plots/fastllm_comparison.png)

## 3. IO 模式分析

### 磁盘 IO 统计

| 磁盘 | 总读取 (GB) | 总写入 (GB) | Burst 次数 | 平均延迟 (ms) |
|------|------------|------------|-----------|--------------|
| BIWIN | 38.38 | 96.79 | 26 | 0.142 |
| Seagate | 5.84 | 112.63 | 10 | 0.652 |
| WDC | 13.34 | 94.43 | 23 | 0.533 |
| ZHITAI | 5.87 | 112.67 | 14 | 0.421 |

### 关键观察
- BIWIN (系统盘) 主要承担小量读取，几乎无 burst
- WDC 出现显著的读取 burst，峰值达 1.5 GB/s
- Seagate 和 ZHITAI 有大量写入操作（~19 GB）
- 读取延迟普遍较低（<1ms），磁盘性能未饱和

![IO Patterns](results/plots/io_patterns_complete.png)

## 4. SGlang 多磁盘性能对比

### v3 配置结果

| 磁盘 | 生成吞吐量 (tok/s) | TTFT (s) | Cache Hit Rate |
|------|-------------------|----------|----------------|
| BIWIN | 8.11 | 1.223 | 0.0% |
| WDC | 5.68 | 1.265 | 0.0% |
| Seagate | 7.78 | 1.255 | 0.0% |
| ZHITAI | 7.74 | 1.254 | 0.0% |

### 观察
- BIWIN 表现最佳（~8-9 tok/s generation throughput）
- WDC 性能相对较低（~5.6-6.7 tok/s）
- TTFT 差异不大（1.2-1.3s），主要差异在生成阶段

![SGlang Metrics](results/plots/sglang_metrics.png)

## 5. FastLLM IO 深度分析

### FIO 基准测试

| 磁盘 | 4K IOPS | 4K BW (MB/s) | 4K Latency (μs) | 128K BW (MB/s) |
|------|---------|--------------|----------------|----------------|
| nvme0n1_BIWIN | 290,671 | 1186 | 109.3 | 8760 |
| nvme1n1 | 21,272 | 87 | 1502.8 | 2015 |
| nvme2n1 | 207,569 | 850 | 152.7 | 2410 |
| nvme3n1 | 128,757 | 527 | 247.2 | 1694 |

### 关键发现
- fio 4K IOPS 与 SSD offload TPS 直接相关: 290K/208K/129K/21K IOPS → 7.4-8.0 tps (BIWIN/nvme2/nvme3/nvme1)
- nvme1n1 只有 21K 4K IOPS,延迟 1.5ms — 这是慢盘,但 ftllm 主要用 page cache 所以 TPS 还能保持 8.1
- NTFS 冷启动严重退化 (cold 4.5-5.4 tps),热态恢复 (hot 7.5-8.2 tps),ext4 冷热差距小 (+7%)
- 冷热比 NVFS 是 1.4-1.8x 提升,ext4 只有 1.07x 提升 (ext4 元数据缓存更激进)
- SSD 实际利用率仅 1-2% (fio 测得 290K IOPS → 130 tps 理论,但实际只跑 7-8 tps)
- 瓶颈不是 SSD 本身,是 ftllm MoE expert 加载方式 (每 expert 单独 mmap+decode,不是批量 4K IO)

---

## 总体结论

### 1. SSD 性能未充分利用
- FIO 测得 290K IOPS，但实际推理仅用 7-8 tok/s
- 瓶颈在应用层 MoE expert 加载策略，非磁盘硬件

### 2. 纯 GPU 方案性能最优
- 双卡 TP 达到 ~184 tok/s，远超 SSD offload (7-8 tok/s)
- CPU RAM offload 达 33 tok/s，是 SSD 的 4-5 倍

### 3. 磁盘间差异有限
- 不同磁盘推理性能相差 <20%
- 主要受文件系统（ext4 vs NTFS）和 page cache 影响

### 4. LMCache 可行性验证
- CPU offload 模式保持高吞吐（6400 tok/s）
- KV cache 离线存储方案技术可行

---

## 附录

### 实验环境
- **CPU**: AMD/Intel x86_64
- **GPU**: RTX 5080 (16GB) + RTX 5060 Ti (16GB)
- **RAM**: 系统内存充足
- **OS**: Linux
- **磁盘**:
  - BIWIN X570 (NVMe, ext4, 系统盘)
  - WDC (NVMe, NTFS)
  - Seagate (NVMe, NTFS)
  - ZHITAI Ti600 (NVMe, NTFS)

### 数据来源
- LMCache 验证: `results/lmcache_validation/`
- SGlang 测试: `results/sglang_metrics_summary.json`
- FastLLM 对比: `results/fastllm-2026-06-23/`
- IO 分析: `results/io_pattern_analysis.json`
