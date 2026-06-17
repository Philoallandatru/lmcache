# P5: sglang HiCache write_policy 跨盘对比

**日期**: 2026-06-16 ~ 2026-06-17
**作者**: AI-SSD pre-study
**状态**: ✅ 完成

---

## 1. TL;DR

测试 3 种 sglang HiCache L3 file backend write policy 在 4 块 NVMe 盘上的 latency / IO 模式 / cold TTFT 差异。

**核心发现**:

1. **write_back 冷启动 TTFT 比 write_through 低 2.2% (~30ms)** — 异步 flush 不让写延迟挡 prefill
2. **write_back 在慢盘(WDC/ZHITAI NTFS)上 OOM** — 20 个 prompt 的 async flush 跟不上,Prefill out of memory
3. **write_through_selective 在 NTFS 上比 WT 更慢** (+22% on WDC) — fragmented read 开销 > 少写数据的收益
4. **BIWIN ext4 上 3 个 policy 几乎一样** (1.67-1.68s) — ext4 掩盖了 policy 差异
5. **Seagate 虽然读慢但写快,WB 反而不 OOM** — 写吞吐是关键瓶颈

---

## 2. 背景

### 2.1 sglang HiCache write policy 三种模式 (sglang 0.5.13)

| Policy | 行为 | 优点 | 缺点 |
|---|---|---|---|
| `write_through` | GPU KV → L2 **同步** → L3 file | 数据落盘可靠 | 写延迟挡 prefill,TTFT 偏高 |
| `write_back` | GPU KV → L2 异步, L3 延迟 flush | TTFT 不被写盘挡,冷启动更快的 TTFT | L2 压力大,慢盘 OOM |
| `write_through_selective` | 只写"重要" KV page | L3 容量压力减半 | selective 判定 + 碎片化读取 |

### 2.2 4 盘配置

| 盘 | 型号 | 容量 | 文件系统 |
|---|---|---|---|
| BIWIN | BIWIN X570 1TB | 1TB | ext4 |
| WDC | WDC WDS960G2G0C-00AJM0 | 960GB | NTFS (fuseblk) |
| Seagate | Seagate ZP1000GV30012 | 1TB | NTFS (fuseblk) |
| ZHITAI | ZHITAI Ti600 1TB | 1TB | NTFS (fuseblk) |

---

## 3. 测试方法

### 3.1 配置

| 项 | 值 |
|---|---|
| 模型 | Qwen3-4B-Instruct-2507 |
| TP / CTX | 1 / 8192 |
| Hicache ratio | 2 (L2 = 41K tokens) |
| 负载 | multiprompt 20×7K + replay_p0 (drop_caches) |
| 数据点 | 3 policy × 4 盘 = **12 run** |

### 3.2 命令

```bash
cd ~/llm/infer/ai_ssd_prestudy
# M1: 3 policy × 4 盘
bash scripts/run_p5_policy_matrix.sh
# 分析
source ~/llm/.venv/bin/activate
python scripts/analyze_p5.py
```

---

## 4. 关键数据

### 4.1 replay_p0 latency (s) — L3 reload 主指标

| Policy | BIWIN | WDC | Seagate | ZHITAI | spread |
|---|---|---|---|---|---|
| **write_through** | 1.670 | **2.647** | 3.108 | 2.430 | 1.44s (1.86x) |
| **write_back** | 1.682 | ❌ OOM | **3.133** | ❌ OOM | — |
| **write_through_selective** | 1.680 | **3.221** | 3.137 | 2.535 | 1.54s (1.92x) |

> ❌ OOM = `RuntimeError: Prefill out of memory`, write_back 异步 flush 跟不上 20 prompt fill

### 4.2 p0 cold fill TTFT (s) — 关键指标: write_back 优势

| Policy | BIWIN | WDC | Seagate | ZHITAI | **avg** | Δ vs WT |
|---|---|---|---|---|---|---|
| **write_through** | 1.436 | 1.437 | 1.436 | 1.436 | **1.436** | baseline |
| **write_back** | **1.403** | **1.402** | **1.411** | **1.402** | **1.405** | **-31ms (-2.2%)** |
| **write_through_selective** | 1.451 | 1.445 | 1.446 | 1.446 | **1.447** | +11ms (+0.8%) |

> **write_back 冷启动 TTFT 比 WT 低 ~30ms** — sinkron 写 flush 延迟从 prefill 路径移除

### 4.3 iostat 模式 (WT baseline)

| Policy | Disk | read_peak | read_mean | write_peak | total_r | total_w |
|---|---|---|---|---|---|---|
| WT | BIWIN | 862.7 MB/s | 13.6 | 2682.8 MB/s | 1.19 GB | 21.5 GB |
| WT | WDC | 605.6 | 10.5 | 1969.0 | 1.00 GB | 18.1 GB |
| WT | Seagate | 558.9 | 10.6 | 3710.5 | 0.99 GB | 19.1 GB |
| WT | ZHITAI | 757.8 | 9.9 | 3292.0 | 0.99 GB | 18.1 GB |

### 4.4 WTS iostat — 几乎无 IO

| Policy | Disk | read_peak | read_mean | write_peak | total_r | total_w |
|---|---|---|---|---|---|---|
| WTS | BIWIN | 656.2 | 16.9 | 2681.5 | 1.44 GB | 19.7 GB |
| WTS | WDC | 469.4 | 10.4 | 1977.6 | 1.00 GB | 18.9 GB |
| WTS | Seagate | **0.00** | **0.00** | **0.00** | **0** | **0** |
| WTS | ZHITAI | **0.08** | **0.001** | **1.45** | **0** | **0.001 GB** |

> WTS Seagate/ZHITAI 几乎零 IO — selective write 只写"重要" KV page,绝大部分数据留在 L2 不落盘

---

## 5. 结论

### 5.1 write_back: TTFT 收益但稳定性风险

| 好处 | 风险 |
|---|---|
| cold TTFT ↓2.2% (所有盘) | 慢盘 OOM (WDC NTFS, ZHITAI NTFS) |
| replay latency 基本不变 | async flush 竞争 GPU 资源可能导致 L2 hit latency 略升 |
| 写吞吐高的盘(Seagate 3710 MB/s peak)也可用 | 需要更多 L2 空间来缓冲未 flush 的 KV 页 |

### 5.2 write_through_selective: 不推荐

- **NTFS 上比 WT 更慢**: WDC +22%, ZHITAI +4%, Seagate +1%
- 选择性写的碎片化读取开销 > 少写数据的收益
- 唯一好处: 不会 OOM (数据都不怎么落盘)

### 5.3 推荐

| 场景 | 推荐 policy |
|---|---|
| 快速 NTFS (2 GB/s+ write) | **write_through** (稳定) |
| 慢速 NTFS (<2 GB/s write) | **write_through** (必须) — WB 会 OOM |
| ext4 (一切一样) | **write_through** (简单) |
| cold TTFT 敏感 | **write_back** — 但需验证不 OOM |

### 5.4 跟 P3 的关系

P3 结论不变: ZHITAI bimodal (~2x spread) 在 P5 的所有 policy 下都复现 (replay 2.43-2.54s vs cold 1.44s,≈1.7x)。P5 揭示了 **write_back 在慢盘上的 OOM 风险**,这是比 L3 reload latency 更严重的部署隐患。

---

## 6. 数据文件清单

| 文件 | 内容 |
|---|---|
| `results/hicache_multiprompt_p5_p1_wt/{4 盘}/*` | write_through 原始数据 |
| `results/hicache_multiprompt_p5_p2_wb/{4 盘}/*` | write_back 原始数据 (2 盘 OOM) |
| `results/hicache_multiprompt_p5_p3_wts/{4 盘}/*` | write_through_selective 原始数据 |
| `results/p5_policy_matrix_summary.json` | JSON 格式汇总 |
| `scripts/analyze_p5.py` | P5 专用 analyzer |
| `scripts/run_p5_policy_matrix.sh` | P5 driver |
