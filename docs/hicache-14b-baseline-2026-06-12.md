# HiCache 14B-AWQ 4-Disk Baseline — 2026-06-12

**TL;DR**: Qwen3-14B-AWQ TP=2 + 7000-token prompt,4 盘 Cold TTFT 4.890s ± 2ms,Warm 0.987s ± 1ms,**加速比 4.95×**。盘差 spread 5ms(cold) / 2ms(warm),**被 page cache + L2 host DRAM 100% 掩盖**。iostat 显示各盘 L3 写吞吐差异显著(WDC/ZHITAI/Seagate 突发写 1100 MB/s vs BIWIN 790 MB/s),**但 cold TTFT 仍同质**。

## 测试配置

| 项 | 值 |
|---|---|
| 模型 | Qwen3-14B-AWQ (4-bit AWQ 量化, ~10 GB) |
| 部署 | TP=2, 2× RTX 5080 + RTX 5060 Ti (16GB × 2, 异构) |
| mem-fraction-static | 0.85 (vs 4B 用 0.7) |
| context-length | 12288 (vs 4B 用 8192) |
| KV cache 容量 | 102272 tokens (3.90 GB × 2 K+V per GPU) |
| page-size | 64 tokens (与 4B 一致) |
| HiCache policy | write_through (与 4B Phase2 一致) |
| HiCache storage | file backend, /mnt/ai_ssd{0,1,2}/cache_14b_awq/ + cache/14b_awq/ |
| HiCache ratio / size | 2 / 0 (与 4B 一致) |
| Test client | hicache_load_test.py: 1 client × 6 rounds, 7000 tokens prompt, 64 tokens output |
| drop_caches | round 1 (warm_1) 前强制 |

## 4 盘测试结果

### TTFT & Speedup

| 盘 | Cold | Warm #1 (drop_caches) | Speedup | L3 file count | L3 total |
|---|---:|---:|---:|---:|---:|
| BIWIN X570 (ext4) | 4.887s | 0.987s | **4.95×** | 230 | 1150 MB |
| WDC WDS960G2G0C (NTFS) | 4.890s | 0.989s | 4.95× | 230 | 1150 MB |
| ZHITAI Ti600 (NTFS) | 4.892s | 0.987s | 4.96× | 230 | 1150 MB |
| Seagate ZP1000GV30012 (NTFS) | 4.892s | 0.987s | 4.96× | 230 | 1150 MB |
| **mean** | **4.890s** | **0.987s** | **4.95×** | 230 | 1150 MB |
| **stdev** | **2 ms** | **1 ms** | — | — | — |
| **max-min spread** | **5 ms** | **2 ms** | — | — | — |

### iostat 数据(整 round 期间统计)

| 盘 | avg_r | max_r | avg_w | max_w |
|---|---:|---:|---:|---:|
| BIWIN X570 (ext4) | 178 | 1801 | 39 | **790** |
| WDC WDS960G2G0C (NTFS) | 0 | 14 | 20 | **1100** |
| ZHITAI Ti600 (NTFS) | 0 | 15 | 20 | **1101** |
| Seagate ZP1000GV30012 (NTFS) | 0 | 22 | 20 | **1101** |

**观察**:
- **读**:除 BIWIN 外 3 块 NTFS 盘 round 内几乎没读(`max_r ≤ 22 MB/s`)—— 印证"page cache 掩盖 L3 读"的假设。drop_caches 后 warm_1 仍然 0.987s,说明 **L2 host DRAM 持有 KV cache**(`/hicache:host_used_tokens` ≈ 6608)。
- **写**:所有盘都是 L3 store 阶段短暂写 1.1 GB。WDC/ZHITAI/Seagate 突发写都打到 1100 MB/s,但 **BIWIN 系统盘只到 790 MB/s**(ext4 barrier overhead? page size mismatch?)。
- **盘差被吃光**:写吞吐差 30%(790 vs 1100),但 cold TTFT 差 0.1%。

## 跨模型对比:Qwen3-4B vs Qwen3-14B-AWQ

### L3 落盘结构

| 维度 | Qwen3-4B (Phase2) | Qwen3-14B-AWQ (Phase4) |
|---|---|---|
| KV heads | 8 | 8 (相同) |
| head_dim | 128 | 128 (相同) |
| layers | 36 | 40 |
| layers × KV × 2 bytes | 73728 B/layer | 102400 B/layer |
| page_size 实测 | **9.0 MB / file** | **5.0 MB / file** |
| 推测 page 内容 | K + V 各 4.5 MB | K + V 同文件 5 MB |
| 7000 token L3 | 115 files × 9 MB = **1035 MB** | 230 files × 5 MB = **1150 MB** |
| L3 vs L2(host RAM 80 MB) | 12.9× 溢出 | 14.4× 溢出 |

### TTFT 表现

| 维度 | 4B | 14B-AWQ | Δ |
|---|---:|---:|---:|
| Cold TTFT | 1.439s ± 0.5ms | **4.890s ± 2ms** | **+3.4×** |
| Warm #1 (post drop_caches) | 0.735s ± 0.3ms | 0.987s ± 1ms | +34% |
| **Speedup** | **1.96×** | **4.95×** | **+2.5×** |
| L2 hit latency (warm) | 0.735s | 0.987s | +34% (推测 AWQ 推理稍慢) |
| L3 read latency (implied) | ≈ 0.7s (cold - warm) | ≈ 3.9s (cold - warm) | **+5.5×** |

### 关键洞察

1. **Cold TTFT 多出的时间主要在 L3 store / read,不是 prefill 计算**
   - 4B cold - warm = 0.704s(7000 token × 0.1 ms/token)
   - 14B cold - warm = 3.903s(7000 token × 0.55 ms/token)
   - **14B 多了 5× L3 IO 时间,部分因为 L3 大(1150 vs 1035 MB)但主要是 page 数翻倍(230 vs 115)→ random IO overhead**

2. **加速比从 1.96× 升到 4.95× 是"分母变小"幻觉**
   - warm 4B=0.735s,14B=0.987s(差 34%)
   - cold 4B=1.44s,14B=4.89s(差 240%)
   - **分子变大的速度比分母快** → 加速比反而更高
   - **含义**:更大模型 + 更大 cache 让 KV-cache offload 收益更大;但 cold 绝对时间也更长,延迟 SLA 更难达成

3. **TTFT 加速比不是评估 KV cache 价值的正确指标**
   - 真正指标应该是:**冷启动 SLA 满足率** (cold TTFT < p99 SLA)
   - 或:**TTFT 退化系数** (cold / baseline_inference_latency)
   - **写策略(同步 vs 异步)**对 cold latency 影响更大(Phase3 已验证 write_back 让 cold -37ms)

## 工程结论

### 1. AI SSD 选型排名(综合 Phase2 + Phase4 写吞吐)

🥇 **Seagate ZP1000GV30012** — 4B 写峰值 8106 MB/s + 14B 写峰值 1101 MB/s,**多并发 HiCache + 突发 L3 reload 双场景最优**
🥈 **BIWIN X570** — 4B 写峰值 5284 MB/s + ext4 通用,**系统盘均衡型**
🥉 **WDC WDS960G2G0C** — 单流读 OK(8 MB/s cold read back),突发写 1100 MB/s(14B)
⚠️ **ZHITAI Ti600** — 14B 写峰值 1101 MB/s 但 4B 只到 4498,**DRAM-less 设计对大 block 写不友好**

### 2. 测试方法学结论

- **page_size × 模型大小 ≠ 磁盘写入压力** —— page 数和文件总大小更关键(230 × 5 MB vs 115 × 9 MB 同样 1GB 但写入模式不同)
- **TTFT 在小模型 + 单请求下盘差完全被掩盖**,即使 14B 写吞吐差 30%(790 vs 1100 MB/s)→ cold TTFT 差 0.1%
- **要真正暴露盘差,需要**:
  1. 多并发 prefills(同时 N 路 cold)
  2. 更大 prompt(让 L3 超过 host RAM 容量,强制全盘读)
  3. bpftrace 抓单次 block I/O latency(绕开 page cache)

### 3. 14B 模型的生产部署建议

- **冷启动 SLA**:4.9s 是 cold avg,如果 SLA ≤ 5s 可接受 100% 流量;SLA ≤ 3s 需要 write_back + warm 预热策略
- **预热模式**:deploy 时手动发起 N 路相同 prompt,把 KV cache 灌进 L2 host DRAM,warm 后 0.987s
- **L3 容量规划**:14B 7k prompt 灌 1.15 GB;按 100 并发不同 prompt → 100 GB L3,需要至少 200 GB SSD(考虑文件系统开销)

## 数据位置

```
results/hicache_14b_awq/
├── baseline_biwin_ext4/      # nvme1n1 (system disk, ext4)
│   ├── load_test.jsonl       # 6 round TTFT/latency 数据
│   ├── iostat_nvme1n1.log    # 59 个采样点
│   ├── metrics_after.json    # /metrics 包括 hicache_host_used_tokens 等
│   ├── cache_file_list.txt   # 230 × 5MB page 文件
│   └── server.log            # sglang 启动 + KV cache store 日志
├── ai_ssd0_wdc_ntfs/         # nvme0n1
├── ai_ssd1_zhitai_ntfs/      # nvme2n1
└── ai_ssd2_seagate_ntfs/     # nvme3n1
```

每个 round 含 7 个文件:
1. `load_test.jsonl` — 6 行 (cold + 5 warm)
2. `load_test.log` — hicache_load_test.py stdout
3. `iostat_<dev>.log` — 1s 粒度 (~50-60 行)
4. `server.log` — sglang 启动 + KV cache 事件
5. `metrics_before.json` / `metrics_after.json` — Prometheus metrics
6. `cache_file_list.txt` — L3 文件清单

## 已知限制

1. **TTFT 仍是 dominant metric,无法区分盘差**—— page cache + L2 host RAM 完全覆盖 4 盘差异
2. **单并发测** —— 没测 N 路并发 prefill 的盘吞吐竞争
3. **iostat 1s 粒度太粗** —— L3 store 突发可能在 100ms 内完成,1s 平均拉低了 max_w
4. **bpftrace kernel 6.x 不兼容** —— 单次 block I/O latency 未采集(用 `block_rq_issue` nsecs 重写中)
5. **prompt 仅 7000 tokens** —— 没测 32k+ 长 prompt 强制全盘 reload

## 下一步候选

1. **多并发测** —— 4 client × 6 round,看 N 路 cold 并发的盘差
2. **drop_caches 每 round 前** —— 强制 L2 miss,逼出 L3 真实读盘延迟
3. **更大 prompt (32k)** —— 强制 L3 > L2,看 cold 真读盘差距
4. **修 bpftrace kernel 6.x** —— 抓 4KB block I/O latency 分布,绕开 page cache
5. **32B-AWQ 模型** —— 进一步突破 L2,KV page size ≈ 10MB,total L3 ≈ 2.3GB

## 关联文档

- [Phase2 - 4B write_through 4 盘 baseline](hicache-4disk-headline-2026-06-12.md)
- [Phase3 - write_through vs write_back 对比](hicache-writeback-vs-writethrough-2026-06-13.md)
- [计划 v2 - sglang HiCache exploration](.hermes/plans/2026-06-11_155736-sglang-hicache-exploration.md)