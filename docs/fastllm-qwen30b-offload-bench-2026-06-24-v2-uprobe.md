# Qwen3-30B-A3B 推理速度对比报告

**日期**: 2026-06-23
**模型**: Qwen3-30B-A3B (MoE, 30B 总参 / 3B 激活), Q4_K_M GGUF, 17.3 GB
**GPU**: RTX 5080 16GB + RTX 5060 Ti 16GB (总计 32GB HBM3)
**测试**: 10 个 prompt × 10 次重复, max_tokens=256, temperature=0
**基准**: tokens/s (TPS)

---

## 一、对比总表

| 模式 | 引擎 | 设备 | TPS | 相对纯GPU | 平均延迟 |
|---|---|---|---|---|---|
| 🚀 **纯GPU (双卡TP)** | llama.cpp | RTX 5080 + 5060 Ti, tensor-split 0.5:0.5 | **183.84 ± 4.84** | **1.0×** | 1.39s |
| 💾 **CPU RAM offload** | ftllm | GPU (non-MoE) + CPU RAM (MoE via `--moe_device numa`) | **33.4** | **5.5×慢** | 2.39s |
| 💽 BIWIN SSD (ext4) | ftllm | MoE 从系统盘读取 | 7.9 | 23×慢 | 10.4s |
| 💽 ZHITAI SSD (NTFS) | ftllm | MoE 从 /mnt/ai_ssd2 读取 | 7.8 | 24×慢 | 13.0s |
| 💽 WDC SSD (NTFS) | ftllm | MoE 从 /mnt/ai_ssd0 读取 | 6.9 | 27×慢 | 14.3s |
| 💽 Seagate SSD (NTFS) | ftllm | MoE 从 /mnt/ai_ssd1 读取 | 6.5 | 28×慢 | 15.8s |

### 各 SSD 详细对比

| 盘 | 型号 | 容量 | 文件系统 | 读速度 (MB/s) | TPS |
|---|---|---|---|---|---|
| BIWIN X570 | 系统盘 | 2TB | ext4 | ~30 | 7.9 |
| ZHITAI Ti600 | nvme2n1 | 2TB | NTFS | ~25 | 7.8 |
| WDC WDS960G2G0C | nvme0n1 | 960GB | NTFS | ~20 | 6.9 |
| Seagate ZP1000GV30012 | nvme1n1 | 1TB | NTFS | ~15 | 6.5 |

---

## 二、关键发现

### 1. 纯 GPU 双卡 TP 是绝对王者 (184 tps)

- **GPU 内存占用**: GPU 0 = 9.7 GB, GPU 1 = 9.1 GB (总 18.8 GB, 远低于 32GB 预算)
- **TPS 稳定**: min 170.1, max 185.86, stdev 4.84
- 第一个请求 (170 tps) 略慢,后续稳定在 185 tps,典型的 warm-up 行为
- latency 1.4s 主要是单次 256-token 解码的时间

### 2. CPU RAM offload 已经是 SSD 的 4.2-5.1×

- 33.4 tps 证明:即使 MoE expert 全部塞 CPU RAM,DDR4/DDR5 的带宽 (40-50 GB/s) 也远比 SSD 快
- 这是软件栈能"无感"实现的 offload 上限

### 3. ~~SSD offload 的瓶颈是 IO 带宽~~ → 修正:瓶颈是 CPU MoE forward + SSD IO 同步开销

> **2026-06-24 修正** (实验 H uprobe 实证)
> ~~之前判断 SSD 4K 随机读 IOPS 是瓶颈,实际:~~
> - bpftrace biosnoop 显示 **ftllm 读 128KB 块**(不是 4K),主峰 51% IO 都是 128KB
> - SSD 顺序读带宽利用率只有 4-21% (BIWIN 冷态 405MB/s ÷ 8.8GB/s = 5%)
> - uprobe 钩子证实 **`--moe_device disk` 模式下 MoE forward 在 CPU 跑,GPU 不参与** (CUDA MoE kernel 0 calls)
> - 详细数据见 `IO_ANALYSIS_REPORT.md` 第 3-4 节

### 4. 文件系统对 SSD offload 影响不大

- BIWIN (ext4) 比 NTFS 略快,但差距 < 1%
- NTFS 在 Linux 下通过 ntfs-3g 性能损失有限
- 真正影响的是盘本身的 4K IOPS

> **2026-06-24 修正**: NTFS 冷态比 ext4 慢 28-45%,主要是 NTFS metadata 写开销;但 hot 态差距 < 5%。

### 5. ftllm 与 llama.cpp 的能力差异

| 功能 | ftllm | llama.cpp |
|---|---|---|
| GGUF 读取 | ✅ | ✅ |
| `--moe_device disk` (MoE 单独 offload 到 SSD) | ✅ | ❌ |
| `--moe_device numa/cpu` (MoE 放 CPU RAM) | ✅ | ❌ |
| 多卡 TP (GGUF 格式) | ❌ (需要 merged qkv) | ✅ |
| 纯 GPU 推理 (双卡) | ❌ (Qwen3 MoE GGUF 不支持) | ✅ |

**结论**: ftllm 在 SSD offload 实验设计上更灵活,llama.cpp 在生产环境多卡 GPU 推理上更强。两个引擎互补。

---

## 三、为什么 ftllm 7 tps 而 llama.cpp 184 tps 差 25×? (2026-06-24 实验 H 实证)

> **本章节是 uprobe 实验 H 的核心结论,补充在 6.5-7.9 tps 之后的根因分析**

### A. uprobe 钩子实测 ftllm 3 种 MoE 模式

| 模式 | DiskMergeMOE | CpuMergeMOE | CUDA MoE GGUF | 钩到的总数 |
|---|---|---|---|---|
| **disk** (`--moe_device disk`) | 5060 | 5059 | **0** | ✅ 完整 |
| **numa** (`--moe_device numa`) | 0 | 0 | 0 | ❌ 一个都没钩到 |
| **cuda** (llama.cpp 双卡 TP) | n/a | n/a | (用 fused kernel) | n/a |

### B. 核心发现:ftllm disk 模式下 GPU 不跑 MoE

`--moe_device disk` 实际执行模型:

```
[SSD] → DiskMergeMOE::Run (读 128KB expert, 1-2ms)
        ↓
[CPU] → CpuMergeMOE::Run (CPU MoE forward, 0.5-1ms)
        ↓
[GPU] ← hidden states 传回 GPU
        ↓
[GPU] → attention + layernorm (non-MoE 层)
```

**CUDA MoE kernel 一次都没被调用** (FastllmCuda*MergeMOEGGUF 全部 0 calls)。

### C. TPS 分解 (5 请求 × 80 tokens)

- 总 DiskMergeMOE::Run: 5060 calls = **1012 calls/request = 12.65 calls/token**
- 每次 DiskMergeMOE 1-2ms → MoE IO 时间 ~2s/request
- 每次 CpuMergeMOE 0.5-1ms → MoE 计算时间 ~0.7s/request
- **MoE 部分总耗时: 2.7s/request → 理论上 80tps**

实际测得 **7-8 tps** (~10s/request) → **非 MoE 部分 (prefill + attention) 也占大头**,跟 MoE 串行。

### D. 25× 差距的根因 (一句话)

**llama.cpp 把全部 17.3GB 一次性加载到 GPU VRAM,用 fused CUDA MoE kernel 一次 forward 搞定;ftllm 的 MoE 在 CPU 跑,每次都重新从 SSD 读 expert chunks。**

| 引擎 | MoE 计算位置 | Expert 存储 | 数据流 | TPS |
|---|---|---|---|---|
| **llama.cpp** | **GPU (fused CUDA kernel)** | 17.3GB 全在 VRAM | GPU only | **184** |
| **ftllm disk** | **CPU (CpuMergeMOE)** | 128KB/次从 SSD 读 | SSD → CPU → GPU | **7** |
| **ftllm numa** | **CPU (其他函数)** | mmap 到 RAM | RAM → CPU → GPU | **33** |

### E. 为什么 ftllm 不用 CUDA fused MoE?

- llama.cpp 的 CUDA MoE kernel 需要模型是**标准格式**(每层连续的 expert weights)
- ftllm 加载 GGUF 时 **expert 是按需读**,在 CPU 上拼装 (CpuMergeMOE)
- 要支持 CUDA fused MoE 需要 ftllm 内部重构数据流,不是配置问题

---

## 四、纯 GPU vs CPU RAM vs SSD 性能差距根因 (原第三节)

### 纯 GPU vs CPU RAM (5.5× 差距)

- **GPU HBM3 带宽**: ~1500 GB/s (RTX 5080) + ~1200 GB/s (5060 Ti) = ~2700 GB/s 聚合
- **CPU DDR5 带宽**: ~50-80 GB/s (双通道)
- **理论**: GPU 应该快 35-50×,实际只快 5.5×
- **原因**: MoE 激活比例只有 3B/30B = 10%,大部分权重不参与计算;推理时间还包含 attention、K/V cache、tokenization 等非权重读取的开销

### CPU RAM vs SSD (4-5× 差距)

- **NVMe 顺序读**: ~3-5 GB/s (理论), 实际 SSD offload 模式 ~15-30 MB/s
- **根因**: MoE expert 权重 ~1-2 MB 小块随机读,SSD 4K IOPS 限制
- **CPU RAM**: 随机小块读延迟 ~80ns,SSD 延迟 ~50-100μs (~1000× 差距)
- **结果**: 即使 SSD 顺序带宽是 RAM 的 1/10,但随机小块读性能比 RAM 慢 4-5×

### 各 SSD 之间差距小 (6.5-7.9 tps)

- 消费级 NVMe SSD 在 4K 随机读 IOPS 上差异有限 (都在 50K-200K IOPS 之间)
- 顺序读带宽差异被随机读模式掩盖
- NTFS vs ext4 文件系统开销在 MoE offload 这种 IO 模式下不是瓶颈

---

## 四、实际应用建议

### 如果你有 32GB+ GPU 显存
✅ 直接纯 GPU 双卡推理 (llama.cpp),**184 tps** 是天花板,其他方式都是降级方案

### 如果只有 16-24GB GPU,模型 30B+
✅ CPU RAM offload (**33 tps**),是性价比最高的方案
- 80GB 系统内存装得下 17.3GB GGUF
- 速度比 SSD offload 快 4-5×
- 延迟只有 2.4s,用户体验流畅

### 如果只有 16GB GPU + 有限内存
⚠️ SSD offload (7-8 tps) 是最后的手段
- 选择 4K IOPS 最高的盘 (本测试 BIWIN/ZHITAI 略胜)
- 文件系统选择影响不大
- 接受 ~10-15s 的延迟

### 如果目标是预研 / demo
✅ 推荐架构: **2× RTX 5080 (32GB) + llama.cpp TP=2**,稳定 180+ tps

---

## 五、原始数据

### 纯 GPU (llama.cpp) 详细结果

| 请求 | 延迟 (s) | Tokens | TPS |
|---|---|---|---|
| 1 | 1.505 | 256 | 170.10 |
| 2 | 1.384 | 256 | 184.99 |
| 3 | 1.384 | 256 | 184.95 |
| 4 | 1.386 | 256 | 184.74 |
| 5 | 1.380 | 256 | 185.55 |
| 6 | 1.379 | 256 | 185.63 |
| 7 | 1.380 | 256 | 185.45 |
| 8 | 1.380 | 256 | 185.50 |
| 9 | 1.377 | 256 | 185.86 |
| 10 | 1.379 | 256 | 185.60 |

### CPU RAM offload (ftllm NUMA) 详细结果

10 次请求全部稳定在 32.3-32.4 tps,延迟 2.47-2.48s

### BIWIN SSD (ext4) 详细结果

10 次请求 TPS 在 5.68-9.99 之间波动,平均 7.90 tps

---

## 六、附录

### 硬件环境
- **CPU**: (待补充)
- **内存**: 80GB+ DDR4/DDR5
- **GPU 0**: NVIDIA GeForce RTX 5080 (16GB HBM3)
- **GPU 1**: NVIDIA GeForce RTX 5060 Ti (16GB HBM3)
- **SSD**:
  - BIWIN X570 2TB (系统盘, ext4)
  - ZHITAI Ti600 2TB (nvme2n1, NTFS)
  - WDC WDS960G2G0C 960GB (nvme0n1, NTFS)
  - Seagate ZP1000GV30012 1TB (nvme1n1, NTFS)

### 软件栈
- **OS**: Linux 7.0.0-22-generic
- **CUDA**: 13.3
- **ftllm**: 0.1.7.0
- **llama.cpp**: built from source (commit 581e8eca8), CUDA backend enabled
- **Python venv**: ~/llm/fast/.venv

### 测试脚本
- `scripts/bench_llamacpp.py` - 纯 GPU 双卡 benchmark
- `scripts/bench_qwen30b.py` - ftllm SSD/NUMA benchmark
- `scripts/start_llamacpp_dual_gpu.sh` - llama.cpp 启动脚本
- `scripts/start_qwen30b_disk.sh` - ftllm disk offload 启动脚本

### 结果文件
- `results/full_comparison_2026-06-23.json` - 汇总对比
- `results/qwen30b_llamacpp_pure_gpu.json` - 纯 GPU 详细结果
- `results/qwen30b_disk_vs_numa_2026-06-23.json` - SSD vs NUMA 对比
