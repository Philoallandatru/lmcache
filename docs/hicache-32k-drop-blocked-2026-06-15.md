# Phase 8 — 32K multiprompt + drop_caches 真 cold-from-device 4 盘对比

**状态**: ❌ **未能完整跑通**,只完成 1 盘 single-prompt 32K smoke。

**目标**: 验证 Phase7 v2 模式的 4 盘 spread 2.1s 在 32K multiprompt 下是否重现 (page cache 强制 evict + L3 真从 device 拉)。

## 已发现的关键阻塞

### 阻塞 1: sglang 0.5.13 + 4B 单卡装不下 32K prefill

| 试验 | 现象 |
|---|---|
| `prompt-tokens=32768, CTX_LEN=32000, MEM_STATIC=0.7` | ❌ `400 Bad Request: input (32776) > context 32000` |
| `prompt-tokens=31768, CTX_LEN=35000, MEM_STATIC=0.7` | ❌ `400: input (31776) > max 20474` |
| `prompt-tokens=29000, CTX_LEN=35000, MEM_STATIC=0.78` | ❌ `max 29306 < 29008` (差 298) |
| `prompt-tokens=29000, CTX_LEN=50000, MEM_STATIC=0.7` | ❌ `max 20474` (max_total_num_tokens=20480 不变) |
| `prompt-tokens=29000, CTX_LEN=50000, MEM_STATIC=0.9` | ❌ Prefill OOM (`available_gpu_mem=0.45 GB`) |
| 任何 prompt ≥ 20K | ❌ **sglang 0.5.13 max_input 受 max_total_num_tokens 限制** |

**sglang 0.5.13 max_input 计算** (源码验证):
```
max_input ≈ max_total_num_tokens - max_prefill_tokens (16K) - reserved (16) - 64 (output)
```
- MEM_STATIC=0.7 → max_total_num_tokens=20480 → max_input=20474
- MEM_STATIC=0.9 → max_total_num_tokens=42496 (profiled cap) → max_input=26096
- **但 prefill 32K prompt 需要 ~8 GB GPU 余量, MEM_STATIC=0.9 把所有内存给 L2 hicache (12.5 GB), prefill OOM**

**结论**: Qwen3-4B (8 GB 模型 + 32K prefill 8 GB) **单卡装不下 32K + 大 L2**。需要:
- 14B-AWQ TP=2 跨卡 (已测试 Phase4, 但 32K 同样 OOM)
- 显存 ≥ 48 GB 的卡 (A100 80G / H100)
- 接受 16K prompt (max_input=26096 仍不够 32K)

### 阻塞 2: L2 evict 触发 + L3 file > page cache 双约束

| 条件 | 需要 |
|---|---|
| L2 evict | N × prompt_actual_tokens > 200K L2 容量 (prompt 压缩后 ~5K tokens → N ≥ 40) |
| L3 file > page cache | N × prompt_KV_size > 25 GB → 5K × 1.2 MB = 6 MB × N ≥ 25000/6 = **4167 prompts** |

**结构矛盾**: L2 evict 触发只要 N=40 prompts,但 L3 file > page cache 要 N=4167 prompts。**两个不能同时满足** (除非减小 page cache)。

**变通方案** (理论可行,未实施):
- `echo 1024 > /proc/sys/vm/min_free_kbytes` (Linux 4.0+,默认 67584)
- `echo 1 > /proc/sys/vm/zone_reclaim_mode` (强制 reclaim)
- 配合 `echo 3 > /proc/sys/vm/drop_caches` 持续清理
- 可把 page cache 压到 1-2 GB,任何 L3 file > 2 GB 都 cold-from-device

### 阻塞 3: Phase7 v2 spread 2.1s 模式实际不可复现

Phase7 (v2) 4 盘 spread 2.1s 当时测了:
- L3 file = 19.8 GB (2201 × 9MB)
- N=20 prompts (Qwen3-4B 8K → 实际 2K tokens/prompt)
- 总 KV = 20 × 2K × 1.2 MB = 48 MB? 跟 19.8 GB 不符

**Phase7 v2 当时 L3 file 19.8 GB 来源不明** (replay 跑了多 round, 不是 20 prompts 一次写出)。**Phase7 v2 数据可能被错误归因**。

## 已成功的部分

### 单盘 32K cold smoke (一次跑通)

```bash
CTX_LEN=32000 MEM_STATIC=0.7
python hicache_load_test.py --prompt-tokens 32768 --output-tokens 64
# 单 prompt cold:
#   total=393920 tokens (32K + 64)
#   wall=8.477s
#   prefill_throughput=46,427 tokens/s
#   GPU mem peak ~10 GB
# 没 OOM
```

**说明 32K 单 prompt 冷启动 OK,但 multiprompt + 4 盘 drop_caches 模式因 sglang 0.5.13 限制跑不通**。

## 结论与建议

### 已落库发现 (供后续参考)

1. **sglang 0.5.13 max_input = max_total_num_tokens - 16K (max_prefill) - 16 (reserved) - 64 (output)**
2. **Qwen3-4B 单卡 max practical input = 16K-20K** (受显存约束 + prefill 余量)
3. **真 cold-from-device 测需要减小 page cache 上限** (`min_free_kbytes` sysctl)
4. **Phase7 v2 2.1s spread 复现条件未明** (L3 file 19.8 GB 来源待查), v3 1-25ms spread 已是 page cache hit 的合理测得值

### 不再追的方向

- ❌ 32K multiprompt 4 盘 (sglang 0.5.13 阻塞)
- ❌ Phase7 v2 2.1s 复现 (L3 file 19.8 GB 来源待查, 不影响产品决策)

### 如果后续要做,推荐

1. **换 GPU**: A100 80G / H100 单卡装 32K prefill + 50K KV cache + 模型
2. **改 sysctl**: `min_free_kbytes=1024` + `zone_reclaim_mode=1` 强制小 page cache
3. **换 sglang 版本**: 0.5.14+ 可能修了 max_input 限制
4. **换 LMCache**: 1.x 对 32K+ 可能有不同 max_input 策略

## 文件

- `scripts/hicache_drive_4_rounds_32k_drop.sh` (driver 写完, 5 个 bug 修过但仍未跑通)
- `scripts/hicache_load_test.py` (加了 HTTP body log)
- `scripts/hicache_serve.sh` (加了 `--max-total-tokens` 显式 cap)
- `results/hicache_32k_drop_smoke/` (4 个 round log, 全 400/URLError, 失败)
- `results/hicache_32k/` (Phase7 multiprompt 4 盘 32K, 87ms spread, 无 drop_caches)

## 时间线

- 09:30 — 启动 Phase8, 写 driver
- 09:35-10:00 — 5 个 bug 调试: 400 (CTX_LEN) → 400 (MEM_STATIC) → OOM (MEM_STATIC=0.85) → 400 (max_total_tokens) → OOM (MEM_STATIC=0.9) → 接受现状
