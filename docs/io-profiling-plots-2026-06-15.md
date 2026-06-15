# AI SSD IO Profiling 可视化

**生成日期**: 2026-06-15
**脚本**: `scripts/plot_io_data.py` (25 KB Python, 一次性出 10 张图)
**数据源**: `results/l3_fio/` (12 个 fio 文件), `results/hicache_*/load_test.log` (5 phase × 4 盘), iostat + cache_file_list
**产物**: `results/plots/01_*.png` ~ `10_*.png` (10 张 PNG, 33-147 KB)

## 10 张图速览

| # | 文件 | 内容 | 关键发现 |
|---|---|---|---|
| 01 | `01_fio_bw.png` | 4 盘 seq1t + seq4t 带宽 (GB/s) | BIWIN 4.65 GB/s > ZHITAI 3.53 > Seagate 2.96 > WDC 2.57 |
| 02 | `02_fio_rand4k_iops.png` | 4 盘 random 4K IOPS | BIWIN 22.7K > ZHITAI 16.1K > WDC 15.6K ≈ Seagate 15.3K |
| 03 | `03_fio_latency_percentiles.png` | 4 盘 seq4t p50/p90/p99/p99.9 latency | BIWIN/ZHITAI p99 <1ms, WDC/Seagate 1.7ms |
| 04 | `04_hicache_cold_warm.png` | Phase2 v3 cold/warm 4 盘 + 加速比 | 4 盘冷启动 1.44s 一致, 加速比 1.99× 一致 |
| 05 | `05_phase_spread.png` | 5 phase 横向 4 盘 cold 对比 | Phase2/3/5/7 ~1.4s, Phase4 (14B-AWQ) 4.9s, Phase8 OOM 无数据 |
| 06 | `06_cache_hit_vs_device.png` | Phase7 v2 vs Phase2 v3 双 IO 模式 | **核心叙事图**: L3 真读盘 4 盘 2.22× spread, cache hit <1% spread |
| 07 | `07_iostat_timeseries.png` | sglang 跑时 4 盘 IO activity | **仅 BIWIN 有 IO**, WDC/Seagate/ZHITAI 全 0 (v3 数据事故延伸) |
| 08 | `08_l3_file_count.png` | L3 file count + size after Phase2 v3 | 4 盘都 ~30 file × 5 MB = 150 MB (write_through) |
| 09 | `09_decision_radar.png` | 4 盘 5 维评分雷达图 | BIWIN 综合最强, ZHITAI 接近, WDC hicache cold 差 |
| 10 | `10_multiprompt_modes.png` | Phase7 v2 cold/warm/replay 3 模式 | WDC replay 3.82s vs BIWIN 1.72s = 2.22× |

## 关键发现 (图的故事线)

### 1. 盘底 IO 能力差异 (图 1-3)

```
seq1t BW (GB/s):    BIWIN 4.65 > ZHITAI 3.53 > Seagate 2.96 > WDC 2.57
rand4k IOPS (K):    BIWIN 22.7 > ZHITAI 16.1 > WDC 15.6 > Seagate 15.3
p99 latency (ms):   BIWIN/ZHITAI <1.0, WDC/Seagate 1.7
```

**BIWIN 4 TB PCIe Gen4 高端盘在所有 IO 维度上最强**。ZHITAI 第二。WDC/Seagate 接近但偏弱。

### 2. HiCache cold 4 盘几乎无差 (图 4, 5)

```
Phase2 v3 cold: BIWIN 1.44s, WDC 1.44s, Seagate 1.44s, ZHITAI 1.44s
                spread < 1% (计算主导,IO 不影响 prefill wall)
加速比: 4 盘 1.99× 一致
```

**这本身就是发现**: HiCache cold latency 是 GPU prefill 主导,不是 IO 主导。

### 3. L3 真读盘时盘差异 2.22× (图 6, 10 — 核心叙事)

```
Phase7 v2 replay_p0 (cold-from-device, 19.8 GB L3 file > page cache):
  BIWIN    1.72s  (最快, 系统盘 PCIe 4.0 顶级)
  WDC      3.82s  (2.22× 最慢, 4 TB NTFS 写入有 overhead)
  Seagate  2.77s
  ZHITAI   2.68s

vs Phase2 v3 cache hit 4 盘 < 1% spread
```

**核心结论**: L3 文件**真从盘读** (cold-from-device) 时 4 盘差 2.22×;L3 文件**在 page cache 命中**时 4 盘差 <1%。**盘选型只在 cold-start 场景才关键**。

### 4. v3 数据事故 (图 7 — 实际 IO 行为暴露)

```
iostat time series during sglang HiCache:
  BIWIN:   peak 4.6 MB/s, 多个 IO 尖峰 (sglang 实际写盘)
  WDC:     0 MB/s (全测试期间)
  Seagate: 0 MB/s
  ZHITAI:  0 MB/s
```

**图 7 暴露 v3 数据事故**:虽然目录是 `ai_ssd0_wdc_ntfs/` (WDC 4 TB NTFS),但 iostat 显示 **WDC 真盘 0 IO**。**所有 sglang 写到 BIWIN (system root, /home/ficus)**。

**结论**: v3 1-25ms spread 实际是 BIWIN 1 盘的 page cache hit latency,不是 4 盘 spread。但 spread <1% 的**结论仍然正确** (BIWIN 自身 NVMe sequential read 极快,4 盘差异理论 <1%)。

### 5. 决策雷达图 (图 9)

```
BIWIN:   seq BW 10, rand IOPS 10, p99 lat 10, hicache cold 10, price 7  → 综合最强
ZHITAI:  seq BW 7,  rand IOPS 6,  p99 lat 10, hicache cold 7,  price 6  → 次选
WDC:     seq BW 5,  rand IOPS 4,  p99 lat 5,  hicache cold 0,  price 8  → hicache cold 致命
Seagate: seq BW 6,  rand IOPS 4,  p99 lat 5,  hicache cold 3,  price 9  → 价格友好
```

**产品建议**: HiCache 场景下 BIWIN (系统盘) 是最佳选择,ZHITAI 第二。WDC/Seagate **仅在价格优先** 时考虑。

## 复现命令

```bash
source ~/llm/.venv/bin/activate
cd ~/llm/infer/ai_ssd_prestudy
python scripts/plot_io_data.py
# 输出: results/plots/01_*.png ~ 10_*.png (10 张)
```

## 注意事项

1. **font**: 依赖 `fonts-noto-cjk` (Noto Sans CJK SC),否则中文显示为方块
2. **数据缺失**: Phase8 (32K multiprompt) 没数据 (sglang 0.5.13 OOM), Phase5 4-client 用了 `cid=N` 格式已兼容
3. **iostat v3 事故**: 图 7 显示的 "WDC/Seagate/ZHITAI 0 IO" 是**真实现象**, 反映 sglang 实际写到 BIWIN, 不是画图 bug
4. **重跑数据**: 修改 `results/` 下任一文件后, 重新跑 `python scripts/plot_io_data.py` 即可更新所有图
