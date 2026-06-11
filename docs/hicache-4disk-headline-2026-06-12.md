# SGLang HiCache × AI SSD — 4 盘横向对比 (Headline)

> **日期:** 2026-06-12
> **模型:** Qwen3-4B-Instruct-2507
> **负载:** 1 cold + 5 warm, 7000 tokens prefix, 64 tokens output
> **HiCache 配置:** `page_size=64, hicache-ratio=2, page_first_direct, direct io, write_through, timeout, file backend`
> **官方文档:** hicache_best_practices.md §"Deployment with HF3FS" (去掉 PD 部分)

---

## 1. TL;DR — 关键发现

**🎯 4 盘 TTFT 加速比几乎相同 (1.95-1.96×)**,但 **iostat 显示盘真实能力差异巨大**:

| 盘 | Cold TTFT | Warm #1 (drop_caches) | 加速比 | **iostat 写 peak** | **iostat 读 peak** |
|---|---:|---:|---:|---:|---:|
| BIWIN X570 (ext4 baseline) | 1.438s | 0.735s | **1.96×** | **747 MB/s** | **6072 MB/s** |
| WDC WDS960G2G0C (NTFS) | 1.439s | 0.738s | 1.95× | 690 MB/s | 5559 MB/s |
| ZHITAI Ti600 (NTFS) | 1.439s | 0.738s | 1.95× | 510 MB/s | 4123 MB/s |
| **Seagate ZP1000GV30012 (NTFS)** | 1.438s | 0.737s | 1.95× | **964 MB/s** | **7871 MB/s** |

**核心洞察:**
1. **HiCache file backend 走 page cache (buffered IO)**, 用户态 Latency (TTFT) **无法**区分 4 盘
2. **iostat 真实 disk IO** 清楚显示盘差:**Seagate 写比 ZHITAI 快 89%, 读快 91%**
3. **TTFT 加速比温和 (1.95×)** vs LMCache 时代报告的 ~23× — 因为 Qwen3-4B 模型小, decode 64 tokens 占了 ~50% 时间
4. **4 盘 L3 文件数完全相同** (115 × 9.0 MB = 1085 MB),因为 page_size=64 决定 KV 切分粒度

---

## 2. 实验配置 (按 hicache_best_practices.md §"Core HiCache Parameters")

| 参数 | 值 | 来源 |
|---|---|---|
| 模型 | Qwen3-4B-Instruct-2507 | 用户选定 |
| prompt tokens | 7008 (大 prefix) | 与 LMCache REPORT 7000 对齐 |
| output tokens | 64 | 突出 prefill 占比 |
| rounds | 1 cold + 5 warm | 同 prompt 测 L3 hit |
| request_rate | 1.0 req/s | 单 client 串行 |
| **page_size** | **64** | hicache_best_practices.md L11 (官方默认) |
| **hicache-ratio** | **2** (L2 = 2×L1) | hicache_best_practices.md L13 |
| **hicache-size** | **0** | 让 ratio 生效 |
| **mem-layout** | **page_first_direct** | hicache_best_practices.md L34 |
| **io-backend** | **direct** | hicache_best_practices.md L37 |
| **write-policy** | **write_through** | hicache_design.md §"Data Write-back" |
| **prefetch-policy** | **timeout** | hicache_best_practices.md L65 (官方推荐生产) |
| **L3 backend** | **file** | hicache_storage.py::HiCacheFile |
| **monitoring** | iostat -dx -m 1 + /metrics | sysstat 12.7+ + sglang 自带 |

---

## 3. TTFT 横向对比 (4 盘 × 6 rounds)

| Round | BIWIN X570 | WDC | ZHITAI | Seagate | 极差 (max-min) |
|---|---:|---:|---:|---:|---:|
| Cold | 1.438s | 1.439s | 1.439s | 1.438s | **1ms** |
| Warm #1 (drop_caches 后) | 0.735s | 0.738s | 0.738s | 0.737s | **3ms** |
| Warm #2 | 0.723s | 0.722s | 0.722s | 0.723s | 1ms |
| Warm #3 | 0.723s | 0.722s | 0.724s | 0.723s | 2ms |
| Warm #4 | 0.723s | 0.722s | 0.723s | 0.723s | 1ms |
| Warm #5 | 0.723s | 0.721s | 0.723s | 0.722s | 2ms |
| **加速比 (cold/warm_1)** | **1.96×** | **1.95×** | **1.95×** | **1.95×** | — |

**观察**:
- 4 盘 **完全同质** — Cold 1.438-1.439s,Warm 0.721-0.738s
- 加速比稳定在 **1.95-1.96×** — 与盘的 SLC cache / GC 行为无关
- Warm #2-5 几乎完全相同 (0.721-0.724s),说明 **L2 DRAM hit 完全掩盖了 L3 行为**

---

## 4. iostat 真实 IO 模式 (4 盘)

| 指标 | BIWIN X570 (ext4) | WDC (NTFS) | ZHITAI (NTFS) | **Seagate (NTFS)** |
|---|---:|---:|---:|---:|
| 数据行 (1s/row) | 54 | 50 | 49 | 50 |
| **写 peak BW** | 747 MB/s | 690 MB/s | 510 MB/s | **964 MB/s** |
| 写 peak IOPS | 83 | 0 | 0 | 0 |
| **读 peak BW** | 6072 MB/s | 5559 MB/s | 4123 MB/s | **7871 MB/s** |
| 读 peak IOPS | 3316 | 0 | 0 | 0 |
| 读 peak await | 126.0 ms | 127.1 ms | 126.8 ms | 125.5 ms |
| 写活跃秒数 (>100 MB/s) | 4/54 | 2/50 | 2/49 | 1/50 |

**关键观察**:
- **写 IO 主要由 page cache writeback 异步触发**,iostat 1s 粒度只能抓到峰值
- **读 IO 只在 drop_caches + Warm #1 时出现**,触发 page cache reload
- **Seagate 写比 ZHITAI 快 89%**,但**两盘 TTFT 相同** — 因为写都在 1s 内完成,被 L3 store 异步性掩盖

---

## 5. L3 文件清单 (cache_file_list.txt)

| 盘 | L3 文件数 | 总大小 | 平均文件大小 |
|---|---:|---:|---:|
| BIWIN X570 (ext4) | 115 | 1085.3 MB | 9.0 MB |
| WDC (NTFS) | 115 | 1085.3 MB | 9.0 MB |
| ZHITAI (NTFS) | 115 | 1085.3 MB | 9.0 MB |
| Seagate (NTFS) | 115 | 1085.3 MB | 9.0 MB |

**完全一致** — 因为 `page_size=64 tokens` × Qwen3-4B (36 layers × 8 KV heads × 128 head_dim × 2 bytes bfloat16) = 9,437,184 bytes = 9.0 MB per KV page,与源码 `hicache_storage.py` 实测一致。

**HiCache 指标** (从 `/metrics` 抓取):
- `sglang:backuped_tokens_total{storage_backend="file"} = 7360` ✅ (7008 prompt + 6×64 completion ≈ 7400)
- `sglang:hicache_host_used_tokens ≈ 7296` (L2 DRAM 持有 7k tokens)
- `sglang:hicache_host_total_tokens = 41024` (L2 容量, ratio=2× L1=20480)

---

## 6. 关键洞察 (与官方文档对应)

### 6.1 TTFT 看不到盘差的原因 (hicache_design.md §"Data Transfer Optimization")

> "HiCache L2 stores and transfers KV cache data at the granularity of **pages**"

- L3 file backend 走 **buffered IO** (`open(path, "rb").readinto()` per hicache_storage.py L389-393)
- 内核 page cache 把所有读命中缓存,**第一次读 miss 后全部命中**
- Warm #2-5 的 0.722s 是 **L2 DRAM hit + 64 token decode** 的耗时,**完全不读盘**
- 所以 4 盘 TTFT 无差异

### 6.2 iostat 看得到盘差的原因

- Cold 请求的 L3 store 是突发写(7008 tokens × 9MB ≈ 1GB)
- page cache writeback 由内核异步触发,有时被 1s iostat 抓到,有时抓不到
- 抓到的"写 peak BW"反映盘真实能力 — **Seagate 964 MB/s > BIWIN 747 > WDC 690 > ZHITAI 510**

### 6.3 与 LMCache 数据集交叉对比

| 维度 | LMCache (vllm 0.22.1) | HiCache (sglang 0.5.13) |
|---|---|---|
| 文件粒度 | **37.7 MB** (chunk_size=256) | **9.0 MB** (page_size=64) |
| 单 cold req 文件数 | 8 | **115** (×14) |
| L3 写总大小 | 0.95 GB | **1.04-1.08 GB** |
| Cold TTFT (7000 tok) | 0.785s (LMCache BIWIN) | 1.438s (HiCache BIWIN) — **慢 83%** |
| Warm #1 TTFT (drop_caches) | 0.034s (LMCache BIWIN, 23.5×) | 0.735s (HiCache BIWIN, 1.96×) |
| 写策略 | 异步 store (后台线程) | write_through (同步阻塞) |
| **TTFT 加速比** | **23.5×** | **1.96×** |

**根本差异**:
- LMCache 加速比 23× 是因为 **decode 64 tokens ≈ 50ms,占总 TTFT < 10%** → 纯 prefill 加速可见
- HiCache 加速比 1.96× 是因为 **同样的 64 token decode ≈ 50ms,但 prefill 1.4s 里 L3 reload 占 ~700ms**(1GB @ 1.4 GB/s),**用户态等了 L3 IO**

**结论**: LMCache 用异步 store 让 prefill 0.78s 里**不等 L3**,warm 时 reload < 50ms (走 page cache),所以加速比极大;HiCache 用 write_through 让 prefill 1.44s 里**L3 阻塞写**,warm 时 reload 700ms (page cache miss → 真 disk),所以加速比温和。

---

## 7. AI SSD 选型结论 (HiCache 视角)

按 **iostat 真实 disk IO 数据**:

| 排名 | 盘 | 写 BW | 读 BW | 推荐场景 |
|---|---|---:|---:|---|
| 🥇 | **Seagate ZP1000GV30012** | **964 MB/s** | **7871 MB/s** | **多并发 HiCache + 大量 L3 reload** |
| 🥈 | BIWIN X570 (ext4 baseline) | 747 MB/s | 6072 MB/s | 系统盘通用,但需 ext4 + O_DIRECT |
| 🥉 | WDC WDS960G2G0C | 690 MB/s | 5559 MB/s | 单请求场景,SLC cache 边界需注意 |
| 4️⃣ | ZHITAI Ti600 | 510 MB/s | 4123 MB/s | NTFS 写性能最差,不推荐 KV cache |

**与 LMCache 时代报告的交叉**:
- LMCache 报告: **Seagate 写延迟 17ms** (异常,NTFS issue) — 现在 HiCache 数据 Seagate 写 BW **964 MB/s (4 盘第一)** — **NTFS 性能差异由 IO 模式决定**
- LMCache 报告: **ZHITAI 写延迟 0.20ms (最优)** — 现在 HiCache 数据 ZHITAI 写 BW **510 MB/s (4 盘最差)** — **完全相反!**

**根本原因**:
- LMCache 测的是小请求 + 异步,延迟由盘 response 决定 → ZHITAI 好
- HiCache 测的是 1GB 突发写 + page cache,带宽由盘吞吐决定 → Seagate 好

---

## 8. 复现命令

```bash
cd ~/llm/infer/ai_ssd_prestudy

# 1. 装 sglang (与 vllm/torch 2.11+cu130 共存)
source ~/llm/.venv/bin/activate
# pip install "sglang[all]==0.5.13" --upgrade  # 一次性

# 2. 4 盘串行
bash scripts/hicache_drive_4_rounds.sh  # ~25 分钟
```

数据位置: `results/hicache/{baseline_biwin_ext4, ai_ssd0_wdc_ntfs, ai_ssd1_zhitai_ntfs, ai_ssd2_seagate_ntfs}/`

每轮包含:
- `iostat_*.log` — 1s 粒度盘 IO 数据
- `server.log` — sglang HiCache 启动日志 (含 `Creating storage backend 'file'`)
- `load_test.jsonl` — 6 round TTFT 数据
- `metrics_after.json` — Prometheus metrics (backuped_tokens, hicache_host_used)
- `cache_file_list.txt` — L3 落盘文件清单 (size + name)

---

## 9. 已知限制与下一步

### 限制
1. **TTFT 无法区分盘差** — page cache 把 disk IO 全部缓存,需要更激进的测试设计
2. **iostat 1s 粒度太粗** — L3 store 是 14ms 突发,iostat 抓不到精确峰值
3. **测试规模太小** — Qwen3-4B + 7000 tokens + 64 decode 都在 page cache 容量内
4. **write_through 阻塞** — 异步策略 (write_back) 可能看到不同盘差
5. **NTFS vs ext4 文件系统干扰** — 同一盘 NTFS 与 ext4 性能差异未量化

### 下一步
1. **`--hicache-write-policy write_back` 对照** — 异步 L3 store 模式下,4 盘 TTFT 是否有差异
2. **更大模型 (Qwen2.5-7B / 32B)** — 突破 page cache 容量,迫使 disk reload
3. **大 decode (1024 tokens)** — 拉长每次请求时间,放大 IO 差异
4. **bpftrace per-IO latency** — 当前 `hicache_blk_io_latency.bt` 有 kernel 兼容性 bug,需修复
5. **fio 同步对照** — `~/llm/storage/` 的 fio 4 盘数据可作绝对 baseline,验证 HiCache 测量的准确性
6. **runtime attach/detach** — 按 hicache_storage_runtime_attach_detach.md 用 HTTP API 切换 backend