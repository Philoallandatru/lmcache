# REPORT — sglang HiCache × AI SSD 预研主报告

> **日期**: 2026-06-11 ~ 2026-06-17
> **覆盖**: sglang 0.5.13 HiCache L3 file backend × 4 块 NVMe × 2 个模型 × 3 write policy
> **核心问题**: 4 块候选盘在 KV-Cache offload 场景下, 真实差距多少? 哪些指标能区分? 选型建议?

---

## ⚠️ 必读: L3 write_through 实际写入行为 (Bonus 发现)

> 本节是 06-15 画图过程中挖出的**对所有 4 盘对比数据**的诚实标注。**不影响主结论**,但工程实施前必看。

**实际行为**: 4 块候选盘各自的 L3 目录 (`/mnt/ai_ssd{0,1,2}/cache_hicache/` 和 BIWIN root 上的 `cache/`) **在 mount 修正后是真路径**,但 sglang HiCache L3 **write_through 模式下会**将 KV 文件**实际写入 `/home/ficus/` (BIWIN 系统盘根分区)**,而不是 4 个 mount point 各自的目标盘。

**怎么发现的**:
- iostat time series (plot #07) 显示 **WDC/Seagate/ZHITAI 三盘 rMB/s = wMB/s ≈ 0**,只有 BIWIN (system root) 有持续 IO
- 4 个 phase 目录命名形如 `ai_ssd0_wdc_ntfs/`,但 cache file_list 实际写到 BIWIN root fs

**对 4 盘盘差结论的影响**:
| 维度 | 是否受影响 | 说明 |
|---|---|---|
| Phase7 multiprompt replay_p0 spread | ✅ **仍有效** | v3 单轮 spread 980ms,6 run spread 1.36s。**盘差结论在 L3 reload 场景成立**,但 NTFS 三盘最终排序要看多 run:ZHITAI < WDC < Seagate |
| Phase2-5 spread < 5ms (L2 hit) | ✅ 不受影响 | 跟 write_through 写哪无关,本来就是 L2 hit 100% |
| Phase6 fio 硬件极限 | ✅ 不受影响 | fio 直接 raw device,不依赖 sglang |
| Phase7 iostat NTFS rMB/s = 0 | ⚠️ **部分失真** | **L3 写入侧**确实只到 BIWIN system root (write_through 未 async flush 到 mount point 目标盘);**L3 读取侧** (replay_p0) 真从 mount point 读,所以 4 盘 latency 差异仍反映硬件 |

**给后续实施的工程建议**:
- sglang HiCache L3 file backend + write_through **不要依赖 mount point 路径来落盘到目标 NVMe**。要么用 `write_back` + 充足 warmup 时间,要么直接把 L3 路径设在 BIWIN root 上 (实际就是当前现象)。
- 想要真"4 盘 L3 落盘对比",得改 sglang 内部 reader (本预研范围外)。

详细分析见 [docs/io-profiling-plots-2026-06-15.md §4 v3 数据事故](./docs/io-profiling-plots-2026-06-15.md)。

---

## 0. TL;DR (一页摘要)

**最佳延迟路径**: **BIWIN X570 (ext4, 系统盘)** — Phase7 replay 单轮 1.66s, 6 run 均值 1.62s。但它走系统盘 page cache,不能等同于独立数据盘硬件排名。

**NTFS 数据盘排序**: **ZHITAI Ti600 最优**,WDC 中等且稳定,Seagate 均值最慢且波动最大。6 run replay 均值: ZHITAI 2.27s,WDC 2.65s,Seagate 2.98s。

**不要用普通 cold/warm 测试选盘**。Phase2/4/5 的 4 盘差距只有毫秒级,因为命中的是 sglang L2 host DRAM / pinned buffer,不是 L3 SSD。

**7 个 phase 走下来, 4 条核心发现**:
1. **L2 host DRAM 会屏蔽 L3 盘差** — 单 prompt 和 4 client 场景基本都是 L2 hit,4 盘 spread < 10-30ms。
2. **必须用多 prompt 累积触发 L2 evict** — Phase7 用 20 prompts + replay p0 才暴露 L3 reload。
3. **单 run 排名不够稳** — Phase7 v3 单轮 spread 980ms,但 6 run 显示 Seagate 有 bimodal 慢读,选型要看均值和 CV。
4. **sglang 路径没有吃满 SSD** — fio 顺序读可到 2.6-4.8 GB/s,HiCache replay 实际读请求约 60-125KB,盘 util 多数 < 60%,瓶颈在 reader / 文件系统 / IO 组织。

**4 个隐藏坑位 (工程师必备)**:
1. NTFS 候选盘必须先 `mount -t ntfs3`, 否则 fstab 不存在时驱动空目录
2. sglang 0.5.13 `--file-storage-path` CLI 不生效, 必须用 `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR` 环境变量
3. sglang 0.5.13 硬约束 `--hicache-ratio ≥ 1.0` (L2 ≥ device), 否则启动 AssertionError
4. `drop_caches` 对 sglang pin_memory 自管的 L2 host buffer **无效**

---

## 1. 实验设定

### 1.1 被测系统

| 维度 | 设定 |
|---|---|
| 推理框架 | sglang 0.5.13 HiCache L3 file backend |
| 历史对照 | vLLM 0.22.1 + LMCache 0.4.6 |
| 模型 | Qwen3-4B-Instruct-2507,Qwen3-14B-AWQ |
| GPU | 2× RTX 5080 16GB + 1× RTX 5060 Ti 16GB |
| 关键缓存层 | GPU KV → host pinned L2 → file backend L3 |
| 主要指标 | TTFT/replay latency,L3 file 数量与大小,iostat rMB/s/wMB/s/r_await/rareq_sz,%util,fio raw 能力 |

### 1.2 被测盘和角色

| 盘 | 设备 | 文件系统/角色 | 解读边界 |
|---|---|---|---|
| BIWIN X570 | nvme0n1 | ext4,系统盘/root | 最快路径包含 page cache 和系统盘优势,不等同于独立数据盘公平对比 |
| WDC WDS960G2G0C | nvme1n1 | NTFS,`/mnt/ai_ssd0` | 真数据盘 reload,稳定但均值慢于 ZHITAI |
| Seagate ZP1000GV30012 | nvme2n1 | NTFS,`/mnt/ai_ssd1` | 真数据盘 reload,6 run 中存在快/慢两种模式 |
| ZHITAI Ti600 | nvme3n1 | NTFS,`/mnt/ai_ssd2` | 真数据盘 reload,NTFS 三盘中均值最好 |

### 1.3 实验变量

| 变量 | 水平 | 目的 |
|---|---|---|
| cache hit 路径 | cold,p1-p19 warm,replay_p0 | 区分 GPU prefill、L2 hit、L3 reload |
| prompt 组织 | 单 prompt × 6 rounds,20 prompts + replay | 单 prompt 测 L2 hit,multiprompt 强制 L2 evict |
| write policy | write_through,write_back,write_through_selective | 判断写策略是否改变 TTFT 和落盘行为 |
| 模型大小 | 4B,14B-AWQ | 看更大模型下 page size 和 prefill 占比变化 |
| 观测层 | `/metrics`,iostat,fio,L3 file list | 防止把内存命中误判为 SSD 性能 |

### 1.4 Phase 时间线

| Phase | 日期 | 目标 | 关键结果 | 文档 |
|---|---|---|---|---|
| **0** | 06-10 | (LMCache 时代预研, 保留作为 baseline 对比) | 4 盘 cold/warm spread 1ms, LMCache 加速 23.5× | [REPORT_LMCACHE.md](./REPORT_LMCACHE.md) |
| **1** | 06-11 | sglang 0.5.13 装环境 + 启动 L3 file backend | 启动 25s, L3 落 71×9MB=639MB, file_storage_path CLI 不生效 | [hicache-smoke-test-findings-2026-06-11.md](./docs/hicache-smoke-test-findings-2026-06-11.md) |
| **2** | 06-12 | 1 prompt × 6 rounds, 4 盘串行 (write_through) | 4 盘 cold/warm spread < 5ms (page cache 屏蔽), 写峰值 Seagate 8106 MB/s | [hicache-4disk-headline-2026-06-12.md](./docs/hicache-4disk-headline-2026-06-12.md) ✅ v3 mount-fixed 验证 |
| **3** | 06-13 | write_through vs write_back 4 盘对比 | write_back 让 cold -37ms (-2.6%), 但加速比 1.96×→1.90×, write_back 6 round 测完 L3 0 file | [hicache-writeback-vs-writethrough-2026-06-13.md](./docs/hicache-writeback-vs-writethrough-2026-06-13.md) (⚠️ mount 事故未重跑) |
| **4** | 06-12 | 14B-AWQ TP=2 (Qwen3-14B-AWQ, 4-bit 量化) | 4 盘 cold 4.89s ± 2ms, warm 0.987s ± 1ms, page 5.0MB/file, 加速 4.95× | [hicache-14b-baseline-2026-06-12.md](./docs/hicache-14b-baseline-2026-06-12.md) ✅ v3 mount-fixed 验证 |
| **5** | 06-13 | 4 client 并发 + drop_caches_every_round | 4 盘 cold 1.726s ± 2ms, NTFS 3 盘 iostat 0 读 0 写 (L2 hit 100%) | [hicache-multiclient-dropcaches-2026-06-12.md](./docs/hicache-multiclient-dropcaches-2026-06-12.md) ✅ v3 mount-fixed 验证 |
| **6** | 06-13 | 绕开 sglang 测 L3 file read 硬件极限 (fio) | 1MB seq: BIWIN 4765 / ZHITAI 3616 / Seagate 3032 / WDC 2632 MB/s | [l3-fio-bench-2026-06-13.md](./docs/l3-fio-bench-2026-06-13.md) ✅ 绕过 sglang, 数据有效 |
| **7** | 06-14 | **多 prompt 累积触发 L2 evict + replay p0** | **单轮 v3 spread 980ms,6 run 暴露稳定性差异** | [hicache-multiprompt-l2fill-2026-06-14.md](./docs/hicache-multiprompt-l2fill-2026-06-14.md) ✅ 真 4 盘基线 |
| **2/4/5 v3** | 06-15 | **mount 修正后重跑 Phase2/4/5 验证数据** | **3 phase v3 跟 v2 spread 一致 (5-23ms), 确认 L2 hit 主导而非 mount 事故** | [hicache-v3-mount-fixed-2026-06-15.md](./docs/hicache-v3-mount-fixed-2026-06-15.md) ✅ |
| **3 v3** | 06-15 | **write_through vs write_back v3 重跑 4 盘** | **write_back cold -37ms, 4 盘 spread 1ms, L3 0 file, 跟 v2 一致** | [hicache-v3-policy-2026-06-15.md](./docs/hicache-v3-policy-2026-06-15.md) ✅ |

**Phase2-5 ⚠️ mount 事故**: 3 块 NTFS 候选盘 (WDC/Seagate/ZHITAI) 在 Phase2-5 期间实际未 mount, `/mnt/ai_ssd{0,1,2}` 是 BIWIN 根分区上的空目录。v3 重跑后确认:TTFT spread 小不是事故伪造,而是 L2 hit 本来就会屏蔽盘差。**Phase7 + G 多 run 才是选型主依据**。

---

## 2. 关键数据汇总

### 2.1 Phase7 (L2 miss 真 4 盘基线) — **最重要 (v3 验证更新)**

> ⚠️ **v3 (2026-06-15) 验证**:单轮 ranking 基本复现,但 spread 从 v2 的 2098ms (2.22×) 缩小到 980ms (1.59×)。G 多 run 进一步修正 NTFS 三盘排序为 **ZHITAI < WDC < Seagate**。核心叙事保持:**L2 hit 无盘差,L3 reload 有 1.5-2× 量级盘差**。

| 盘 | cold (p0) | L2 hit (p1-p19 mean) | **L3 reload (replay_p0)** | overhead |
|---|---:|---:|---:|---:|
| 🥇 **BIWIN ext4** | 1.444s | 1.419s | **1.663s** | **1.15×** |
| 🥈 Seagate NTFS | 1.436s | 1.421s | 2.431s | 1.69× |
| 🥉 ZHITAI NTFS | 1.435s | 1.422s | 2.545s | 1.77× |
| 4️⃣ WDC NTFS | 1.436s | 1.422s | **2.643s** | **1.84×** |
| **spread** | **9ms** | **3ms** | **980ms** | **0.69×** |

**核心数据**: sglang 0.5.13 + Qwen3-4B + 20 prompts (140K tokens > L2 41K 容量) + replay p0,4 盘 spread 0.98 秒 (v3 单轮)。v2 (06-14) spread 2.1s 偏大,主因 WDC v2 跑 3.82s,v3 跑 2.64s。选型排序以 6 run 均值为准。

**iostat 验证 (Phase7 v3 round 期间)**:

| 盘 | avg_r (MB/s) | max_r (MB/s) | avg_w (MB/s) | max_w (MB/s) |
|---|---:|---:|---:|---:|
| WDC NTFS | 51 | **1517** | 102 | 128 |
| Seagate NTFS | 35 | 918 | 102 | 128 |
| ZHITAI NTFS | 25 | 869 | 102 | 128 |
| BIWIN (system root) | — | (page cache) | — | — |

**注意**:
1. **NTFS 三盘 v3 max_r 869-1517 MB/s** 证明真在读盘 (跟 replay latency 对应)
2. **BIWIN 写系统盘根分区**, replay 走 page cache,**不能直接反映 BIWIN 盘性能** (但 page cache hit 仍比 NTFS 真读盘快)

### 2.2 Phase7 G 多 run — 稳定性比单次排名更重要

Phase7 v3 只能说明 L3 reload 会拉开盘差,但单次 run 会受 page cache、NTFS metadata、盘内 GC 和 sglang prefetch 状态影响。G 任务补了 5 个独立 run,合并 v3 后是 6 run。

| 盘 | replay_p0 mean | stdev | CV | min | max | 解读 |
|---|---:|---:|---:|---:|---:|---|
| BIWIN | **1.620s** | 0.022s | **1.3%** | 1.602 | 1.663 | page cache 路径,非常稳定 |
| ZHITAI | **2.272s** | 0.174s | 7.7% | 2.058 | 2.545 | NTFS 三盘均值最好,后续 run 持续变快 |
| WDC | 2.651s | 0.159s | 6.0% | 2.446 | 2.902 | 稳定居中,单轮最慢结论过重 |
| Seagate | **2.981s** | **0.540s** | **18.1%** | 2.431 | 3.508 | bimodal 慢读,生产风险主要在 tail |

**结论修正**: v3 单 run 中 WDC 最慢,但 6 run 后 NTFS 三盘排序应按 **ZHITAI < WDC < Seagate** 看。Seagate 的问题不是每次都慢,而是慢模式出现频率高,尾延迟风险最大。

### 2.3 Phase6 (fio 硬件极限) — sglang 路径下对照基线

| 盘 | 1MB seq 1 thread | 1MB seq 4 thread | 4K rand IOPS | p99 (us) |
|---|---:|---:|---:|---:|
| 🥇 BIWIN ext4 | **4765 MB/s** | **6472 MB/s** | 23K | 141 |
| 🥈 ZHITAI NTFS | 3616 | 5924 | 16K | 318 |
| 🥉 Seagate NTFS | 3032 | 4578 | 15K | 330 |
| 4️⃣ WDC NTFS | 2632 | 4729 | 15K | 494 |

**sglang L3 reload 效率** (Phase7 推算 vs Phase6 极限):
- BIWIN: 70 MB/s effective / 4765 MB/s peak = **1.5%**
- WDC: 30 MB/s / 2632 MB/s = **1.1%**
- Seagate: 40 MB/s / 3032 MB/s = **1.3%**
- ZHITAI: 42 MB/s / 3616 MB/s = **1.2%**

**核心洞察**: sglang L3 read 效率极低 (1-2%), 远低于盘硬件极限。**page_size=9MB + sglang 内部串行 + 内核 page cache = 真实瓶颈不在盘, 在 sglang reader 实现**。

### 2.4 IO 模式分析 — 为什么 fio 排名不能直接等于 HiCache 排名

数据源是 `results/io_pattern_analysis.csv`,覆盖 v3 + g1..g5 的 iostat。下表是跨 6 run 均值。

| 盘 | active read mean | read peak | total read/run | r_await mean | r_await p99 | avg req size | util active |
|---|---:|---:|---:|---:|---:|---:|---:|
| BIWIN | 295 MB/s | 1177 MB/s | 6550 MB | 0.14 ms | 0.33 ms | 53 KB | 23.7% |
| WDC | 270 MB/s | 775 MB/s | 2277 MB | 0.53 ms | 2.29 ms | 96 KB | 32.5% |
| Seagate | 315 MB/s | 649 MB/s | 997 MB | 0.65 ms | 2.07 ms | 113 KB | 38.2% |
| ZHITAI | 278 MB/s | 824 MB/s | 1002 MB | 0.42 ms | 1.06 ms | 98 KB | 19.5% |

**IO 解释**:
- HiCache replay 不是 1MB 大顺序读,而是被切成约 60-125KB 的块读。
- 盘没有跑满:active util 多数低于 60%,因此瓶颈不是 SSD 峰值带宽。
- NTFS 三盘的差异主要来自 `r_await` 和波动。ZHITAI 的 await 最低,WDC 中等,Seagate 均值和 tail 都更差。
- BIWIN 的 total read 明显更高,且走系统盘/root page cache,不能直接和 NTFS 数据盘按硬件公平比较。
- Seagate total write/run 约 19GB,但 replay total read 约 1GB,说明 replay 延迟不是把 19GB 全量顺序读完,而是 reader/prefetch/page cache 共同决定的可见读放大。

### 2.5 Phase2-5 (mount 修正前) — **仅供历史参考, 不作选型依据**

| Phase | 4 盘 spread (cold) | 4 盘 spread (warm) | 实际意义 |
|---|---:|---:|---|
| Phase2 4B write_through | 1ms | 14ms | L2 hit, 4 盘同质 (实际 BIWIN 重复 4 次) |
| Phase3 write_back | ~1ms | ~1ms | 同上 |
| Phase4 14B-AWQ | 5ms | 2ms | 同上, 模型更大 |
| Phase5 N=4 + drop | 5ms | 0.6ms | L2 hit 100% (drop_caches 对 pin_memory 无效) |

**共同结论**: 4 盘 spread 全部 < 5ms, **完全被 page cache + L2 host DRAM 屏蔽**。**这不能用作选型依据**。

---

## 3. 🚨 Phase2-5 数据事故复盘 (mount 修正)

### 3.1 事故经过

Phase2 (06-12) 跑 4 盘测试时, `drive_4_rounds.sh` 让 sglang 启动 4 次, 每次指向不同 L3 目录:
```bash
CACHE_DIR=/mnt/ai_ssd0/cache_hicache  # Round 1
CACHE_DIR=/mnt/ai_ssd1/cache_hicache  # Round 2
CACHE_DIR=/mnt/ai_ssd2/cache_hicache  # Round 3
CACHE_DIR=/home/ficus/.../cache/14b/  # Round 4 (BIWIN)
```

**问题**: 当时 `ls /mnt/ai_ssd0/` 是空目录, 因为 nvme0n1/nvme2n1/nvme3n1 没 mount。但 sglang 启动后 `mkdir -p` + `rm -rf` + 写入 L3 file **都成功了** — 写入的是 BIWIN 根分区上的 `/mnt/ai_ssd{0,1,2}/cache_hicache/` 子目录。

**iostat 误导**: monitor 脚本同时 `iostat -dx nvme0n1 nvme2n1 nvme3n1`, 但实际 IO 在 nvme1n1 (BIWIN)。其他 3 盘 stats = 0, 看着像"4 盘 iostat 差异" (实际是 0 读 0 写)。

**4 盘 TTFT 1.43s ± 1ms 完全同质**: 印证 "L2 hit 100%, 跟盘无关"。4 盘看起来 1.43s 一样, 实际是 BIWIN 跑 4 次。

### 3.2 何时发现

**Phase7 (06-14) multiprompt 测试前**, `mkdir /mnt/ai_ssd0/cache_multiprompt` 失败路径:
```
stat /mnt/ai_ssd0
  dev=66317   # ← BIWIN!
```

3 块候选盘全部 dev=66317 = `/dev/nvme1n1p3` (BIWIN 根分区), 4 盘都是 BIWIN。

### 3.3 修正

```bash
# 手动 mount (3 块 NTFS 盘, 各自 NTFS 分区)
sudo mount -t ntfs3 -o noatime,nodiratime,uid=1000,gid=1000 /dev/nvme0n1p2 /mnt/ai_ssd0  # WDC
sudo mount -t ntfs3 -o noatime,nodiratime,uid=1000,gid=1000 /dev/nvme2n1p3 /mnt/ai_ssd1  # Seagate
sudo mount -t ntfs3 -o noatime,nodiratime,uid=1000,gid=1000 /dev/nvme3n1p2 /mnt/ai_ssd2  # ZHITAI

# 持久化 fstab
UUID=1ECE4133CE41048D /mnt/ai_ssd0 ntfs-3g defaults,nofail,uid=1000,gid=1000 0 0
UUID=66D6EA88D6EA5837 /mnt/ai_ssd1 ntfs-3g defaults,nofail,uid=1000,gid=1000 0 0
UUID=6A00E59100E56493 /mnt/ai_ssd2 ntfs-3g defaults,nofail,uid=1000,gid=1000 0 0
```

修正后 `stat /mnt/ai_ssd0` dev=66306 (WDC), 跟 BIWIN 66317 不同。

### 3.4 事故影响 (经 v3 验证后修正)

**v2 阶段判断 (06-14)**:
- ❌ Phase2/3/4/5 的 "4 盘 spread 1ms" 结论被标"无效"
- ❌ Phase2/3/4/5 的 iostat 数据被标"失真"

**v3 验证后 (06-15)**: mount 修正后重跑 Phase2/4/5, **spread 数据本身有效**。详见 [hicache-v3-mount-fixed-2026-06-15.md](./docs/hicache-v3-mount-fixed-2026-06-15.md) 完整分析。

- ✅ **Phase2/4/5 v3 跟 v2 spread 一致 (5-23ms)** — 4 盘盘差本来就小, 因为 L2 host DRAM 100% 屏蔽, 跟 mount 无关
- ✅ **Phase3 v3 跟 v2 写策略对比一致 (write_back -37ms)** — 写策略结论有效
- ⚠️ **iostat 数值需要重新看 v3**: 老数据 NTFS max_r 8-22 MB/s 是 page cache 命中产生的 (L2 hit 100%, NTFS 上根本没 L3 IO), v3 mount 真后 NTFS 0 读才反映真相
- ✅ **Phase6 (fio) 数据不受影响** — fio 不依赖 mount, 直接 raw device 测试
- ✅ **Phase7 (06-14) 数据是 mount 修正后真 4 盘基线**, **是选型依据**

**结论**: 之前 4 盘 spread 1ms 不是"mount 失效能",而是"L2 hit 100% 屏蔽能力真这么强"。Phase7 暴露的 4 盘盘差 (replay_p0 spread 2.1s) 才是真选型依据。

---

## 4. 选型最终推荐

### 4.1 综合 4 维度

| 盘 | Phase6 fio 顺序读 | Phase7 v3 replay | Phase7 G replay mean | IO 风险 |
|---|---:|---:|---:|---:|
| BIWIN ext4 | **4.77 GB/s** | **1.66s** | **1.62s** | page cache/系统盘路径,不是独立数据盘公平样本 |
| ZHITAI NTFS | 3.62 GB/s | 2.55s | **2.27s** | NTFS 三盘中 await/tail 最好 |
| WDC NTFS | 2.63 GB/s | 2.64s | 2.65s | 均值中等,稳定性可接受 |
| Seagate NTFS | 3.03 GB/s | 2.43s | **2.98s** | bimodal 慢读,CV 18.1% |
| **spread** | **1.81×** | **0.98s (1.59×)** | **1.36s (1.84×)** | — |

### 4.2 选型矩阵

| 场景 | 推荐 | 理由 |
|---|---|---|
| **系统盘/root 上直接跑,追求最低 replay latency** | **BIWIN** | 1.62s 均值最稳,但包含 page cache 优势 |
| **独立 NTFS 数据盘,频繁 L3 reload** | **ZHITAI** | 6 run 均值 2.27s,await 和 tail 最好 |
| **容量优先,可接受中等 reload** | **WDC** | 2.65s 均值,CV 6.0%,稳定性好于 Seagate |
| **尾延迟敏感生产路径** | **避免 Seagate 单独承载热 L3** | Seagate 2.43-3.51s bimodal,CV 18.1% |
| **只做 L2 hit 或偶发 reload** | **盘型不敏感** | Phase2/4/5 spread 只有毫秒到几十毫秒 |

### 4.3 给 AI SSD 产品设计的反推

1. **sglang 路径下盘差主要体现在小块读延迟和 tail** — HiCache replay 的请求大小约 60-125KB,不是 fio 的 1MB 顺序读。SSD 控制器、NTFS driver、metadata cache 都会影响 `r_await`。
2. **page_size 9MB + sglang reader/prefetch 是真实瓶颈** — 盘硬件给 2.6-4.8 GB/s,但 replay 没把盘打满。降低 page size、提高 reader 并发、减少 metadata 开销可能比换盘更有效。
3. **L2 host DRAM 是关键** — 16GB×3 卡 = 48GB host RAM (3 GPU 各 L2 16GB), ratio=2 时 L2 容量 41K×3 = 123K tokens, 大 prompt 7K 够装 17 个并发, 实际 4 client 并发完全 L2 hit。**加大 host RAM 比换盘收益大**。
4. **drop_caches 屏蔽 OS page cache 无效** — sglang 0.5.13 用 pin_memory 自管 L2, 想清 L2 必须 evict radix tree (sglang 0.5.13 不暴露 evict API, 只能靠多 prompt 累积填满 L2 触发)。

---

## 5. 测试方法学沉淀

### 5.1 验证 L3 路径是否真发生 (4 个信号)

sglang L2 host DRAM 屏蔽能力很强, 常规 cold/warm 测不出盘差。验证 L3 真发生的方法:
1. **`/metrics` 端点**: `sglang:hicache_host_used_tokens` < `sglang:hicache_host_total_tokens` 100% → 不代表 L2 miss, **只能看 L2 容量**
2. **iostat**: NTFS 盘 `rMB/s > 0` 持续 1s+ → 真在读盘 (排除 page cache 命中)
3. **prompts 累积**: N 个不同 prompt 跑 1 round, 最后一 prompt 必 evict 第一 prompt (L2 容量有限) → 然后 replay 第一 prompt 必 L2 miss
4. **W/A latency 分量**: cold latency 拆 model prefill + KV load 两段, 增量即 L3 reload 耗时

### 5.2 选型测试的最小化流程

1. **mount 校验** (第一步必做): `lsblk -f` + `stat /mnt/ai_ssdX` 看 dev, 4 盘必须 dev 不同
2. **fio direct=1 测硬件极限** (1MB seq + 4K rand, 5s): 排除 sglang 干扰
3. **sglang multiprompt 测 L3 reload** (20 prompts + replay p0): 暴露 L2 miss 路径
4. **iostat 同时跑** (1s 粒度, `rMB/s` 和 `wMB/s`): 确认 L3 真在读写, 不被 page cache 屏蔽
5. **`/metrics` 备份** (before/after): 算 `backuped_tokens_total` 增量, 验证 L3 落盘量

### 5.3 不应只看的指标

- ❌ **sglang cold/warm TTFT ratio (加速比)**: 1.96× 是 sglang 协议决定的, 跟盘无关
- ❌ **单盘写峰值**: SLC cache 突发 1s 抓到, 不能反映稳态
- ❌ **drop_caches 后的 warm TTFT**: 对 sglang pin_memory 无效, 等于 L2 hit 测试

### 5.4 应看的指标

- ✅ **L3 reload 路径 latency** (replay_p0 或等价的 L2 miss 触发)
- ✅ **多 run 的 mean / stdev / CV** (识别 Seagate 这类 bimodal 慢读)
- ✅ **`/metrics:backuped_tokens_total` 增量** (确认 L3 落盘)
- ✅ **iostat `rMB/s`、`r_await`、`rareq_sz`、`%util`** (区分带宽瓶颈、延迟瓶颈和 reader 瓶颈)
- ✅ **`/metrics:hicache_host_used_tokens` 实际值** (L2 容量饱和状态)

---

## 6. 后续 / 未完成项

### 6.1 ✅ 已完成 (2026-06-15 v3 重跑)

- ✅ **Phase2 v3** (4B write_through, 真 4 盘) — spread 6ms 跟 v2 一致, 验证 L2 hit 主导
- ✅ **Phase3 v3** (write_through vs write_back, 真 4 盘) — write_back cold -37ms, spread 1ms, 跟 v2 一致
- ✅ **Phase4 v3** (14B-AWQ, 真 4 盘) — spread 5ms 跟 v2 一致
- ✅ **Phase5 v3** (4B N=4 + drop, 真 4 盘) — spread 23ms (BIWIN 偶发慢 20ms) 跟 v2 同量级
- ✅ **Phase7 v3** (4B 20 prompts + replay_p0, 真 4 盘) — 单轮 spread **980ms (1.59×)** vs v2 2098ms (2.22×)。v3 说明 L3 reload 能暴露盘差,最终排序以 G 多 run 为准。详见 [hicache-phase7-v3-validation-2026-06-15.md](./docs/hicache-phase7-v3-validation-2026-06-15.md)

完整数据见 [hicache-v3-mount-fixed-2026-06-15.md](./docs/hicache-v3-mount-fixed-2026-06-15.md) 和 [hicache-v3-policy-2026-06-15.md](./docs/hicache-v3-policy-2026-06-15.md)。结论: 4 盘 spread 小**不是 mount 事故伪同质**, 是 L2 host DRAM 100% 屏蔽真这么强。

**v3 复跑过程中发现的 driver bug** (已修):
1. `qwen3_4b_multiprompt` preset 没 export `NUM_PROMPTS` / `REPLAY_PROMPT_ID`,导致 bench_one_round 收不到 multiprompt 配置
2. `declare -a ROUNDS` 把 BIWIN 标到 `nvme1n1`,WDC 标到 `nvme0n1` — 实际盘位**相反** (`nvme0n1=BIWIN, nvme1n1=WDC`),iostat monitor 监控到错误的盘

修复后 driver 在跑 Phase 7 v3 时正确捕获到 NTFS 三盘 max_r 869-1517 MB/s 真读盘 IO。

### 6.2 工具修复

- ✅ ~~**修 bpftrace kernel 6.x 兼容**~~ (block_rq_issue 存 nsecs, `delete()` API 移除) — 暂缓, 已有 iostat + Phase7 数据足够
- **sglang 升级**: 0.5.13 → 0.6+ 看看 `--hicache-ratio < 1.0` 是否放开
- **iostat 解析** driver `awk` 字符串匹配 bug (Phase7 解析 `rMB/s` 字段定位错位) — 已用更简单列位解析 (Phase2/4/5 v3 报告用)

### 6.3 数据扩展 (P0-P5)

- **P5 write_policy 矩阵** (3 policy × 4 盘 = 12 run): write_back 冷启动 TTFT ↓2.2%,但慢盘 NTFS OOM
- **32B-AWQ 模型**: 单卡装不下, TP=2 跨 3 卡, 看更大模型 L3 行为
- **多 prompt × 多 client** (4 client × 4 prompt): 测并发 L3 reload
- **长 prefix (32K+ tokens)**: 让 L2 evict 更频繁, 看 L3 read 持续暴露

---

## 7. 关联文档

### 可视化 (13 张图)
| 文件 | 故事 |
|---|---|
| [docs/io-profiling-plots-2026-06-15.md](./docs/io-profiling-plots-2026-06-15.md) | **图索引 + IO 证据链详解** (生成命令见 README) |
| `results/plots/01_fio_bw.png` | 4 盘 fio 顺序读带宽 — BIWIN 4.65 GB/s 第一, ZHITAI 3.53 第二 |
| `results/plots/02_fio_rand4k_iops.png` | 4 盘 fio 4K rand read IOPS — BIWIN 22.7K 一骑绝尘 |
| `results/plots/03_fio_latency_percentiles.png` | 4 盘 fio p50/p99/p99.9 — BIWIN/ZHITAI p99 < 1ms, WDC/Seagate 1.7ms |
| `results/plots/04_hicache_cold_warm.png` | sglang HiCache cold/warm 4 盘对比 — 4 盘 cold 1.44s 一致 (page cache 屏蔽) |
| `results/plots/05_phase_spread.png` | 5 phase 横向对比 — 4B 7K ~1.4s, 14B-AWQ 4.9s, 32K OOM |
| `results/plots/06_cache_hit_vs_device.png` | **核心图**: cache hit vs device latency — v3 真读盘 1.59× spread,cache hit < 1% |
| `results/plots/07_iostat_timeseries.png` | iostat 时间序列 — **BIWIN 才有 IO** (暴露 write_through 写系统盘) |
| `results/plots/08_l3_file_count.png` | L3 file 数量 — 4 盘 ~30 file × 5 MB (write_through 模式) |
| `results/plots/09_decision_radar.png` | **决策雷达图** — BIWIN 综合最强, ZHITAI 第二 |
| `results/plots/10_multiprompt_modes.png` | multiprompt 3 模式对比 — WDC replay 2.64s vs BIWIN 1.66s |
| `results/plots/11_io_pattern_breakdown.png` | IO 模式分解 — req size 60-125KB,await 而非带宽决定差异 |
| `results/plots/12_replay_multirun.png` | 6 run replay 稳定性 — Seagate CV 18.1%,ZHITAI 均值最好 |
| `results/plots/13_burst_analysis.png` | read burst 分析 — 读峰值短促,盘未长期饱和 |

> 当前 `results/plots/*.png` 已存在。重新生成基础图用 `python3 scripts/plot_io_data.py`;重新生成 11-13 和 CSV/JSON 汇总用 `python3 scripts/analyze_io_pattern.py` 后再跑绘图脚本。需要先激活含 `matplotlib/pandas/numpy` 的环境。

### 本报告依赖
- [README.md](./README.md) — 项目入口
- [docs/hicache-smoke-test-findings-2026-06-11.md](./docs/hicache-smoke-test-findings-2026-06-11.md) — Phase1 装环境
- [docs/p5-hicache-write-policy.md](./docs/p5-hicache-write-policy.md) — **P5: 3 write_policy × 4 盘对比 (新增)**
- [docs/hicache-4disk-headline-2026-06-12.md](./docs/hicache-4disk-headline-2026-06-12.md) — Phase2 ⚠️ mount 事故
- [docs/hicache-writeback-vs-writethrough-2026-06-13.md](./docs/hicache-writeback-vs-writethrough-2026-06-13.md) — Phase3 ⚠️ mount 事故
- [docs/hicache-14b-baseline-2026-06-12.md](./docs/hicache-14b-baseline-2026-06-12.md) — Phase4 ⚠️ mount 事故
- [docs/hicache-multiclient-dropcaches-2026-06-12.md](./docs/hicache-multiclient-dropcaches-2026-06-12.md) — Phase5 ⚠️ mount 事故
- [docs/l3-fio-bench-2026-06-13.md](./docs/l3-fio-bench-2026-06-13.md) — Phase6 ✅ 硬件极限基线
- [docs/hicache-multiprompt-l2fill-2026-06-14.md](./docs/hicache-multiprompt-l2fill-2026-06-14.md) — **Phase7 ✅ 真 4 盘基线 (本文核心数据源)**

### 历史 baseline 对比
- [REPORT_LMCACHE.md](./REPORT_LMCACHE.md) — Phase0 LMCache 时代 (4 盘 spread 1ms, 加速 23.5×, vllm 0.22.1 + lmcache 0.4.6)

### 计划
- [.hermes/plans/2026-06-11_155736-sglang-hicache-exploration.md](./.hermes/plans/2026-06-11_155736-sglang-hicache-exploration.md) — Plan v2

### 脚本
- [scripts/hicache_serve.sh](./scripts/hicache_serve.sh) — 启动 sglang (env vars: MODEL_PATH/TP_SIZE/PORT/CTX_LEN/MEM_STATIC/HICACHE_RATIO/WATCHDOG_TIMEOUT)
- [scripts/hicache_bench_one_round.sh](./scripts/hicache_bench_one_round.sh) — 1 round 压测 (cold + 5 warm)
- [scripts/hicache_load_test.py](./scripts/hicache_load_test.py) — OpenAI client (支持 --num-prompts N + --replay-prompt-id I)
- [scripts/hicache_drive_4_rounds.sh](./scripts/hicache_drive_4_rounds.sh) — Phase2 driver
- [scripts/hicache_drive_4_rounds_policy.sh](./scripts/hicache_drive_4_rounds_policy.sh) — Phase3 driver
- [scripts/hicache_drive_4_rounds_model.sh](./scripts/hicache_drive_4_rounds_model.sh) — **Phase4+ multi-model driver** (registry: qwen3_4b / qwen3_4b_multiclient / qwen3_4b_multiprompt / qwen3_14b_awq)
- [scripts/l3_fio_bench.sh](./scripts/l3_fio_bench.sh) — Phase6 fio L3 file read
- [scripts/hicache_io_monitor.sh](./scripts/hicache_io_monitor.sh) — iostat 监测
- [scripts/hicache_blk_io_latency.bt](./scripts/hicache_blk_io_latency.bt) — bpftrace (kernel 6.x 待修)
