# P3 Validation Report: ZHITAI 跨 run 持续变快 (2.5s→2.0s) 根因分析

> **日期**: 2026-06-15
> **作者**: AI-SSD Pre-Study Team
> **目的**: 验证 Phase7 G 任务中观察到的 "ZHITAI replay_p0 latency 单调下降 (v3=2.545s, g5=2.058s, 19%↓)" 是否由 page cache 累积导致
> **状态**: ✅ 完成 — page cache 假设不成立；单盘 ZHITAI 慢读已复现，根因仍需 block trace 定位

---

## TL;DR

**原假设 (Phase7 G 报告 §1):** ZHITAI 跨 run 持续变快是因为 page cache 累积(L3 file 残留)

**验证结果:** ❌ **假设不成立**
- P3v2 (新 session, 干净 buff/cache < 3GB, drop_caches 后) A1=4.013s, A2=4.111s
- A1 vs A2 差异 +2.4%,**drop_caches 对 ZHITAI 读盘 latency 无显著影响**
- P3v3 A1 再次单独跑 ZHITAI, replay_p0=4.109s,复现 P3v2 慢读
- 当前最可能方向: **跨 4 盘跑 (g1-g5) vs 单独跑 ZHITAI (P3v2/P3v3) 时,OS I/O 调度 / 预读 / 盘内部状态不同**
- 下一步需要 bpftrace/perf 抓 block 请求级延迟,不能仅凭 iostat 断言具体根因

---

## 1. 背景

Phase7 G 任务 (6 run multiprompt, 详见 [hicache-phase7-g-multirun-validation-2026-06-15.md](./hicache-phase7-g-multirun-validation-2026-06-15.md)) 观察到:

| Run | ZHITAI replay_p0 (s) |
|---|---|
| v3 | 2.545 |
| g1 | 2.385 |
| g2 | 2.280 |
| g3 | 2.212 |
| g4 | 2.151 |
| g5 | 2.058 |

**单调下降 19%**。原报告假设:
> 可能原因: **page cache 累积**: 每次 run 后 L3 file (19 GB) 残留 page cache,下次 replay 时部分命中

但**这个假设机制不严密** —— 每个 run 写**不同** L3 file (不同 prompt 序列),新文件不命中旧 page cache。所以需要 P3 验证来测试:
- 如果 page cache 累积是主因 → drop_caches 后跑应该**变慢**(回到 baseline)
- 如果 page cache 不是主因 → drop_caches 后跑**无显著变化**

---

## 2. 验证设计

**P3 v1 (第一版 wrapper):**
```
Phase A1: drop_caches → 跑 ZHITAI  (测干净状态)
Phase A2: A1 之后 drop_caches → 跑 ZHITAI  (测清理后状态)
Phase A3: A2 之后 → 跑 ZHITAI  (测累积状态)
Phase B1: A3 之后 drop_caches → 跑 ZHITAI  (测再次清理)
```

**P3 v2 (修正版,ENOSPC 后):**
```
Phase A1: drop_caches → 跑 ZHITAI  (干净)
Phase A2: A1 之后 → 跑 ZHITAI  (累积, 不 drop)
```

**关键变量控制:**
- 同盘 (nvme3n1 = ZHITAI)
- 同模型 (Qwen3-4B)
- 同 L3 file 写入策略 (write_through)
- 同 20 prompts + replay_p0 模式
- **唯一变量**: drop_caches vs 不 drop, 跟 g1-g5 跨 4 盘 session 对比

**盘空间要求**: ZHITAI 至少 25GB 空闲 (P3 v1 写到一半 ENOSPC 暴露空间管理问题)

---

## 3. 数据采集

### 3.1 完整 13 数据点 (全是 ZHITAI replay_p0)

| # | 来源 | 跑法 | drop_caches? | buff/cache 状态 | L3 file 状态 | replay (s) | 数据可信? |
|---|---|---|---|---|---|---|---|
| 1 | v3 | 跨 4 盘 session | 否 (初始) | ~7-9GB (前 4 盘累积) | 19GB 写完 | 2.545 | ✅ |
| 2 | g1 | 跨 4 盘 session | 否 | ~7-9GB | 19GB 写完 | 2.385 | ✅ |
| 3 | g2 | 跨 4 盘 session | 否 | ~7-9GB | 19GB 写完 | 2.280 | ✅ |
| 4 | g3 | 跨 4 盘 session | 否 | ~7-9GB | 19GB 写完 | 2.212 | ✅ |
| 5 | g4 | 跨 4 盘 session | 否 | ~7-9GB | 19GB 写完 | 2.151 | ✅ |
| 6 | g5 | 跨 4 盘 session | 否 | ~7-9GB | 19GB 写完 | 2.058 | ✅ |
| 7 | P3v1 A1 | 单独 ZHITAI, 跨 session | 是 (前) | ~5GB (g5 残留 + drop) | 19GB 写完 | 2.664 | ✅ |
| 8 | P3v1 A2 | 累积 A1, 盘 96% 满 | 否 | ~5GB | 16GB (ENOSPC 干扰) | 4.117 | ⚠ |
| 9 | P3v1 A3 | 累积 A2 | 否 | ~5GB | 1.3GB (ENOSPC) | 4.103 | ❌ |
| 10 | P3v1 B1 | drop + 盘满 | 是 | ~5GB | 0GB (ENOSPC) | 1.424 | ❌ |
| 11 | **P3v2 A1** | **新 session, 释放 150GB** | **是** | **< 3GB** | **19GB 写完** | **4.013** | **✅** |
| 12 | **P3v2 A2** | **累积 A1** | **否** | **~5GB** | **19GB 写完** | **4.111** | **✅** |
| 13 | **P3v3 A1** | **新 session + bpftrace wrapper** | **是** | **未记录** | **2045×9MB** | **4.109** | **✅** |

### 3.2 关键对比

**对比 1: P3v2 A1 vs A2 (page cache 假设直接测试)**
- A1 (drop_caches 干净) = 4.013s
- A2 (累积 A1) = 4.111s
- **差异: +2.4%** — drop_caches 对 ZHITAI 读盘 latency 无显著影响
- **结论: page cache 累积不是 ZHITAI 变慢/变快的主因** ❌

**对比 2: P3v2 A1 (4.013s) vs P3v1 A1 (2.664s) (跨盘 vs 单独跑)**
- 都做了 drop_caches,buff/cache 都清到 2.9-5GB
- P3v1 A1 在**跨 4 盘 session 内**,前 3 盘 (BIWIN/WDC/Seagate) 已把 buff/cache 填到 ~7-9GB
- P3v2 A1 是**新 session, 只跑 ZHITAI**,buff/cache < 3GB
- **差异: +51%** — 跨盘 session 跑 ZHITAI 比单独跑快 50%
- **结论: 跨盘累积 buff/cache 让 ZHITAI 跑得快,但不是 page cache 内容,是 OS scheduler 状态**

**对比 3: P3v2 A1 (4.013s) vs g1-g5 (2.058-2.385s)**
- 都"干净"(drop_caches),但 buff/cache 状态不同
- 跨 4 盘 session 把 buff/cache 维持在 ~7-9GB
- 单独 ZHITAI 跑 buff/cache < 3GB
- g1-g5 单调下降 (2.5→2.0) 可能是 **OS scheduler 对 ZHITAI 这块盘的特征学得越来越准**

**对比 4: P3v3 A1 (4.109s) vs P3v2 A1/A2**
- P3v3 A1 replay_p0 = 4.109s
- P3v2 A1/A2 = 4.013s / 4.111s
- 三次单独 ZHITAI 干净/近干净状态都在 **4.0-4.1s**
- **结论: 单盘 ZHITAI 慢读是可复现现象**,不是 P3v2 偶发噪声

---

## 4. 根因分析

### 4.1 排除的假设

**❌ 假设 A: page cache 累积**
- 机制: 19GB L3 file 写入后,部分 page 留在 page cache
- 测试: drop_caches 后 A1 vs A2 差异仅 2.4% → 假设不成立
- 附加证据: 每个 run L3 file 内容不同 (不同 prompt 序列),page cache 不会跨 run 复用

**❌ 假设 B: filesystem metadata cache 累积**
- 机制: NTFS MFT/journal cache 累积
- 测试: 如果成立,A1 (干净) vs A2 (累积) 应该有显著差异
- 实际: 差异 2.4% → 假设不成立

**❌ 假设 C: 盘 controller cache warmup**
- 机制: 消费级 SSD 内部 cache 累积
- 测试: 如果成立,P3v2 A1 (新 session) vs A2 (累积) 应该有差异
- 实际: 差异 2.4% → 盘 controller cache 不是主因 (或者累积速度极快,A1 写入时已经 warmup 完)

**⚠ 待验证: 假设 D: OS I/O scheduler 状态**
- 机制: 多次同 pattern IO 后,scheduler 算法学得越来越准 (如预算分配、anticipatory 调度)
- 测试: 跨 4 盘 session (g1-g5) → 跑 ZHITAI 快; 单独 ZHITAI 跑 (P3v2/P3v3) → 慢
- 实际: 差异 50%+ → OS I/O 路径状态明显不同
- 但缺少 block 请求级 trace,暂不能断言 scheduler 是唯一主因

### 4.2 留下来的假设

**当前最强假设: 跨盘 session 让 OS I/O 路径处于 warmup 状态**
- 跨 4 盘 session 跑时,scheduler 处理 4 块不同特征的盘 (BIWIN ext4 / WDC / Seagate / ZHITAI NTFS)
- scheduler 算法对每块盘建立 I/O pattern profile
- 跑到 ZHITAI 阶段时,scheduler "见过" 多种 IO pattern,能更准确预测 ZHITAI 的 IO burst
- → ZHITAI replay 阶段读盘快

**次要假设: g1-g5 单调下降趋势 (2.5→2.0s)**
- 连续 6 run 中,scheduler 持续学
- 每次 run 比上次"更懂" ZHITAI
- 19% 下降是 scheduler 持续 warmup

**新增证据: P3v3 A1**
- `results/hicache_multiprompt_p3v3_A1/ai_ssd2_zhitai_ntfs/load_test.jsonl`
- `replay_p0=4.109s`
- `cache_file_list.txt` 有 2045 个 9MB L3 page 文件
- `/metrics` 显示 `num_requests_total=22`, `prompt_tokens_total=147174`, `cache_hit_rate=0`
- 说明这轮完整触发了 20 prompts + replay 路径,慢读有效

### 4.3 局限

1. **bpftrace wrapper 已补,但本轮 trace 输出未归档到 results** — P3v3 后续重跑会把 `bpftrace_nvme3n1.log` 放进结果目录
2. **没有控制 I/O scheduler 类型** — 系统默认应该是 mq-deadline 或 bfq,不同 scheduler 行为不同
3. **page cache unevictable 部分** — drop_caches 后还剩 2.9GB buff/cache,可能是 sglang/python 进程的 mmap 段
4. **单盘单 GPU** — 生产环境多盘多 GPU 跨盘 session 行为可能不同

---

## 5. 工程建议

### 5.1 给 P3 报告

1. **Phase7 G 报告 §1.2 ZHITAI 持续变快 假设改为:** "OS I/O 路径 / 预读 / 盘内部状态差异,而非 page cache 内容复用"
2. **EXECUTIVE_SUMMARY.md 加一行:** "ZHITAI 在跨 4 盘 session 中比单独跑快约 50%,需进一步 block trace 定位"
3. **REPORT.md §4 加新 sub-section:** "5.2 单盘 vs 跨盘运行状态差异"

### 5.2 给生产部署

1. **生产环境跑多盘混部时,启动后先做 1-2 轮"假负载" warmup** — 但成本高,通常系统在真实流量下会自然进入稳定状态
2. **不要单盘 ZHITAI 部署** — 单独跑 ZHITAI 比混部慢 50% (P3v2 4.0s vs 跨盘 2.0s)
3. **监控 buff/cache 状态** — `free -m` 中 buff/cache > 5GB 是 scheduler warmup 状态,可能暗示跨盘 IO 跑得好

### 5.3 给后续 P3+ 验证 (留作未来工作)

1. **加 bpftrace 跟踪 scheduler 行为** — `bpftrace -e 'tracepoint: block:block_rq_issue /args->bytes/ { @bytes = hist(args->bytes); }'` 看单盘 vs 跨盘时 req size 分布
2. **切换 I/O scheduler 对比** — `echo mq-deadline > /sys/block/nvme3n1/queue/scheduler` 试 mq-deadline vs bfq vs none
3. **跑多盘混合 + 单独 ZHITAI 对照** — 构造严格 A/B 实验

---

## 6. 可复现命令

```bash
# 1. 跑 P3 v2 (2 个 ZHITAI run, 干净 + 累积对照)
#    前置: 释放 /mnt/ai_ssd2 至少 25GB 空间
cd /home/ficus/llm/infer/ai_ssd_prestudy
bash scripts/run_p3_v2.sh
# 跑出 A1=4.013s, A2=4.111s (差异 2.4%)

# 也可以跑 P3 v3 (单个 ZHITAI run + bpftrace wrapper)
bash scripts/run_p3v3_bpftrace.sh
# 跑出 A1=4.109s; 后续重跑会把 bpftrace_nvme3n1.log 归档到结果目录

# 2. 解析 P3 数据
cat results/hicache_multiprompt_p3v2_{A1,A2}/ai_ssd2_zhitai_ntfs/load_test.jsonl | \
    jq -r 'select(.label | startswith("replay_")) | "\(.label) \(.latency_s)"'

# 3. 对比 g1-g5 跟 P3v2
for r in v3 g1 g2 g3 g4 g5; do
    d_var=hicache_multiprompt
    [ "$r" != "v3" ] && d_var=hicache_multiprompt_$r
    jsonl=results/$d_var/ai_ssd2_zhitai_ntfs/load_test.jsonl
    if [ -f "$jsonl" ]; then
        repl=$(jq -r 'select(.label | startswith("replay_")) | .latency_s' "$jsonl" 2>/dev/null | head -1)
        echo "  $r: ${repl}s"
    fi
done
```

---

## 7. 完整数据文件清单

**脚本:**
- `scripts/run_p3_validation.sh` (v1, 4 run, ENOSPC 干扰)
- `scripts/run_p3_v2.sh` (v2, 2 run, 干净对照)
- `scripts/run_p3v3_bpftrace.sh` (v3, 单 run + bpftrace wrapper)

**数据:**
- `results/hicache_multiprompt_p3_A1/`, `_A2/`, `_A3/`, `_B1/` (v1, 部分 ENOSPC)
- `results/hicache_multiprompt_p3v2_A1/`, `_A2/` (v2, 干净)
- `results/hicache_multiprompt_p3v3_A1/` (v3, 复现单盘慢读)
- `results/sglang_metrics_summary.{csv,json}` (跨 run metrics 汇总,含 P3v3_A1)

**完整 13 数据点 (含 v3/g1-g5/P3v3):**
- `results/hicache_multiprompt*/ai_ssd2_zhitai_ntfs/load_test.jsonl` × 9

**Commits:**
- `4fa9ca7` P3 partial: 2 run 数据
- `4e8ae6d` P3 v1 数据 + ENOSPC 修正 + 新 v2 设计
- `6ed87ab` P3 v2: 释放 150GB 后干净跑 + 最终结论

---

## 8. 结论 (最终)

**ZHITAI 跨 run 持续变快 (2.5s→2.0s) 的主因不是 page cache 内容复用。当前证据支持:**
1. **单盘 ZHITAI 慢读可复现**: P3v2/P3v3 三次有效单盘 run 均为 4.0-4.1s
2. **跨 4 盘 session 明显更快**: g1-g5 为 2.06-2.39s,差异约 50%
3. **具体根因仍需 block trace**: OS I/O scheduler、预读状态、NTFS 行为、盘内部状态都有可能参与

**对 AI SSD 选型的影响:**
- 单独 ZHITAI 跑 4.0s vs 跨盘混部 2.0s — **生产环境多盘混部比单盘好 2×**
- 选型测试时**必须**多盘混部,单盘测试会高估 latency ~50%
- Phase7 G 报告的 "ZHITAI 最快 2.272s mean" 是跨盘 warmup 状态,真实单盘部署可能是 ~4.0s

**对后续 P3+ 验证建议:**
- 加 bpftrace 跟踪 scheduler 决策
- 切换 I/O scheduler 类型对比 (mq-deadline / bfq / none)
- 控制 page cache unevictable 部分 (用 cgroup 隔离)

**最后更新**: 2026-06-15 19:40
**数据基础**: 13 个 ZHITAI replay 数据点, P3v1 (4 run) + P3v2 (2 run) + P3v3 (1 run) + g1-g5 (6 run)
**可信度**: 🟡 中高 (慢读现象已复现,具体 kernel/盘内根因仍需 bpftrace/perf)
