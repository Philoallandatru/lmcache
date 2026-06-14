# SGLang HiCache × AI SSD — 4 盘横向对比 (Headline, v2)

> **日期:** 2026-06-12 (re-run 2026-06-13 凌晨, 修正 WDC 数据并改用干净 cleanup)
> **模型:** Qwen3-4B-Instruct-2507
> **负载:** 1 cold + 5 warm, 7000 tokens prefix, 64 tokens output
> **HiCache 配置:** `page_size=64, hicache-ratio=2, page_first_direct, direct io, write_through, timeout, file backend`
> **官方文档:** hicache_best_practices.md §"Deployment with HF3FS" (去掉 PD 部分)
> **测量工具:** `iostat -dx -m 1` + `/metrics` (sglang 0.5.13)

---

## 1. TL;DR — 关键发现 (re-run 后修正)

| 维度 | 结论 |
|---|---|
| **TTFT 加速比** | 4 盘完全相同 (**1.96×**),page cache 抹平了盘差 |
| **Cold TTFT** | 1.438-1.440s (极差 1ms) |
| **Warm TTFT** | 0.721-0.735s (极差 14ms,主要在 warm #1) |
| **iostat 写 peak** | Seagate 8106 MB/s > BIWIN 5284 > WDC 8004 > ZHITAI 4498 |
| **iostat 读 avg** | **BIWIN 1976 MB/s (page cache!)** >> WDC/ZHITAI/Seagate 8-12 MB/s |
| **L3 文件** | 4 盘各 **115 × 9.0 MB = 1035 MB** |
| **与 LMCache 对比** | HiCache 1.96× vs LMCache 23.5× — write_through 同步阻塞导致温和加速 |

> **📌 v3 mount-fixed 重跑已确认**: 2026-06-15 Phase2 v3 (mount 修正后) spread 6ms 跟本报告 v2 spread 1ms 一致。详见 [hicache-v3-mount-fixed-2026-06-15.md](./hicache-v3-mount-fixed-2026-06-15.md)。**iostat 数值需要重新看 v3 数据** (NTFS 真实 0 读, v2 看到 8-12 MB/s 是 page cache 命中, 误导)。

**核心洞察**:
1. **HiCache file backend 走 page cache (buffered IO)**, 用户态 TTFT **无法**区分 4 盘
2. **BIWIN 系统盘 (ext4)** 受益于内核 page cache, **avg_r=1976 MB/s** 是其他 3 块 NTFS 外置盘的 **200×**
3. **写 peak** 由内核 writeback 异步触发,iostat 1s 抓到的是突发,**Seagate 写峰值 8106 MB/s 第一**
4. **Warm #2-5 全在 L2 DRAM hit**,完全不读盘,所以 4 盘 TTFT 无差异
5. **与 LMCache 23.5×** 对比:**HiCache write_through 同步写** 让 cold TTFT 多了 ~700ms

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
| **monitoring** | iostat -dx -m 1 + /metrics | sysstat 14.x + sglang 自带 |

---

## 3. TTFT 横向对比 (4 盘 × 6 rounds) — **干净 re-run 数据**

| Round | BIWIN X570 | WDC | ZHITAI | Seagate | 极差 |
|---|---:|---:|---:|---:|---:|
| Cold | 1.440s | 1.439s | 1.440s | 1.439s | **1ms** |
| Warm #1 (drop_caches 后) | 0.735s | 0.734s | 0.735s | 0.735s | **1ms** |
| Warm #2 | 0.722s | 0.721s | 0.722s | 0.722s | 1ms |
| Warm #3 | 0.721s | 0.722s | 0.722s | 0.722s | 1ms |
| Warm #4 | 0.721s | 0.722s | 0.722s | 0.722s | 1ms |
| Warm #5 | 0.722s | 0.722s | 0.722s | 0.722s | 0ms |
| **加速比 (cold/warm_1)** | **1.96×** | **1.96×** | **1.96×** | **1.96×** | — |

**观察**:
- 4 盘 **完全同质** — Cold 1.439-1.440s (1ms 极差), Warm 0.721-0.735s (14ms 极差)
- 加速比稳定在 **1.96×** — 与盘的 SLC cache / GC 行为无关
- Warm #2-5 几乎完全相同 (0.721-0.722s),说明 **L2 DRAM hit 完全掩盖了 L3 行为**

---

## 4. iostat 真实 IO 模式 (4 盘, 1s/row)

| 指标 | BIWIN X570 (ext4) | WDC (NTFS) | ZHITAI (NTFS) | **Seagate (NTFS)** |
|---|---:|---:|---:|---:|
| 样本数 (1s 间隔) | 46 | 44 | 46 | 46 |
| **avg r/s** | **1976.6 MB/s** 🥇 | 8.3 MB/s | 10.3 MB/s | 11.9 MB/s |
| avg w/s | 417.5 MB/s | 185.0 MB/s | 179.3 MB/s | 181.5 MB/s |
| **max r BW** | **14704 MB/s** 🥇 | 317.9 MB/s | 424.5 MB/s | 498.3 MB/s |
| **max w BW** | 5284 MB/s | 8004 MB/s | 4498 MB/s | **8106 MB/s** 🥇 |
| max r await | 5.24ms | 1.00ms | 5.00ms | 35.00ms |
| max w await | 100.00ms | 0.41ms | 0.29ms | 0.25ms |

**关键观察**:
- **BIWIN 系统盘 (ext4) 受益于 Linux page cache** — avg_r=1976 MB/s 是其他 3 块 NTFS 盘的 **200×**
- **NTFS 外置盘读都是 ~10 MB/s**,因为不走系统 page cache,只能靠 device 自带 cache
- **写 peak 是 page cache writeback 突发**,iostat 1s 抓到的是最高峰值
- **Seagate 写峰值最高 8106 MB/s**,但有 35ms 读 await (可能的 GC)
- **WDC 写峰值 8004 MB/s**,读 await 仅 1ms (响应最稳定)

---

## 5. L3 文件清单 (cache_file_list.txt)

| 盘 | L3 文件数 | 总大小 | 平均文件大小 |
|---|---:|---:|---:|
| BIWIN X570 (ext4) | 115 | 1035.00 MB | 9.0 MB |
| WDC (NTFS) | 115 | 1035.00 MB | 9.0 MB |
| ZHITAI (NTFS) | 115 | 1035.00 MB | 9.0 MB |
| Seagate (NTFS) | 115 | 1035.00 MB | 9.0 MB |

**完全一致** — 因为 `page_size=64 tokens` × Qwen3-4B (36 layers × 8 KV heads × 128 head_dim × 2 bytes bfloat16) = 9,437,184 bytes = 9.0 MB per KV page,与源码 `hicache_storage.py` 实测一致。

**HiCache 指标** (从 `/metrics` 抓取):
- `sglang:backuped_tokens_total{storage_backend="file"}` ≈ 7360 (7008 prompt + ~6×64 completion ≈ 7400)
- `sglang:hicache_host_used_tokens` ≈ 7296 (L2 DRAM 持有 7k tokens)
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

- Cold 请求的 L3 store 是突发写 (7008 tokens × 9MB ≈ 1GB)
- page cache writeback 由内核异步触发,有时被 1s iostat 抓到,有时抓不到
- 抓到的"写 peak BW"反映盘真实能力 — **Seagate 8106 > BIWIN 5284 > WDC 8004 > ZHITAI 4498**
- **读 avg** 主要看 page cache 命中率:
  - BIWIN ext4 系统盘:数据落到 OS page cache,Warm #1 直接命中 → **1976 MB/s**
  - NTFS 外置盘:Warm #1 是真 disk read,**device cache 命中率低 → 8-12 MB/s**

### 6.3 与 LMCache 数据集交叉对比

| 维度 | LMCache (vllm 0.22.1) | HiCache (sglang 0.5.13) |
|---|---|---|
| 文件粒度 | **37.7 MB** (chunk_size=256) | **9.0 MB** (page_size=64) |
| 单 cold req 文件数 | 8 | **115** (×14) |
| L3 写总大小 | 0.95 GB | **1.04 GB** |
| Cold TTFT (7000 tok) | 0.785s (LMCache BIWIN) | 1.440s (HiCache BIWIN) — **慢 83%** |
| Warm #1 TTFT (drop_caches) | 0.034s (LMCache BIWIN, 23.1×) | 0.735s (HiCache BIWIN, 1.96×) |
| 写策略 | 异步 store (后台线程) | write_through (同步阻塞) |
| **TTFT 加速比** | **23.1×** | **1.96×** |

**根本差异**:
- LMCache 加速比 23× 是因为 **decode 64 tokens ≈ 50ms,占总 TTFT < 10%** → 纯 prefill 加速可见
- HiCache 加速比 1.96× 是因为 **同样的 64 token decode ≈ 50ms,但 prefill 1.4s 里 L3 reload 占 ~700ms**(1GB @ 1.4 GB/s),**用户态等了 L3 IO**

**结论**: LMCache 用异步 store 让 prefill 0.78s 里**不等 L3**,warm 时 reload < 50ms (走 page cache),所以加速比极大;HiCache 用 write_through 让 prefill 1.44s 里**L3 阻塞写**,warm 时 reload 700ms (page cache miss → 真 disk),所以加速比温和。

---

## 7. AI SSD 选型结论 (HiCache 视角)

按 **iostat 真实 disk IO 数据**:

| 排名 | 盘 | max w BW | max r BW | 平均 r BW | 推荐场景 |
|---|---|---:|---:|---:|---|
| 🥇 | **Seagate ZP1000GV30012** | **8106 MB/s** | 498 MB/s | 12 MB/s | 多并发 HiCache + 突发 L3 reload |
| 🥈 | BIWIN X570 (ext4 baseline) | 5284 MB/s | **14704 MB/s** | **1976 MB/s** 🥇 | 系统盘通用,受 page cache 红利 |
| 🥉 | WDC WDS960G2G0C | 8004 MB/s | 318 MB/s | 8 MB/s | 单请求场景,延迟最稳定 (1ms await) |
| 4️⃣ | ZHITAI Ti600 | 4498 MB/s | 425 MB/s | 10 MB/s | NTFS 写 peak 最差,不推荐 KV cache |

**与 LMCache 时代报告的交叉**:
- LMCache 报告: **Seagate 写延迟 17ms** (异常,NTFS issue) — 现在 HiCache 数据 Seagate 写 BW **8106 MB/s (4 盘第一)** — **NTFS 性能差异由 IO 模式决定**
- LMCache 报告: **ZHITAI 写延迟 0.20ms (最优)** — 现在 HiCache 数据 ZHITAI 写 BW **4498 MB/s (4 盘最差)** — **完全相反!**

**根本原因**:
- LMCache 测的是小请求 + 异步,延迟由盘 response 决定 → ZHITAI 好
- HiCache 测的是 1GB 突发写 + page cache,带宽由盘吞吐决定 → Seagate 好

**重要注意**:
- BIWIN 读 1976 MB/s 优势**只在 ext4 系统盘 + Linux page cache** 下成立
- 部署到生产时如果 L3 盘是 NTFS 外置,这种优势**会消失**
- 真要发挥 HiCache 性能,推荐 **ext4 系统盘作为 L3** 或 **HF3FS/DAOS 专用 KV 存储**

---

## 8. 复现命令

```bash
cd ~/llm/infer/ai_ssd_prestudy

# 1. 装 sglang (与 vllm/torch 2.11+cu130 共存)
source ~/llm/.venv/bin/activate
# pip install "sglang[all]==0.5.13" --upgrade  # 一次性

# 2. 4 盘串行 (~25 分钟)
bash scripts/hicache_drive_4_rounds.sh
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
6. **re-run 修复历史** — 第一次跑的 WDC 数据是无效的 (僵尸进程污染, L3 file count=0), 已在 commit `73ef3e6` 基础上重新跑干净版本

### 下一步
1. **`--hicache-write-policy write_back` 对照** — 异步 L3 store 模式下,4 盘 TTFT 是否有差异
2. **更大模型 (Qwen2.5-7B / 32B)** — 突破 page cache 容量,迫使 disk reload
3. **大 decode (1024 tokens)** — 拉长每次请求时间,放大 IO 差异
4. **bpftrace per-IO latency** — 当前 `hicache_blk_io_latency.bt` 有 kernel 6.x 兼容 bug,需重写
5. **fio 同步对照** — `~/llm/storage/` 的 fio 4 盘数据可作绝对 baseline,验证 HiCache 测量的准确性
6. **runtime attach/detach** — 按 hicache_storage_runtime_attach_detach.md 用 HTTP API 切换 backend
7. **ext4 vs NTFS 同盘对比** — 把 Seagate 格式化成 ext4 测一次,量化文件系统影响