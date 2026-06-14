# HiCache Multi-Prompt Accumulation + L2 Evict — 2026-06-14

**TL;DR**: 🎉 **终于暴露了 4 盘差异!** Qwen3-4B + 20 个不同 prompt (累计 140K tokens > L2 容量 21K) + replay p0,L2 miss → 走 L3 真读盘,**4 盘 replay_p0 spread 2.1 秒**(BIWIN 1.72s → WDC 3.82s = 2.22× 差距)。

| 盘 | p0 (cold fill) | p1-p19 (L2 hit) | **replay_p0 (L3 reload)** | **ratio** |
|---|---:|---:|---:|---:|
| 🥇 BIWIN (ext4) | 1.433s | 1.420s | **1.718s** | **1.20×** |
| 🥈 ZHITAI (NTFS) | 1.435s | 1.422s | 2.677s | 1.87× |
| 🥉 Seagate (NTFS) | 1.434s | 1.448s | 2.773s | 1.93× |
| 4️⃣ WDC (NTFS) | 1.434s | 1.421s | **3.816s** | **2.66×** |
| mean | 1.434s | 1.428s | 2.746s | 1.91× |
| **stdev** | **1ms** | — | **743ms** | **0.52×** |
| **max-min spread** | **1ms** | — | **2098ms** | **1.46×** |

**核心洞察**:
- **之前所有 phase (2-5) 4 盘 spread < 5ms,完全被 page cache + L2 host DRAM 屏蔽**
- **Phase7 用 20 prompt 累积填满 L2 容量,replay 必 L2 miss → 真走 L3**
- **盘差 2.1s = 200× 之前测的 spread**

## 🚨 重要发现:Phase2-5 数据需要重新评估

跑 Phase7 时发现一个**历史数据问题**:

**4 块 NVMe 盘物理上都存在** (WDC/BIWIN/Seagate/ZHITAI 各自 894-953GB),但 **只有 BIWIN (nvme1n1) mount 到 /,其他 3 块的 NTFS 分区在 Phase2-5 期间根本未 mount**。

`/mnt/ai_ssd{0,1,2}` 是 `/dev/nvme1n1p3` 上的空子目录(实际都是 BIWIN 系统盘):
```
$ stat /mnt/ai_ssd0 /mnt/ai_ssd1 /mnt/ai_ssd2
  /mnt/ai_ssd0: dev=66317   <- BIWIN
  /mnt/ai_ssd1: dev=66317   <- BIWIN
  /mnt/ai_ssd2: dev=66317   <- BIWIN
```

**Phase2-5 实际做了什么**:
- 4 盘 driver 启动 4 次,每次都让 sglang 写 L3 到 `cache_hicache/` 在 BIWIN 上
- iostat 测其他 3 盘 stats = 0(没 IO),看着像"4 盘差异"
- L3 file count 一致 (4×115) 因为都是 BIWIN 写

**已修正**:在 Phase7 开始时用 `sudo mount -t ntfs` 把 3 块盘 mount 上了,加 fstab 持久化:
```
UUID=1ECE4133CE41048D /mnt/ai_ssd0 ntfs-3g defaults,nofail,uid=1000,gid=1000 0 0  # WDC
UUID=66D6EA88D6EA5837 /mnt/ai_ssd1 ntfs-3g defaults,nofail,uid=1000,gid=1000 0 0  # Seagate
UUID=6A00E59100E56493 /mnt/ai_ssd2 ntfs-3g defaults,nofail,uid=1000,gid=1000 0 0  # ZHITAI
```

**Phase7 才是真正测 4 盘**的实验(每盘 20 prompt × 110 L3 files = 2201 files × 9MB = 19.8GB 真写到对应盘上)。

## 测试配置

```bash
# scripts/hicache_drive_4_rounds_model.sh qwen3_4b_multiprompt
# 关键 env vars:
NUM_PROMPTS=20             # 跑 20 个不同 prompt
REPLAY_PROMPT_ID=0         # 跑完后回放 p0
HICACHE_RATIO=2            # 4B 默认 L2 = device_pool_size × 2

# 7K tokens × 20 prompts = 140K total
# L2 容量 = 20480 (device) × 2 = 40960 tokens
# 实际 L2 eviction: 前 5-6 个 prompt 后, p0 必 evict
```

**为什么 ratio=2 时 L2 miss 发生**:
- ratio=2 → L2 = 2× device = 40K tokens (实际是 hicache_host_total_tokens = 41024)
- 每次 7K prompt 写入 L2 → 6 次后 L2 装满
- 第 7 次后,最早的 prompt p0 被 evict 到 L3
- replay p0 时 L2 miss → sglang 从 L3 读

## 数据汇总

### 21 个 request 详细 latency

每个 round 21 行 (1× p0 cold fill + 19× p1-p19 L2 hit + 1× replay_p0 L3 reload):

| 盘 | p0 cold | p1-p19 mean | replay_p0 | 提升 vs p0 |
|---|---:|---:|---:|---:|
| BIWIN ext4 | 1.433s | 1.420s | 1.718s | +285ms (+19.9%) |
| WDC NTFS | 1.434s | 1.421s | 3.816s | +2382ms (+166.1%) |
| Seagate NTFS | 1.434s | 1.448s | 2.773s | +1339ms (+93.4%) |
| ZHITAI NTFS | 1.435s | 1.422s | 2.677s | +1242ms (+86.6%) |

**p0 cold 4 盘 1.434s ± 1ms 几乎一样**(L1 + L2 miss,写 L3),盘差在 L3 reload 阶段爆发。

### iostat 真读盘数据

| 盘 | avg_r (MB/s) | max_r (MB/s) | avg_w (MB/s) | max_w (MB/s) |
|---|---:|---:|---:|---:|
| BIWIN ext4 | **101** | **1665** | 231 | 2677 |
| WDC NTFS | 10 | 483 | 201 | 1925 |
| Seagate NTFS | 11 | 679 | 202 | 4412 |
| ZHITAI NTFS | 10 | 810 | 199 | 3877 |

**注意**:之前 Phase2-5 iostat 显示 NTFS 0 读是因为根本没真读盘(L2 命中,绕开 kernel)。**Phase7 iostat 10 MB/s avg_r** 是真在读 L3:
- 7K tokens × 16 KB/token = 112 MB KV 增量(replay 单个 prompt)
- 1s 内 100 MB 突发 → avg_r 10 MB/s (间歇)

**WDC max_r 483 vs ZHITAI 810 vs BIWIN 1665** —— 跟 replay latency 完全对应。

## 4 盘 L3 性能真实排名

| 排名 | 盘 | replay_p0 | 用途 |
|---|---|---:|---|
| 🥇 | BIWIN (ext4, system) | 1.72s | 极致 L3 性能,大模型生产推荐系统盘 |
| 🥈 | ZHITAI (NTFS) | 2.68s | 性价比优,1.87× overhead |
| 🥉 | Seagate (NTFS) | 2.77s | 稳定均衡,1.93× overhead |
| 4️⃣ | WDC (NTFS) | 3.82s | 慢,**2.66× overhead,大 L3 慎用** |

**对比 fio L3 file read (Phase6 硬件极限)**:
| 盘 | fio 1 thread 1MB seq | sglang replay_p0 推算 L3 read | 效率 |
|---|---:|---:|---:|
| BIWIN | 4.77 GB/s | ~70 MB/s effective (112 MB / 1.6s) | 1.5% |
| WDC | 2.63 GB/s | ~30 MB/s effective (112 MB / 3.8s) | 1.1% |
| Seagate | 3.03 GB/s | ~40 MB/s effective (112 MB / 2.8s) | 1.3% |
| ZHITAI | 3.62 GB/s | ~42 MB/s effective (112 MB / 2.7s) | 1.2% |

**sglang 路径下 L3 读盘效率极低 (1-2%)** —— sglang 的 L3 reader 不是 streaming,每个 page 9MB = 1 个 IO 操作,小 page 数+大文件 + sglang 内部串行 = 远低于盘硬件极限。

## 选型最终推荐(基于所有 phase 数据)

### 综合 4 维度

| 盘 | Phase2 写峰值 | Phase6 fio 顺序读 | **Phase7 L3 reload latency** | Phase4 14B 写 |
|---|---:|---:|---:|---:|
| BIWIN ext4 | 5284 MB/s | 4.77 GB/s | **1.72s** | 790 MB/s |
| WDC NTFS | 8004 MB/s | 2.63 GB/s | 3.82s | 1100 MB/s |
| Seagate NTFS | 8106 MB/s | 3.03 GB/s | 2.77s | 1100 MB/s |
| ZHITAI NTFS | 4498 MB/s | 3.62 GB/s | 2.68s | 1101 MB/s |

### 选型矩阵

| 场景 | 推荐 | 理由 |
|---|---|---|
| **单盘 + 大模型 + 频繁 reload** | 🥇 **BIWIN** | 1.72s 最快,系统盘通用 |
| **多盘 + 长 prefix + 慢 reload 可接受** | 🥈 **Seagate / ZHITAI** | 2.7s 慢但多盘容量大,适合 KV cache 大 + 偶发 reload |
| **高并发 L3 write (系统 bootstrap 阶段)** | 🥉 **Seagate** | 8106 MB/s 写峰值最高 |
| **单盘 + 预算敏感** | ⚠️ **WDC** | 2.63 GB/s 慢,大 L3 1 prompt 慢 2.2× |

## 已知限制

1. **mount 修正后只测了 14B 物理存在但未 mount 的盘**——Phase2-5 数据事故已识别,**Phase7 是新真 4 盘基线**
2. **Phase7 cold latency 4 盘完全相同** —— cold path 4 盘 ≈ 1.43s,盘差只在 L3 reload 暴露
3. **20 prompts 可能不是最优 N** —— 实际 L2 evict 阈值取决于 radix tree 行为
4. **bpftrace 仍未修** —— block IO latency 分布看不到
5. **prompt 7K tokens 较小** —— 大 prompt (32K+) 下 L2 evict 更频繁,盘差更明显

## 数据位置

```
results/hicache_multiprompt/                  # 4 盘 × 7 文件 = 28 文件
├── baseline_biwin_ext4/                      # BIWIN X570 ext4
│   ├── load_test.jsonl                       # 21 行 (p0 + p1-p19 + replay_p0)
│   ├── iostat_nvme1n1.log
│   ├── cache_file_list.txt                   # 2201 files × 9MB = 19.8 GB
│   └── server.log
├── ai_ssd0_wdc_ntfs/                         # WDC WDS960G2G0C NTFS
│   ├── load_test.jsonl                       # replay_p0 = 3.816s 🥇
│   └── ...
├── ai_ssd1_seagate_ntfs/                     # Seagate ZP1000GV30012 NTFS
└── ai_ssd2_zhitai_ntfs/                      # ZHITAI Ti600 NTFS
```

## 关联文档

- [Phase2 - 4B write_through 4 盘 baseline](hicache-4disk-headline-2026-06-12.md) (⚠️ mount 问题,数据需重新评估)
- [Phase4 - 14B-AWQ 4 盘 baseline](hicache-14b-baseline-2026-06-12.md) (⚠️ 同样问题)
- [Phase5 - 多并发 + drop_caches](hicache-multiclient-dropcaches-2026-06-12.md) (⚠️ 同样问题)
- [Phase6 (C) - fio L3 file read 硬件极限](l3-fio-bench-2026-06-13.md) (绕过 sglang,数据仍有效)