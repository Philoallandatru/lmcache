# Phase 3 v3 — write_through vs write_back Mount 修正后 4 盘对比 (2026-06-15)

> **目的**: Phase3 老数据 (write_through vs write_back) 也是 mount 事故期间跑的,盘映射反了 (ai_ssd1=ZHITAI 但 nvme2n1=Seagate)。
> **本报告**: mount 修正 + 盘映射修正后重跑 write_back 4 盘, 跟 Phase2 v3 (write_through) 对比。

## 核心结论: write_back 让 cold 快 37ms, 跟 v2 老数据完全一致

| 策略 | 4 盘 cold spread | mean cold | mean warm_1 | L3 files | 加速比 |
|---|---:|---:|---:|---:|---:|
| **write_through (Phase2 v3)** | 6ms | **1.440s** | 0.740s | 115×9MB = 1035MB | 1.95× |
| **write_back (Phase3 v3)** | 1ms | **1.403s** | 0.737s | **0** (worker 未落盘) | 1.90× |
| **delta** | -5ms | **-37ms** | -3ms | — | -0.05× |

**验证 Phase3 老数据结论**: write_back 让 cold 快 37ms (老 1.96× vs 老 1.90×), 跟 v2 数据完全一致。

## Phase3 v3 详细数据 (write_back, 4 盘)

### 配置 (跟 Phase3 v2 老数据完全一致)
- 模型: Qwen3-4B-Instruct-2507
- 负载: 1 cold + 5 warm, 7000 tokens prefix, 64 tokens output
- HiCache: `page_size=64, hicache-ratio=2, page_first_direct, direct io, **write_back**, timeout, file backend`
- L3 目录: BIWIN 根分区 `cache/baseline_wb_v3/`, NTFS 各自 `/mnt/ai_ssd{0,1,2}/cache_hicache_wb_v3/`

### 4 盘 TTFT

| 盘 | cold | warm_1 (drop) | warm_2 | warm_3 | warm_4 | warm_5 | 加速比 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 🥇 BIWIN ext4 | 1.403s | 0.737s | 0.723s | 0.723s | 0.722s | 0.722s | 1.90× |
| WDC NTFS | 1.403s | 0.735s | 0.723s | 0.722s | 0.721s | 0.721s | 1.91× |
| Seagate NTFS | 1.404s | 0.736s | 0.723s | 0.723s | 0.722s | 0.723s | 1.91× |
| ZHITAI NTFS | 1.404s | 0.739s | 0.722s | 0.722s | 0.722s | 0.723s | 1.90× |
| **mean** | **1.403s** | 0.737s | 0.723s | 0.723s | 0.722s | 0.722s | **1.90×** |
| **spread** | **1ms** | 4ms | 1ms | 1ms | 1ms | 1ms | — |

### iostat v3 (write_back)

| 盘 | avg_r | max_r | avg_w | max_w |
|---|---:|---:|---:|---:|
| **BIWIN ext4** | **179** | **1550** | 35 | 128 |
| WDC NTFS | 0 | 0 | 2 | 111 |
| Seagate NTFS | 0 | 0 | 2 | 108 |
| ZHITAI NTFS | 0 | 0 | 2 | 94 |

**核心发现**:
- 跟 write_through 一致, NTFS 3 盘 iostat 0 读 (L2 hit 100%)
- BIWIN 仍然 179 MB/s page cache 命中
- 写: write_back 2-35 MB/s, write_through 4-13 MB/s (write_back 写异步分散, write_through 同步集中)

### L3 落盘

| 策略 | L3 file count (4 盘合计) | L3 total |
|---|---:|---:|
| write_through | 4 × 115 = 460 | 4 × 1035 MB = 4140 MB |
| **write_back v3** | **4 × 0 = 0** ⚠️ | **0** |

**跟 v2 老数据结论一致**: write_back 6 round 测试完, **L3 worker 还没启动 / 写完**, 0 file 落盘。

## vs Phase2 v3 (write_through) vs Phase3 v3 (write_back) 完整对比

| 指标 | write_through (Phase2 v3) | write_back (Phase3 v3) | delta |
|---|---:|---:|---:|
| Cold 4 盘 mean | 1.440s | 1.403s | **-37ms** |
| Cold 4 盘 spread | 6ms | 1ms | -5ms |
| Warm_1 mean | 0.740s | 0.737s | -3ms |
| Warm_5 mean (L2 hit) | 0.722s | 0.722s | 0ms |
| 加速比 (cold/warm_1) | 1.95× | 1.90× | -0.05× |
| L3 file count | 115×4 = 460 | 0×4 = 0 | -460 |
| L3 total | 4140 MB | 0 MB | -4140 MB |
| NTFS max_r | 0 MB/s | 0 MB/s | 0 (L2 hit) |
| BIWIN avg_r | 1 MB/s | 179 MB/s | +178 (write_back 让 page cache 更活跃) |
| NTFS avg_w | 5-7 MB/s | 2 MB/s | -3-5 (write_back 写异步分散) |
| BIWIN max_w | 97 MB/s | 128 MB/s | +31 |

## 跟 v2 老数据 (Phase3) 对比

| 指标 | v2 (老) | v3 (新) | delta |
|---|---:|---:|---:|
| write_through cold mean | 1.440s | 1.440s | 0 ✅ |
| write_back cold mean | 1.403s | 1.403s | 0 ✅ |
| write_back 4 盘 spread | 1ms | 1ms | 0 ✅ |
| L3 file count (write_back) | 0 | 0 | 0 ✅ |

**v2 老数据跟 v3 几乎完全一致**, mount 事故没影响 cold/warm 数据(跟 Phase2/4/5 结论一致)。

## 选型相关结论 (跟 Phase3 v2 一致)

### 1. write_back 优势
- ✅ Cold TTFT 快 37ms (-2.6%) — 因为 cold 不等 L3 写
- ✅ Cold 4 盘 spread 更小 (1ms vs 6ms) — L3 写同步阻塞 = 写时长被 4 盘差异放大, write_back 异步 = 屏蔽
- ✅ 4 盘 iostat 写更分散, 写延迟不集中在测试关键路径

### 2. write_back 劣势
- ❌ 6 round 测试场景下 L3 0 file 落盘 → warm_2 之后理论上要走 L3 reload (但实际 L2 hit 100%, 看不盘差)
- ❌ 生产环境**需要长时间测试**才能看到稳态 L3 落盘 + reload 行为
- ❌ Cold 37ms 提升的代价: 数据可靠性差 (worker crash → 数据丢失), 测试时序不一致

### 3. 生产建议 (跟 v2 一致)
- 单请求场景 (低并发) → **write_through** (数据一致性 + 后续 warm 必命中)
- 高并发 + 长运行场景 → **write_back** (cold 不阻塞, 接受 worker 异步落盘)

## 数据位置

```
results/hicache_writeback_v3/         # write_back 4 盘
├── baseline_biwin_ext4_wb/           # BIWIN ext4
├── ai_ssd0_wdc_ntfs_wb/              # WDC WDS960G2G0C NTFS
├── ai_ssd1_seagate_ntfs_wb/          # Seagate ZP1000GV30012 NTFS
└── ai_ssd2_zhitai_ntfs_wb/           # ZHITAI Ti600 NTFS
```

每盘 7 文件: load_test.jsonl, iostat_*.log, server.log, metrics_before/after.json, cache_file_list.txt, load_test.log

## 配套脚本改动

### `scripts/hicache_drive_4_rounds_policy.sh` (Phase3 driver)
- ✅ 修盘映射: `ai_ssd1_seagate:nvme2n1, ai_ssd2_zhitai:nvme3n1` (跟 Phase2/4/5 v3 driver 一致)
- ✅ 加 v3 SUBDIR 隔离 (`*_v3` 后缀)
- ✅ 加 4 盘 cache_dir 预创建 (避免 bench_one_round.sh precheck fail)
- ✅ verify 路径已用 `${SUBDIR}` 而非写死, 直接兼容 v3

### `.gitignore`
- ✅ 加 `results/hicache_writeback_v3/*/cache/` 排除 (L3 文件不入版本)

## 跟 v2 老数据比较总表

| Phase | v2 老 | v3 新 | 一致? |
|---|---:|---:|:---:|
| Phase2 (4B write_through) | cold 1.440s ± 2ms | cold 1.440s ± 3ms | ✅ |
| Phase3 (4B write_back) | cold 1.403s ± 0.7ms | cold 1.403s ± 0.5ms | ✅ |
| Phase4 (14B-AWQ write_through) | cold 4.890s ± 2ms | cold 4.894s ± 2ms | ✅ |
| Phase5 (4B N=4+drop) | cold 1.726s ± 2ms | cold 1.734s ± 12ms | ✅ |

**4 个 phase v2/v3 全部一致**, 验证:
- **mount 事故没有影响 cold/warm spread 数据** (L2 host DRAM 屏蔽真这么强)
- **iostat 数值需要重新看 v3** (NTFS 真 0 读, 老数据 8-22 MB/s 是 page cache 命中)
- **写策略结论 (write_back 让 cold -37ms) v2/v3 一致**
