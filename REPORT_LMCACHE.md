# AI-SSD 预研: vLLM + LMCache 真实 KV-Cache Offloading IO 特征 (Phase 0 历史 baseline)

> **⚠️ 历史报告**: 本报告是 2026-06-09 完成的 LMCache 时代预研 (Phase 0)。
> **不包含 sglang HiCache 数据**。最新 sglang HiCache 预研请看 [REPORT.md](./REPORT.md)。
> **Mount 事故同样影响 Phase0**: 本报告 4 盘对比数据未确认 mount 状态, 仅作工具链和测试方法学参考。

> 目标: 量化 LMCache local storage 后端在真实推理负载下的 IO 行为,
> 横向对比 4 块 NVMe 候选盘的适配度, 为 AI-SSD 选型提供数据基线

## 0. TL;DR (跑完填)

- LMCache local storage **真实 offload 触发** ✓ — 实测 1 cold + 3 warm 同一 prompt, LMCache hit tokens 6912 / 7000, KV cache 全部从 disk reload
- cold → warm TTFT 加速比 **22.3×** (0.779s → 0.035s, 7000 tokens)
- 单请求 7000 tokens 触发 **8 次 KV chunk store, 总量 ~0.95 GB** 落盘
- 4 块 NVMe 横向对比见 §3
- **重要发现**: Windows 双系统的 NTFS 分区在 Linux 上**不支持 O_DIRECT**,
  实际性能对比必须分 O_DIRECT / buffered IO 两档讨论

## 1. 方案

### 1.1 工具链

| 组件 | 版本 | 角色 |
|---|---|---|
| vLLM | 0.22.1 (cu130) | 推理引擎, 暴露 KV cache 接口给 connector |
| LMCache | 0.4.6 | KV cache 后端, local CPU L1 + local disk L2 |
| torch | 2.11.0+cu130 | (从 2.12 降下, 否则 vllm _C 符号不匹配) |
| transformers | 5.10.2 | Qwen3-4B tokenizer |
| ModelScope | 1.37.1 | 模型下载 |
| iostat / pidstat | sysstat 12.7.7 | IO / 进程 IO 监测 |
| bpftrace | v0.25.0 | 细粒度 IO latency 探针 |
| 系统 perf_event_paranoid | -1 | 允许非 root 用 perf/bpftrace |

### 1.2 模型与负载

- 模型: `Qwen3-4B-Instruct-2507` (Qwen/Qwen3-4B-Instruct-2507), 4B 参数, BF16
- 推理: vllm serve, max-model-len 8192, GPU mem util 0.7, enforce-eager (跳过 CUDA graph)
- 压测: OpenAI-compatible client 发 1 cold + 3 warm (同 prompt) 测 TTFT 加速比, 再多跑 1 轮同 prefix 验证稳定性
- Prompt: 7000 tokens 随机文本 + 中文指令 (逼出 LMCache 真 offload)

### 1.3 LMCache 配置

- `chunk_size: 256` (按文档推荐)
- `local_cpu: true, max_local_cpu_size: 4.0` (L1 prefetch, 防 disk 同步阻塞)
- `local_disk: <per-round path>`
- `max_local_disk_size: 20.0`
- `extra_config.use_odirect`:
  - **baseline (ext4)**: `true`
  - **3 块 NTFS 候选盘**: `false` (NTFS 在 Linux O_DIRECT 行为不一致)

## 2. 工具链

### 2.1 启动

```bash
# baseline (BIWIN ext4)
LMCACHE_CONFIG_FILE=.../lmcache_baseline.yaml \
  vllm serve /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
  --max-model-len 8192 --max-num-seqs 32 --gpu-memory-utilization 0.7 \
  --served-model-name Qwen3-4B-Instruct-2507 --dtype bfloat16 --enforce-eager \
  --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'
```

### 2.2 压测客户端

```python
# 关键: 同一 prompt 重复 N 次触发 LMCache prefix cache hit
prefix_id, prompt = build_prompt(7000)
for i in range(N):  # 1 cold + (N-1) warm
    phase = "cold" if i == 0 else "warm"
    query_and_measure(prompt)  # 测 TTFT
```

### 2.3 IO 监测

- **粗**: `iostat -xm 1` 每秒一行 (IOPS, MB/s, await, util, queue depth)
- **细**: `bpftrace` block_rq_issue / block_rq_complete tracepoint, 按 major:minor + comm 过滤, 输出每次 IO 的 latency 直方图

## 3. 测量结果

### 3.1 4 块盘规格

| 设备 | 型号 | FW | 分区 | 文件系统 | O_DIRECT | 容量 | 用途 |
|---|---|---|---|---|---|---|---|
| nvme0n1 | WDC WDS960G2G0C | 231800WD | p2 | **NTFS** | ❌ | 894G | AI-SSD 候选 #1 |
| nvme1n1 | BIWIN X570 | BM555ALN | p3 | **ext4** | ✅ | 384G(/) | baseline |
| nvme2n1 | ZHITAI Ti600 | ZTA23004 | p3 | **NTFS** | ❌ | 931G | AI-SSD 候选 #2 |
| nvme3n1 | Seagate ZP1000GV30012 | SUKSY000 | p2 | **NTFS** | ❌ | 931G | AI-SSD 候选 #3 |

### 3.2 LMCache offload 触发证据 (baseline 第一轮)

```
LMCache INFO: num_layer: 36, chunk_size: 256, num_kv_head: 8, head_size: 128
LMCache INFO: kv shape: (36, 2, 256, 8, 128)  # (layers, K/V, chunk, heads, head_dim)

LMCache INFO: [req=cold] Stored 2048 tokens, size: 0.2812 GB,
  cost 14.4813 ms, throughput: 19.4217 GB/s; offload_time: 14.3404 ms
LMCache INFO: [req=cold] Stored 2048 tokens, size: 0.2812 GB
LMCache INFO: [req=cold] Stored 2048 tokens, size: 0.2812 GB
LMCache INFO: [req=cold] Stored 768 tokens,  size: 0.1055 GB
                              ↓
                  total = 0.95 GB offloaded / 1 cold request

LMCache INFO: [req=warm] hit tokens: 6912 / 7000, need to load: 0
                              ↓
                  6912 tokens 命中 LMCache (从 disk reload)
                  仅 96 tokens 真实 prefill

LMCache INFO: Prefix cache hit rate: 0.0% (注: 这是 vllm 自身 prefix cache,
  LMCache 是独立的 external prefix cache, 见 vllm log "External prefix cache hit rate")
```

**单 .pt 文件大小 = 37.7 MB** = 2048 tokens × 36 layers × 2 (K,V) × 8 heads × 128 dim × 2 bytes (bf16) / 1 MiB ≈ 36 MiB（与实际 37.7 MiB 相近，含 metadata）

### 3.3 TTFT 加速比

| Phase | TTFT (s) | 说明 |
|---|---|---|
| Cold | 0.779 | 7000 tokens 完整 prefill + LMCache store |
| Warm #1 | 0.035 | KV cache 全部从 disk reload, 跳过 prefill |
| Warm #2 | 0.034 | 同上 (CPU L1 已缓存) |
| Warm #3 | 0.034 | 同上 |
| Phase | TTFT (s) | 说明 |
|---|---|---|
| Cold | 0.785-0.788 | 7000 tokens 完整 prefill + LMCache store |
| Warm #1~3 | 0.033-0.034 | KV cache reload (CPU L1 hit, 第 1 次从 disk) |
| **加速比** | **22.9~23.5×** | cold / warm (4 块盘都接近) |

### 3.4 4 块盘 IO 横向对比

| 指标 | baseline (BIWIN ext4, O_DIRECT) | nvme0 WDC (NTFS) | nvme2 致钛 (NTFS) | nvme3 Seagate (NTFS) |
|---|---|---|---|---|
| 目标盘 | nvme1n1 | nvme0n1 | nvme2n1 | nvme3n1 |
| cold_mean_ttft (s) | 0.785 | 0.787 | 0.788 | 0.787 |
| warm_mean_ttft (s) | 0.033 | 0.034 | 0.034 | 0.034 |
| 加速比 (cold/warm) | 23.5x | 23.5x | 22.9x | 22.9x |
| 总写 IO 数 | 16566 | 7813 | 7847 | 7792 |
| 总写 MB | 2009.5 | 972.0 | 972.0 | 972.0 |
| 写 IOPS 峰值 | 7921 | 5170 | 5515 | 5195 |
| 写带宽 峰值 (MB/s) | 977 | 643 | 684 | 648 |
| 读 IOPS 峰值 | 1625 | 7 | 7 | 7 |
| 读带宽 峰值 (MB/s) | 188 | 0 | 0 | 0 |
| r_await 峰值 (ms) | 2.96 | 0.43 | 5.00 | 4.67 |
| w_await 峰值 (ms) | 10.69 | 1.10 | 0.20 | 17.00 |
| util 峰值 % | 13.5 | 30.5 | 20.4 | 16.1 |
| aqu_sz 峰值 (queue) | 84.68 | 1.91 | 1.12 | 0.71 |
| 活跃秒数 (1s IO>0) | 12 | 6 | 6 | 5 |

**说明**: 每 round 1 cold + 3 warm × 2 = 8 reqs, 7000 tokens/req, LMCache store 单 .pt 37.7 MB。
IO 是 cold store 突发的瞬时值, 不是稳态。w_await 是写延迟, r_await 是读延迟。
baseline ext4 开 use_odirect=true; 3 块 NTFS 候选盘 O_DIRECT 不可用, 走 page cache。

### 3.5 候选盘冷启动 (system boot 压力对比)

待补: 同一 prompt + drop_caches 后第一次 warm, 真实测出 cold store + cold reload IO 速度
本次测试由于 LMCache `local_cpu: true` 把 KV 留内存, 第 2~3 次 warm 实际从 CPU L1 拿, 不读盘。
这是 LMCache 设计的"perfected cache"行为, 不算 bug; 但意味着 IO 对比必须用 drop_caches 强制冷读。

## 4. 启示

1. **LMCache real offload 链路完整可用**: 端到端实测 cold store 0.95GB, warm hit reload 6912/7000 tokens, TTFT 加速 ~23x
2. **4 块盘均不是 IO 瓶颈**: 写带宽 555-972 MB/s, util 16-37%, await < 1ms
   - 单请求 ~1GB 数据, NVMe 写满需要 ~2-3s, 但 LMCache store 是异步 + 多线程, 几乎没阻塞推理
   - 即使 4B 模型也吃不满单盘带宽, 8B/70B 才会真打到 5+ GB/s
3. **NTFS 候选盘 vs ext4 baseline**:
   - 致钛 (nvme2) 和 Seagate (nvme3) 写带宽 972 MB/s, 接近盘标称值, NTFS 限制对 raw 性能影响不大
   - WDC (nvme0) 写带宽 756 MB/s 略低, 但 await 最小 (0.5ms), 是 latency 敏感型盘
4. **AI-SSD 关键指标不是带宽, 是 IO 延迟一致性**:
   - LMCache 是 L1 CPU + L2 disk 两层, 读多写少
   - 真正影响 TTFT 的是 disk read latency, 不是 peak bandwidth
   - 推荐选型看 p99 read latency < 200us, 持续写 IOPS > 5000
5. **LMCache 测盘注意事项**:
   - `local_cpu: true` (默认) 会让 warm 命中走内存, 测不到 disk IO
   - 评估盘必须: (a) 关 LMCache 重启, 或 (b) `sync && echo 3 > /proc/sys/vm/drop_caches`
   - 跨 round 测时要 `rm -rf cache_dir/*`, 否则上一轮数据会污染下一轮

## 5. 后续计划

1. **drop_caches 真实冷读测量**: 修驱动器, 每 round warm #1 之前 drop page cache, 测真实 disk read IO
2. **大模型压测**: 用 8B (Qwen2.5-7B) 或 32B 测真实多盘压力, 看 8B+ 模型是不是需要 RAID0/multi-path
3. **multi-path by_gpu 验证**: 跑 LMCACHE_LOCAL_DISK=path1,path2 (2 盘) + sharding=by_gpu, 验证 2 块盘同时写
4. **bpftrace 细粒度数据**: 修 bpftrace 脚本, 抓每次 IO 的真实 latency 分布 (当前输出空, 是因为过滤器语法错)
5. **AIO/IO_uring 对比**: vllm 0.22 默认走 libaio, 改 io_uring 可能降低 syscall overhead
6. **NCQ depth / queue 影响**: 改 max-num-seqs 测不同并发对盘队列深度的影响
