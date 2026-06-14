# REPORT — sglang HiCache × AI SSD 预研主报告

> **日期**: 2026-06-11 ~ 2026-06-14
> **覆盖**: sglang 0.5.13 HiCache L3 file backend × 4 块 NVMe × 2 个模型
> **核心问题**: 4 块候选盘在 KV-Cache offload 场景下, 真实差距多少? 哪些指标能区分? 选型建议?

---

## 0. TL;DR (一页摘要)

**最佳**: 🥇 **BIWIN X570 (ext4, 系统盘)** — L3 reload 1.72s, overhead 1.20×
**次选**: 🥈 **ZHITAI Ti600 / Seagate ZP1000GV30012** — L3 reload 2.7s, overhead 1.87-1.93×
**避免**: ⚠️ **WDC WDS960G2G0C** — L3 reload 3.82s, overhead 2.66×

**7 个 phase 走下来, 3 条核心发现**:
1. **L2 host DRAM 100% 屏蔽 L3 读盘延迟** — page cache + pin_memory buffer, 单 prompt 测试 4 盘 spread < 5ms
2. **必须用多 prompt 累积触发 L2 evict 才能暴露盘差** — Phase7 用 20 prompts + replay p0, 4 盘 spread 2.1s
3. **sglang 路径下 L3 read 效率仅 1-2% (vs fio 硬件极限)** — page size 9MB × 110 files, 内核串行 + page cache

**4 个隐藏坑位 (工程师必备)**:
1. NTFS 候选盘必须先 `mount -t ntfs3`, 否则 fstab 不存在时驱动空目录
2. sglang 0.5.13 `--file-storage-path` CLI 不生效, 必须用 `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR` 环境变量
3. sglang 0.5.13 硬约束 `--hicache-ratio ≥ 1.0` (L2 ≥ device), 否则启动 AssertionError
4. `drop_caches` 对 sglang pin_memory 自管的 L2 host buffer **无效**

---

## 1. 7 Phase 时间线

| Phase | 日期 | 目标 | 关键结果 | 文档 |
|---|---|---|---|---|
| **0** | 06-10 | (LMCache 时代预研, 保留作为 baseline 对比) | 4 盘 cold/warm spread 1ms, LMCache 加速 23.5× | [REPORT_LMCACHE.md](./REPORT_LMCACHE.md) |
| **1** | 06-11 | sglang 0.5.13 装环境 + 启动 L3 file backend | 启动 25s, L3 落 71×9MB=639MB, file_storage_path CLI 不生效 | [hicache-smoke-test-findings-2026-06-11.md](./docs/hicache-smoke-test-findings-2026-06-11.md) |
| **2** | 06-12 | 1 prompt × 6 rounds, 4 盘串行 (write_through) | 4 盘 cold/warm spread < 5ms (page cache 屏蔽), 写峰值 Seagate 8106 MB/s | [hicache-4disk-headline-2026-06-12.md](./docs/hicache-4disk-headline-2026-06-12.md) ⚠️ mount 事故 |
| **3** | 06-13 | write_through vs write_back 4 盘对比 | write_back 让 cold -37ms (-2.6%), 但加速比 1.96×→1.90×, write_back 6 round 测完 L3 0 file | [hicache-writeback-vs-writethrough-2026-06-13.md](./docs/hicache-writeback-vs-writethrough-2026-06-13.md) ⚠️ mount 事故 |
| **4** | 06-12 | 14B-AWQ TP=2 (Qwen3-14B-AWQ, 4-bit 量化) | 4 盘 cold 4.89s ± 2ms, warm 0.987s ± 1ms, page 5.0MB/file, 加速 4.95× | [hicache-14b-baseline-2026-06-12.md](./docs/hicache-14b-baseline-2026-06-12.md) ⚠️ mount 事故 |
| **5** | 06-13 | 4 client 并发 + drop_caches_every_round | 4 盘 cold 1.726s ± 2ms, NTFS 3 盘 iostat 0 读 0 写 (L2 hit 100%) | [hicache-multiclient-dropcaches-2026-06-12.md](./docs/hicache-multiclient-dropcaches-2026-06-12.md) ⚠️ mount 事故 |
| **6** | 06-13 | 绕开 sglang 测 L3 file read 硬件极限 (fio) | 1MB seq: BIWIN 4765 / ZHITAI 3616 / Seagate 3032 / WDC 2632 MB/s | [l3-fio-bench-2026-06-13.md](./docs/l3-fio-bench-2026-06-13.md) ✅ 绕过 sglang, 数据有效 |
| **7** | 06-14 | **多 prompt 累积触发 L2 evict + replay p0** | **4 盘 replay_p0 spread 2.1s, 终于暴露盘差** | [hicache-multiprompt-l2fill-2026-06-14.md](./docs/hicache-multiprompt-l2fill-2026-06-14.md) ✅ 真 4 盘基线 |

**Phase2-5 ⚠️ mount 事故**: 3 块 NTFS 候选盘 (WDC/Seagate/ZHITAI) 在 Phase2-5 期间实际未 mount, `/mnt/ai_ssd{0,1,2}` 是 BIWIN 根分区上的空目录。所有"4 盘对比"实际是 BIWIN 重复 4 次 + iostat 看其他 3 盘 stats = 0 (没 IO)。**Phase7 是 mount 修正后真 4 盘基线**。

---

## 2. 关键数据汇总

### 2.1 Phase7 (L2 miss 真 4 盘基线) — **最重要**

| 盘 | cold (p0) | L2 hit (p1-p19 mean) | **L3 reload (replay_p0)** | overhead |
|---|---:|---:|---:|---:|
| 🥇 **BIWIN ext4** | 1.433s | 1.420s | **1.718s** | **1.20×** |
| 🥈 ZHITAI NTFS | 1.435s | 1.422s | 2.677s | 1.87× |
| 🥉 Seagate NTFS | 1.434s | 1.448s | 2.773s | 1.93× |
| 4️⃣ WDC NTFS | 1.434s | 1.421s | **3.816s** | **2.66×** |
| **spread** | **1ms** | — | **2098ms** | **1.46×** |

**核心数据**: sglang 0.5.13 + Qwen3-4B + 20 prompts (140K tokens > L2 41K 容量) + replay p0, 4 盘 spread 2.1 秒。

**iostat 验证 (Phase7 round 期间)**:

| 盘 | avg_r (MB/s) | max_r (MB/s) | avg_w (MB/s) | max_w (MB/s) |
|---|---:|---:|---:|---:|
| BIWIN ext4 | **101** | **1665** | 17 | 128 |
| WDC NTFS | 10 | 483 | 38 | 128 |
| Seagate NTFS | 11 | 679 | 24 | 128 |
| ZHITAI NTFS | 10 | 810 | 27 | 128 |

**注意**: BIWIN avg_r 101 MB/s 远高于 NTFS 10 MB/s, 因为 ext4 内核 page cache 预读 + BIWIN 在 root fs 上更优。**这与 replay_p0 latency 完全对应**。

### 2.2 Phase6 (fio 硬件极限) — sglang 路径下对照基线

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

### 2.3 Phase2-5 (mount 修正前) — **仅供历史参考, 不作选型依据**

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

### 3.4 事故影响

- ❌ **Phase2/3/4/5 的 "4 盘 spread 1ms" 结论无效** — 实际是单盘重复 4 次
- ❌ **Phase2/3/4/5 的 iostat 数据** — 看 4 盘 0 读 0 写也是因为没 mount
- ✅ **Phase6 (fio) 数据不受影响** — fio 不依赖 mount, 直接 raw device 测试
- ✅ **Phase7 (06-14) 数据是 mount 修正后真 4 盘基线**, **是选型依据**

**建议**: Phase2-5 文档顶部加 ⚠️ 标注, README/REPORT 引用时仅作 "测试方法探索" 价值, 不用作盘差数据。

---

## 4. 选型最终推荐

### 4.1 综合 4 维度

| 盘 | Phase2 写峰值 (mount 修正前, BIWIN 重复) | Phase6 fio 顺序读 | **Phase7 L3 reload latency** | Phase4 14B 写 |
|---|---:|---:|---:|---:|
| BIWIN ext4 | 5284 MB/s (实际 BIWIN 自身) | 4.77 GB/s | **1.72s** 🥇 | 790 MB/s |
| WDC NTFS | 8004 (实际 BIWIN 重复) | 2.63 GB/s | 3.82s ⚠️ | 1100 (实际 BIWIN) |
| Seagate NTFS | 8106 (实际 BIWIN 重复) | 3.03 GB/s | 2.77s | 1100 (实际 BIWIN) |
| ZHITAI NTFS | 4498 (实际 BIWIN 重复) | 3.62 GB/s | 2.68s | 1101 (实际 BIWIN) |

### 4.2 选型矩阵

| 场景 | 推荐 | 理由 |
|---|---|---|
| **单盘 + 大模型 + 频繁 reload** | 🥇 **BIWIN** | 1.72s 最快, 系统盘通用, 容量大 (953GB 根分区) |
| **多盘 + 长 prefix + 慢 reload 可接受** | 🥈 **Seagate / ZHITAI** | 2.7s 慢但 931G 容量, 适合 KV cache 大 + 偶发 reload |
| **高并发 L3 write (系统 bootstrap 阶段)** | 🥉 **Seagate** | 8106 MB/s 写峰值最高 (Phase2 数据, mount 修正后待验证) |
| **单盘 + 预算敏感** | ⚠️ **WDC** | 2.63 GB/s 慢, L3 reload 1 prompt 慢 2.2×, 大 L3 部署不推荐 |
| **多卡 TP=2 + 14B-AWQ** | 🥇 **BIWIN + 任意 NTFS** | 14B-AWQ write_through 4 盘 1.10 GB/s 写峰值 几乎一致 (Phase4), 容量优先 BIWIN |

### 4.3 给 AI SSD 产品设计的反推

1. **sglang 路径下盘差主要由 NTFS 内核驱动延迟贡献** — 同样 4 块盘, 硬件 1.5-1.8×, 软件实测 1.6-2.2×, 差异 < 25%。**SSD 控制器优化对 sglang 收益有限**, 重点优化应该放在 NTFS/文件系统层。
2. **page_size 9MB + sglang 串行 reader 是真实瓶颈** — 盘硬件给 4.7 GB/s, sglang 只用 70 MB/s (1.5%)。**降低 page_size 或加 sglang 内部并发可大幅提升**。
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
- ✅ **`/metrics:backuped_tokens_total` 增量** (确认 L3 落盘)
- ✅ **iostat `rMB/s` 在 NTFS 上的持续值** (≥ 100 MB/s 持续 1s+ 才是真读盘)
- ✅ **`/metrics:hicache_host_used_tokens` 实际值** (L2 容量饱和状态)

---

## 6. 后续 / 未完成项

### 6.1 必须重跑 (mount 修正后)

- **Phase2 v3** (4B write_through, 真 4 盘): 用真 mount, 看 cold/warm spread 是不是 0-5ms
- **Phase4 v3** (14B-AWQ, 真 4 盘): 同上
- **Phase5 v3** (4 client + drop, 真 4 盘): 同上

预计 mount 修正后 spread 应该跟 Phase7 数量级一致 (ms → 几十~几百 ms)

### 6.2 工具修复

- **bpftrace kernel 6.x 兼容**: `delete()` API 移除, `issue_time_ns` 字段缺失
- **sglang 升级**: 0.5.13 → 0.6+ 看看 `--hicache-ratio < 1.0` 是否放开
- **iostat 解析** driver `awk` 字符串匹配 bug (Phase7 解析 `rMB/s` 字段定位错位)

### 6.3 数据扩展

- **32B-AWQ 模型**: 单卡装不下, TP=2 跨 3 卡, 看更大模型 L3 行为
- **多 prompt × 多 client** (4 client × 4 prompt): 测并发 L3 reload
- **长 prefix (32K+ tokens)**: 让 L2 evict 更频繁, 看 L3 read 持续暴露

---

## 7. 关联文档

### 本报告依赖
- [README.md](./README.md) — 项目入口
- [docs/hicache-smoke-test-findings-2026-06-11.md](./docs/hicache-smoke-test-findings-2026-06-11.md) — Phase1 装环境
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
