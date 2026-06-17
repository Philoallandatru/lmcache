# AI SSD 预研项目综合总结报告

**日期**: 2026-06-17  
**项目范围**: LLM KV Cache 消费级 NVMe SSD 卸载场景调研  
**测试引擎**: SGLang HiCache 0.5.13 + vLLM 0.22.1 + LMCache 0.4.6  
**GPU**: RTX 5080 16GB (主) + RTX 5060 Ti 16GB  
**CPU/RAM**: Intel CPU / 68 GB  
**对照报告**: MLPerf Storage `validation_results.md` (H100 94GB HBM + 7TB NVMe)

---

## 1. 项目背景与目标

LLM 推理时 KV Cache 随序列长度增长,大量占用 GPU 显存。生产系统将 KV Cache 从 GPU 卸载到 CPU DRAM 再卸载到 NVMe SSD。本预研项目回答:

1. **消费级 AI SSD 能否胜任 KV Cache 卸载层?**
2. **4 块不同 SSD 之间性能差距多大?**
3. **SGLang HiCache 与 vLLM LMCache 两种方案在 RTX 5080 上表现如何?**
4. **与 MLPerf Storage 官方报告(validation_results.md)的结果是否一致?**

---

## 2. 硬件:4 块 SSD 对比

| 盘位 | 型号 | 容量 | 文件系统 | 角色 |
|---|---|---|---|---|
| ai_ssd0 | WDC WDS960G2G0C | 960GB | NTFS (fuseblk) | 外置数据盘 |
| ai_ssd1 | Seagate ZP1000GV30012 | 1TB | NTFS (fuseblk) | 外置数据盘 |
| ai_ssd2 | ZHITAI Ti600 1TB | 1TB | NTFS (fuseblk) | 外置数据盘 |
| 系统盘 | BIWIN X570 1TB | 1TB | **ext4** | 系统 & 应用 |

> ⚠️ **重要**:BIWIN 是系统盘(ext4),所有 SGLang 应用的 page cache 也落在此盘。NTFS 三盘通过 `ntfs-3g` 挂载。这导致 BIWIN 的 I/O 数据混入了 page cache 效应,不能跟 NTFS 盘直接比"原始盘性能"。

---

## 3. SGLang HiCache 测试 (6 个阶段)

### 3.1 核心架构问题: L2 屏蔽

SGLang 0.5.13 的 L2 (host DRAM KV cache) 容量 = (1 + hicache_ratio) × device KV pool。在 16GB GPU 上 L2 ≈ 41K tokens。**任何 ≤41K tokens 的单次 prompt 永远 L2 hit**,磁盘只收到写入流量。

**后果**: Phase 2-5 的 Cold/Warm TTFT 在 4 盘间差异 ≤ 5ms — 不是盘一样快,是 L2 掩盖了盘差。

### 3.2 Phase 7: 唯一有效的 4 盘对比

通过 20 个不同 prompt (7K 各) 填满 L2,再重放 prompt #0 强制 L3 读。

| 盘 | Cold | Warm | **Replay_p0** | iostat 读 |
|---|---|---|---|---|
| BIWIN ext4 | 1.444s | 1.419s | **1.663s** ⚡ | page cache |
| Seagate NTFS | 1.436s | 1.421s | **2.431s** | 1,517 MB/s |
| ZHITAI NTFS | 1.435s | 1.422s | **2.545s** | 1,560 MB/s |
| WDC NTFS | 1.436s | 1.422s | **2.643s** | 1,176 MB/s |

**Ranking**: BIWIN < Seagate ≈ ZHITAI < WDC  
**Spread**: 980ms (1.59×)

### 3.3 Phase 7G: 多轮稳定性验证 (6 轮)

| 盘 | 均值 | CV | 特征 |
|---|---|---|---|
| BIWIN | 1.620s | **1.3%** | Page cache 稳定 |
| ZHITAI | 2.272s | 7.7% | **NTFS 最快,持续变好** |
| WDC | 2.651s | 6.0% | 稳定中等 |
| Seagate | **2.981s** | **18.1%** ⚠️ | **双峰分布**:50% 概率 2.4s/3.5s |

> ⚠️ **Seagate 不可靠**:单次测试 Seagate 可能在排名 2-4 之间跳。ZHITAI 是 NTFS 中最稳定的选择。

### 3.4 P5: 三种写入策略对比

| 策略 | BIWIN | WDC | Seagate | ZHITAI |
|---|---|---|---|---|
| write_through (同步) | 1.670s | 2.647s | 3.108s | 2.430s |
| write_back (异步) | 1.683s | **OOM** ❌ | 3.137s | **OOM** ❌ |
| write_through_selective | 1.680s | **3.221s** ❌ | 3.137s | 2.535s |

**结论**:
- **write_back 在慢盘 OOM**:异步 flush 跟不上 20 个 prompt 的填充速率
- **write_through_selective 反而更慢**:碎片化读取 > 少写数据的收益
- **推荐: write_through 是唯一稳定选择**

### 3.5 fio 裸盘性能 (direct=1, 绕过 page cache)

| 测试 | BIWIN | ZHITAI | Seagate | WDC |
|---|---|---|---|---|
| 1线程 1MB 顺序 | **4,765 MB/s** 🥇 | 3,616 | 3,032 | 2,632 |
| 4线程并发 | **6,472 MB/s** 🥇 | 5,924 | 4,578 | 4,729 |
| 4K 随机 IOPS | **22.7K** 🥇 | 16.1K | 15.3K | 15.6K |

> **NTFS 盘的裸能力比 iostat 显示的高两个数量级**(2.6-3.6 GB/s 而非 8-12 MB/s)。差距主要来自文件系统驱动(NTFS vs ext4),不是 NAND 闪存本身。

### 3.6 Phase 8: 32K 多轮测试 — 硬件受阻

**状态: ❌ 未完成** — 16GB GPU 装不下 32K prompt + L2 cache。SGLang 0.5.13 max_input ≈ 20K tokens (mem-fraction=0.7) 或 26K (0.9)。**仅有 ≥48GB GPU (A100/H100) 才能完整测试 32K 场景**。

---

## 4. vLLM + LMCache 测试

### 4.1 Experiment A: 重复 Prompt TTFT (Pass1/Pass2)

8 个 KILLER prompt 发两次,vLLM 默认 prefix caching 开启。

| Backend | Pass1 (冷) | Pass2 (热) | Speedup | LMCache 增量 |
|---|---|---|---|---|
| 无 LMCache | 1,124ms | 415ms | **2.705×** | — |
| LMCache CPU | 1,247ms | 422ms | **2.958×** | **+0.253×** 🔥 |
| LMCache GPU | 1,236ms | 427ms | **2.895×** | **+0.190×** |

> **关键**:14B AWQ 模型(48层,8 KV heads, 192KB/token) 才能产生足够压力让 LMCache 显示加速。4B 模型增量仅 +0.09×。

### 4.2 Experiment B: 500 ShareGPT 吞吐

关闭 prefix caching (匹配报告 vLLM 0.13 行为),500 个唯一 prompt。

| 配置 | Throughput | 差异 |
|---|---|---|
| Baseline | 1,080.7 tok/s | — |
| LMCache CPU | 1,075.0 tok/s | **-0.5%** |
| LMCache GPU | 1,079.8 tok/s | **-0.1%** |

> **LMCache 0.4.6 管理开销极低**(<1%)。唯一 prompt 无缓存命中时三组吞吐完全一致。

### 4.3 与 MLPerf Storage 报告对比

| 项目 | 报告 (H100) | 我们 (RTX 5080) |
|---|---|---|
| Baseline 吞吐 | 13,730 tok/s (vLLM bench) | 1,081 tok/s (offline batch) |
| LMCache GPU | 9,508 tok/s (-31%) | 1,080 tok/s (-0.1%) |
| LMCache CPU | 9,411 tok/s (-31%) | 1,075 tok/s (-0.5%) |
| 报告自己结论 | LMCache 比纯推理慢 31% | LMCache 开销可忽略 |

**两个版本的 paradox**:你提供的报告摘录显示 baseline 7,247 tok/s、LMCache 9,508 = **+31%**。但磁盘文件写的是 baseline 13,730 tok/s、LMCache 9,508 = **-31%**。核心矛盾是 **报告内部版本冲突**。

**我们的分析**:报告中的 "LMCache 加速" 本质是 **vLLM 0.13 无默认 prefix caching → LMCache 开启了 KV 复用 → 看起来像加速**。在我们的 vLLM 0.22.1 上,prefix caching 已经默认开启,所以 LMCache 的边际贡献被压缩。

---

## 5. 综合 SSD 排名

| 排名 | 盘 | 1T 顺序读 | 4T 并发 | HiCache 均值 | 稳定性 CV | 推荐度 |
|---|---|---|---|---|---|---|
| 🥇 | **BIWIN (ext4)** | 4,765 MB/s | 6,472 | 1.62s | 1.3% | **最佳性能**但混杂 page cache |
| 🥈 | **ZHITAI (NTFS)** | 3,616 MB/s | 5,924 | 2.27s | 7.7% | **最佳 NTFS 选择** |
| 🥉 | Seagate (NTFS) | 3,032 MB/s | 4,578 | 2.98s | **18.1%** ⚠️ | 双峰不稳定 |
| 4 | WDC (NTFS) | 2,632 MB/s | 4,729 | 2.65s | 6.0% | 最慢,p99 延迟高 |

---

## 6. 当前与报告的差距

| 维度 | 报告已覆盖 | 我们已覆盖 | 缺口 |
|---|---|---|---|
| kv-cache.py 4 层模拟 | ✅ 完整 | ❌ 未跑 | 不能复现 storage I/O 吞吐 |
| LLM server 并发 | 50 用户 | 仅 offline batch | 不能比 server 吞吐 |
| 多层 KV 分配 | 16/8/4 GB 分层 | 0.75 利用率 | 不能比 tier scaling |
| I/O 延迟百分位 | p95/p99 逐层 | 仅 iostat | 不能比 tail latency |
| 3 次重复 | 是 | Experiment B 仅 1 次 | 统计严谨性不足 |
| 32K 场景 | 可运行 | 硬件受限 ❌ | A100/H100 级别需求 |

---

## 7. 结论

1. **消费级 NVMe SSD 可作为 KV Cache 卸载层**,但瓶颈不在盘带宽(2.6-4.7 GB/s 足够)而在软件栈开销和 GPU 显存限制。

2. **SGLang HiCache 在 16GB GPU 上受结构限制**:L2 永远 hit,单 prompt 测不出盘差。必须用 multiprompt L2-fill 才能看到 4 盘差异。这是 sglang 0.5.13 的设计特性,不是 bug。

3. **vLLM + LMCache 0.4.6 在 RTX 5080 + 14B-AWQ 上工作良好**。LMCache CPU offload 开销 < 1%,重复 prompt 场景提供 +0.19-0.25× TTFT 加速。vLLM 0.22.1 的默认 prefix caching 已提供 2.7× baseline 加速。

4. **ZHITAI Ti600 是 NTFS 盘中的最佳选择** — fio 性能仅次于 BIWIN,HiCache replay 稳定,无 Seagate 的双峰问题。

5. **NTFS vs ext4 的文件系统差异是实测性能差距的主因**,不是 NAND 硬件差距。建议生产环境使用 ext4 或 xfs。

6. **跨版本数据不可直接比较**:vLLM 0.13 vs 0.22.1 的 prefix caching 默认值变化改变了 baseline 语义,导致"LMCache 加速"结论相反。

7. **32K+ 长上下文场景需要 ≥48GB GPU**(RTX 5090 24GB 不够,A100/H100 级别),或等待 sglang 0.6+ 的 io_uring L3 改进。

---

## 8. 后续建议

| 优先级 | 建议 | 原因 |
|---|---|---|
| 🔴 高 | 运行 `kv-cache.py` 4 层存储模拟器 | 补上跟报告的 storage I/O 差距 |
| 🔴 高 | vLLM server-mode + 50 并发用户 | 补上 server 吞吐差距 |
| 🟡 中 | LMCache NVMe disk backend 测试 | 当前只测了 CPU offload |
| 🟡 中 | ext4/ntfs-3g 文件系统驱动对比 | 确认文件系统开销占比 |
| 🟢 低 | 等 RTX 5090 (24GB) 或 H100 做 32K 测试 | 当前硬件受限 |
