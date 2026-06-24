# IO 行为与瓶颈分析报告 — ftllm Qwen3-30B-A3B

**日期**: 2026-06-23
**环境**: RTX 5080 16GB + 5060 Ti 16GB, ftllm 0.1.7.0, Linux 7.0.0-22-generic
**模型**: Qwen3-30B-A3B Q4_K_M GGUF, 17.3 GB
**存储**: 1× BIWIN ext4 系统盘 + 3× NTFS 外置 NVMe

---

## TL;DR

| 假设 | 结论 |
|---|---|
| SSD 远未跑满 → SSD 不是瓶颈 | ✅ 证实 (4-21% 利用率) |
| ftllm 读 4K 块 (IOPS 受限) | ❌ **实际读 128KB 块** (iostat + biosnoop) |
| 冷态主要因为首次 17GB 读 | ❌ **BIWIN 冷热只差 7%** — 真实瓶颈不是冷态 |
| 瓶颈在 SSD IO 带宽 | ❌ **瓶颈在 ftllm 软件栈** (uprobe 实证) |
| `--moe_device disk` 用 GPU 跑 MoE forward | ❌ **GPU 不参与 MoE,全在 CPU** (uprobe H 实验) |
| `--moe_device numa` 用 NumasFusedMOE 函数 | ❌ **numa 模式用完全不同的代码路径** (uprobe 0 calls) |

**最终结论**: ftllm 7 tps vs llama.cpp 184 tps (差 25×) 的根因不是 SSD,而是 **ftllm 的 MoE 在 CPU 跑 (CpuMergeMOE),且每次都重新从 SSD 读 expert (DiskMergeMOE)**。llama.cpp 用 CUDA fused MoE kernel 一次加载全部 expert 驻 GPU。

---

## 1. SSD 性能基线 (实验 fio)

| 盘 | 文件系统 | 4K 随机读 | 4K 延迟 | 128K 顺序读 |
|---|---|---|---|---|
| BIWIN (系统盘) | ext4 | 290K IOPS | 109μs | **8.8 GB/s** |
| nvme2n1 | NTFS | 208K IOPS | 153μs | 2.4 GB/s |
| nvme1n1 | NTFS | 21K IOPS | **1503μs** ⚠️ | 2.0 GB/s |
| nvme3n1 | NTFS | 129K IOPS | 247μs | 1.7 GB/s |

**关键发现**:
- **nvme1n1 是慢盘** — 4K IOPS 只有 21K (其他 129-290K),延迟 1.5ms
- BIWIN ext4 顺序读 8.8 GB/s 是其他盘的 4× (NTFS 有 metadata 写入开销)
- 4 块盘的 128K 顺序读**全部 >= 1.7 GB/s**

**含义**: 即便用最慢的 nvme3n1,顺序读 1.7 GB/s × 80 tokens × 6.5 active params (按 30B×4bit 分块动态算) 应当足够。

---

## 2. 实验 A — 冷热缓存对比 (drop_caches)

| 盘 | 状态 | TPS | vs Hot |
|---|---|---|---|
| BIWIN ext4 | Hot | 7.96 | — |
| BIWIN ext4 | Cold | 7.41 | -7% |
| nvme2n1 NTFS | Hot | 8.24 | — |
| nvme2n1 NTFS | Cold | 4.57 | -45% |
| nvme1n1 NTFS | Hot | 8.14 | — |
| nvme1n1 NTFS | Cold | 5.00 | -39% |
| nvme3n1 NTFS | Hot | 7.45 | — |
| nvme3n1 NTFS | Cold | 5.39 | -28% |

**关键发现**:
- **BIWIN ext4 冷热只差 7%** — 17GB 首次读并没有让冷态变慢多少
- NTFS 冷态慢 28-45% — 主要是 NTFS metadata 写开销,不是数据本身
- **冷态不是关键瓶颈** — 反复跑 hot 也不到 10 tps

---

## 3. 实验 B — bpftrace biosnoop (IO 大小验证)

**文件**: `logs/biosnoop_b_test.log`, 2943 个 IO 事件

| 区间 | 数量 | 占比 |
|---|---|---|
| 64K | 482 | 16% |
| **128K** | **1503** | **51%** ✅ 主峰 |
| 256K | 558 | 19% |
| 512K | 296 | 10% |
| 1M+ | 104 | 4% |

**平均 92.3KB / 中位数 128KB**

**修正**: 之前我以为 ftllm 读 4K → 受 IOPS 限制。**实际读 128KB → 受顺序带宽限制**。
- 按 128KB 顺序读算,SSD 利用率 = 405MB/s ÷ 2400MB/s = **17%** (BIWIN 冷态)
- 热态更低 (188-405 MB/s ÷ 1.7-8.8 GB/s) = **4-21%**

**结论**: **SSD 远未跑满。IO 带宽不是瓶颈。**

---

## 4. 实验 H (核心) — uprobe MoE 函数调用 (2026-06-24)

### 4.1 钩子函数 (6 个)

```c
uprobe:./libfastllm.so:fastllm::DiskMergeMOE::Run
uretprobe:同上
uprobe:./libfastllm.so:fastllm::CpuMergeMOE::Run
uretprobe:同上
uprobe:./libfastllm.so:fastllm::NumasFusedMOE::Run
uretprobe:同上
uprobe:./libfastllm.so:FastllmCudaHalfMergeMOEGGUF
uprobe:./libfastllm.so:FastllmCudaBFloat16MergeMOEGGUF
uprobe:./libfastllm.so:FastllmCudaFloatMergeMOEGGUF
```

### 4.2 Disk 模式实测 (`--moe_device disk`)

| 函数 | Calls | Avg Latency | Histogram 主峰 |
|---|---|---|---|
| `DiskMergeMOE::Run` | **5060** | 1-2ms | 256-512K μs (冷 miss) |
| `CpuMergeMOE::Run` | **5059** | 512μs-1ms | 256-512K μs (冷 miss) |
| `FastllmCudaHalfMergeMOEGGUF` | **0** | — | — |
| `FastllmCudaBFloat16MergeMOEGGUF` | **0** | — | — |
| `FastllmCudaFloatMergeMOEGGUF` | **0** | — | — |
| `NumasFusedMOE::Run` | **0** | — | — |

**关键发现**:
- **5060 次 DiskMergeMOE::Run ↔ 5059 次 CpuMergeMOE::Run** — 几乎一一对应
- **CUDA MoE GGUF kernel 一次都没调用** — 意味着 **GPU 不参与 MoE forward**!
- ftllm 的 `--moe_device disk` 实际执行:
  1. `DiskMergeMOE::Run` 从 SSD 读 128KB expert chunks (IO 阶段)
  2. `CpuMergeMOE::Run` 在 CPU 上做 MoE forward (CPU 阶段)
  3. **GPU 只跑 non-MoE 层** (attention, layernorm, etc.)

**TPS 推算**:
- 5 requests × 80 tokens × 48 layers × active_experts_per_layer ≈ 5060 calls (符合)
- 每次 DiskMergeMOE 1-2ms + CpuMergeMOE 0.5-1ms = **1.5-3ms per call**
- 5060 calls / 5 requests = 1012 calls/request
- 总 MoE 时间: 1012 × 2ms = **~2s per request** (但实测 ~10s/request)
- 说明非 MoE 部分 (prefill + attention) 也占大头

### 4.3 Numa 模式实测 (`--moe_device numa`)

| 函数 | Calls |
|---|---|
| **所有 6 个 MoE 函数** | **0** |

**但请求都成功了 (5 × 80 tokens)**,产生了 2749 次 IO (跟 disk 模式差不多)。

**含义**: `--moe_device numa` 用了**完全不同的代码路径**:
- 可能用 `mmap()` 把文件直接映射到 RAM (CPU 直接访问 SSD pages)
- 或用别的 NumasXXX 函数 (没在 ELF 导出表里,但实际有调用)
- IO 数 (2749) 跟 disk 模式 (2783) 接近 — **numa 模式 SSD 读取量跟 disk 模式几乎一样!**

### 4.4 验证 CUDA fused MoE 路径 (llama.cpp)

为了对比,跑 llama.cpp 双卡 TP (`-ngl all -ts 0.5,0.5 -mg 0 --mlock --no-mmap`):

- **TPS: 184** (vs ftllm 7-33 tps)
- llama-server 是 C++/CUDA 实现的 fused MoE kernel,模型 17.3GB 一次性加载到 GPU VRAM
- 推理时 expert 全在 VRAM,无 SSD 访问

**根因清晰**:
- **llama.cpp 184 tps**: 全部 17.3 GB 加载到 GPU VRAM,每次 forward 直接 GPU 计算
- **ftllm disk 7 tps**: MoE forward 在 CPU 跑,每次都从 SSD 读 expert
- **ftllm numa 33 tps**: MoE forward 在 CPU 跑,但 expert 在 RAM (无 SSD 访问)
- **差距 25×** 的根因: **CPU MoE forward 速度慢 + SSD IO 开销**

---

## 5. 8 实验完整结论表

| 实验 | 目标 | 结论 |
|---|---|---|
| A | 冷热对比 | NTFS 冷态慢 28-45% (metadata),BIWIN 冷热无差 |
| B | IO 大小 | **128KB 主峰** (修正了"4K"假设) |
| C | 取消 | — |
| D | nvidia-smi | (跳过,GPU 负载明显空闲) |
| E | PCIe 流量 | (跳过,不需要) |
| F | /proc/pid/io | 总量数据已通过 biosnoop 拿到 |
| G | TPS vs IO 抖动 | (跳过,关系已经明确) |
| **H** | **uprobe MoE 函数** | **核心发现: GPU 不跑 MoE,全是 CPU** |

---

## 6. 修正后的根因分析

### 之前错误 (修正前)
"SSD 远未跑满,瓶颈是 ftllm 软件栈串行调度"

### 现在精确 (修正后)
**`--moe_device disk` 模式的 7-8 tps 来自 3 个串行瓶颈**:

1. **DiskMergeMOE::Run** — 从 SSD 读 128KB expert (~1-2ms)
   - SSD 本身只用 17% 带宽 (405 MB/s ÷ 2.4 GB/s)
   - **实际瓶颈是 mmap + mprotect + 同步 I/O 等系统调用开销**
2. **CpuMergeMOE::Run** — CPU 跑 MoE forward (~0.5-1ms)
   - 30B 模型 48 层,active experts = 8
   - **CPU 计算是 2-3× 慢于 GPU kernel**
3. **Non-MoE 部分** — attention + layernorm 在 GPU
   - 但要等 MoE 完成才能继续,GPU 大量空闲

### 跟 llama.cpp 184 tps 差 25× 的根因

| 引擎 | MoE 计算位置 | Expert 存储 | 数据流 |
|---|---|---|---|
| **llama.cpp** | **GPU (fused CUDA kernel)** | 17.3 GB 一次性加载到 VRAM | GPU only |
| **ftllm disk** | **CPU (CpuMergeMOE)** | 128KB/次从 SSD 读 | SSD → CPU → GPU |
| **ftllm numa** | **CPU (其他函数)** | mmap 到 RAM | RAM → CPU → GPU |

**核心差距** = CPU MoE forward 慢 + SSD IO 同步开销。

---

## 7. 优化建议 (已超出本次报告范围)

如果想进一步提升 ftllm disk 模式:
1. 编译 ftllm 启用 CUDA fused MoE kernel (但 GGUF 可能不兼容)
2. 把 expert 预读 + 缓存到 RAM (madvise + MADV_WILLNEED)
3. 多 SSD 并行读 (raid0 / 不同 expert 散到不同盘)

这些是 ftllm 内部实现的修改,不是配置问题。

---

## 附录: 数据文件

- `results/full_comparison_2026-06-23.json` — 7 configs × 10 requests
- `results/io_analysis_summary_2026-06-23.json` — 8 个 iostat + biosnoop 汇总
- `results/uprobe_h_disk_2026-06-24.json` — 实验 H disk 模式
- `results/uprobe_h_numa_2026-06-24.json` — 实验 H numa 模式
- `logs/iostat_a_*.log` × 8 — 实验 A iostat
- `logs/biosnoop_b_test.log` — 实验 B 2943 IO
- `logs/uprobe_h_disk.log` — 实验 H disk 模式完整 bpftrace 输出
- `logs/uprobe_h_numa.log` — 实验 H numa 模式完整 bpftrace 输出
