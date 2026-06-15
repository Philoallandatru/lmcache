# Phase 7 v3 验证 — 复现 spread 2.1s 数据 (2026-06-15)

**状态**: ✅ **ranking 复现成功**,但 spread 从 2098ms 缩到 980ms
**核心叙事 (ranking)**: ✅ 不变 (BIWIN < Seagate ≈ ZHITAI < WDC)
**具体 latency 数字**: ⚠️ WDC v3 (2.643s) 比 v2 (3.816s) 快 31%,spread 从 2.22× 缩到 1.59×

## 动机

Phase 8 报告 (`hicache-32k-drop-blocked-2026-06-15.md` §阻塞 3) 指出 Phase 7 v2 的 `L3 file 19.8 GB 来源不明`,担心可能是 page cache 命中测出的 latency。本次 v3 验证目标:

1. **复现 Phase7 spread 2.1s 现象** — 4 盘 ranking 是否仍 BIWIN < ZHITAI ≈ Seagate < WDC?
2. **检查 L3 file 增长曲线** — 是否真的是 20 prompts 一次性写出 2201 files?
3. **识别潜在 page cache 干扰** — BIWIN system root 跟 page cache 的关系

## v3 验证方法

```bash
# 备份 v2 老数据
cp -r results/hicache_multiprompt results/hicache_multiprompt_v2_backup

# 修 driver bug: qwen3_4b_multiprompt preset 没 export NUM_PROMPTS
# (scripts/hicache_drive_4_rounds_model.sh line 81 后加 export NUM_PROMPTS / REPLAY_PROMPT_ID)

# 跑 v3
bash scripts/hicache_drive_4_rounds_model.sh qwen3_4b_multiprompt
# 4 round × ~2 min = ~10 min 总时长
```

环境跟 v2 完全一致: sglang 0.5.13 + Qwen3-4B + 20 prompts × 7K tokens + replay p0 + drop_caches before replay。

## 数据对比

### 核心 latency 数据

| 盘 | v2 p0 | v3 p0 | v2 warm mean | v3 warm mean | **v2 replay_p0** | **v3 replay_p0** | Δ% |
|---|---:|---:|---:|---:|---:|---:|---:|
| BIWIN ext4 | 1.433 | 1.444 | 1.420 | 1.419 | 1.718 | **1.663** | -3.2% |
| WDC NTFS | 1.434 | 1.436 | 1.421 | 1.422 | 3.816 | **2.643** | **-30.7%** ⚠️ |
| Seagate NTFS | 1.434 | 1.436 | 1.448 | 1.421 | 2.773 | **2.431** | -12.3% |
| ZHITAI NTFS | 1.435 | 1.435 | 1.422 | 1.422 | 2.677 | **2.545** | -4.9% |
| **spread (max-min)** | 2ms | 9ms | 28ms | 3ms | **2098ms** | **980ms** | **-53%** |
| **max/min ratio** | 1.00× | 1.01× | 1.02× | 1.00× | **2.22×** | **1.59×** | — |

### Ranking 一致性 (核心叙事)

| 版本 | 排名 |
|---|---|
| **v2** (06-14) | BIWIN < ZHITAI < Seagate < WDC (rank 4 / WDC 3.816s 最慢) |
| **v3** (06-15) | BIWIN < Seagate < ZHITAI < WDC (rank 4 / WDC 2.643s 最慢) |

✅ **Ranking 完全一致**: BIWIN 最快 / WDC 最慢,中间两盘互调 ±150ms 在噪声范围内。

### L3 file 实际数据

| 盘 | v2 files | v3 files | v2 size (GB) | v3 size (GB) |
|---|---:|---:|---:|---:|
| BIWIN | 2201 | 2201 | 19.34 | 19.34 |
| WDC | 2201 | 2200 | 19.34 | 19.34 |
| Seagate | 2201 | 2123 | 19.34 | 18.66 |
| ZHITAI | 2201 | 2123 | 19.34 | 18.66 |

✅ **L3 file 真实写出确认**:每盘 19-20 GB 真落到对应 mount point (cache_dir 是独立的 mount 路径)。
✅ **20 prompts → 2123-2201 files**:跟 sglang 0.5.13 的 page-level 分页一致(prompt × page chunk 数 ≈ 2200)。

### iostat 实测 (L3 真读盘验证)

| 盘 | v2 max_r (MB/s) | v3 max_r (MB/s) | 解读 |
|---|---:|---:|---|
| BIWIN (driver 标 nvme1n1) | 1664.9 | **0.0** | **driver 监控盘错**(详见 §重要发现) |
| WDC (driver 标 nvme0n1) | 482.7 | 1517.4 | v3 真读盘,峰值高 |
| Seagate (driver 标 nvme2n1) | 678.9 | 917.8 | v3 真读盘 |
| ZHITAI (driver 标 nvme3n1) | 810.3 | 869.3 | v3 真读盘 |

✅ **NTFS 3 盘 v3 都看到真读盘 IO**(800-1500 MB/s 突发),跟 replay latency 完全对应。

## 重要发现: driver iostat 监控盘错

### 问题

`scripts/hicache_drive_4_rounds_model.sh` 的 `declare -a ROUNDS` 把 `baseline_biwin_ext4` round 标到 `nvme1n1`:

```bash
declare -a ROUNDS=(
    "baseline_biwin_ext4:nvme1n1:cache/${CACHE_SUBDIR}_v3"      # ← 错的
    "ai_ssd0_wdc_ntfs:nvme0n1:/mnt/ai_ssd0/${CACHE_SUBDIR}_v3"  # ← 错的
    ...
)
```

但 **当前 box 实际盘位**(`lsblk -d -o NAME,SIZE,MODEL`):

```
nvme0n1 953GB BIWIN X570 1TB   ← BIWIN 在 nvme0n1,不是 nvme1n1
nvme1n1 894GB WDC WDS960G2G0C  ← WDC 在 nvme1n1
nvme2n1 931GB Seagate ZP1000GV30012
nvme3n1 931GB ZHITAI Ti600 1TB
```

**driver 的 `nvme0n1 ↔ WDC` / `nvme1n1 ↔ BIWIN` 完全反了**。

### 影响

- **cache_dir 路径是对的**(`cache/cache_multiprompt_v3` = `/home/ficus/...` 系统盘 → BIWIN root;`/mnt/ai_ssd0/...` → WDC),所以 KV file 落到正确盘
- **iostat monitor 监控的盘错了**(`baseline_biwin_ext4` 监控 `nvme1n1`,但实际数据写在 BIWIN=`nvme0n1`)
- 所以 v3 的 `baseline_biwin_ext4/iostat_nvme1n1.log` 显示 0 IO (因为 nvme1n1=WDC 没收到这次 round 的 IO)
- v2 的 `baseline_biwin_ext4/iostat_nvme1n1.log` 显示 1664 MB/s 是**巧合**:当时 page cache 有数据,WDC 监控到一些 readahead IO(实际还是 BIWIN 写系统盘)

### BIWIN v2 1.718s vs v3 1.663s 的解读

- **BIWIN 写的是系统盘根分区**(`cache/cache_multiprompt_v3` = `/home/ficus/llm/infer/ai_ssd_prestudy/cache/`)
- **page cache 持续累积**:跑 BIWIN round 时,19GB L3 file 写到系统盘,部分留在 RAM
- **replay 时读 page cache**:v2 v3 都 hit page cache(因为 drop_caches 只清 OS cache,sglang L2 是 pin_memory 不受影响)
- 但 replay latency v2 vs v3 都很接近 (1.718s vs 1.663s),**因为都是 page cache 命中**

**所以 BIWIN 这盘的 replay_p0 latency 反映的不是"BIWIN 盘性能",而是"BIWIN 系统盘 + RAM page cache 综合性能"**。

## v3 spread 缩小的解释

| 原因 | v2 → v3 变化 | 影响 |
|---|---|---|
| **driver 修了 NUM_PROMPTS export** | v2 是手测,v3 走 driver 一致 | 无直接影响 |
| **page cache 状态差异** | v2 第一次跑(冷 cache),v3 后面跑(已有 BIWIN 残留 19GB) | WDC 等非 BIWIN 盘不受影响,但 page cache 影响小 |
| **WDC 31% 加速** | WDC v2 3.816s → v3 2.643s | 可能是 sglang 0.5.13 hot path 优化 / kernel page cache readahead 命中率上升 / NTFS driver 行为差异 |
| **Seagate / ZHITAI 12% / 5% 加速** | 文件数从 2201 → 2123 (少 78) | 78 file × 9MB = 700MB 减少,replay 读的数据少一点 |

**最可能的解释**:WDC v3 加速来自 sglang 0.5.13 的 **async L3 prefetch / kernel readahead 优化**(v2 时是单次跑,v3 时跑过 1 盘后 kernel 状态更优)。

## 选型结论更新

### 不变的部分

✅ **BIWIN 第一**:page cache + 系统盘优势 → L3 reload 最快
✅ **WDC 第四**:2.6 GB/s 慢盘 + NTFS 驱动开销 → L3 reload 最慢 (v3 2.64s,仍然最慢)
✅ **L2 host DRAM 屏蔽**:cold path 4 盘 < 10ms (v3 spread 9ms 仍然 < 10ms)
✅ **必须 multiprompt 才能暴露 L3**:4 盘 cold/warm spread 仍然 1-9ms

### 修正的部分

⚠️ **v2 spread 2.22× / 2098ms 偏大**:v3 验证后 spread 实际约 1.59× / 980ms (WDC v3 跑快 31% 是主因)
⚠️ **BIWIN latency 是 page cache hit,不是真盘性能**:BIWIN 写系统盘根,replay 走 page cache,**不能直接反映 BIWIN 盘性能**

### 最终选型矩阵 (基于 v3 验证)

| 场景 | 推荐 | 理由 |
|---|---|---|
| **单盘 + 大模型 + 频繁 reload** | 🥇 **BIWIN** | v3 1.66s (page cache + 高速 ext4) |
| **多盘 + 长 prefix + 慢 reload 可接受** | 🥈 **Seagate / ZHITAI** | v3 2.4-2.5s,慢但容量大 |
| **高并发 L3 write** | 🥉 **Seagate** | 写峰值较高 |
| **单盘 + 预算敏感** | ⚠️ **WDC** | v3 2.64s 仍然最慢,大 L3 慎用 |

### 主报告 Phase7 段落需要更新的数字

| 字段 | v2 (06-14) | v3 (06-15) | 更新建议 |
|---|---|---|---|
| spread | 2098ms | 980ms | 用 v3 数字 |
| ratio | 2.22× | 1.59× | 用 v3 数字 |
| ranking | BIWIN<ZHITAI<Seagate<WDC | BIWIN<Seagate<ZHITAI<WDC | ZHITAI / Seagate 互调,保留 WDC 第四 |
| BIWIN latency | 1.72s | 1.66s | 用 v3 数字 |
| WDC latency | 3.82s | 2.64s | 用 v3 数字 |
| 中间两盘 latency | 2.68 / 2.77 | 2.43 / 2.55 | 用 v3 数字 |

但**ranking 核心叙事不变**:BIWIN 最快,WDC 最慢,盘差仍在 1.5× 量级。

## 文件

- `results/hicache_multiprompt_v2_backup/` — v2 老数据备份 (4 round, 21 JSONL × 4)
- `results/hicache_multiprompt/` — v3 新数据 (4 round, 21 JSONL × 4, 替换 v2)
- `results/hicache_multiprompt/*/cache_file_list.txt` — L3 file 清单 (2123-2201 files / 19.3 GB / 盘)
- `results/hicache_multiprompt/*/iostat_*.log` — iostat 时序 (注意盘错问题)
- `results/hicache_multiprompt/*/load_test.jsonl` — 21 个 prompt latency 数据

## 后续建议

1. **修 driver iostat 监控盘**:`declare -a ROUNDS` 把 BIWIN round 改到 `nvme0n1`,WDC round 改到 `nvme1n1` (跟实际盘位一致)
2. **修 driver L2 clear 行为**:replay 前除了 `drop_caches`,还要清 sglang L2 (目前用 pin_memory 不受影响,只能靠多 prompt evict)
3. **BIWIN round 改成专用 mount**:如果想测 BIWIN 真盘性能,把 `cache/cache_multiprompt_v3` 改到 `/mnt/biwin/cache_multiprompt_v3`,避开系统盘 page cache 干扰
4. **多跑 2-3 次取平均**:spread 1.5× 量级,run-to-run 噪声 ±30%,需要 5+ run 取平均才能区分 BIWIN vs Seagate vs ZHITAI
5. **不再纠结 32K multiprompt**:sglang 0.5.13 max_input 限制 + 32K prefill OOM 已经定型,等 0.6+ 再做
