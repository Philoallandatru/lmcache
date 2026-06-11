# SGLang HiCache × AI SSD 预研实施计划 (v2 — 基于 HiCache 官方文档)

> **目标:** 完全依据 SGLang 官方文档 (`hicache_design.md` / `hicache_best_practices.md` /
> `hicache_storage_runtime_attach_detach.md` / `benchmark/hicache/`) 重新设计实验。
>
> **定位:** 不参考之前的 LMCache/vLLM 脚本代码,所有配置与 benchmark 工具均来自
> `sgl-project/sglang` 仓库 main 分支(2026-06-11 拉取)。
>
> **落点:** `~/llm/infer/ai_ssd_prestudy/` (新引擎独立目录)
> **状态:** 探索阶段 — 计划文档,不直接执行。
>
> **关键文档/代码引用 (commit pin):**
> - `docs/advanced_features/hicache_design.md` (157 lines)
> - `docs/advanced_features/hicache_best_practices.md` (217 lines)
> - `docs/advanced_features/hicache_storage_runtime_attach_detach.md` (132 lines)
> - `benchmark/hicache/{bench_multiturn,bench_long_context,bench_serving}.py`
> - `python/sglang/srt/mem_cache/hicache_storage.py::HiCacheFile` (lines 319+)
> - `python/sglang/srt/mem_cache/storage/backend_factory.py` (lines 188+)

---

## 0. TL;DR

按 SGLang 官方推荐范式做 4 盘 × AI SSD 选型预研:

| 维度 | 选型 | 来源 |
|---|---|---|
| 推理引擎 | SGLang (latest stable) | sgl-project/sglang main |
| KV 后端 | HiCache 3 层 (L1 GPU + L2 DRAM + L3 disk) | hicache_design.md §"Overall Architecture" |
| L3 后端 | `file` (本地 fs,`.bin` pages) | hicache_best_practices.md + HiCacheFile 源码 |
| page_size | 64 (官方默认) | hicache_best_practices.md L11 |
| L2 配置 | `--hicache-ratio 2` (L2 = 2 × L1) | hicache_best_practices.md L13 |
| mem layout | `page_first_direct` (与 `direct` io 配套最优) | hicache_best_practices.md L34 |
| io backend | `direct` (无须 GPU-assisted kernel,兼容性最广) | hicache_best_practices.md L37 |
| write policy | `write_through` (主) / `write_back` (副) | hicache_design.md §"Data Write-back" |
| prefetch policy | `timeout` (生产推荐) | hicache_best_practices.md L65 |
| Benchmark 工具 | `benchmark/hicache/bench_multiturn.py` (官方) | benchmark/hicache/README.md |
| 模型 | Qwen3-4B (轻量,4B 参数) — 与现有 lmcache 数据可比 | 用户已有 |

---

## 1. 候选盘与已有资产盘点 (只列事实,不假设)

### 1.1 4 块 NVMe 候选盘 (来自 `REPORT.md` §3.1)

| 设备 | 型号 | FW | 分区 | FS | 容量 |
|---|---|---|---|---|---|
| nvme0n1 | WDC WDS960G2G0C | 231800WD | p2 | NTFS | 894G |
| nvme1n1 | BIWIN X570 | BM555ALN | p3 | ext4 | 384G (系统盘) |
| nvme2n1 | ZHITAI Ti600 | ZTA23004 | p3 | NTFS | 931G |
| nvme3n1 | Seagate ZP1000GV30012 | SUKSY000 | p2 | NTFS | 931G |

### 1.2 已部署的运行时环境 (来自 `README.md` "关键依赖")

- vllm 0.22.1 (cu130) + lmcache 0.4.6 + torch 2.11.0+cu130 + transformers 5.10.2 (在 `~/llm/.venv/`)
- sysstat 12.7.7 (iostat/pidstat)、bpftrace v0.25.0、perf_event_paranoid=-1
- 模型:`~/llm/models/Qwen/Qwen3-4B-Instruct-2507/` (BF16)
- 候选盘 mount 点:`/mnt/ai_ssd0` (WDC) / `/mnt/ai_ssd1` (ZHITAI) / `/mnt/ai_ssd2` (Seagate)

### 1.3 不复用的资产 (按用户指令)

- `lmcache_*.yaml` × 4 (LMCache 配置,不参考)
- `load_test.py` (vLLM + LMCache 客户端,不参考)
- `serve_lmcache.sh` / `drive_rounds.sh` (LMCache driver,不参考)
- `io_monitor.py` / `blk_io_latency.bt` (LMCache 时代的 iostat+ bpftrace脚本,需要重写以适配 sglang 时代)

> **新工作原则:** 所有脚本 (启动 / 驱动 / 监测 / 报告生成) 全部从 HiCache 官方文档 / benchmark 脚本派生。

---

## 2. HiCache 技术要点 (全部来自 sglang 官方文档)

### 2.1 三层架构 (hicache_design.md §"Overall Architecture")

```
L1 = GPU VRAM         (per-instance,private)
 │
 ↓  write_through / write_back / write_through_selective
L2 = Host DRAM        (per-instance,private,"mem_pool_host")
                      由 --hicache-ratio (相对 L1) 或 --hicache-size (绝对) 控制
 │
 ↓  write-back / async prefetch
L3 = Storage backend  (cluster-shared,可选)
   ├── file         (本地 fs,.bin per page)
   ├── mooncake     (RDMA,MoE)
   ├── hf3fs        (DeepSeek 3FS,K8s)
   ├── nixl         (GDS / S3 / plugins)
   ├── aibrix       (生产级 KVCache 框架)
   ├── eic          (Intel EIC)
   └── lmcache      (与 LMCache 互操作)
```

**L1↔L2 数据传输布局 (hicache_design.md §"Data Transfer Optimization"):**

| 布局 | 适用 io-backend | 备注 |
|---|---|---|
| `layer_first` | `direct` / `kernel` | 默认,层优先 |
| `page_first` | **仅 `kernel`** | 零拷贝,3x faster,但需 kernel 后端 |
| `page_first_direct` | **仅 `direct`** | 与 page_first 性能相近但兼容 fa3 |

**结论**: 选 `page_first_direct` + `direct` (兼容性最广,无需额外 kernel 编译)

### 2.2 写策略 (hicache_design.md §"Data Write-back")

| 策略 | 行为 | 适用 |
|---|---|---|
| `write_through` | 每次访问立即写下一层 | 带宽充足 + 强缓存效益 |
| `write_through_selective` | 访问次数超阈值才写 | 只备热数据,降低 IO |
| `write_back` | 仅淘汰时写下一层 | 容量受限时降低 IO |

**结论**: 主测 `write_through` (最贴近官方 best practices 例子 L143)

### 2.3 预取策略 (hicache_design.md §"Prefetch from L3")

| 策略 | 行为 | 适用 |
|---|---|---|
| `best_effort` | GPU 能跑就立即返回 | 延迟极敏感 |
| `wait_complete` | 全部预取完才返回 | 高命中率 |
| `timeout` | 超时/完成即返回 | **生产推荐 (官方原话)** |

**结论**: 用 `timeout`,这是 `hicache_best_practices.md` L65 明确推荐的策略。

### 2.4 L3 = `file` 后端实现细节 (hicache_storage.py::HiCacheFile,lines 319+)

| 属性 | 值 | 来源 |
|---|---|---|
| 文件命名 | `{key}{model_name}_{tp_rank}_{tp_size}.bin` | L356-364 |
| 缓存目录 | `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR` 环境变量,默认 `/tmp/hicache` | L344 |
| 写 | `open(path, "wb").write(tensor_bytes)` (源码后续行) | standard buffered |
| 读 | `open(path, "rb", buffering=0).readinto(torch_uint8_buf)` L389-393 | **直接读入 torch tensor** |
| 淘汰 | `LRUFileEvictor` 按大小 + LRU,可配置 max_size | L370-376 |
| Batch 大小 | `STORAGE_BATCH_SIZE = 128` pages 单次 IO | L20 |

**关键观察:** HiCacheFile 走标准 buffered IO,**不支持 O_DIRECT** —— 这是与 LMCache (use_odirect 选项) 的关键差异。

### 2.5 运行时切换 L3 (hicache_storage_runtime_attach_detach.md)

可 **不重启** 切换 L3 后端:

```bash
# 查询当前
curl -s http://127.0.0.1:30000/hicache/storage-backend

# 挂载 file 后端
curl -X PUT http://127.0.0.1:30000/hicache/storage-backend \
    -H 'Content-Type: application/json' \
    -d '{"hicache_storage_backend":"file"}'

# 卸载
curl -X DELETE http://127.0.0.1:30000/hicache/storage-backend
```

**严格 idle 检查:** 切换要求 scheduler 完全 idle (无 running/waiting 请求)。这是文档 §2 明确要求。

### 2.6 关键启动参数 (来自官方 best_practices L11-19)

```bash
--page-size 64                       # Page size for cache management
--enable-hierarchical-cache          # Enable HiCache
--hicache-ratio 2                    # Host memory ratio (2x GPU memory)
--hicache-size 100                   # GBs,会覆盖 ratio
--hicache-io-backend kernel          # I/O backend between CPU and GPU
--hicache-write-policy write_through # Cache write policy from GPU to CPU
--hicache-storage-backend file       # L3 backend (file/mooncake/hf3fs/nixl/aibrix)
```

启动器: `python -m sglang.launch_server` (per `benchmark/hicache/README.md` L4-22)

---

## 3. 实验设计 (从 sglang 官方 benchmark 派生)

### 3.1 Benchmark 工具选型

来自 `benchmark/hicache/README.md` 与源码:

| 工具 | 用途 | 适用场景 |
|---|---|---|
| `bench_multiturn.py` | 多轮并发请求,模拟真实 chatbot | **主测 (多用户冷启动)** |
| `bench_long_context.py` | 长 context (如 loogle) | 副测 (压测 KV cache 体积) |
| `bench_serving.py` | 标准 serving 压测 (支持 sharegpt/ultrachat/loogle/nextqa) | 副测 (标准负载) |
| `bench_mix.py` | 混合负载 | 视情况 |

**官方用法 (README L11-18):**
```bash
# 启用 HiCache
python -m sglang.launch_server \
    --model-path Qwen/Qwen2.5-14B-Instruct \
    --port 30000 \
    --enable-hierarchical-cache

# 跑多轮 benchmark
python bench_multiturn.py --model-path Qwen/Qwen2.5-14B-Instruct
```

### 3.2 主测配置 (1 cold + 5 warm per round)

**Server (per `hicache_best_practices.md` L80-98 PD 例子,但去掉 PD 部分):**
```bash
python -m sglang.launch_server \
    --model-path /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --port 30000 \
    --page-size 64 \
    --enable-metrics \
    --enable-cache-report \
    --mem-fraction-static 0.7 \
    --enable-hierarchical-cache \
    --hicache-ratio 2 \
    --hicache-size 0 \
    --hicache-mem-layout page_first_direct \
    --hicache-io-backend direct \
    --hicache-write-policy write_through \
    --hicache-storage-backend file \
    --hicache-storage-prefetch-policy timeout
```

**L3 目录:**
```bash
export SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/mnt/ai_ssdN/cache_hicache
```

**Client (官方 bench_multiturn.py,改造为 1 cold + N warm):**

按 `benchmark/hicache/bench_multiturn.py` 默认参数: `--num-clients 256 --max-parallel 128 --request-length 512 --output-length 64 --num-rounds 5`

**但要改两点:**
1. `--num-clients 1 --num-rounds 6` (1 个 client,跑 6 轮 = 1 cold + 5 warm)
2. `--request-length 7000 --output-length 64` (大 prefix,与 AI-SSD 场景匹配)

### 3.3 副测矩阵

| 副测 | 维度变化 | 目的 |
|---|---|---|
| A | `--hicache-write-policy write_back` × BIWIN + Seagate | 验证策略对盘差的影响 |
| B | `--hicache-storage-prefetch-policy wait_complete` × BIWIN | 验证预取策略对延迟的影响 |
| C | (待 Phase 4 后决定) 多用户并发 `--num-clients 32` × BIWIN | 验证多用户场景下 IO 放大 |

### 3.4 4 盘 × 主测 = 4 轮

| Round | 设备 | 挂载点 | L3 目录 |
|---|---|---|---|
| 1 | nvme1n1 (BIWIN, baseline ext4) | / (系统盘) | `/home/ficus/llm/infer/ai_ssd_prestudy/cache/baseline` |
| 2 | nvme0n1 (WDC NTFS) | /mnt/ai_ssd0 | `/mnt/ai_ssd0/cache_hicache` |
| 3 | nvme2n1 (ZHITAI NTFS) | /mnt/ai_ssd1 | `/mnt/ai_ssd1/cache_hicache` |
| 4 | nvme3n1 (Seagate NTFS) | /mnt/ai_ssd2 | `/mnt/ai_ssd2/cache_hicache` |

**串行原则** (沿用 LMCache 时代的硬约束): 任何时刻只测 1 块盘。

---

## 4. 监测方案 (从零设计,不沿用旧脚本)

### 4.1 监测栈

按 SGLang + Linux 工具最佳实践:

| 指标 | 工具 | 命令范式 | 频率 |
|---|---|---|---|
| TTFT / ITL / latency | `bench_multiturn.py` 自带 | `--log-file` 输出 JSONL | 每请求 |
| cache hit / hit rate | SGLang `--enable-cache-report` + `--enable-metrics` | Prometheus `/metrics` | 实时 |
| L3 文件落盘 | `inotifywait` 或 `watch ls` | 事件驱动 | 测后 |
| Block IO (设备级) | `iostat -dx -m 1` | sysstat 12.7+ | 1s |
| 单 IO latency | `bpftrace` `tracepoint:blk_rq_issue/complete` | 见 §4.3 | 事件驱动 |
| Page cache 状态 | `free -m` + `/proc/meminfo` | shell | 测前/后 |

### 4.2 bpftrace 脚本设计 (从零写)

按 HiCache IO 模式特点 (`STORAGE_BATCH_SIZE = 128` pages,每 page ≈ 9.4 MB,
**理论单 IO ≈ 1.2 GB** —— 但实际受盘能力限制会拆为多个 IO):

```c
#!/usr/bin/env bpftrace
// scripts/hicache_blk_io_latency.bt
// 跟踪指定设备 (maj:min) 上所有 block IO 完成的 latency + size

#include <linux/blk_types.h>

BEGIN {
    @start_ts = 0;
}

tracepoint:block:block_rq_issue(
    args->dev == $1 /* major */ << 20 | $2 /* minor */
) {
    @start_ts[args->bio, (uint64)args->dev] = nsecs;
}

tracepoint:block:block_rq_complete(
    args->dev == $1 << 20 | $2
) {
    $start = @start_ts[args->bio, (uint64)args->dev];
    if ($start == 0) { return; }
    @lat_us = (nsecs - $start) / 1000;
    @size_sectors = args->nr_sector;
    @size_kb = @size_sectors * 512 / 1024;
    @count++;
    @total_size_kb += @size_kb;
    @lat_hist = lhist(@lat_us, 100, 1, 1000000);   // 100us → 1s log-binned
    @size_hist = lhist(@size_kb, 1, 1, 1048576);   // 1KB → 1GB log-binned
    delete(@start_ts[args->bio, (uint64)args->dev]);
}

END {
    print("=== HiCache Block IO Latency Distribution ===");
    print(@lat_hist);
    print("\n=== HiCache Block IO Size Distribution ===");
    print(@size_hist);
    print("\n=== Summary ===");
    printf("Total IO count: %d\n", @count);
    printf("Total IO size:  %d KB\n", @total_size_kb);
}
```

> 注:HiCacheFile 实际 IO 由 torch tensor 内存 → `f.write` → 内核 page cache →
> 后台 writeback → 物理盘。所以 bpftrace 抓到的是 **page cache writeback** 的 IO,
> 不是 user-space 调用的 IO。这是 page-cache buffered IO 的固有限制。

### 4.3 iostat 监测脚本 (从零写)

```bash
#!/bin/bash
# scripts/hicache_io_monitor.sh
# 用法: hicache_io_monitor.sh <device> <output_dir>
#   <device>: nvme0n1 / nvme1n1 / nvme2n1 / nvme3n1
#   <output_dir>: iostat log 目录

set -e
DEV=${1:?"device required"}
OUT=${2:?"output_dir required"}
mkdir -p "$OUT"

LOG="$OUT/iostat_$DEV.log"
SUMMARY="$OUT/iostat_summary_$DEV.log"

# 1s 粒度 iostat, 与 SGLang server 共生命周期
iostat -dx -m 1 "$DEV" > "$LOG" &
IOSTAT_PID=$!
echo "iostat pid: $IOSTAT_PID"

# 注册 trap,确保 SIGTERM 也停止 iostat
trap "kill $IOSTAT_PID 2>/dev/null; wait $IOSTAT_PID 2>/dev/null" EXIT TERM INT

# 等待 — 调用者会发 SIGTERM 终止
wait $IOSTAT_PID
```

### 4.4 关键观察点

**Phase 1 必须确认的事** (冒烟阶段):

1. **L3 真的写盘** —— `ls $CACHE_DIR` 有 `.bin` 文件,大小与预期 (cold 0.95 GB) 匹配
2. **HiCache metrics 可见** —— `curl http://127.0.0.1:30000/metrics | grep hicache` 有输出
3. **runtime attach/detach 工作** —— curl PUT 切换 backend 返回 success
4. **TTFT 加速比合理** —— cold ≈ 1s, warm ≈ 0.05s, 加速比 ≈ 20x (与官方 blog 报道一致)
5. **bpftrace 抓到 IO** —— `bpftrace` 退出后 `@count > 0`

---

## 5. 报告与产物 (按 docs/advanced_features 文档结构对齐)

### 5.1 报告目录结构

```
~/llm/infer/ai_ssd_prestudy/
├── docs/
│   ├── hicache-4disk-headline-2026-06-XX.md       # 4 盘 TTFT 对比主表
│   ├── hicache-4disk-io-pattern-2026-06-XX.md     # 4 盘 IO 模式分析
│   ├── hicache-write-policy-comparison-2026-06-XX.md   # write_through vs write_back
│   └── hicache-design-decisions-2026-06-XX.md     # 关键决策记录
├── scripts/
│   ├── setup_sglang.sh                            # 安装 (独立 venv)
│   ├── hicache_serve.sh                           # server 启动 (来自 best_practices)
│   ├── hicache_drive_4_rounds.sh                  # 4 盘串行 driver
│   ├── hicache_bench_one_round.sh                 # 单轮驱动 (基于 bench_multiturn.py)
│   ├── hicache_io_monitor.sh                      # iostat 后台监测
│   └── hicache_blk_io_latency.bt                  # bpftrace 脚本
├── results/
│   └── hicache/
│       ├── baseline_biwin_ext4/                   # Round 1
│       │   ├── iostat_nvme1n1.log
│       │   ├── bpftrace_nvme1n1.log
│       │   ├── server.log
│       │   ├── bench_multiturn.jsonl              # TTFT / ITL / cached_tokens
│       │   ├── metrics_prometheus.log             # /metrics 抓取
│       │   └── cache_file_list.txt                # L3 落盘文件清单
│       ├── ai_ssd0_wdc_ntfs/                      # Round 2
│       ├── ai_ssd1_zhitai_ntfs/                   # Round 3
│       └── ai_ssd2_seagate_ntfs/                  # Round 4
└── cache/                                         # L3 落盘目录 (git ignored)
    ├── baseline/
    ├── ai_ssd0/
    ├── ai_ssd1/
    └── ai_ssd2/
```

### 5.2 headline 报告骨架 (4 盘 TTFT + IO 横向对比)

按 `docs/advanced_features/hicache_design.md` 的"HiCache 三层架构"组织内容:

```markdown
# SGLang HiCache 4 盘 KV Offload 横向对比

## 1. 实验配置 (按官方 best_practices)

| 参数 | 值 | 来源 |
|---|---|---|
| 模型 | Qwen3-4B-Instruct-2507 | 用户选定 |
| page-size | 64 | hicache_best_practices.md L11 |
| hicache-ratio | 2 | hicache_best_practices.md L13 |
| mem-layout | page_first_direct | hicache_best_practices.md L34 |
| io-backend | direct | hicache_best_practices.md L37 |
| write-policy | write_through | hicache_design.md §"Data Write-back" |
| prefetch-policy | timeout | hicache_best_practices.md L65 |
| L3 backend | file | hicache_storage.py::HiCacheFile |
| benchmark | bench_multiturn.py | benchmark/hicache/README.md |

## 2. TTFT 对比

| Phase | baseline (BIWIN ext4) | WDC NTFS | ZHITAI NTFS | Seagate NTFS |
|---|---|---|---|---|
| Cold (s) | ... | ... | ... | ... |
| Warm #1 (s) | ... | ... | ... | ... |
| Warm #2 (s) | ... | ... | ... | ... |
| Warm #5 (s) | ... | ... | ... | ... |
| 加速比 (cold/warm1) | ... | ... | ... | ... |

## 3. IO 模式分析

| 指标 | baseline | WDC | ZHITAI | Seagate |
|---|---|---|---|---|
| L3 写总 IO 数 | ... | ... | ... | ... |
| L3 写总 MB | ... | ... | ... | ... |
| L3 写带宽峰值 (MB/s) | ... | ... | ... | ... |
| L3 写 await P99 (ms) | ... | ... | ... | ... |
| L3 读 await P99 (ms) | ... | ... | ... | ... |
| L3 file count (cold) | ... | ... | ... | ... |
| L3 avg file size (MB) | ... | ... | ... | ... |
| L3 单 IO size P50 (KB) | ... | ... | ... | ... |
| 命中缓存 token 总数 | ... | ... | ... | ... |
| L2 DRAM 命中率 (from /metrics) | ... | ... | ... | ... |

## 4. 关键观察 (按 hicache_design.md 的"设计决策"组织)

### 4.1 L3 file 后端的 IO 模式
- 单 IO size 分布 (bpftrace 数据)
- 写突发 vs 平滑
- 与设计文档 §"Batch-Oriented Data Organization" 的预期对照

### 4.2 各盘的 SLC cache 表现
- 短突发 (cold 1 个请求) vs 长稳态 (5 轮 warm + drop_caches)
- 与 HiCacheFile 9.4 MB/page 单 file 粒度的关系

### 4.3 HiCache 三层的实际利用情况
- L1 GPU 命中率 (from /metrics)
- L2 DRAM 命中率 (from /metrics)
- L3 disk 命中率 (从 warm #1 的真实 disk read 推出)
```

### 5.3 报告与官方文档的引用规范

每条关键结论必须能映射回 `docs/advanced_features/` 文档:
- IO 模式 → `hicache_design.md` §"Data Transfer Optimization"
- 写策略影响 → `hicache_design.md` §"Data Write-back"
- 预取策略 → `hicache_design.md` §"Prefetch from L3"
- 启动参数 → `hicache_best_practices.md` §"Core HiCache Parameters"
- 运行时切换 → `hicache_storage_runtime_attach_detach.md`

---

## 6. 风险与不确定性 (按 sglang 官方文档的不确定项组织)

### 6.1 HiCacheFile 源码层面的已知行为

来自 `hicache_storage.py::HiCacheFile` (lines 319+) 的源码事实:

| 行为 | 源码位置 | 对 AI SSD 选型的影响 |
|---|---|---|
| 走 buffered IO,不支持 O_DIRECT | L389-393 | 实测读性能会受 page cache 影响,需 `drop_caches` 强制冷读 |
| L3 写是 prefill 后台触发 | HiCache controller | 与 LMCache 的 store 后台线程类似 |
| Batch 上限 128 pages (≈ 1.2 GB) | L20 `STORAGE_BATCH_SIZE` | 单请求写 IO 可能被合并成大块 |
| 文件命名带 tp_rank 区分 | L356-364 | TP>1 时 L3 文件不共享 |
| LRUFileEvictor 按 size 淘汰 | storage/file/lru_file_evictor.py | 长跑需要清 cache |

### 6.2 必须先解决的 Blocker

| Blocker | 现状 | 解决方式 |
|---|---|---|
| SGLang 未装在 `~/llm/.venv/` | `pip show sglang` 失败 | 新建独立 venv `~/llm/.venv-sglang/`,避免与 torch 2.11 冲突 |
| bench_multiturn.py 依赖 `sglang.test.kits.cache_hit_kit` | 需 sglang 全量安装 | `pip install "sglang[all]"` |
| 与 LMCache 实验共享 GPU 资源 | lmcache 实验可能残留 vllm 进程 | 测试前 `pkill -f vllm; pkill -f sglang` |
| `drop_caches` 在 page cache buffered IO 下的公平性 | HiCacheFile 走 page cache,writeback 由内核异步 | 测后用 `sync && echo 3 > /proc/sys/vm/drop_caches`,但只能保证 **下一次读** 是冷读;不能保证 iostat 抓到的"写 IO"真实来自 HiCache (内核可能合并) |

### 6.3 与官方文档可能存在出入的不确定项

| 不确定项 | 文档说 | 实际情况待验证 |
|---|---|---|
| `page_first_direct` 实际零拷贝性能 | "same zero-copy performance as page_first" (best_practices L34) | 需测 |
| `timeout` 预取默认 2s + 0.1s/K tokens (hicache_storage.py L42) | 默认 base=2s, per_ki_token=0.1s, max=30s | 实际生产可能需调 |
| HiCacheFile 不支持 O_DIRECT | (源码事实) | 是否需要 `fsync` 强制落盘以让 iostat 抓到? |

---

## 7. 分阶段实施任务 (Bite-sized, 完全从零)

### Phase 0: SGLang 环境准备 (1-2 h)

#### Task 0.1: 装 SGLang 到独立 venv

**Files:**
- Create: `~/llm/infer/ai_ssd_prestudy/scripts/setup_sglang.sh`

**Step 1**: 写安装脚本 (参考 `hicache_best_practices.md` + `benchmark/hicache/README.md` 默认模型 Qwen2.5-14B 推算需要的 cuda 版本)
```bash
#!/bin/bash
# scripts/setup_sglang.sh
set -e
cd ~/llm

# 独立 venv, 不污染 lmcache 时代的 torch 2.11
uv venv .venv-sglang --python 3.12
source .venv-sglang/bin/activate

# SGLang 官方推荐 cu128 (per hicache_best_practices.md 示例)
uv pip install torch==2.7.0 --index-url https://download.pytorch.org/whl/cu128

# SGLang [all] 含所有后端
uv pip install "sglang[all]" --upgrade

# bench 依赖
uv pip install tqdm aiohttp

# 验证
python -c "
import sglang, torch
print(f'sglang: {sglang.__version__}')
print(f'torch: {torch.__version__}, cuda: {torch.cuda.is_available()}')
"
```

**Step 2**: 跑通
```bash
bash scripts/setup_sglang.sh
# Expected: "sglang: 0.4.x" + "torch: 2.7.0+cu128, cuda: True"
```

**Step 3**: 冒烟启动 (按 `benchmark/hicache/README.md` L4-22)
```bash
source ~/llm/.venv-sglang/bin/activate
export SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/tmp/sglang_smoke

python -m sglang.launch_server \
    --model-path /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --port 30000 --max-model-len 8192 --mem-fraction-static 0.7 \
    --page-size 64 --enable-hierarchical-cache \
    --hicache-ratio 2 --hicache-size 0 \
    --hicache-mem-layout page_first_direct --hicache-io-backend direct \
    --hicache-write-policy write_through \
    --hicache-storage-backend file \
    --hicache-storage-prefetch-policy timeout &

SERVER_PID=$!

# 等就绪 (per hicache_best_practices.md,首次加载约 30-60s)
for i in {1..180}; do
    if curl -s http://127.0.0.1:30000/v1/models > /dev/null 2>&1; then
        echo "server ready after ${i}s"
        break
    fi
    sleep 1
done

# smoke test (per README L11-18)
curl -s http://127.0.0.1:30000/v1/models | python -m json.tool

kill $SERVER_PID
```

**Expected**: 返回 `{"data":[{"id":"Qwen/Qwen3-4B-Instruct-2507",...}]}`

#### Task 0.2: 下载官方 benchmark 数据集 (可选,Phase 2 需要)

**Step 1**: 按 `benchmark/hicache/download.sh` (README L38-40)
```bash
cd ~/llm/infer/ai_ssd_prestudy
mkdir -p benchmark_data
cd benchmark_data
# 官方下载脚本路径 (待确认是相对 sglang repo 根目录)
# 如不可访问,可改用 huggingface ShareGPT 数据集
```

> 注: 若不想下载,Phase 1 可用 `bench_multiturn.py` 的随机负载模式
> (`--request-length 7000 --output-length 64`)

#### Task 0.3: git 配置 (确保 cache 不入 commit)

**Step 1**: 修改 `.gitignore`
```bash
cd ~/llm/infer/ai_ssd_prestudy
cat >> .gitignore << 'EOF'

# HiCache L3 落盘目录
cache/
results/hicache/*/cache/
*.bin
EOF
```

### Phase 1: 单盘冒烟 (BIWIN ext4 baseline) — 半天

#### Task 1.1: 启动脚本 `hicache_serve.sh`

**Files:**
- Create: `~/llm/infer/ai_ssd_prestudy/scripts/hicache_serve.sh`

**完全按 `hicache_best_practices.md` §"Deployment with HF3FS" 例子的 P-D 去掉 PD 部分 (L80-98):**

```bash
#!/bin/bash
# scripts/hicache_serve.sh
# 按 hicache_best_practices.md L80-98 (去掉 PD 部分) 启动 SGLang HiCache
# 用法: hicache_serve.sh <cache_dir> [write_policy]
set -e

CACHE_DIR=${1:?"cache_dir required"}
WRITE_POLICY=${2:-write_through}

source ~/llm/.venv-sglang/bin/activate
export SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR="$CACHE_DIR"
rm -rf "$CACHE_DIR"/*
mkdir -p "$CACHE_DIR"

python -m sglang.launch_server \
    --model-path /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --port 30000 \
    --page-size 64 \
    --enable-metrics \
    --enable-cache-report \
    --mem-fraction-static 0.7 \
    --enable-hierarchical-cache \
    --hicache-ratio 2 \
    --hicache-size 0 \
    --hicache-mem-layout page_first_direct \
    --hicache-io-backend direct \
    --hicache-write-policy "$WRITE_POLICY" \
    --hicache-storage-backend file \
    --hicache-storage-prefetch-policy timeout
```

#### Task 1.2: iostat 监测脚本 `hicache_io_monitor.sh`

**Files:**
- Create: `~/llm/infer/ai_ssd_prestudy/scripts/hicache_io_monitor.sh`

(见 §4.3 完整脚本)

#### Task 1.3: bpftrace IO latency 脚本 `hicache_blk_io_latency.bt`

**Files:**
- Create: `~/llm/infer/ai_ssd_prestudy/scripts/hicache_blk_io_latency.bt`

(见 §4.2 完整脚本)

#### Task 1.4: 单轮驱动 `hicache_bench_one_round.sh`

**Files:**
- Create: `~/llm/infer/ai_ssd_prestudy/scripts/hicache_bench_one_round.sh`

```bash
#!/bin/bash
# scripts/hicache_bench_one_round.sh
# 用法: hicache_bench_one_round.sh <round_name> <device> <cache_dir>
set -e

ROUND=$1
DEV=$2
CACHE_DIR=$3
OUT=~/llm/infer/ai_ssd_prestudy/results/hicache/$ROUND
mkdir -p "$OUT"

source ~/llm/.venv-sglang/bin/activate

# 1. 启动 iostat
nohup bash ~/llm/infer/ai_ssd_prestudy/scripts/hicache_io_monitor.sh "$DEV" "$OUT" \
    > "$OUT/iostat_runner.log" 2>&1 &
IOSTAT_PID=$!
echo "iostat pid: $IOSTAT_PID"

# 2. 启动 bpftrace (需要 sudo, 取设备 maj:min)
MAJ_MIN=$(ls -l /dev/$DEV | awk '{print $5":"$6}' | tr ',' ':')
MAJOR=$(echo $MAJ_MIN | cut -d: -f1)
MINOR=$(echo $MAJ_MIN | cut -d: -f2)

nohup sudo -n bpftrace ~/llm/infer/ai_ssd_prestudy/scripts/hicache_blk_io_latency.bt \
    "$MAJOR" "$MINOR" > "$OUT/bpftrace_$DEV.log" 2>&1 &
BPF_PID=$!
echo "bpftrace pid: $BPF_PID"

# 3. 启动 SGLang server
nohup bash ~/llm/infer/ai_ssd_prestudy/scripts/hicache_serve.sh "$CACHE_DIR" \
    > "$OUT/server.log" 2>&1 &
SERVER_PID=$!
echo "server pid: $SERVER_PID"

# 4. 等 server 就绪
for i in {1..180}; do
    if curl -s http://127.0.0.1:30000/v1/models > /dev/null 2>&1; then
        echo "server ready after ${i}s"
        break
    fi
    sleep 1
done

# 5. 第一次 warm 前 drop page cache, 确保 cold read 真实走盘
sync && sudo -n sh -c 'echo 1 > /proc/sys/vm/drop_caches'

# 6. 抓 /metrics baseline
curl -s http://127.0.0.1:30000/metrics > "$OUT/metrics_before.json" 2>/dev/null || true

# 7. 跑 benchmark (按 README L11-18 改造: 1 client, 6 rounds = 1 cold + 5 warm)
# 注: bench_multiturn 默认 --num-clients 256, 我们改成 1
cd ~/llm/infer/ai_ssd_prestudy
python benchmark/hicache/bench_multiturn.py \
    --model-path /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --port 30000 \
    --num-clients 1 \
    --max-parallel 1 \
    --request-length 7000 \
    --output-length 64 \
    --num-rounds 6 \
    --request-rate 1.0 \
    --log-file "$OUT/bench_multiturn.jsonl" \
    2>&1 | tee "$OUT/bench_multiturn.log"

# 8. warm #1 之前再次 drop cache (测真实 disk read)
# (在 bench_multiturn 6 rounds 中: round 1=cold, round 2=warm#1=drop后冷读, 3-6=warm)
# 实际需要在 round 2 之前 drop, 但 bench_multiturn 没有 hook 点
# 简化: 测后 drop 再补一发 warm 单独测

# 9. 抓 /metrics after
curl -s http://127.0.0.1:30000/metrics > "$OUT/metrics_after.json" 2>/dev/null || true

# 10. 收尾
kill $SERVER_PID $IOSTAT_PID $BPF_PID 2>/dev/null || true
sleep 5

# 11. 收集 L3 落盘文件清单
ls -la "$CACHE_DIR" | awk 'NR>3 {print $5, $9}' > "$OUT/cache_file_list.txt"
echo "L3 file count: $(wc -l < $OUT/cache_file_list.txt)"
echo "L3 total size: $(du -sh $CACHE_DIR | cut -f1)"

# 12. 清理 L3, 为下一轮准备
rm -rf "$CACHE_DIR"/*

echo "==== ROUND $ROUND DONE ===="
```

#### Task 1.5: 跑 BIWIN baseline round

```bash
cd ~/llm/infer/ai_ssd_prestudy
bash scripts/hicache_bench_one_round.sh baseline_biwin_ext4 nvme1n1 \
    /home/ficus/llm/infer/ai_ssd_prestudy/cache/baseline
```

**Expected:**
- server.log 显示启动成功
- iostat_nvme1n1.log 有 200-300 行 (5 分钟 @ 1s)
- bench_multiturn.jsonl 有 6 行 TTFT 数据
- cache_file_list.txt 有 30-100 行 (HiCache page-level)
- L3 total size ≈ 100 MB - 1 GB (per cold 7000-token req)

#### Task 1.6: 验证冒烟 4 项关键

```bash
# 1. L3 真的写盘
ls -la ~/llm/infer/ai_ssd_prestudy/cache/baseline/ | head
du -sh ~/llm/infer/ai_ssd_prestudy/cache/baseline/

# 2. HiCache metrics 可见
grep -E "hicache|sglang:" ~/llm/infer/ai_ssd_prestudy/results/hicache/baseline_biwin_ext4/metrics_after.json | head

# 3. TTFT 加速比合理 (cold ~1s, warm ~0.05s)
python -c "
import json
with open('/home/ficus/llm/infer/ai_ssd_prestudy/results/hicache/baseline_biwin_ext4/bench_multiturn.jsonl') as f:
    lines = [json.loads(l) for l in f if l.strip()]
for i, l in enumerate(lines[:6]):
    print(f'round {i+1}: ttft={l.get(\"ttft\", \"?\"):.3f}s')
"

# 4. bpftrace 抓到 IO
grep "Total IO count" ~/llm/infer/ai_ssd_prestudy/results/hicache/baseline_biwin_ext4/bpftrace_nvme1n1.log
```

### Phase 2: 4 盘串行 driver + headline 报告 — 半天

#### Task 2.1: 4 盘串行 driver `hicache_drive_4_rounds.sh`

**Files:**
- Create: `~/llm/infer/ai_ssd_prestudy/scripts/hicache_drive_4_rounds.sh`

```bash
#!/bin/bash
# scripts/hicache_drive_4_rounds.sh
# 按官方 docs/advanced_features/* 推荐的 4 盘串行测试 (与 LMCache 实验同序)
set -e

declare -A ROUNDS=(
    ["baseline_biwin_ext4"]="nvme1n1:/home/ficus/llm/infer/ai_ssd_prestudy/cache/baseline"
    ["ai_ssd0_wdc_ntfs"]="nvme0n1:/mnt/ai_ssd0/cache_hicache"
    ["ai_ssd1_zhitai_ntfs"]="nvme2n1:/mnt/ai_ssd1/cache_hicache"
    ["ai_ssd2_seagate_ntfs"]="nvme3n1:/mnt/ai_ssd2/cache_hicache"
)

cd ~/llm/infer/ai_ssd_prestudy

for round in "${!ROUNDS[@]}"; do
    IFS=':' read -r dev cache_dir <<< "${ROUNDS[$round]}"
    echo "==== START: $round on $dev ===="
    bash scripts/hicache_bench_one_round.sh "$round" "$dev" "$cache_dir"
    echo "==== DONE: $round ===="
    sleep 30  # 盘冷却
done

echo "==== ALL 4 ROUNDS DONE ===="
```

#### Task 2.2: 后台跑 4 盘

```bash
cd ~/llm/infer/ai_ssd_prestudy
nohup bash scripts/hicache_drive_4_rounds.sh > /tmp/hicache_4rounds.log 2>&1 &
echo $! > /tmp/hicache_4rounds.pid
```

**预期耗时**: 4 × 5 分钟 = 20 分钟 server 启动 + benchmark + drop_caches

#### Task 2.3: 写 4 盘 headline 报告

**Files:**
- Create: `~/llm/infer/ai_ssd_prestudy/docs/hicache-4disk-headline-2026-06-XX.md`

按 §5.2 的骨架填数据。

### Phase 3: 写策略对比 (可选) — 半天

#### Task 3.1: write_back × BIWIN + Seagate

**修改**: `hicache_bench_one_round.sh` 加 `--write-policy` 参数,跑 2 轮
```bash
bash scripts/hicache_bench_one_round.sh writeback_biwin_ext4 nvme1n1 \
    /home/ficus/llm/infer/ai_ssd_prestudy/cache/writeback_biwin \
    write_back
```

#### Task 3.2: 写策略对比报告

**Files:**
- Create: `~/llm/infer/ai_ssd_prestudy/docs/hicache-write-policy-comparison-2026-06-XX.md`

---

## 8. 决策记录 (Decision Log)

| 日期 | 决策 | 来源 |
|---|---|---|
| 2026-06-11 | L3 后端选 `file` (不用 mooncake/hf3fs/nixl) | 本机 NVMe,无需 RDMA/K8s/GDS |
| 2026-06-11 | mem-layout 选 `page_first_direct` + io-backend `direct` | hicache_best_practices.md L34 + L37 兼容性最广 |
| 2026-06-11 | write-policy 主测 `write_through` | hicache_best_practices.md L143 默认例子 |
| 2026-06-11 | prefetch-policy `timeout` | hicache_best_practices.md L65 (官方推荐生产) |
| 2026-06-11 | page-size 64 (官方默认) | hicache_best_practices.md L11 |
| 2026-06-11 | hicache-ratio 2 (L2 = 2x L1) | hicache_best_practices.md L13 |
| 2026-06-11 | 模型 Qwen3-4B | 与 lmcache 时代实验可比 |
| 2026-06-11 | benchmark 工具 `bench_multiturn.py` (官方) | benchmark/hicache/README.md L11-18 |
| 2026-06-11 | 不用之前 lmcache 的脚本 | 用户明确指令 |
| 2026-06-11 | 独立 venv `~/llm/.venv-sglang/` | 避免污染 torch 2.11 (lmcache 时代) |

---

## 9. 待用户决策的问题

### Q1: SGLang 版本

- **A**: 最新 stable (从 PyPI 拉,跟随官方 main 分支) — 推荐
- **B**: 锁版本 (指定某个 release tag)

### Q2: 副测范围

- **A**: 只跑主测 (4 盘 × write_through) — 半天
- **B**: A + write_back × 2 盘对照 — 1 天
- **C**: A + B + 多用户并发 (--num-clients 32) — 2 天

### Q3: 数据集

- **A**: 用 bench_multiturn.py 的随机负载 (`--request-length 7000 --output-length 64`)
- **B**: 下载官方 benchmark 数据集 (ShareGPT / Loogle) — 更真实

### Q4: 与 LMCache 时代的报告对照

- **A**: 不主动对照 (HiCache 独立报告)
- **B**: 在 headline 报告末尾加 "与 LMCache 实验对照" 章节 — 需要重读 LMCache 报告

### Q5: GPU 冲突处理

- **A**: 测试前 `pkill -f vllm`,确保独占
- **B**: 与 lmcache 时代实验交错进行

---

## 10. 验收清单 (Definition of Done)

- [ ] Phase 0 完成: SGLang 装好,smoke test 通过,/v1/models 返回正确
- [ ] Phase 1 完成: BIWIN baseline round 数据齐全 (iostat / bpftrace / bench_multiturn / cache 文件)
- [ ] Phase 2 完成: 4 盘 headline 数据齐全,`hicache-4disk-headline-2026-06-XX.md` 已写
- [ ] Phase 3 (可选): write_back 对照完成
- [ ] 所有脚本在 `scripts/` 前缀 `hicache_`
- [ ] 所有报告在 `docs/` 前缀 `hicache-`
- [ ] `.gitignore` 已排除 `cache/` 目录
- [ ] 所有 commit 通过 git push 备份
- [ ] 文档引用全部映射回 `docs/advanced_features/` 官方文档章节

---

## 11. 文档引用映射表 (审计用)

| 我的脚本/报告 | 引用的官方文档章节 |
|---|---|
| `hicache_serve.sh` | hicache_best_practices.md §"Core HiCache Parameters" + §"Deployment with HF3FS" (去 PD) |
| `hicache_io_monitor.sh` | sysstat 12.7+ 文档 (sglang 无依赖,但需符合其 IO 监测范式) |
| `hicache_blk_io_latency.bt` | linux kernel `tracepoint:block:*` (sglang 无依赖) |
| `hicache_bench_one_round.sh` | benchmark/hicache/README.md §"Run synthetic multi-turn benchmark" |
| `hicache-4disk-headline-*.md` | docs/advanced_features/hicache_design.md §"Overall Architecture" + §"Data Transfer Optimization" + §"Data Write-back" + §"Prefetch from L3" |
| Phase 3 写策略报告 | hicache_design.md §"Data Write-back" 三策略对照 |
| Q&A 引用 | docs/advanced_features/hicache_storage_runtime_attach_detach.md (备用切换机制) |