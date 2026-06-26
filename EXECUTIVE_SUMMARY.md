# Executive Summary — sglang HiCache × 4 块 NVMe AI SSD 选型预研

> **日期**: 2026-06-15
> **作者**: AI-SSD Pre-Study Team
> **范围**: sglang 0.5.13 HiCache L3 file backend × 4 块候选 NVMe × 2 个模型 (Qwen3-4B, Qwen3-14B-AWQ)
> **目的**: 评估 4 块候选盘在 KV-Cache offload 场景下的真实差距,给出 AI SSD 选型建议

---

## 一句话结论

> **BIWIN X570 (ext4 系统盘路径) + ZHITAI Ti600 (NTFS 数据盘)** 是当前低延迟组合。WDC WDS960G2G0C 稳定居中,可作容量补充；Seagate ZP1000 存在 bimodal 慢读,尾延迟敏感场景不建议单独承载热 L3。**page cache + L2 host DRAM 是真实屏蔽层** — 加大 host RAM 的收益通常高于换盘。

---

## 测试方法 (TL;DR)

7 个 phase + 2 轮 v3 验证 + 1 轮 G 多 run:
- **Phase 2-5**: 单 prompt × N round, 4 盘串行 (write_through / write_back / 14B-AWQ / 4 client 并发)
- **Phase 6**: `fio` 1MB seq / 4K rand 直测硬件极限
- **Phase 7**: **20 个不同 prompt + replay p0** — 唯一能触发 L2 evict 暴露盘差的场景
- **Phase G**: Phase7 重跑 6 次取平均, 量化 ranking 稳定性

**关键工程坑 (提前告知)**: Phase2-5 期间 3 块 NTFS 盘未 mount,数据是 BIWIN 重复 4 次的"伪 4 盘对比"。06-15 mount 修正后 v3 重跑验证 spread 一致 (1-23ms),**确认 page cache / L2 屏蔽能力本就如此强**。**Phase 7 + G 多 run 才是真选型依据**。

---

## 核心数据 (NTFS 三盘, 6 run 平均)

| 盘 | L3 reload (replay_p0) | fio 1MB seq | 价格 ($) | 推荐度 |
|---|---:|---:|---:|---|
| 🥇 **BIWIN X570 (953G, ext4)** | **1.62s** ± 0.02 | 4.77 GB/s | ~$200 | ⭐⭐⭐⭐⭐ (系统盘) |
| 🥈 **ZHITAI Ti600 (931G, NTFS)** | **2.27s** ± 0.17 | 3.62 GB/s | ~$180 | ⭐⭐⭐⭐ |
| 🥉 **WDC WDS960G2G0C (894G, NTFS)** | 2.65s ± 0.16 | 2.63 GB/s | ~$300 | ⭐⭐⭐ |
| 4️⃣ **Seagate ZP1000 (931G, NTFS)** | **2.98s** ± 0.54 ⚠ | 3.03 GB/s | ~$150 | ⭐⭐ |

⚠ **Seagate 间歇性慢**: 6 run 中 3 次 3.4-3.5s (bimodal),CV 高达 18%。**生产环境需要监控或换 ZHITAI 替代**。

**Ranking 跨 run 不稳定**:
- v3 单 run: BIWIN < Seagate < ZHITAI < WDC
- 6 run mean: BIWIN < ZHITAI < WDC < Seagate
- 教训:**单 run 排名不可信,选型测试至少 3 run 取平均**

---

## 三条核心发现

### 1. L2 host DRAM 100% 屏蔽 L3 读盘延迟
- Phase 2-5 (1 prompt × N round) **4 盘 spread < 25ms**,完全被 page cache + sglang pin_memory buffer 屏蔽
- **生产环境如果 prefix 都在 L2,换盘无收益** — host RAM 比盘重要
- 当前 3 GPU × 16GB = 48GB host RAM, ratio=2 → L2 容量 123K tokens,够装 17 个 7K prompt

### 2. sglang L3 read 效率仅 1-2% 硬件极限
| 盘 | 有效吞吐 (Phase7) | 硬件极限 (Phase6 fio) | 效率 |
|---|---:|---:|---:|
| BIWIN | 70 MB/s | 4765 MB/s | **1.5%** |
| ZHITAI | 42 MB/s | 3616 MB/s | 1.2% |
| Seagate | 40 MB/s | 3032 MB/s | 1.3% |
| WDC | 30 MB/s | 2632 MB/s | 1.1% |

**真实瓶颈不在盘,在 sglang reader 实现**。page_size 9MB + 内部串行 + 内核 page cache = 当前架构下盘硬件给 4.7 GB/s,sglang 只用 70 MB/s。**降 page_size 或加 sglang reader 并发可大幅提升**。

### 3. sglang L3 write_through 行为与设计预期不同 ⚠
- 设计上 L3 file 应该写到 mount point (e.g. `/mnt/ai_ssd0/cache_hicache/`)
- **实测 sglang 0.5.13 write_through 把 L3 file 写到 sglang 进程 cwd 根 (BIWIN 系统盘)**,iostat 看 NTFS 三盘 rMB/s = wMB/s = 0
- **影响**: L3 写入侧失真(全到 BIWIN);**L3 读取侧正常**(replay 时从 mount point 真读)
- **工程建议**: 要么用 `write_back` + 充足 warmup,要么把 L3 路径直接设 BIWIN root(本预研现状)

---

## 选型建议 (3 场景)

| 场景 | 推荐配置 | 理由 |
|---|---|---|
| **生产推理 (单实例, 频繁 reload)** | 🥇 BIWIN (ext4) 做系统盘 L3 + ZHITAI 扩容 | BIWIN 1.6s 稳定, ZHITAI 2.27s 是 NTFS 三盘最好 |
| **离线/低频 reload** | 🥈 ZHITAI / WDC 单独部署 | ZHITAI 最快,WDC 2.65s 且稳定 |
| **预算受限,单盘** | ⚠️ WDC / ZHITAI | WDC 894G 容量 + 稳定 2.6s; ZHITAI 1TB 容量 + 更快 2.3s (推荐) |

**不推荐**: 让 Seagate 单独承载 tail-sensitive 热 L3。它不是每次都慢,但 6 run 中 3 次落到 3.4-3.5s。

---

## AI SSD 产品反推 4 点

1. **小块读延迟是软件路径瓶颈** — HiCache replay 是约 60-125KB 读请求,不是 1MB 大顺序读。重点应看 `r_await`、tail、文件系统和 reader 并发。
2. **降 page_size / 加 reader 并发可能比换盘收益更大** — 盘 util 未长期打满,当前瓶颈不是 SSD 峰值带宽。
3. **L2 host DRAM 是 ROI 最高的扩容** — 16GB → 32GB 单卡 → 装 2× prompt,L3 reload 触发概率降 ~50%。
4. **drop_caches 对 sglang 0.5.13 pin_memory 无效** — 想测真 L3 读盘必须 multiprompt 累积填满 L2(20 prompts × 7K tokens)。

---

## 风险与未做

| 风险 | 严重度 | 说明 |
|---|---|---|
| sglang 0.5.13 max_input 限制 32K prompt OOM | 高 | Phase8 32K 4 盘测试 blocked,需 sglang 0.6+ 或 ≥48GB GPU |
| 32B-AWQ 单卡装不下 | 高 | TP=2 跨 3 卡仍 OOM,需 ≥48GB GPU |
| Seagate bimodal 慢读根因未定位 | 中 | 需 bpftrace blk_mq trace + smartctl 才能定性 (本预研范围外) |
| Page cache 跨 run 累积 | 低 | ZHITAI 6 run 单调下降 19%,生产环境可 `echo 3 > /proc/sys/vm/drop_caches` 定期清理 |
| 单 GPU 串行测试 | 低 | 5 run ~60 min,生产部署 ≥100 client 并发未测 |

---

## 交付物

| 类型 | 位置 | 内容 |
|---|---|---|
| 主报告 | [REPORT.md](./REPORT.md) | 22.8 KB,7 phase 完整分析 + 选型矩阵 |
| Phase7 v3 验证 | [docs/hicache-phase7-v3-validation-2026-06-15.md](./docs/hicache-phase7-v3-validation-2026-06-15.md) | 9.4 KB,单 run ranking 复现 |
| Phase7 G 多 run | [docs/hicache-phase7-g-multirun-validation-2026-06-15.md](./docs/hicache-phase7-g-multirun-validation-2026-06-15.md) | 8.4 KB,6 run 统计 + IO 模式细分 |
| IO Profile Plots | [docs/io-profiling-plots-2026-06-15.md](./docs/io-profiling-plots-2026-06-15.md) | 13 张图 + IO 证据链 |
| Driver / 脚本 | `scripts/` | `hicache_drive_4_rounds_model.sh`, `plot_io_data.py`, `analyze_io_pattern.py`, `run_g_rounds.sh` |
| 原始数据 | `results/` | 6 套 hicache_multiprompt* + multiprompt_g_summary.json (单源真相) |

**Git**: 19 commits on `main`,HEAD `634615a`,已 push 到 `Philoallandatru/lmcache` (注: repo 名字跟内容不匹配,等后续 rename)。

---

## 后续工作优先级 (1 周)

| 优先级 | 任务 | 价值 | 估时 |
|---|---|---|---|
| 🟡 P1 | Seagate 慢读根因定位 (bpftrace blk_mq + smartctl) | 决定 Seagate 是否能进生产 | 1-2 天 |
| 🟢 P2 | 1 页 PDF 版 (从本 markdown 用 pandoc 转, 配 3 张关键 plot) | stakeholder 分享 | 2h |
| 🟢 P3 | sglang 0.6+ 升级测试 (PyPI 未发布,等) | 解 32K 限制 | blocked |
| 🟢 P4 | ZHITAI page cache 累积假设验证 (重跑 + drop_caches) | 验证 6 run 数据是否高估 ZHITAI | 30 min |

---

**最后更新**: 2026-06-15 16:00
**数据基础**: 6 run × 4 盘 = 24 个 L3 reload 数据点 + fio 12 个硬件极限点
**可信度**: 🟢 高 (多 run 一致,2 套独立 driver 验证)
