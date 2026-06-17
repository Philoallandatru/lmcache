# LMCache 基准测试综合报告

**日期**：2026-06-17  
**仓库**：`lmcache` (`https://github.com/Philoallandatru/lmcache.git`)  
**本次结果目录**：`results/`、`results/lmcache_500prompts/`  
**对照报告**：`~/llm/storage/kv_cache_benchmark/vllm_lmcache_validate/validation_results.md`

> 重要说明：用户提供的原始报告摘录称 Section 7 中 `vLLM Baseline = 7,247 +/- 85 tok/s`、`LMCache CPU/GPU = 9,411/9,508 tok/s`，并据此声称 LMCache 有约 `+30%` 吞吐提升。但当前磁盘上的 `validation_results.md` 第 7 节实际写的是 `vLLM Baseline = 13,730 +/- 9 tok/s`，且结论是 baseline 比 LMCache 更快约 31%。本报告会同时记录这个版本差异：主线分析按用户给出的“+30% 原始摘录”解释悖论，同时在“与当前文件的差异”中指出磁盘文件内容并不支持该声称。

## 1. Executive Summary

本次工作围绕 LMCache 0.4.6 与 vLLM 0.22.1 做了两类验证：

1. **Experiment A：8 个 KILLER prompts 的 TTFT 冷/热缓存测试**
   - 每轮发送 8 个相同 prompt，每个约 337 tokens。
   - Pass1 为冷 prefill，用于写入 KV cache。
   - Pass2 为热复用，用于读取或命中已有 KV cache。
   - 每个 backend 跑 3 次。
   - vLLM 0.22.1 默认 `enable_prefix_caching=True`。

2. **Experiment B：500 个 ShareGPT prompts 的离线 batch throughput 测试**
   - 使用 ShareGPT 数据集前 500 个 human prompts。
   - `max_tokens=128`，`temperature=0.7`。
   - 显式设置 `enable_prefix_caching=False`，以模拟原始报告中 vLLM 0.13 行为。
   - 每个 backend 跑 1 次。
   - 这是离线 batch inference，不是 server-mode benchmark。

核心结论：

- **vLLM 0.22.1 的默认 prefix caching 已经让“无 LMCache baseline”获得明显 Pass2 加速**：Experiment A 中 disabled baseline 的 Pass1/Pass2 TTFT 已有 `2.705x` speedup。
- **LMCache 在 Experiment A 中只提供边际增益**：CPU backend 的 speedup ratio 比 disabled baseline 高 `+0.253x`，GPU backend 高 `+0.190x`。
- **在 500 个互不重复的 ShareGPT prompts 上，LMCache 几乎不改变吞吐**：baseline、LMCache CPU、LMCache GPU 均在 `~1,075-1,081 tok/s`，差距小于 `0.5%`。
- **“原始报告 +30%”与“本次 -0.5%”不矛盾，前提是原始 +30% 确实来自用户摘录版本**：它比较的是“无 KV cache 复用”与“有 KV cache 复用”的效果；本次 disabled baseline 已经有 vLLM prefix caching，或者在关闭 prefix caching 且 prompt 全唯一时没有可复用 KV。
- **与原始报告仍有明显 gap**：本次尚未跑 `kv-cache.py` 四层存储模拟器、vLLM server mode、50 并发用户、多层 GPU/CPU/NVMe allocation、I/O latency percentile profiling，也没有为 throughput 做 3 次重复试验。

## 2. Hardware Setup

### 2.1 本次测试硬件

| Component | Specification |
|---|---|
| GPU | RTX 5080 16GB + RTX 5060 Ti 16GB |
| 实际用于单卡测试的 GPU | RTX 5080 16GB |
| CPU | Intel CPU |
| Host RAM | 约 68 GB |
| Model | Qwen2.5-14B-Instruct-AWQ |
| Model size | 约 9.3 GiB loaded |
| Quantization | AWQ 4-bit, Marlin |
| Layers | 48 |
| KV heads | 8 |
| Software | vLLM 0.22.1, LMCache 0.4.6 |

### 2.2 与原始报告环境的差异

| Item | 原始报告 | 本次复现 |
|---|---:|---:|
| GPU | H100 NVL, ~94GB HBM3 | RTX 5080, 16GB |
| CPU/RAM | 双路 Xeon + 256GB RAM | Intel CPU + 约 68GB RAM |
| Model | Mistral-7B-Instruct-v0.2 | Qwen2.5-14B-Instruct-AWQ |
| Precision/quantization | bf16/fp16 类推理配置 | AWQ 4-bit + Marlin |
| vLLM | 0.13.0 | 0.22.1 |
| LMCache | 0.3.12 | 0.4.6 |
| Benchmark mode | 原始报告包含 storage simulator 与 real inference reference | 本次包含 TTFT 与 offline batch inference |

这些差异意味着绝对吞吐不能直接比较。H100 的 HBM 容量和带宽、模型大小、量化方式、vLLM 版本、prefix caching 默认行为都不同。

## 3. Experiment A：8 KILLER Prompts TTFT

### 3.1 Methodology

本实验目标是观察同一组 prompts 在冷启动和热复用之间的 TTFT 差异。

```text
Backend matrix:
- disabled: 不启用 LMCache
- LMCache CPU: LMCache 使用 host CPU RAM backend
- LMCache GPU: LMCache 使用 GPU-only backend

Protocol:
- 每个 pass 发送同样的 8 个 prompts
- 每个 prompt 约 337 tokens
- Pass1 = cold prefill，构建或写入 KV cache
- Pass2 = hot reuse，读取或命中 KV cache
- 每个 backend 3 trials
- vLLM 0.22.1 默认 enable_prefix_caching=True
```

### 3.2 Results

| Backend | Pass1 TTFT (ms) | Pass2 TTFT (ms) | Speedup |
|---|---:|---:|---:|
| disabled | 1,124 | 415 | 2.705x |
| LMCache CPU | 1,247 | 422 | 2.958x |
| LMCache GPU | 1,236 | 427 | 2.895x |

### 3.3 Interpretation

最重要的观察是：**disabled baseline 已经有 `2.705x` Pass2 TTFT speedup**。这说明即使完全不启用 LMCache，vLLM 0.22.1 的默认 prefix caching 也已经复用了相同 prompt 的前缀 KV。

LMCache 的额外贡献体现在 speedup ratio 上：

| Backend | Speedup | Marginal delta vs disabled |
|---|---:|---:|
| disabled | 2.705x | baseline |
| LMCache CPU | 2.958x | +0.253x |
| LMCache GPU | 2.895x | +0.190x |

需要谨慎解读的是，LMCache CPU/GPU 的 Pass2 TTFT 绝对值并没有比 disabled baseline 更低：

- disabled Pass2：`415 ms`
- LMCache CPU Pass2：`422 ms`
- LMCache GPU Pass2：`427 ms`

LMCache 的 speedup ratio 更高，主要是因为 Pass1 带有额外写入和管理开销，导致 Pass1 更慢；Pass2 仍然保持在同一量级。因此本实验能说明 LMCache 参与了缓存路径，但不能简单表述为“Pass2 绝对 TTFT 比 vLLM prefix caching 更快”。

### 3.4 LMCache 日志观察

LMCache CPU backend 日志显示了 256-token chunk 的存储行为：

```text
Stored 256 out of total 256 tokens.
size: 0.0469 GB
cost: about 2.4-3.3 ms
throughput: about 14-20 GB/s
```

典型 chunk store throughput 在 `17-19 GB/s` 附近，单个 256-token chunk 约 `2.5 ms`。这说明 LMCache 0.4.6 的 CPU offload 路径本身开销不大；在本实验中，真正决定 TTFT 变化的仍然是 vLLM prefix caching 与 prefill/decode 调度。

## 4. Experiment B：500 ShareGPT Prompts Throughput

### 4.1 Methodology

本实验目标是尽量模拟原始 vLLM 0.13 行为，将 prefix caching 关闭后比较 baseline 与 LMCache 的整体离线 batch throughput。

```text
Dataset:
- ShareGPT_V3_unfiltered_cleaned_split_no_imsorry.json
- 取前 500 个 human prompts

Generation:
- max_tokens = 128
- temperature = 0.7
- num_prompts = 500

Important control:
- enable_prefix_caching = False

Mode:
- offline batch inference
- 非 vLLM serve
- 非 50 concurrent users server-mode
- 每个 backend 1 trial
```

### 4.2 Results

| Config | Total Tokens | Prompt | Output | Throughput (tok/s) | Output Thru (tok/s) | Time (s) |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (no LMCache) | 117,852 | 54,505 | 63,347 | 1,080.7 | 580.9 | 109.05 |
| LMCache CPU | 117,852 | 54,505 | 63,347 | 1,075.0 | 577.9 | 109.62 |
| LMCache GPU | 117,852 | 54,505 | 63,347 | 1,079.8 | 580.4 | 109.14 |

### 4.3 Delta vs baseline

| Config | Throughput (tok/s) | Delta vs baseline |
|---|---:|---:|
| Baseline (no LMCache) | 1,080.7 | baseline |
| LMCache CPU | 1,075.0 | -0.53% |
| LMCache GPU | 1,079.8 | -0.08% |

### 4.4 Interpretation

在关闭 `enable_prefix_caching` 且 500 个 ShareGPT prompts 基本互不重复的情况下，LMCache 没有可复用的 KV cache。因此三组吞吐几乎完全相同：

- LMCache CPU 相比 baseline 慢约 `0.53%`
- LMCache GPU 相比 baseline 慢约 `0.08%`
- 差距处于单次 trial 的正常波动范围内

这说明：

1. **没有重复前缀时，LMCache 不会凭空制造吞吐提升。**
2. **LMCache 0.4.6 的管理开销很低**，即使命中为 0，总体 overhead 也小于约 `1%`。
3. **本实验不是 server-mode benchmark**，因此不能直接对比原始报告中 50 concurrent users 场景。

## 5. 与原始 `validation_results.md` 的比较

### 5.1 用户提供的原始报告摘录

用户提供的 Section 7 摘录如下：

| Config | Throughput (tok/s) |
|---|---:|
| vLLM Baseline | 7,247 +/- 85 |
| LMCache CPU | 9,411 +/- 131 |
| LMCache GPU | 9,508 +/- 116 |

按这组数字计算：

| Config | Throughput (tok/s) | Delta vs baseline |
|---|---:|---:|
| vLLM Baseline | 7,247 | baseline |
| LMCache CPU | 9,411 | +29.9% |
| LMCache GPU | 9,508 | +31.2% |

该版本的结论是 LMCache 在 real-world integration 中带来约 `+30-31%` speedup。

### 5.2 当前磁盘文件中的 Section 7

当前磁盘上的 `validation_results.md` 第 7 节实际数据为：

| Config | Throughput (tok/s) | Notes |
|---|---:|---|
| vLLM Baseline | 13,730 +/- 9 | No KV caching, pure inference |
| LMCache GPU | 9,508 +/- 32 | KV cache in GPU memory |
| LMCache CPU Offload | 9,411 +/- 91 | KV cache with CPU tier |

当前文件自己的观察结论是：

- vLLM baseline 比 LMCache 快约 31%。
- LMCache GPU 与 CPU 差距约 1%，说明 CPU offload 路径效率较高。
- baseline 与 LMCache 的 token counts 不同，因此该节本身也提示了比较口径不完全一致。

因此，**当前磁盘文件并不支持“LMCache 比 baseline 快 30%”这个结论**。如果需要严格复现当前文件，应把它解读为“LMCache 引入 KV 管理后在该脚本口径下吞吐低于 vLLM bench baseline”。如果需要解释用户摘录中的 `7,247 -> 9,4xx` 版本，则应使用下一节的 gap analysis。

## 6. Gap Analysis：为什么原始 +30%，本次约 -0.5%？

### 6.1 悖论表述

表面上看，两组结论冲突：

| Source | Baseline | LMCache | 表面结论 |
|---|---:|---:|---|
| 用户提供的原始摘录 | 7,247 tok/s | 9,411-9,508 tok/s | LMCache +30-31% |
| 本次 500 prompts | 1,080.7 tok/s | 1,075.0-1,079.8 tok/s | LMCache 约 0%，CPU 为 -0.5% |

根因不是 LMCache 在新版本“失效”，而是**比较对象变了**。

### 6.2 原始 +30% 的真实含义

如果原始报告中的 baseline 确实是 vLLM 0.13 的无 prefix-cache baseline，那么：

- 原始 baseline = “纯推理路径”：每个 request 都重新计算 prefill KV。
- 原始 LMCache = “KV cache reuse 路径”：相同或共享前缀的 KV 可以被缓存并复用。
- `+30%` 主要测到的是 **KV cache reuse effect**。
- 它不是单纯的 **LMCache offload effect**，也不是 CPU/GPU 存储层本身带来的 30% 加速。

换句话说，原始 +30% 更适合表述为：

```text
无 KV 复用的 vLLM baseline
vs
有 KV 复用能力的 LMCache 集成路径
```

而不是：

```text
vLLM prefix caching
vs
LMCache offload
```

### 6.3 本次 Experiment A 的控制变量不同

本次 vLLM 0.22.1 默认 `enable_prefix_caching=True`，所以 disabled baseline 已经具备 KV 前缀复用：

| Backend | Pass1 TTFT | Pass2 TTFT | Speedup |
|---|---:|---:|---:|
| disabled | 1,124 ms | 415 ms | 2.705x |

这会压缩 LMCache 可见收益。LMCache CPU/GPU 的边际 speedup ratio 增益分别只有：

- CPU：`+0.253x`
- GPU：`+0.190x`

因此 Experiment A 的结论不是“LMCache 没有用”，而是：

> 在现代 vLLM 默认 prefix caching 已经生效的前提下，LMCache 与 vLLM 内建 prefix cache 的收益高度重叠；LMCache 的边际收益需要通过更复杂的跨请求、跨进程、跨层级存储场景来体现。

### 6.4 本次 Experiment B 的 workload 没有缓存命中

Experiment B 刻意关闭 `enable_prefix_caching=False`，这一点更接近 vLLM 0.13 行为。但它使用的是前 500 个 ShareGPT human prompts，prompt 基本唯一。

结果是：

- baseline 没有 prefix cache。
- LMCache 有缓存系统，但没有重复前缀可命中。
- LMCache 日志显示 hit 为 0 或没有有效复用。
- 三组吞吐只剩管理 overhead 和正常测量波动。

因此出现：

| Config | Throughput | Explanation |
|---|---:|---|
| Baseline | 1,080.7 tok/s | 无 LMCache，无复用 |
| LMCache CPU | 1,075.0 tok/s | 无命中，只有很小管理开销 |
| LMCache GPU | 1,079.8 tok/s | 无命中，几乎等同 baseline |

### 6.5 版本变化的影响

vLLM 0.13 与 vLLM 0.22.1 的关键差异是 prefix caching 的默认行为和引擎实现已经变化。对 LMCache benchmark 来说，这会改变 baseline 定义：

| vLLM version | Baseline 含义 | 对 LMCache speedup 的影响 |
|---|---|---|
| vLLM 0.13 | 更接近无内建 prefix reuse 的纯推理 baseline | LMCache 的 KV reuse 收益更显著 |
| vLLM 0.22.1 | 默认 `enable_prefix_caching=True` | baseline 已经吃掉大部分 prefix reuse 收益 |

因此跨版本比较时必须明确回答一个问题：

```text
baseline 是否已经启用了 KV/prefix cache reuse？
```

如果答案不同，speedup 数字不能直接比较。

## 7. Remaining Gaps vs Original Report

| Gap | 原始报告 | 本次复现 | Impact |
|---|---|---|---|
| `kv-cache.py` storage simulator | Full 4-tier comparison: GPU/CPU/NVMe | Not run | 不能比较 storage I/O throughput |
| vLLM server mode | 50 concurrent users | Offline batch only | 不能比较 server throughput 与并发调度 |
| Multi-tier allocation | 16/8/4 GB GPU tiers | `gpu_memory_utilization=0.75`，可用 KV pool 受 16GB 显存限制 | 不能比较 tier scaling |
| I/O profiling | latency percentiles per tier | Not done | 不能比较 offload I/O pattern 与 tail latency |
| 3 trials | throughput 有 3 trials | Experiment B 只有 1 trial，Experiment A 为 3 trials | 500-prompt throughput 统计严谨性不足 |
| GPU | H100 94GB HBM3 | RTX 5080 16GB | 显存少约 6 倍，无法跑大 KV pool |
| Model | Mistral-7B bf16/fp16 | Qwen2.5-14B-AWQ | 架构、层数、量化、KV footprint 不同，吞吐不可直接横比 |
| LMCache version | 0.3.12 | 0.4.6 | connector、backend、日志与默认行为可能变化 |
| vLLM version | 0.13.0 | 0.22.1 | prefix caching 默认行为变化，是最大语义差异 |

## 8. Recommendations for Next Steps

### 8.1 复现 storage simulator

运行 `kv-cache.py` 的四层存储模拟器，复现原始报告的核心 storage I/O 对比：

```bash
# 建议使用原始报告中的 mistral-7b config.yaml
# 该 benchmark 是 storage-only simulator，不依赖真实 LLM inference。
python3 kv-cache.py \
  --config config.yaml \
  --tier gpu

python3 kv-cache.py \
  --config config.yaml \
  --tier gpu_cpu

python3 kv-cache.py \
  --config config.yaml \
  --tier gpu_cpu_nvme

python3 kv-cache.py \
  --config config.yaml \
  --tier nvme
```

重点记录：

- `storage_throughput_tokens_per_sec`
- GPU/CPU/NVMe read/write latency percentiles
- hit rate
- total read/write volume
- waterfall eviction 行为

### 8.2 跑 vLLM server-mode benchmark

使用 `vllm serve` + `bench.py` 或原始报告相同 pattern，构建 50 concurrent users 场景：

```bash
vllm serve /home/ficus/llm/models/Qwen/Qwen2___5-14B-Instruct-AWQ \
  --gpu-memory-utilization 0.75 \
  --max-model-len 2048
```

然后分别测试：

- vLLM baseline，`enable_prefix_caching=True`
- vLLM baseline，`enable_prefix_caching=False`
- LMCache CPU
- LMCache GPU

这样才能拆分：

1. vLLM 内建 prefix caching 的收益
2. LMCache 相对 prefix caching 的边际收益
3. LMCache 在 server 并发调度下的 overhead
4. CPU offload 对 tail latency 的影响

### 8.3 Profile LMCache offload I/O

建议用 `iostat`、`pidstat`、`perf` 或 `bpftrace` 对 LMCache CPU/NVMe offload 路径做 profiling：

```bash
iostat -xz 1
pidstat -d -r -u 1
```

如果后续启用 NVMe tier，应重点观察：

- per-chunk read/write latency
- queue depth
- p95/p99 I/O latency
- CPU copy overhead
- pinned memory 或 page fault 行为

### 8.4 扫描不同 `gpu_memory_utilization`

本次主要使用 `gpu_memory_utilization=0.75`。建议扫描：

| Value | Purpose |
|---:|---|
| 0.60 | 制造更小 KV pool，观察 LMCache offload 是否更早介入 |
| 0.75 | 当前 baseline |
| 0.85 | 提高 GPU KV cache 容量 |
| 0.90 | 接近显存上限，观察 OOM 风险与吞吐 |

目标是找出 RTX 5080 16GB 上可稳定运行的最大 KV pool，并观察 LMCache CPU backend 是否在 GPU KV 压力上升后体现更大收益。

### 8.5 增加重复试验

Experiment B 目前每个 backend 只有 1 trial。建议至少跑 3 次：

```text
baseline:     3 trials
LMCache CPU:  3 trials
LMCache GPU:  3 trials
```

报告均值、标准差和 CV，避免把单次波动误判为性能差异。

## 9. Conclusions

- 本次复现表明，在 vLLM 0.22.1 默认 `enable_prefix_caching=True` 时，无 LMCache baseline 已经获得 `2.705x` Pass2 TTFT speedup；LMCache 的可见收益被现代 vLLM 内建 prefix caching 大幅压缩。
- 在 500 个唯一 ShareGPT prompts、且 `enable_prefix_caching=False` 的离线 batch 测试中，LMCache CPU/GPU 与 baseline 吞吐差距小于 `0.5%`，说明没有 cache hit 时 LMCache 基本只带来极低管理开销。
- 用户摘录版原始报告中的 `+30-31%` 更应解释为“KV cache reuse 相对无复用 baseline 的收益”，而不是“LMCache offload 本身相对现代 vLLM prefix caching 的收益”。
- 当前磁盘上的 `validation_results.md` 与用户提供的原始摘录不一致：磁盘文件第 7 节实际写的是 vLLM baseline `13,730 tok/s`，高于 LMCache `9,4xx tok/s`，因此需要先确认使用哪个报告版本作为权威对照。
- 本次尚未覆盖原始报告最核心的 storage simulator、多层 tier allocation、server-mode 50 并发和 I/O percentile profiling；这些是后续复现与归因的主要缺口。

---

## 附录：术语解释

| 术语 | 中文 | 说明 |
|---|---|---|
| **KV Cache** | 键值缓存 | LLM 推理时存储中间注意力数据的缓存。序列越长，缓存越大。 |
| **TTFT** | 首 Token 延迟 | 从用户发出请求到模型输出第一个词的时间。用户体验最敏感的指标。 |
| **Throughput** | 吞吐量 | 每秒处理的 token 数量（tok/s）。 |
| **Speedup** | 加速比 | 冷启动耗时 / 热启动耗时。2.0× 表示热启动比冷启动快一倍。 |
| **Prefix Caching** | 前缀缓存 | vLLM 内建功能。相同前缀只计算一次 KV，后续直接复用。 |
| **LMCache** | — | KV Cache 卸载方案，把 KV 数据存到 CPU 内存或 SSD。 |
| **SGLang HiCache** | — | 另一 KV Cache 卸载方案，GPU→CPU→NVMe 三级缓存。 |
| **enable_prefix_caching** | 前缀缓存开关 | vLLM 0.22.1 默认开启，vLLM 0.13 默认关闭。这个差异是"报告 vs 我们结论相反"的根本原因。 |
| **Offline batch** | 离线批量推理 | 一次性把 500 个请求全扔给模型，批量处理。不是逐个响应的服务器模式。 |
| **Server-mode** | 服务器模式 | 启动 HTTP 服务器，客户端逐个发请求，模拟真实用户并发。原始报告用 50 并发用户。 |
| **OOM** | 显存溢出 | 模型+缓存超过 GPU 可用显存，推理崩溃。 |
| **page cache** | 操作系统页缓存 | Linux 自动缓存最近读过的文件，加速重复读但掩盖真实盘速。 |
| **chunk store** | 分块存储 | LMCache 把 KV 切成 256 token 小块分别存储，每块 ~2.5ms。 |
| **CV** | 变异系数 | 标准差/均值×100%，衡量数据稳定性。CV > 10% 表明不稳定。 |
| **bimodal / 双峰** | — | 同一操作有时快有时慢，呈现两种截然不同的速度。 |
| **KV transfer config** | KV 传输配置 | vLLM 中启用 LMCache 的配置对象：`KVTransferConfig(kv_connector="LMCacheConnectorV1")`。 |
| **storage simulator** | 存储模拟器 | `kv-cache.py` 是一个纯存储 I/O 模拟工具，不跑真正的 LLM 推理，只测存储层的读写延迟和吞吐。 |

