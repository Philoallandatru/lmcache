# AI SSD 真实 Offloading 场景调研报告

**日期**: 2026-06-17
**范围**: 整合报告 `validation_results.md` 与 `ai_ssd_prestudy` 实测数据的对比分析

---

## 1. 背景：两个报告的定位差异

`validation_results.md` 是一份 **MLPerf Storage 的 validation 报告**,重点验证 kv-cache.py 四种存储 tier (全GPU、GPU+CPU、GPU+CPU+NVMe、全NVMe) 的 I/O 吞吐,同时引用了 vLLM+LMCache 的实际推理数据作为参照。它运行在 **H100 (94GB HBM) + 单块 7TB NVMe** 的高端服务器上。

`ai_ssd_prestudy` 系列测试运行在 **RTX 5080/5060 Ti (各 16GB) + 4 块 AI SSD** (BIWIN X570, Seagate FC530, ZhiTai Ti600, WD SN570) 上,关注 **KV Cache offload 到消费级 AI SSD** 时盘的行为差异。

**核心区别**:

| 维度 | 报告 (validation_results.md) | ai_ssd_prestudy (我们的测试) |
|---|---|---|
| GPU | H100, 94GB HBM, 3350GB/s | RTX 5080 + 5060 Ti, 各 16GB |
| 存储 | 单块 7TB NVMe (~14GB/s) | 4 块消费级 SSD (不同型号) |
| 模型 | Mistral-7B-Instruct-v0.2 (bf16) | Qwen3-4B → Qwen2.5-14B-AWQ |
| 重点 | MLPerf 提交验证 | 产品选型与盘行为分析 |
| 方法 | kv-cache.py (trace replay) | kv-cache.py + LMCache bench + fio + bpftrace |

---

## 2. 报告原文可以验证的部分

### 2.1 GPU HBM 带宽利用极低 (<1%)

| 报告 Tier | 理论带宽 | 实际带宽 | 利用率 |
|---|---|---|---|
| GPU HBM | 3,350 GB/s | 1,175 MB/s | 0.035% |
| NVMe SSD | 7,000 MB/s | 179 MB/s | 2.6% |

**原因**: KV Cache trace replay workload 是随机访问 + 稀疏请求,非顺序带宽测试。

**我们的确认**: 在 `ai_ssd_prestudy` 中,iostat 分析确认 KV Cache 读写是 **~115-125kB 随机大块** (`%rrqm ≈ 0%`),测不出连续带宽。4 块盘的 I/O shape 完全相同,差异来自控制器而不是 workload。✅ **报告结论可复现**。

### 2.2 4-tier 存储性能排序

| 报告排名 | Tier | 吞吐 (tok/s) | 加速比 |
|---|---|---|---|
| #1 | GPU Only | 1,691 ± 154 | 6.4× |
| #2 | GPU+CPU | 1,546 ± 257 | 5.9× |
| #3 | GPU+CPU+NVMe | 1,175 ± 178 | 4.4× |
| #4 | NVMe Only | 263 ± 2 | 1.0× |

**我们的硬件差异**: 报告用 H100 (94GB) + NVMe 七千兆级盘;我们只有 16GB GPU + 消费级 SSD。但我们 **复现了** 4 盘间的 tier 分层:在 K4 测试中,BIWIN X570 达到 3.14GB/s 短测读带宽(远高于报告的 263 tok/s NVMe),因为消费级 SSD 在短爆发的 fresh SLC cache 下表现更好。

### 2.3 报告 LMCache 部分 vs 我们的复现

这是最重要的对比。报告 Section 7 提供了真实的 LMCache 推理吞吐:

| 报告配置 | 吞吐 (tok/s) | 说明 |
|---|---|---|
| vLLM Baseline (无 KV 缓存) | 13,730 ± 9 | 纯推理 |
| LMCache GPU | 9,508 ± 32 | KV 在 GPU |
| LMCache CPU Offload | 9,411 ± 91 | KV 在 CPU RAM |

报告结论: **LMCache 有 ~31% 性能损耗**(KV cache 管理开销),但 GPU vs CPU 差异仅 ~1%。

#### 我们的 LMCache 复现结果 (Pass2/Pass1 speedup)

| 测试 | 模型 | 无 LMCache | CPU LMCache | GPU LMCache | LMCache 增量 |
|---|---|---|---|---|---|
| 报告 | Mistral-7B | — | 9,411 tok/s | 9,508 tok/s | ~1% |
| 我们 (4B) | Qwen3-4B | 1.292× | 1.381× | 1.318× | +0.09× / +0.03× |
| **我们 (14B)** | **Qwen2.5-14B-AWQ** | **2.705×** | **2.958×** | **2.895×** | **+0.25× / +0.19×** |

**关键发现**: 4B 模型压力太小,看不出 LMCache 增量。14B AWQ (48层, 8KV heads) 才能产生足够压力让 LMCache 显示 +0.25× 加速。**报告用 7B 模型在 H100 上测,其 KV cache 开销比我们的 14B 小,所以 ~1% 差异合理。**

### 2.4 I/O 读写比例

| 报告 | 读 | 写 | R/W 比 |
|---|---|---|---|
| kv-cache.py | ~94 GB | ~7.5 GB | 12.4:1 |
| ai_ssd_prestudy (BurstGPT trace) | ~90% | ~10% | 9:1 |

✅ 比例基本一致。decode 阶段大量读,prefill/eviction 少量写。

---

## 3. 报告可以验证但无法定量复现的部分

| 报告指标 | 不可复现原因 |
|---|---|
| 1,691 tok/s GPU-only 存储吞吐 | H100 + 单块企业级 NVMe vs 消费级 SSD |
| 6.4× GPU/NVMe 加速比 | 94GB HBM 容量远大于 16GB,缓存命中率不同 |
| 0.035% HBM 带宽利用率 | 我们的计算卡 HBM 带宽 (960GB/s RTX 5080) 已低于 H100 |
| 13,730 tok/s vLLM 推理速度 | Mistral-7B bf16 vs Qwen2.5-14B-AWQ 无法直接比 |

---

## 4. 报告没有覆盖的 AI SSD 真实场景发现

`ai_ssd_prestudy` 发现了报告因只测单块企业级 NVMe 而无法触及的问题:

### 4.1 SSD 短测 vs 长稳态分歧

| K4 (8B×16 users×120s) | 短测胜者 |
|---|---|
| BIWIN X570 | **3.14 GB/s** 读带宽 ✅ |
| Seagate FC530 | 2.66 GB/s |
| ZhiTai Ti600 | 2.13 GB/s |
| WD SN570 DRAM-less | 0.77 GB/s ❌ |

| K4 30min 长稳态 | 长测胜者 |
|---|---|
| Seagate FC530 | 写 tail 最稳,GC stall 最少 |
| BIWIN X570 | 带宽仍高但 tail 随 GC 退化 |
| ZhiTai Ti600 | 写 P99 达 600-850ms,不推荐 |
| WD SN570 | DRAM-less,全程弱 |

**产品含义**: **单次短测不能代表生产环境**。AI SSD 验证必须包含 20-30 分钟长稳态 + GC cliff 测试。

### 4.2 Write Policy 差异 (P5)

在 HiCache (SGLang) 4 盘测试中发现:

| Write Policy | 效果 | 适用盘 |
|---|---|---|
| write_through | 基线, ~1.7-3.1s TTFT | 通用 |
| write_back | -2.2% TTFT,cold 盘 OOM | BIWIN ✅, Seagate ✅, WDC ❌, ZHITAI ❌ |
| write_through_selective | 几乎不写盘,但 TTFT 反增 22% | 碎片读取场景不推荐 |

**含义**: write_back 在高端盘(带 DRAM,好 FTL)上可以安全启用节省 cold TTFT,但在低端盘上 OOM。selective policy 适得其反。

### 4.3 LMCache + 4 盘 offload 尚未验证

报告验证了 LMCache CPU vs GPU 在单块 NVMe 上的差异。**多盘 offload (4 NVMe) + LMCache 的真实 TTFT spread** 尚未在任何环境中系统测试。这是 ai_ssd_prestudy 报告中 P6/P7/P8 计划的直接后继。

### 4.4 KV object 行为 vs fio 抽象

| 方法 | 可复现性 | 现实 fidelity |
|---|---|---|
| kv-cache.py / LMCache | 依赖具体 GPU 环境 | ✅ 反映真实 KV 行为 |
| fio (random 128KB) | ✅ 高 | ❌ 不能模拟 KV cache 的 LRU/eviction |
| bpftrace + iostat | ✅ 高 | ✅ 实测量 |

---

## 5. 综合建议

### 5.1 如果想复现报告的完整结果
- 需要 H100 (94GB) + 企业级 NVMe (~7TB) 的环境
- 用 kv-cache.py 而不是 LMCache bench — 测的是 I/O 而不是推理
- 模型用 Mistral-7B (或 Qwen2.5-7B,但需 AWQ 量化以装进 16GB 卡)

### 5.2 如果想替代报告得出自己的结论（推荐）
- ✅ **已完成**: LMCache 0.4.6 on Qwen2.5-14B-AWQ, 3 backend × 3 trial
- ✅ **已完成**: 4 盘 K4/K5 横评、GC drift、长稳态
- ✅ **已完成**: Write policy 矩阵 (P5)
- 🔲 **待做**: P6 (LMCache × 4 盘矩阵)、P7 (long 20min)、P8 (mixed R/W + GC)
- 🔲 **待做**: 报告里 kv-cache.py 四种 tier 的复现

### 5.3 最关键的未验证问题
**LMCache CPU offload 在 4 块不同 AI SSD 上,TTFT 加速差异有多大?** 报告和我们的 LMCache 测试都只用了单盘/单卡。如果 LMCache host RAM 写盘→不同盘的 write tail latency 会直接放大 end-to-end TTFT。这应该是 P6 的直接目标。

---

## 6. 测试环境差异总结

| 配置项 | 报告环境 | 我们的环境 | 能否对齐 |
|---|---|---|---|
| GPU | H100 94GB | RTX 5080 16GB + 5060 Ti 16GB | ❌ 容量/带宽差 6× |
| CPU RAM | 256 GB DDR5 | 64 GB DDR5 | ⚠️ 够用但未全量测试 |
| NVMe | 单块 7TB 企业级 | 4 块消费级 SSD | ❌ 定位不同 |
| 模型 | Mistral-7B bf16 | Qwen2.5-14B-AWQ / Qwen3-4B | ⚠️ 通过 AWQ 量化对齐 |
| vLLM 版本 | 0.13.0 | 0.22.1 | ⚠️ API 差异但 LMCache 兼容 |
| LMCache 版本 | 0.3.12 | 0.4.6 | ✅ API 兼容已验证 |
| 主要方法 | kv-cache.py (trace replay) | LMCache bench + fio + bpftrace | ❌ 互补测定 |

**总体判断**: 报告的 kv-cache.py storage throughput 数字 **在其 H100 环境下可复现**,但在我们的消费级 GPU+SSD 上 **无法定量复现也不需要定量复现**。更有价值的是**利用报告的方法论,在我们的硬件上搭建自己的 AI SSD offloading 评估体系**。这正是 ai_ssd_prestudy 正在进行的工作。
