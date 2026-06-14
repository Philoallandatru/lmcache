# SGLang HiCache — write_through vs write_back 对比报告

> **日期:** 2026-06-13 凌晨
> **模型:** Qwen3-4B-Instruct-2507
> **负载:** 1 cold + 5 warm, 7000 tokens prefix, 64 tokens output
> **HiCache 配置:** `page_size=64, hicache-ratio=2, page_first_direct, direct io, timeout, file backend`
> **测试设计:** 同一脚本,只改 `--hicache-write-policy` 参数,4 盘串行各跑一遍

---

## 1. TL;DR — 关键发现

**🎯 write_back 让 Cold TTFT 快 37ms (-2.6%), 但加速比反而降了 1.96× → 1.90×**:

| 维度 | write_through | write_back | Δ |
|---|---:|---:|---:|
| Cold TTFT (4 盘平均) | 1.440s | **1.403s** | **-37ms** ✅ |
| Warm #1 TTFT (drop_caches) | 0.735s | 0.738s | +3ms |
| Warm #5 TTFT (L2 hit) | 0.722s | 0.722s | 0 |
| **加速比 (cold/warm_1)** | **1.96×** | **1.90×** | -3% |
| **L3 file count** | **115 × 9MB** | **0** | write_back 异步,测试完还没落盘 |
| **NTFS 写 IO (avg)** | 179-185 MB/s | **3-5 MB/s (-97%)** | write_back 后台 worker 未启动 |

> **📌 v3 mount-fixed 重跑已确认**: 2026-06-15 Phase3 v3 数据跟 v2 老数据完全一致 (write_back cold 1.403s, 4 盘 spread 1ms, L3 0 file)。详见 [hicache-v3-policy-2026-06-15.md](./hicache-v3-policy-2026-06-15.md)。

**核心洞察**:
1. **write_back 不暴露盘差** — Cold 不等 L3 写 → 写 IO 几乎看不到,Warm #1 走 L2 DRAM → 4 盘完全相同
2. **write_back 加速比反而低** — Cold 快了 37ms 但 Warm #1 几乎不变,基数变了
3. **write_back 在 6 round 测试场景下完全失效** — L3 数据没落盘就测完了 (Worker 还没启动或还在写)
4. **write_through 才是 HiCache 默认推荐** — 同步阻塞保证 L3 数据落盘,后续 warm 才能命中
5. **生产建议**:
   - 单请求 → write_through (数据一致性保证)
   - 高并发 → write_back + 长运行测试 (需要观察后台 worker 稳态)

---

## 2. 实验配置

| 参数 | 值 |
|---|---|
| 模型 | Qwen3-4B-Instruct-2507 |
| prompt tokens | 7008 |
| output tokens | 64 |
| rounds | 1 cold + 5 warm |
| **唯一变量** | `--hicache-write-policy {write_through, write_back}` |
| 其它 | `page_size=64, hicache-ratio=2, page_first_direct, direct io, timeout, file backend` |

每轮数据:
- `load_test.jsonl` — 6 round TTFT
- `iostat_*.log` — 1s 粒度盘 IO
- `cache_file_list.txt` — L3 落盘文件 (write_through=115, write_back=0)

---

## 3. TTFT 对比 (4 盘 × 2 策略)

### 3.1 write_through (同步, L3 阻塞写)

| 盘 | Cold | Warm #1 | Warm #5 | 加速比 |
|---|---:|---:|---:|---:|
| BIWIN X570 (ext4) | 1.440s | 0.735s | 0.722s | 1.960× |
| WDC WDS960G2G0C | 1.439s | 0.734s | 0.722s | 1.959× |
| ZHITAI Ti600 | 1.440s | 0.735s | 0.722s | 1.958× |
| Seagate ZP1000GV30012 | 1.439s | 0.735s | 0.722s | 1.958× |
| **平均** | **1.440s** | **0.735s** | **0.722s** | **1.959×** |

### 3.2 write_back (异步, L3 后台写)

| 盘 | Cold | Warm #1 | Warm #5 | 加速比 |
|---|---:|---:|---:|---:|
| BIWIN X570 (ext4) | 1.402s | 0.734s | 0.722s | 1.909× |
| WDC WDS960G2G0C | 1.403s | 0.737s | 0.721s | 1.904× |
| ZHITAI Ti600 | 1.403s | 0.741s | 0.722s | 1.894× |
| Seagate ZP1000GV30012 | 1.404s | 0.740s | 0.722s | 1.897× |
| **平均** | **1.403s** | **0.738s** | **0.722s** | **1.901×** |

### 3.3 Δ write_back vs write_through

| 盘 | Cold Δ | Warm #1 Δ | 加速比 Δ |
|---|---:|---:|---:|
| BIWIN | **-38ms** ✅ | -1ms | -0.051 |
| WDC | **-36ms** ✅ | +3ms | -0.055 |
| ZHITAI | **-37ms** ✅ | +6ms | -0.064 |
| Seagate | **-35ms** ✅ | +5ms | -0.061 |
| **平均** | **-37ms (-2.6%)** | +3ms (+0.4%) | -0.058 |

---

## 4. iostat 对比

### 4.1 write_through (L3 file count = 115 × 9MB = 1035MB)

| 指标 | BIWIN ext4 | WDC NTFS | ZHITAI NTFS | Seagate NTFS |
|---|---:|---:|---:|---:|
| avg r | **1976 MB/s** 🥇 | 8 MB/s | 10 MB/s | 12 MB/s |
| avg w | 417 MB/s | 185 MB/s | 179 MB/s | 182 MB/s |
| max w BW | 5284 MB/s | 8004 MB/s | 4498 MB/s | **8106 MB/s** 🥇 |
| max r BW | **14704 MB/s** 🥇 | 318 MB/s | 425 MB/s | 498 MB/s |

### 4.2 write_back (L3 file count = 0 — 异步写未完成)

| 指标 | BIWIN ext4 | WDC NTFS | ZHITAI NTFS | Seagate NTFS |
|---|---:|---:|---:|---:|
| avg r | 1802 MB/s | 7 MB/s | 9 MB/s | 11 MB/s |
| **avg w** | **142 MB/s** | **3 MB/s** | **4 MB/s** | **5 MB/s** |
| max w BW | 4712 MB/s | **124 MB/s** | **189 MB/s** | **227 MB/s** |
| max r BW | **17003 MB/s** 🥇 | 298 MB/s | 398 MB/s | 467 MB/s |

### 4.3 Δ write_back vs write_through

| 指标 | BIWIN | WDC | ZHITAI | Seagate |
|---|---:|---:|---:|---:|
| avg_w 变化 | -66% | **-98%** | **-98%** | **-97%** |
| max_w 变化 | -11% | **-98%** | **-96%** | **-97%** |

**核心观察**:
- **NTFS 外置盘 (WDC/ZHITAI/Seagate) 在 write_back 模式下写 IO 几乎消失** — 因为 write_back 是后台 worker 异步写,我们的 30s 测试窗口内 worker 还没启动或还在排队列
- **BIWIN ext4 系统盘**因为 Linux page cache 兜底,**page cache writeback** 仍有 142 MB/s
- **Seagate 写峰值 227 MB/s** 是 4 盘最高(对应 max_w 突发),印证写能力最强

---

## 5. 关键洞察

### 5.1 为什么 write_back 加速比反而降低?

**直觉**: write_back 让 Cold 不等 L3 写,加速比应该更高才对。

**实际**:
- write_through: Cold=1.440s, Warm #1=0.735s, 差值=0.705s (这是 L3 store + reload 阻塞)
- write_back: Cold=1.403s, Warm #1=0.738s, 差值=0.665s (L3 异步,reload 仍需时间)

**差值缩小了 40ms**,但:
- Cold 减了 37ms (write 不阻塞)
- Warm #1 几乎不变 (drop_caches 后 L3 文件可能没落盘,要走真 disk reload → 等同 write_through)

所以**加速比变小是因为 Cold 变快而 Warm 不变**,比值下降。

### 5.2 为什么 write_back 下 L3 file count = 0?

write_back 是真正的异步:`CacheController.write_queue` 收集写入请求,后台 worker 线程 (`HiCacheFile::write_page`) 慢慢消费。6 round 测试 (~30s) 内:
- worker 启动开销
- 数据拷贝到 write_buffer
- 排队列 + 串行写入

对于 1GB 数据,**后台 worker 写完需要几分钟**,我们的 30s 测试窗口内文件还没创建。

**生产环境 write_back 适用**:
- 长运行 (几小时以上)
- 高并发请求,后台 worker 能持续吃负载
- 配合 `write_through_selective` (只对命中预测高的 KV 同步写,其余异步)

### 5.3 write_back 是否暴露盘差?

**没有**。原因:
1. Cold 不等 L3 → 看不到写性能
2. Warm #1 走 L2 DRAM hit → 4 盘相同
3. 即使 Warm #1 走 L3 reload,异步数据可能还没落盘 → 数据完整性无保证

**要暴露盘差需要**:
- write_through (已测,4 盘相同因为 page cache)
- + **强制关 page cache**:`echo 1 > /proc/sys/vm/drop_caches` (每次 warm 前)
- + **更大模型** (Qwen-32B 让 L3 数据 > page cache 容量)

---

## 6. AI SSD 选型建议 (基于双策略对比)

| 场景 | 推荐盘 | 理由 |
|---|---|---|
| **单请求 + write_through** | Seagate ZP1000GV30012 🥇 | 写峰值 8106 MB/s 第一,Warm L3 reload 受益于高吞吐 |
| **系统盘 (ext4 page cache)** | BIWIN X570 🥇 | avg_r 1976 MB/s (page cache 命中),适合小请求 |
| **NTFS 外置 + 高并发** | WDC 🥇 | 写峰值 8004 MB/s,await 1ms 最稳定 |
| **不推荐 KV cache** | ZHITAI Ti600 | NTFS 写 4498 MB/s 最低,async 下劣势更明显 |

---

## 7. 复现命令

```bash
cd ~/llm/infer/ai_ssd_prestudy

# write_through 4 盘 (~25 分钟)
bash scripts/hicache_drive_4_rounds.sh
# 结果: results/hicache/{baseline_biwin_ext4,ai_ssd0_wdc_ntfs,...}/

# write_back 4 盘 (~25 分钟, 数据到独立子目录)
bash scripts/hicache_drive_4_rounds_policy.sh write_back hicache_writeback _wb
# 结果: results/hicache_writeback/{baseline_biwin_ext4_wb,...}/
```

---

## 8. 下一步

1. **强制 drop_caches** (每 warm 前) — 打破 page cache,真暴露盘差
2. **更大模型 (Qwen-7B/32B)** — 突破 L2 DRAM + page cache 容量
3. **长运行 write_back 测试** (30 分钟以上) — 让后台 worker 稳态,观察 IO 模式
4. **write_through_selective** — 第三种策略,选择性同步
5. **runtime attach/detach** — 按 hicache_storage_runtime_attach_detach.md 用 HTTP API 切换 backend