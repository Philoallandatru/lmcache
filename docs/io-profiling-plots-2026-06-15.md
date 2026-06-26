# AI SSD IO Profiling 图表说明

**生成日期**: 2026-06-15
**脚本**: `scripts/plot_io_data.py`, `scripts/analyze_io_pattern.py`
**数据源**: `results/l3_fio/`, `results/hicache_*/*/load_test.log`, `load_test.jsonl`, `iostat_*.log`, `cache_file_list.txt`
**产物**: `results/plots/01_*.png` 到 `13_*.png`

这组图不是单纯展示“哪块盘带宽高”。它们要回答三个问题：

1. SSD raw 能力是否足以解释 HiCache 延迟？
2. sglang HiCache 哪些测试真的走到 L3 SSD？
3. replay 延迟差异来自带宽、请求形态、await,还是 run-to-run 波动？

## 图表索引

| # | 文件 | 回答的问题 | 关键结论 | 注意边界 |
|---|---|---|---|---|
| 01 | `01_fio_bw.png` | 4 盘顺序读上限是多少？ | BIWIN > ZHITAI > Seagate > WDC | 这是 direct fio,不是 HiCache 实际读形态 |
| 02 | `02_fio_rand4k_iops.png` | 小随机读上限如何？ | BIWIN 领先,WDC/Seagate 接近 | HiCache 请求不是 4K,但能反映小 IO 能力 |
| 03 | `03_fio_latency_percentiles.png` | fio tail latency 谁更差？ | WDC/Seagate p99 高于 BIWIN/ZHITAI | fio 使用 1MB seq4t,不能直接等价 replay |
| 04 | `04_hicache_cold_warm.png` | 普通 cold/warm 能区分盘吗？ | 不能。4 盘 cold 约 1.44s,warm 约 0.72s | 这主要是 L2 hit/prefill 行为 |
| 05 | `05_phase_spread.png` | 各 phase 的盘差何时出现？ | Phase2/3/4/5 小,Phase7 才拉开 | Phase7 要看 replay,不是 p0 cold |
| 06 | `06_cache_hit_vs_device.png` | L2 hit 与 L3 reload 差别多大？ | v3 replay spread 980ms / 1.59x | BIWIN 走系统盘 page cache,需单独解释 |
| 07 | `07_iostat_timeseries.png` | Phase2 普通测试有没有真读盘？ | NTFS 三盘基本 0 IO | 证明普通 cold/warm 不能选盘 |
| 08 | `08_l3_file_count.png` | L3 是否有文件生成？ | Phase2 只生成小规模 L3 文件 | 有文件不代表 replay 一定从 SSD 读 |
| 09 | `09_decision_radar.png` | 综合评分直观看什么？ | BIWIN 路径综合最强 | 雷达图含人工 price 权重,不能当唯一依据 |
| 10 | `10_multiprompt_modes.png` | p0、p1-p19、replay 各代表什么？ | replay_p0 才是 L3 reload 指标 | p1-p19 均值主要还是 L2 hit |
| 11 | `11_io_pattern_breakdown.png` | replay 的 IO 形态是什么？ | 60-125KB 小块读,util 未满 | 差异更多来自 await/tail |
| 12 | `12_replay_multirun.png` | 单 run 排名可信吗？ | 不可信。Seagate CV 18.1% | 选型要看 mean/stdev/CV |
| 13 | `13_burst_analysis.png` | 读 burst 是否长期打满盘？ | burst 短促,峰值高但不持续 | 不支持“盘带宽被打满”解释 |

## 证据链

### 1. fio 证明硬件上限,但不能直接给 HiCache 排名

图 1-3 显示 raw disk 能力：

| 盘 | 1MB seq1t | 1MB seq4t | 4K rand IOPS | seq4t p99 |
|---|---:|---:|---:|---:|
| BIWIN | 4.77 GB/s | 6.47 GB/s | 23K | 0.141 ms |
| ZHITAI | 3.62 GB/s | 5.92 GB/s | 16K | 0.318 ms |
| Seagate | 3.03 GB/s | 4.58 GB/s | 15K | 0.330 ms |
| WDC | 2.63 GB/s | 4.73 GB/s | 15K | 0.494 ms |

如果 HiCache 是大顺序读,这个排名应高度映射到 replay 延迟。但后面的 iostat 显示实际不是这样。

### 2. 普通 cold/warm 测试主要验证缓存链路,不是 SSD

图 4、5、7、8 共同说明：

- Phase2/4/5 的 TTFT spread 只有毫秒到几十毫秒。
- NTFS 三盘在普通测试中 iostat 基本没有持续读。
- L3 有文件生成,但后续 warm 多数被 host L2 / pinned buffer 接住。
- `drop_caches` 不能清 sglang 自己管理的 L2 host buffer。

因此,普通 cold/warm 图能证明 HiCache 有效,但不能证明哪块 SSD 更适合 L3 reload。

### 3. Phase7 multiprompt 才触发 L2 miss

Phase7 的设计是 20 个 7K token prompts 连续写入,超过 4B 配置下约 41K tokens 的 L2 容量,然后 replay 第一个 prompt。

图 6 和图 10 的核心数据：

| 盘 | p0 cold | p1-p19 mean | replay_p0 | overhead |
|---|---:|---:|---:|---:|
| BIWIN | 1.444s | 1.419s | **1.663s** | 1.15x |
| Seagate | 1.436s | 1.421s | 2.431s | 1.69x |
| ZHITAI | 1.435s | 1.422s | 2.545s | 1.77x |
| WDC | 1.436s | 1.422s | **2.643s** | 1.84x |

v3 单轮结论是：L2 hit 几乎无差,replay_p0 才拉开到 980ms / 1.59x。

BIWIN 需要单独标注。它的 L3 路径在系统盘/root 上,更容易命中 page cache,所以它代表“系统盘 ext4 + page cache 路径”的效果,不是和 NTFS 数据盘完全公平的硬件横评。

### 4. 6 run 证明稳定性才是选型关键

图 12 使用 `results/multiprompt_g_summary.json`。v3 + g1..g5 合计 6 run：

| 盘 | mean | stdev | CV | min | max |
|---|---:|---:|---:|---:|---:|
| BIWIN | **1.620s** | 0.022s | **1.3%** | 1.602 | 1.663 |
| ZHITAI | **2.272s** | 0.174s | 7.7% | 2.058 | 2.545 |
| WDC | 2.651s | 0.159s | 6.0% | 2.446 | 2.902 |
| Seagate | **2.981s** | **0.540s** | **18.1%** | 2.431 | 3.508 |

这修正了单轮 v3 的排序。单轮 v3 看起来 Seagate 比 ZHITAI/WDC 好,但 6 run 后 Seagate 均值最慢、波动最大。NTFS 三盘更可靠的排序是：

```text
ZHITAI < WDC < Seagate
```

### 5. IO 模式说明瓶颈不是峰值带宽

图 11 和图 13 使用 `results/io_pattern_analysis.csv`。跨 6 run 均值：

| 盘 | active read mean | read peak | total read/run | r_await mean | r_await p99 | avg req size | util active |
|---|---:|---:|---:|---:|---:|---:|---:|
| BIWIN | 295 MB/s | 1177 MB/s | 6550 MB | 0.14 ms | 0.33 ms | 53 KB | 23.7% |
| WDC | 270 MB/s | 775 MB/s | 2277 MB | 0.53 ms | 2.29 ms | 96 KB | 32.5% |
| Seagate | 315 MB/s | 649 MB/s | 997 MB | 0.65 ms | 2.07 ms | 113 KB | 38.2% |
| ZHITAI | 278 MB/s | 824 MB/s | 1002 MB | 0.42 ms | 1.06 ms | 98 KB | 19.5% |

关键解释：

- HiCache replay 被拆成约 60-125KB 的读请求,不是 fio 图里的 1MB 顺序读。
- `%util` 没有长期接近 100%,说明盘没有被持续打满。
- NTFS 三盘的 replay 差异主要来自 `r_await` 和 tail,不是带宽峰值。
- ZHITAI 的 await/tail 最好,所以多 run 均值最好。
- Seagate 的平均 active 带宽不低,但 latency 波动大,所以 end-to-end replay 反而最差。

## 结论如何使用

做选型时,优先看图 6、10、11、12、13。图 1-3 是硬件边界,图 4-5 是缓存链路验证,图 7-8 是排除误判的证据。

推荐判断顺序：

1. 先看 Phase7 replay_p0 是否真的触发 L3 reload。
2. 再看多 run mean/stdev/CV,不要按单次最快排序。
3. 然后看 iostat 的 `r_await`, `rareq_sz`, `%util`,判断是盘延迟、文件系统还是 reader 造成。
4. 最后才用 fio 解释硬件上限。

## 复现命令

```bash
source ~/llm/.venv/bin/activate
cd ~/llm/infer/ai_ssd_prestudy

# 生成 IO 汇总 CSV/JSON
python3 scripts/analyze_io_pattern.py

# 生成 01-13 图
python3 scripts/plot_io_data.py
```

如果只改了 `results/hicache_multiprompt_g*/` 或 iostat log,先跑 `analyze_io_pattern.py`,再跑 `plot_io_data.py`。

## 已知限制

1. BIWIN 是系统盘/root 路径,有 page cache 优势,不能和 NTFS 数据盘做完全公平硬件横评。
2. G 多 run 之间没有严格清空所有 OS/page cache 状态,ZHITAI 后续 run 变快可能包含缓存累积。
3. iostat 是 1s 粒度聚合,不能定位单个 bio 的尾延迟。Seagate 慢读需要 bpftrace/perf/smartctl 继续确认。
4. sglang 0.5.13 限制 `hicache-ratio >= 1.0`,且没有暴露清 L2 API,所以 L2 miss 只能靠 multiprompt evict 间接制造。
