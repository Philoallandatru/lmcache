# Mooncake SSD Offload 主测结果报告

**日期**: 2026-06-26 12:34-12:51
**模型**: Qwen3-4B-Instruct-2507 (HF)
**GPU**: RTX 5080 16GB (单卡,TP=1)
**测试**: 8 clients × 6 rounds × 4096 input × 1 output = 48 请求/配置
**Mooncake**: 8GB DRAM pool (TCP localhost, P2PHANDSHAKE)

---

## 总表

| 配置 | avg TTFT | P90 TTFT | P99 TTFT | Input tput | 总体 Cache% |
|---|---|---|---:|---:|---:|---:|
| GPU only | 5.166s | 12.363s | 12.431s | 3652 tok/s | 4.2% |
| HiCache L1+L2 | 4.939s | 12.726s | 12.763s | 3744 tok/s | 14.3% |
| +Mooncake (no SSD) | 4.550s | 12.665s | 12.698s | 3966 tok/s | 19.7% |
| **+Mooncake +SSD** | 4.546s | 12.672s | 12.693s | 3969 tok/s | 19.6% |

## Per-Round 对比 (TTFT / Cache Hit%)

| 配置 | R0 | R1 | R2 | R3 | R4 | R5 |
|---|---|---|---:|---:|---:|---:|---:|
| GPU only | 0.71s / 0% | 1.92s / 13% | 3.52s / 8% | 5.00s / 9% | 8.24s / 0% | 11.61s / 0% |
| HiCache L1+L2 | 0.72s / 0% | 1.38s / **47%** | 2.70s / **37%** | 4.69s / 19% | 8.26s / 4% | 11.88s / 0% |
| Mooncake | 0.76s / 0% | 1.20s / **44%** | 2.77s / **31%** | 4.80s / 17% | 6.98s / **20%** | 10.78s / **10%** |
| Mooncake+SSD | 0.76s / 0% | 1.21s / **44%** | 2.77s / **31%** | 4.78s / 17% | 6.98s / **20%** | 10.78s / **10%** |

## 关键发现

### 1. Mooncake 在 R4/R5 明显优于 HiCache L1+L2
- HiCache L1+L2 在 R4 丢到 4%,R5 归零 (0%)
- Mooncake 在 R4 维持 20%,R5 维持 10%
- 这是 **Mooncake 8GB DRAM pool 的 cache persistence 效果**

### 2. Mooncake+SSD vs Mooncake 完全一致
- 所有 run 数据完全一致 (TTFT、cache hit、throughput)
- 说明 **SSD offload 路径没被触发**
- 原因: `MOONCAKE_OFFLOAD_FILE_STORAGE_PATH` 环境变量没有传递到 sglang 内置的 mooncake client (sglang 0.5.13 已知限制)

### 3. GPU 是限制因素
- 所有配置的 TTFT 随时间快速劣化 (R0 0.7s → R5 11.6s)
- context 长度积累 → sglang decode 变慢 → 大量请求排队
- 这是 sglang 单 GPU 的调度限制,不是 SSD 或缓存能解决的

## 下一步行动

| # | 行动 | 原因 |
|---|---|---|
| 1 | **环境变量传 SSD offload 路径** | offload api 在 sglang 内部没继承 env vars |
| 2 | **升压力到 clients=12/16 或 request_length=6144** | 8G segment + 8G host memory 目前足够,没触发 pool cliff |
| 3 | **加 iostat 采集** | IO 数据缺失,不能做设备层归因 |
| 4 | **减少 rounds 到 4 + 升 clients** | 6 round 后 TTFT 已爆高 (10s+),数据价值下降 |

## 已知的文档修正

| # | 文档 | 修正内容 |
|---|---|---|
| 1 | §1 | "nvidia-smi 不可用" → 已恢复 (595.71.05) |
| 2 | §2.2 | "NTFS 只读" → 可写 (2.7 GB/s) |
| 3 | §3.3 | 新增 venv 激活步骤 |
| 4 | §5.5 | master 去掉 http_metadata_server (单机 P2P) |
| 5 | §5.5 | client 用 --protocol=tcp 代替 rdma |
| 6 | §5.5 | MOONCAKE_GLOBAL_SEGMENT_SIZE 必须非 0 (sglang 内置 client) |
| 7 | §5.5 | 新增 set -u 下 activate 的 LD_LIBRARY_PATH 保护 |
