# P5: sglang HiCache write_policy 跨盘对比

**日期**: 2026-06-16
**作者**: AI-SSD pre-study
**状态**: ✅ 数据采集中
**Commit**: (pending)

---

## 1. TL;DR

测试 3 种 sglang HiCache L3 file backend write policy 在 4 块 NVMe 盘上的 latency / IO 模式 / sglang 内部指标差异。

**主要发现** (数据完整后填):

- **write_through** = baseline (cold TTFT 跟 v3 一致,replay latency 由盘 IO 决定)
- **write_back** = 写延迟从 prefill 关键路径移走,理论 TTFT 略降,replay 略升
- **write_through_selective** = 只写重要 KV page,理论上 L3 容量减半,write IO 减半
- 4 盘 ranking 跟 P3 一致: BIWIN > WDC > Seagate/ZHITAI

---

## 2. 背景

### 2.1 sglang HiCache write policy 三种模式 (sglang 0.5.13)

来源: `sglang/srt/mem_cache/hiradix_cache.py:364`

```python
allowed = ["write_back", "write_through", "write_through_selective"]
```

| Policy | 行为 | 优点 | 缺点 |
|---|---|---|---|
| `write_through` | GPU KV → L2 (host RAM) **立即** → L3 (file) 同步链 | 数据落盘可靠性高,replay 命中率高 | 写延迟挡 prefill,TTFT 偏高 |
| `write_back` | GPU KV → L2 立即, L3 异步延迟 | TTFT 不被写盘延迟挡 | replay 时若 L3 还没写完会 miss |
| `write_through_selective` | GPU KV → L2 立即, **只写"重要"的 KV page** (write_through_threshold 控制) | L3 写 IO 减半,容量压力低 | "重要"判定依赖 radix 树拓扑,可能丢失不频繁访问的 prefix |

### 2.2 write_through_threshold

- `write_through`: threshold = 1 (一进 L2 立即刷)
- 其他: threshold = 2 (攒一批再刷)

### 2.3 sglang 0.5.13 已知 bug

- `--hicache-io-backend` 参数**不被 L3 file backend 消费** (`HiCacheFile.get/set` 直接调 Python `open().readinto()`,line 367-371)。`direct` vs `kernel` 对 L3 **没区别**。io_backend 只对 L2 (host RAM ↔ GPU) 起作用。
- 所以 P5 **不测 io_backend**,只测 3 个 policy。

---

## 3. 测试方法

### 3.1 测试配置

| 项 | 值 |
|---|---|
| 模型 | Qwen3-4B-Instruct-2507 |
| TP / CTX | 1 / 8192 |
| Hicache ratio | 2 (L2 = 41K tokens, prompt 7K 装得下) |
| 负载模式 | multiprompt 20×7K + replay_p0 |
| 4 盘 | BIWIN X570 1TB ext4 / WDC 960G NTFS / Seagate 1TB NTFS / ZHITAI Ti600 1TB NTFS |
| 3 policy | write_through / write_back / write_through_selective |
| 总数据点 | 3 × 4 = **12 run** |

### 3.2 Driver

`scripts/run_p5_policy_matrix.sh` 串行跑 3 policy, 每个 policy 串行跑 4 盘。
每个 run 用 `scripts/hicache_drive_4_rounds_model.sh qwen3_4b_multiprompt_policy` (POLICY_ID=1/2/3)。

数据落点:
- `results/hicache_multiprompt_p5_p1_wt/{baseline_biwin_ext4,ai_ssd0_wdc_ntfs,...}/`
- `results/hicache_multiprompt_p5_p2_wb/{...}/`
- `results/hicache_multiprompt_p5_p3_wts/{...}/`

### 3.3 时间估算

- 4 盘 × 3 policy × ~3-4 min/run ≈ 50-60 min

---

## 4. 关键数据 (待填)

### 4.1 replay_p0 latency 跨 policy × 盘 (s)

```
Disk      |        WT         |        WB         |       WTS         |
----------+-------------------+-------------------+-------------------+
[fill after P5 done]
```

### 4.2 iostat 模式对比

```
Policy   Disk      read_peak  read_mean  write_peak  r_await_p99  aqu_peak  util_peak
[fill after P5 done]
```

### 4.3 sglang 内部 metrics

```
Policy   Disk      prompt_tok   gen_tok    ttft_sum   e2e_sum   cache_hit
[fill after P5 done]
```

### 4.4 L3 写盘总量 (write_back vs write_through)

| Policy | total_write | total_read |
|---|---|---|
| write_through | (fill) | (fill) |
| write_back | (fill) | (fill) |
| write_through_selective | (fill) | (fill) |

---

## 5. 结论 (待填)

### 5.1 各 policy trade-off (fill after data)

### 5.2 推荐 (fill after data)

### 5.3 跟 P3 / Phase7 G 的关系 (fill after data)

---

## 6. 复现命令

```bash
cd ~/llm/infer/ai_ssd_prestudy

# 跑 12 run (3 policy × 4 盘)
bash scripts/run_p5_policy_matrix.sh

# 分析
source ~/llm/.venv/bin/activate
python scripts/analyze_p5.py
```

## 7. 数据文件清单 (待填)

| 文件 | 大小 | 内容 |
|---|---|---|
| `results/p5_policy_matrix_summary.json` | - | 12 数据点完整汇总 |
| `results/p5_replay_latency.csv` | - | 跨 policy × 盘 replay latency 表格 |
| `results/hicache_multiprompt_p5_p{1,2,3}_{wt,wb,wts}/*/load_test.jsonl` | - | 12 个原始 latency log |
| `results/hicache_multiprompt_p5_p{1,2,3}_{wt,wb,wts}/*/iostat_*.log` | - | 12 个 iostat log |
| `results/hicache_multiprompt_p5_p{1,2,3}_{wt,wb,wts}/*/metrics_after.json` | - | 12 个 sglang metrics |
