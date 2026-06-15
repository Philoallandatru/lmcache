# Phase7 G: Multi-Run Validation + IO 模式深度分析 (2026-06-15)

## TL;DR

Phase7 v3 multiprompt (20 prompts × 7K tokens + replay_p0) 跑了 **6 个独立 run** (v3 + g1..g5),核心结论:

| 盘 | mean (s) | stdev (s) | min | max | CV (%) | 跨 run spread |
|---|---|---|---|---|---|---|
| **BIWIN** | 1.620 | 0.022 | 1.602 | 1.663 | **1.3%** | 0.061s |
| **ZHITAI** | 2.272 | 0.174 | 2.058 | 2.545 | 7.7% | 0.487s |
| **WDC** | 2.651 | 0.159 | 2.446 | 2.902 | 6.0% | 0.456s |
| **Seagate** | **2.981** | **0.540** | 2.431 | 3.508 | **18.1%** | 1.077s |

**Ranking (按 mean, NTFS 三盘): ZHITAI < WDC < Seagate**

- **BIWIN 走 page cache,1.6s 稳定**(几乎无变化,CV 1.3%)
- **ZHITAI 最快且稳定** (~2.2-2.3s,比 WDC 快 ~15%)
- **WDC 中等** (~2.5-2.9s,变化小)
- **Seagate 间歇性慢** (2.4-3.5s bimodal,50% 概率出现 3.4-3.5s 慢读)

⚠️ **单 run ranking 不可信** —— Seagate 在 g1/g3/g4 是 4 盘最慢 (3.4-3.5s),在 v3/g2/g5 又是中间 (2.4-2.6s)。v3 单 run 报告的 "1.59× spread / ZHITAI 优于 WDC 2%" 是真实信号,但 v3 把 Seagate 排第 2 是 sampling 噪声,实际 mean Seagate 比 WDC 慢 ~12%。

## 数据 (6 run × 4 盘 × 1 replay_p0 = 24 个数据点)

```
BIWIN   : n=6  [1.663, 1.613, 1.616, 1.612, 1.612, 1.602]  mean=1.620  CV=1.3%
WDC     : n=6  [2.643, 2.718, 2.902, 2.525, 2.446, 2.674]  mean=2.651  CV=6.0%
Seagate : n=6  [2.431, 3.462, 2.439, 3.441, 3.508, 2.605]  mean=2.981  CV=18.1%  ⚠ bimodal
ZHITAI  : n=6  [2.545, 2.385, 2.280, 2.212, 2.151, 2.058]  mean=2.272  CV=7.7%  📉 持续变快
```

数据源:
- `results/multiprompt_g_summary.json` (单源真相)
- `results/io_pattern_analysis.csv` (iostat 跨 run 汇总)
- `results/io_pattern_analysis.json` (含 burst 详情)
- `results/hicache_multiprompt_g{1..5}/` (5 套完整 4 盘 data)

## 关键观察

### 1. BIWIN 稳定 ~1.6s 反映 page cache,非盘性能
BIWIN 是 ext4 根分区,L3 file (`/home/ficus/llm/.../cache_multiprompt_v3/`) 写到系统盘根 → 走 page cache hit,replay 时 **sglang 几乎不读盘**。CV 1.3% 是 page cache + sglang overhead 本身的稳定性,**不能用来评估 BIWIN 盘硬件性能**。Phase7 v3 报告里也标注过这点(REPORT.md §6.1)。

### 2. ZHITAI 持续变快 (2.545 → 2.058)
6 个 run replay_p0 单调下降: 2.545, 2.385, 2.280, 2.212, 2.151, 2.058。共下降 **19%**。可能原因:
- **page cache 累积**: 每次 run 后 L3 file (19 GB) 残留 page cache,下次 replay 时部分命中
- **filesystem 优化**: NTFS metadata 缓存累积
- **sglang 内部预热**: scheduler/disk 缓存

虽然每盘 L3 cache_dir 独立 (`/mnt/ai_ssd0/cache_multiprompt_gN_v3/`) 但都是 ZHITAI 的同一盘,page cache 是 OS 全局共享,确实会跨 run 累积。
→ **建议**:重跑时 `sync && echo 3 > /proc/sys/vm/drop_caches` 在每个 run 之间清 page cache。

### 3. Seagate bimodal 分布
Seagate 的 6 个 run:
- **快模式 (n=3)**: 2.431, 2.439, 2.605 (均值 2.49s) — 跟 WDC 接近
- **慢模式 (n=3)**: 3.462, 3.441, 3.508 (均值 3.47s) — 比 WDC 慢 35%

慢模式出现频率 50%。可能原因:
- **NTFS journal flush**: NTFS 在 metadata 操作时偶发长延迟
- **盘内部 GC / TRIM**: 消费级 SSD 内部 garbage collection 偶发阻塞
- **SMART 状态**: 需用 `smartctl -a /dev/nvme2n1` 确认盘健康度
- **sglang hicache 内部行为**: 跟 sglang 0.5.13 hicache-storage-prefetch-policy=timeout 的 timeout 值有关

→ **建议**:Phase8 可加 `smartctl` + `bpftrace` trace block I/O + `perf` record 来定位 Seagate 慢读的根因(超出 G 任务范围,留作后续工作)。

### 4. WDC 稳定 ~2.6s
WDC 6 run 范围 2.45-2.90s,CV 6%,是 NTFS 三盘中**最稳的**。
- max (g2) 2.90s 可能是 cold start 噪声
- min (g4) 2.45s 跟 Seagate 快模式持平
- 结论:WDC 是生产环境稳健选择

## IO 模式细分 (基于 iostat -dx -m 1 logs)

### 4 盘 read burst 特征 (mean across 6 run)
| 盘 | read_peak (MB/s) | read_mean_act (MB/s) | r_await (ms) | req size (KB) | %util | %rrqm |
|---|---|---|---|---|---|---|
| **BIWIN** | ~1500 | 250-430 | 0.14-0.19 | 58-69 | 30-37 | ~50% |
| **WDC** | 500-1500 | 125-500 | 0.17-1.70 | 88-125 | 25-51 | 30-50% |
| **Seagate** | 460-820 | 197-334 | 0.21-1.52 | 92-125 | 19-59 | 30-50% |
| **ZHITAI** | 660-1010 | 200-500 | 0.07-0.76 | 73-125 | 16-25 | **20-40%** |

**核心 IO 模式观察:**
- **所有盘的 req size 都在 60-125 KB** → sglang hicache 8.8 MB L3 page 被切成 ~64-128 KB 读请求
- **%util 都不超过 60%** → 盘未饱和,延迟差异不是 queue depth 引起,而是盘内部 latency (r_await)
- **r_await 差异巨大**: ZHITAI 0.07-0.76ms, WDC 0.17-1.70ms, Seagate 0.21-1.52ms → 反映盘本身的读取延迟
- **%rrqm 20-50%** → read merge 比例中等,sglang 不是严格 sequential 访问(可能是 radix tree 跳读)

### Burst 检测 (连续 read >0.5 MB/s 段)
- **BIWIN**: 1-7 burst/run, peak 1000-1500 MB/s,duration 1-3 samples(1-3 秒)→ 短而尖,跟 page cache 行为一致
- **WDC**: 1-8 burst/run, peak 500-1500 MB/s → 跟 BIWIN 类似
- **Seagate**: 1-3 burst/run, peak 460-820 MB/s → 慢盘的 burst 数少 + peak 低
- **ZHITAI**: 1-4 burst/run, peak 660-1010 MB/s → 中等

**重要发现**: 慢盘的 burst 持续时间并不比快盘长 → **盘慢不是因为 IO 持续时间长,而是单次 IO 延迟高**。这印证了 r_await 才是 latency 差异的根因。

## Plots

新生成 3 张:
- `results/plots/11_io_pattern_breakdown.png` — 6 IO 指标跨盘对比 (mean across 6 runs)
- `results/plots/12_replay_multirun.png` — 4 盘 mean ± stddev bar + n=6 标注
- `results/plots/13_burst_analysis.png` — 4 盘 top burst peak BW + duration (v3 + g1 副图)

## 可复现命令

```bash
# 1. 跑 5 个独立 run (g1..g5, v3 已有)
bash scripts/run_g_rounds.sh 1 5   # 约 60-75 min

# 2. 解析 iostat + replay latency 汇总
source ~/llm/.venv/bin/activate
python scripts/analyze_io_pattern.py
# 输出: results/{io_pattern_analysis.csv,io_pattern_analysis.json,multiprompt_g_summary.json}

# 3. 重生成所有 plot (含新增 3 张)
python scripts/plot_io_data.py
# 输出: results/plots/{01..13}_*.png (10 张老 + 3 张新)
```

## 局限

- **Page cache 跨 run 累积**: 6 run 间没有 `drop_caches`,BIWIN/ZHITAI 的 1.62→2.05s 下降可能受 page cache 影响(但 NTFS 三盘的 page cache 跟盘 mount 独立,影响小)
- **单 GPU 串行**: 5 run 串行跑(~60 min),期间 OS 文件系统/温度/调度器状态可能漂移
- **没有 kernel block trace**: 只看 iostat 22 列聚合,无法定位 Seagate 慢读的具体请求级延迟(bio 层)
- **NTFS 文件系统**: Linux NTFS-3g driver 行为可能跟 ext4 不同,影响对比公平性(BIWIN ext4, WDC/Seagate/ZHITAI NTFS)

## 后续工作 (建议)

1. **Phase8+ Seagate 慢读根因定位** (高价值,1-2 天):
   - `bpftrace` trace `blk_mq_start_request` / `blk_mq_end_request` 看 Seagate 慢读时的 kernel 行为
   - `perf` record + `perf script` 看 on-CPU 时间
   - `smartctl -a /dev/nvme2n1` 看 SMART 属性 (Media and Data Integrity Errors / Percentage Used)
   - `fio` 在 Seagate 上跑 `randread 4k bs=128k` 模拟 hicache 模式,验证是否复现 bimodal

2. **Hypothesis: page cache 跨 run 累积导致 ZHITAI 持续变快**
   - 重跑 5 run,中间插 `sync && echo 3 > /proc/sys/vm/drop_caches`
   - 如果 ZHITAI 跨 run 不再单调下降,假设成立

3. **32K-class 4 盘** (低价值,需解 OOM):
   - 仍 blocked by sglang 0.5.13 max_input + 单卡 16 GB
   - 等 sglang 0.6+ 或换 32B+ GPU

## 文件清单

新增 (本次 G 任务):
- `results/hicache_multiprompt_g1/`, `_g2/`, `_g3/`, `_g4/`, `_g5/` (5 套 × 4 盘 = 20 个 round 完整 data)
- `results/io_pattern_analysis.csv`
- `results/io_pattern_analysis.json`
- `results/multiprompt_g_summary.json`
- `results/plots/11_io_pattern_breakdown.png`
- `results/plots/12_replay_multirun.png`
- `results/plots/13_burst_analysis.png`
- `scripts/analyze_io_pattern.py` (iostat log parser + burst detector + summary writer)
- `scripts/run_g_rounds.sh` (串接 RUN_ID=N 跑 N run)
- `scripts/hicache_drive_4_rounds_model.sh` (+ `qwen3_4b_multiprompt_run` preset)
- `scripts/plot_io_data.py` (+ 3 new plots: 11/12/13)
- `docs/hicache-phase7-g-multirun-validation-2026-06-15.md` (本文件)

修改 (本任务):
- 无 (REPORT.md §4 引用此 doc,但本身是 reference)
