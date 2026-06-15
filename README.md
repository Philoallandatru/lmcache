# sglang HiCache × AI SSD 预研

> **日期范围**: 2026-06-11 ~ 2026-06-14
> **状态**: ✅ 完成 7 phases,L2-miss 路径盘差已暴露 (4 盘 spread 2.1s)
> **覆盖**: sglang 0.5.13 + Qwen3-4B-Instruct-2507 + Qwen3-14B-AWQ × 4 块 NVMe
> **核心交付**: AI-SSD 选型基线 + 已知坑位清单 + 端到端可复现脚本

## 目录

| [REPORT.md](./REPORT.md) — 主报告 (TL;DR / 7 phase 时间线 / 选型推荐 / 后续) |
| [docs/](./docs/) — 各 phase 详细数据报告 (7 篇) |
| [scripts/](./scripts/) — 启动 / 压测 / IO 监测 / 驱动器 |
| [results/](./results/) — 原始测试数据 (iostat / jsonl / cache_file_list / metrics) |
| [results/plots/](./results/plots/) — **10 张 IO profiling 可视化图** (PNG, gitignore, 本地生成) |

## 可视化图索引

> 10 张 IO profiling 图,本地生成不入 git。运行 `python scripts/plot_io_data.py` 一键生成 (`results/plots/*.png`)。
> ⚠️ 需要 `matplotlib`,先 `source ~/llm/.venv/bin/activate` (复用 vllm 时代的 venv)。

| # | 图 | 关键故事 |
|---|---|---|
| 01 | `01_fio_bw.png` | 4 盘 fio 顺序读 — BIWIN 4.65 GB/s 第一 |
| 02 | `02_fio_rand4k_iops.png` | 4 盘 fio 4K rand — BIWIN 22.7K 一骑绝尘 |
| 03 | `03_fio_latency_percentiles.png` | 4 盘 fio p50/p99/p99.9 — BIWIN/ZHITAI p99 < 1ms |
| 04 | `04_hicache_cold_warm.png` | sglang HiCache cold/warm — 4 盘 cold 1.44s 一致 |
| 05 | `05_phase_spread.png` | 5 phase 横向 — 4B 7K ~1.4s, 14B-AWQ 4.9s |
| 06 | `06_cache_hit_vs_device.png` | **核心图**: cache hit vs device — 真读盘 2.22× spread |
| 07 | `07_iostat_timeseries.png` | iostat 时序 — **BIWIN 才有 IO** (暴露 write_through 写系统盘) |
| 08 | `08_l3_file_count.png` | L3 file 数量 — 4 盘 ~30 file × 5 MB |
| 09 | `09_decision_radar.png` | **决策雷达** — BIWIN 综合最强 |
| 10 | `10_multiprompt_modes.png` | multiprompt 3 模式 — WDC replay 3.82s vs BIWIN 1.72s |

详情见 [REPORT.md §7 可视化](./REPORT.md#7-关联文档) 和 [docs/io-profiling-plots-2026-06-15.md](./docs/io-profiling-plots-2026-06-15.md)。

## 速览 (TL;DR)

**4 块候选盘在 sglang HiCache L3 read 场景下的真实排名** (Phase7 v3 复现, multiprompt 触发 L2 evict):

| 排名 | 盘 | L2 hit (p1-p19) | L3 reload (replay_p0) | overhead |
|---|---|---:|---:|---:|
| 🥇 | **BIWIN X570 (ext4, system)** | 1.42s | **1.66s** | 1.15× |
| 🥈 | Seagate ZP1000GV30012 (NTFS) | 1.42s | 2.43s | 1.69× |
| 🥉 | ZHITAI Ti600 (NTFS) | 1.42s | 2.55s | 1.77× |
| 4️⃣ | WDC WDS960G2G0C (NTFS) | 1.42s | **2.64s** | **1.84×** |

> v2 (06-14) spread 2.1s (2.22×) 偏大,v3 (06-15) 验证后 spread **980ms (1.59×)**,但 ranking 不变。详见 [docs/hicache-phase7-v3-validation-2026-06-15.md](./docs/hicache-phase7-v3-validation-2026-06-15.md)。

**关键洞察**:
- L2 host DRAM hit 时 4 盘 **完全无差异** (cold/warm spread < 10ms)
- L2 miss → L3 读盘时 4 盘 spread **980ms (v3) / 1.59×** (BIWIN vs WDC)
- NTFS 比 ext4 慢 1.5-1.6× (kernel 驱动开销, BIWIN 1.66s vs NTFS 2.43-2.64s)
- WDC 仍是最慢 (1.84× overhead),大 L3 部署不推荐

## 重现

```bash
# 1) 装环境 (复用 vllm 时代的 ~/llm/.venv/)
cd ~/llm && source .venv/bin/activate

# 2) 拉模型 (4B 8G, 14B-AWQ 10G; ModelScope 自动)
python -c "from modelscope import snapshot_download; snapshot_download('Qwen/Qwen3-4B-Instruct-2507', cache_dir='/home/ficus/llm/models')"

# 3) 挂载候选盘 (4 块 NVMe 都要 mount!)
#    fstab 已持久化, 重启自动挂; 首次手动 mount:
sudo -n mount -t ntfs3 -o noatime,nodiratime,uid=1000,gid=1000 /dev/nvme0n1p2 /mnt/ai_ssd0   # WDC
sudo -n mount -t ntfs3 -o noatime,nodiratime,uid=1000,gid=1000 /dev/nvme2n1p3 /mnt/ai_ssd1   # Seagate
sudo -n mount -t ntfs3 -o noatime,nodiratime,uid=1000,gid=1000 /dev/nvme3n1p2 /mnt/ai_ssd2   # ZHITAI
#    /dev/nvme1n1p3 (BIWIN) 是系统盘, 根分区

# 4) 跑 L2-miss baseline (Phase7, 20 prompts + replay)
MODEL_KEY=qwen3_4b_multiprompt bash scripts/hicache_drive_4_rounds_model.sh

# 5) 跑 L2-hit baseline (Phase2, 1 prompt × 6 rounds)
MODEL_KEY=qwen3_4b bash scripts/hicache_drive_4_rounds.sh

# 6) 跑 14B-AWQ baseline (Phase4)
MODEL_KEY=qwen3_14b_awq bash scripts/hicache_drive_4_rounds_model.sh
```

## 关键依赖

| 工具 | 版本 | 用途 |
|---|---|---|
| sglang | 0.5.13 | HiCache L3 file backend |
| vllm | 0.22.1 | 历史 LMCache baseline 对比 (Phase0) |
| lmcache | 0.4.6 | 历史 baseline |
| torch | 2.11.0+cu130 | sglang/vllm 通用 |
| transformers | 5.10.2 | Qwen3 tokenizer |
| sysstat | 12.7.7 | iostat / pidstat |
| bpftrace | 0.25.0 | block IO latency (⚠️ kernel 6.x 兼容待修) |
| fio | (apt) | Phase6 L3 file read 硬件极限基线 |
| ntfs-3g | (apt) | mount NTFS 候选盘 |

## 注意事项 / 已知坑位

### mount 修正 (历史 Phase2-5 数据事故)
**Phase2-5 跑测试时 3 块 NTFS 盘 (WDC/Seagate/ZHITAI) 实际未 mount**,`/mnt/ai_ssd{0,1,2}` 是 BIWIN 根分区上的空目录。
**Phase7 起已修正**: `sudo mount -t ntfs3` + fstab 持久化 (`UUID=... ntfs-3g defaults,nofail,uid=1000,gid=1000 0 0`)。
**Phase2-5 的"4 盘对比"数据是 BIWIN 重复 4 次**, 需用 Phase7 数据为准。
详见 [REPORT.md §3 数据事故复盘](./REPORT.md) 和 [docs/hicache-multiprompt-l2fill-2026-06-14.md §🚨 重要发现](./docs/hicache-multiprompt-l2fill-2026-06-14.md)。

### sglang 0.5.13 行为约束
1. **L2 ≥ device** (--hicache-ratio ≥ 1.0): 0.5.13 硬约束, `ratio < 1.0` 启动期 `AssertionError: The host memory should be larger than the device memory`
2. **L2 host 容量** = device pool × hicache-ratio (4B device pool 20480 tok, ratio 2 → L2 41024 tok)
3. **CLI `--file-storage-path` 不生效**: 0.5.13 `hicache_storage.py::HiCacheFile.__init__` 只读 `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR` 环境变量
4. **device KV 上限** ~20K tokens (4B), 装不下 40K+ 大 prompt → ratio 调大也无效
5. **page_size=64 固定**: sglang 0.5.13 强制 (不暴露 CLI), Qwen3-4B 1 page = 9.0 MB, 14B-AWQ 1 page = 5.0 MB
6. **write_back 在 6 round 测试场景下完全失效**: L3 worker 还没启动就测完了

### Page cache 屏蔽
- sglang L2 host DRAM (4B = 41K tokens) 完全屏蔽 L3 读盘延迟
- 即使 `drop_caches` 清 OS page cache, sglang pin_memory 自管 host buffer **不受影响**
- 必须用 **多 prompt 累积** 触发 L2 evict 才能暴露 L3 读盘 (Phase7 方法)

### 多卡异构
- 测试用 2× RTX 5080 + 1× RTX 5060 Ti 16GB, sglang 启动时打 `Detected different devices` 警告
- 建议加 `CUDA_DEVICE_ORDER=PCI_BUS_ID` 环境变量
- TP=1 (4B) 自动选主卡, TP=2 (14B) 跨卡

## 后续 / 未完成项

- ✅ ~~重跑 Phase2/4/5 (mount 已修, 真 4 盘基线)~~ (06-15 v3 完成, 验证 spread 跟 v2 一致, 详见 [hicache-v3-mount-fixed-2026-06-15.md](./docs/hicache-v3-mount-fixed-2026-06-15.md))
- [ ] bpftrace kernel 6.x 兼容修复 (block_rq_issue 存 nsecs, `delete()` API 移除) — 暂缓, 已有 iostat + Phase7 数据足够
- [ ] 32B-AWQ 模型 (更大压力, TP=2 跨 3 卡)
- [ ] 多 prompt 累积场景下的 4 盘 vs 4 client × 4 prompt 对比 (核并发 reload)

## 文档索引

| 报告 | 描述 |
|---|---|
| [REPORT.md](./REPORT.md) | **主报告** — 7 phase 时间线 / 选型推荐 / 后续 / 数据事故复盘 |
| [REPORT_LMCACHE.md](./REPORT_LMCACHE.md) | Phase 0 历史 baseline (vLLM + LMCache 时代, 4 盘 spread 1ms) |
| [docs/hicache-smoke-test-findings-2026-06-11.md](./docs/hicache-smoke-test-findings-2026-06-11.md) | Phase 1 装环境 + 启动验证 |
| [docs/hicache-4disk-headline-2026-06-12.md](./docs/hicache-4disk-headline-2026-06-12.md) | Phase 2 4B write_through 4 盘 baseline ✅ v3 验证 |
| [docs/hicache-writeback-vs-writethrough-2026-06-13.md](./docs/hicache-writeback-vs-writethrough-2026-06-13.md) | Phase 3 write_through vs write_back 对比 ✅ v3 验证 |
| [docs/hicache-14b-baseline-2026-06-12.md](./docs/hicache-14b-baseline-2026-06-12.md) | Phase 4 14B-AWQ TP=2 4 盘 baseline ✅ v3 验证 |
| [docs/hicache-multiclient-dropcaches-2026-06-12.md](./docs/hicache-multiclient-dropcaches-2026-06-12.md) | Phase 5 4 client + drop_caches 每 round ✅ v3 验证 |
| [docs/l3-fio-bench-2026-06-13.md](./docs/l3-fio-bench-2026-06-13.md) | Phase 6 fio 4 盘 L3 file read 硬件极限 |
| [docs/hicache-multiprompt-l2fill-2026-06-14.md](./docs/hicache-multiprompt-l2fill-2026-06-14.md) | **Phase 7 multiprompt + replay ✅ 真 4 盘基线 (选型依据, v2 数据)** |
| [docs/hicache-v3-mount-fixed-2026-06-15.md](./docs/hicache-v3-mount-fixed-2026-06-15.md) | **Phase 2/4/5 v3 mount-fixed 重跑 ✅ 验证 spread 跟 v2 一致** |
| [docs/hicache-v3-policy-2026-06-15.md](./docs/hicache-v3-policy-2026-06-15.md) | **Phase 3 v3 write_through vs write_back 重跑 ✅ 验证 write_back -37ms** |
| [docs/hicache-phase7-v3-validation-2026-06-15.md](./docs/hicache-phase7-v3-validation-2026-06-15.md) | **Phase 7 v3 复现 ✅ ranking 不变, spread 980ms (v2 2098ms 偏大)** |

## 计划文档

- [Plan v2](./.hermes/plans/2026-06-11_155736-sglang-hicache-exploration.md) — sglang hicache 探索 7 phase 计划
