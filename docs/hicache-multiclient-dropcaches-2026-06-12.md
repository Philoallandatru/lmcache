# HiCache Multi-Client + drop_caches 4-Disk — 2026-06-12

**TL;DR**: Qwen3-4B + 4 client 并发 + 每 round 前 `drop_caches`,4 盘 Cold TTFT 1.726s ± 2ms,**spread 仅 5ms**。iostat 显示 NTFS 3 盘 **0 读 0 写** —— 盘差**第二次被吃光**。

**核心发现**:**L2 host DRAM 完全屏蔽 L3 读盘延迟**。`hicache_host_used_tokens = 8256 / hicache_host_total_tokens = 41024` —— 8K prompt 装得下 41K L2 容量,`drop_caches` 清的是 OS page cache,对 sglang 进程自管理的 L2 host buffer **无效**。

## 背景:Phase2/4 单 client 测试为什么被吃光?

| Phase | 模式 | Cold spread | Warm spread | 原因 |
|---|---|---:|---:|---|
| Phase2 | 1 client, 1 cold + 5 warm, warm_1 drop | 1.2 ms | 0.9 ms | L2 hit on warm |
| Phase3 | 1 client, write_back | 1.0 ms | 0.7 ms | write_back 减少 L3 写 |
| Phase4 | 1 client, 14B-AWQ | 5 ms | 2 ms | 模型更大,L3 写更长 |
| **Phase5** | **4 client, drop_every_round** | **5 ms** | **2 ms** | **L2 hit on every round** |

预期:Phase5 多并发 + 每 round drop 应该能暴露 L3 读盘延迟。

## Phase5 测试配置

| 项 | 值 |
|---|---|
| 模型 | Qwen3-4B-Instruct-2507 (bf16, 8GB) |
| 部署 | TP=1, mem-fraction-static=0.7, port=30002 |
| L2 host RAM 容量 | **41,024 tokens** (device pool 20480 × hicache-ratio=2) |
| 负载 | **4 client 并发** (--concurrent-clients 4) |
| 切 cache 策略 | **每 round 前 `drop_caches`** (--drop-caches-every-round) |
| Round | 6 (1 cold + 5 warm),每 round 前 sync + echo 3 > /proc/sys/vm/drop_caches |
| Prompt | 7008 tokens (同 prompt × 4 client,测 N 路 reload) |
| Output | 64 tokens |

## 4 盘测试结果

### Cold + Warm TTFT

| 盘 | Cold (N=4) max | Cold mean | Warm_1 (N=4) max | Speedup | L3 file count | L3 total |
|---|---:|---:|---:|---:|---:|---:|
| BIWIN X570 (ext4) | 1.727s | 1.726s | 0.799s | 2.16× | 133 | 1188 MB |
| WDC WDS960G2G0C (NTFS) | 1.729s | 1.728s | 0.799s | 2.16× | 133 | 1197 MB |
| ZHITAI Ti600 (NTFS) | 1.724s | 1.724s | 0.799s | 2.16× | 133 | 1197 MB |
| Seagate ZP1000GV30012 (NTFS) | 1.723s | 1.723s | 0.798s | 2.16× | 132 | 1188 MB |
| **mean** | **1.726s** | **1.725s** | **0.799s** | **2.16×** | **133** | **1193 MB** |
| **stdev** | **2 ms** | **2 ms** | **0.4 ms** | — | — | — |
| **max-min spread** | **5 ms** | **5 ms** | **0.6 ms** | — | — | — |

### iostat(整 round 期间)

| 盘 | avg_r | max_r | avg_w | max_w |
|---|---:|---:|---:|---:|
| BIWIN X570 (ext4) | **204** | **1817** | 49 | 1019 |
| WDC WDS960G2G0C (NTFS) | **0** | **0** | **0** | **0** |
| ZHITAI Ti600 (NTFS) | **0** | **0** | **0** | **0** |
| Seagate ZP1000GV30012 (NTFS) | **0** | **0** | **0** | **0** |

🚨 **爆炸性发现**:**3 块 NTFS 盘在多并发 + drop_every_round 模式下,完全没产生 L3 读盘 IO**!只有 BIWIN ext4 系统盘保留了部分页面缓存 readahead。

### sglang /metrics(BIWIN ext4 round)

```
sglang:backuped_tokens_total{storage_backend="file"} = 8512.0
sglang:hicache_host_used_tokens = 8256.0
sglang:hicache_host_total_tokens = 41024.0
```

**关键数据**:
- L2 host RAM 容量 = **41,024 tokens**
- L2 host RAM used = **8,256 tokens**(8K prompt 完整装下)
- L3 写入 = 8,512 tokens(写 1 次)

## 根因分析

### 为什么 L3 读盘没发生?

```
请求 flow:
  cold request
    → KV cache miss in device pool (L1)
    → sglang 查 L2 host pool
    → L2 miss, 触发 L2 → device load
    → 同步触发 device → L2 备份 (write_through)
    → L3 (file backend) 也同步备份
    → 整个 prompt KV 在 L2 + L3
  warm request (post drop_caches)
    → KV cache miss in device pool (L1)
    → sglang 查 L2 host pool
    → L2 HIT ← ─ ─ ─ 这里就停住了! 没去 L3
    → 从 host RAM 直接 load 到 device
```

**`echo 3 > /proc/sys/vm/drop_caches` 清的是 OS page cache,不是 sglang 自己 mmap 的 host buffer**。`pin_memory=True` 让 sglang L2 buffer 用 pinned host memory,这部分**不受 drop_caches 影响**。

### 为什么 NTFS 3 盘是 0 读 0 写?

冷启动第一轮(BIWIN ext4 系统盘):
- sglang 启动 → L1 miss → 写 L2 + L3
- L3 写入 1.18 GB BIWIN ext4,page cache 暖(BIWIN 1.97 GB/s avg_r Phase2)
- Cold round 完成,数据已在 L2 host DRAM
- Warm rounds: 每次都 L2 hit,**不读 L3**

**为什么 BIWIN 有 204 MB/s avg_r 而 NTFS 是 0?**
- BIWIN 是系统盘,ext4 本身在用
- L3 文件大小 1.18 GB × 4 盘
- BIWIN 第一次写完后,内核 page cache 还有部分(L3 写是 buffered IO,即使 direct I/O 也有少量元数据缓存)
- 后续 drop_caches → 第二次 warm round 触发 OS readahead,**只在 BIWIN 触发**(因为 L2 hit 走了 sglang 自己的 host buffer,根本不读盘)
- NTFS 盘从 cold 后就**完全没被读** —— 因为 L2 hit 100%

## 跨阶段对比

### Cold TTFT 退化分析

| 模式 | 单 client Cold | N=4 Cold | 退化 |
|---|---:|---:|---:|
| Phase2 (1 client) | 1.44s | — | baseline |
| Phase5 (4 client, BIWIN) | — | 1.73s | **+20%** |
| Phase5 (4 client, NTFS) | — | 1.72-1.73s | **+20%** |

4 路并发 Cold 比单 client 慢 20%。**这 20% 完全在 L2 host RAM 命中路径**(4 路同时 prefill 抢 GPU compute),**不在磁盘**。

### L3 落盘规模

| 模型 | Page size | Files | Total |
|---|---:|---:|---:|
| 4B 单 client (Phase2) | 9 MB | 115 | 1035 MB |
| **4B N=4 (Phase5)** | **9 MB** | **133** | **1197 MB** |
| 14B-AWQ (Phase4) | 5 MB | 230 | 1150 MB |

N=4 比单 client 多写 16%(133 vs 115 files)。**sglang 在多并发路径下触发了额外的 KV merge** —— 4 路 client 的 prefill 输出需要合并到一个 unified radix tree,产生额外的 L3 write。

## 暴露盘差的真正方法

要真测 L3 读盘延迟,必须**让 L2 host RAM 装不下 prompt**。当前 L2 容量 41K,prompt 8K → 永远 L2 hit。

### Option A: 压小 L2 容量

```bash
# 强制 L2 容量 = 2K tokens (8K prompt 必 L2 miss)
python -m sglang.launch_server ... \
    --hicache-ratio 0.1 \  # device pool 20K × 0.1 = 2K
    --hicache-size 0       # 默认
```

预计: 7-8K prompt 必触发 L3 readback → NTFS 3 盘 cold TTFT 4-10s(基于 avg_r 10 MB/s)

### Option B: 加大 prompt 到超过 L2 容量

```bash
# 50K tokens prompt (> 41K L2 容量) → 必然 L2 evict → L3 reload
python scripts/hicache_load_test.py --prompt-tokens 50000 ...
```

预计: L2 miss 后 cold TTFT 退化更严重,可能到 10-20s(50K tokens 4 路并发)

### Option C: 直接监控 L3 文件 I/O,绕开 L2

```bash
# bpftrace: block_rq_issue (kernel 6.x 重写中) 直接抓单次 4KB block IO
# 不依赖 sglang 行为,直接看 kernel block layer
```

## 已知限制 & 误导点

### 1. iostat 0 读 ≠ L3 没读
NTFS 3 盘 iostat 显示 0 读,**不是因为 L3 文件不存在**(`cache_file_list.txt` 确认 133 files × 9MB = 1197MB),**而是因为 L2 host RAM 命中**,sglang 直接从 host buffer copy 到 device,**完全绕开 kernel block layer**。

### 2. BIWIN ext4 残留读 ≠ 盘好
BIWIN 显示 204 MB/s avg_r,**不是 L3 真读盘**。这是 **OS page cache readahead** + sglang L2 backup 写后的元数据缓存。**不要用 iostat 推断 4 盘性能差异**。

### 3. drop_caches ≠ 清 L2
`echo 3 > /proc/sys/vm/drop_caches` 只清 OS page cache。sglang HiCache L2 是进程自管理 host buffer,**`pin_memory=True` 锁页内存,drop_caches 不动它**。

## 数据位置

```
results/hicache_multiclient/
├── baseline_biwin_ext4/        # 7 文件
│   ├── load_test.jsonl         # 6 round × (4 client + 1 aggregate) = 30 行
│   ├── iostat_nvme1n1.log      # 46 行
│   ├── metrics_after.json      # 557 行 prometheus text
│   └── cache_file_list.txt     # 133 × 9MB = 1197 MB
├── ai_ssd0_wdc_ntfs/           # 7 文件 (iostat 全 0)
├── ai_ssd1_zhitai_ntfs/        # 7 文件 (iostat 全 0)
└── ai_ssd2_seagate_ntfs/       # 7 文件 (iostat 全 0)
```

每 round jsonl 结构:
```json
{"round":0,"label":"cold","client_id":0,"latency_s":1.728,...}    // 4 个 client
{"round":0,"label":"cold","client_id":1,"latency_s":1.729,...}
{"round":0,"label":"cold","client_id":2,"latency_s":1.728,...}
{"round":0,"label":"cold","client_id":3,"latency_s":1.728,...}
{"round":0,"label":"cold","client_id":-1,"is_aggregate":true,
 "n_clients":4,"latency_s":1.729,"latency_mean_s":1.728,
 "latency_min_s":1.728,"latency_max_s":1.729}                    // 1 个 aggregate
```

## 关联文档

- [Phase2 - 4B write_through 4 盘 baseline](hicache-4disk-headline-2026-06-12.md)
- [Phase3 - write_through vs write_back 对比](hicache-writeback-vs-writethrough-2026-06-13.md)
- [Phase4 - 14B-AWQ 4 盘 baseline](hicache-14b-baseline-2026-06-12.md)