# Qwen3-30B-A3B 详细 IO 分析计划

**目标**: 深入理解 MoE 推理中,各存储层 (GPU HBM3 / CPU RAM / SSD) 之间的 IO 行为,找出真正的瓶颈点,为后续优化提供数据支撑。

**日期**: 2026-06-23
**前置报告**: `REPORT.md` (已包含 6 配置对比总表)

---

## 0. 当前数据缺口 (Report 没回答的问题)

| 问题 | 当前数据 | 需要补充 |
|---|---|---|
| MoE expert 每次读取多大? | 推测 ~1-2 MB | 实测每次 expert 读取字节数 |
| SSD 顺序读 vs 随机读谁主导? | 推测随机 | 实测 4K vs 128K IO 分布 |
| 多卡 TP 的 KV cache 流量? | 未知 | 实测两卡之间的 PCIe/NVLink 流量 |
| 4 块 SSD 的 4K IOPS 实测? | 只有 rMB/s | 加 iostat `-d -p` 拿 IOPS |
| 第一次请求 vs 后续请求 IO 差异? | 仅纯 GPU 有 (170 vs 185) | 各模式都要测冷/热缓存 |
| expert 命中模式? | 未知 | 用 bpftrace 抓 MoE expert 访问路径 |

---

## 1. 工具链准备

### 1.1 已有工具
- ✅ `iostat` (sysstat 包) - 设备级 MB/s / IOPS / await
- ✅ `bpftrace` - 内核 tracepoint 抓文件系统层 IO
- ✅ `perf` - 抓 CPU 调用栈
- ✅ `nvidia-smi dmon` - GPU 实时采样
- ✅ `pidstat` - 进程级 IO

### 1.2 需要新装的工具
- `iotop` - 进程级实时 IO 排序
- `biosnoop` (bpftrace 工具) - 单次 IO 事件记录 (大小、延迟、扇区号)
- `biolatency` (bpftrace 工具) - IO 延迟直方图
- `perf c2c` - cache-to-cache 流量分析 (PCIe 跨卡访问)

### 1.3 llama.cpp 自带工具
- `--log-disable` - 关 verbose
- `--verbose` / server `/v1/stats` - 每请求 TPS 详情
- `/proc/<pid>/io` - 进程级 IO 累计值

---

## 2. 测试矩阵 (8 个实验)

### 实验 A: SSD 单盘冷/热缓存分离测试

**目的**: 区分 SSD 真实 IO 性能 vs page cache 命中后的性能

**步骤**:
1. **冷缓存**: `echo 3 > /proc/sys/vm/drop_caches` 清理 page cache
2. 启动 ftllm (--moe_device disk) 在 BIWIN
3. 跑 5 个请求,记录 iostat `r/s`, `rkB/s`, `rareq-sz`, `await`
4. **热缓存**: 不清理,继续跑 5 个请求
5. 重复对 ZHITAI / WDC / Seagate

**预期输出**:
- 冷缓存: TPS 6-7, SSD r/s 50-200, rareq-sz 8-16 KB
- 热缓存: TPS 8-10, SSD r/s 10-50, rareq-sz 32-128 KB
- 量化 page cache 对 SSD offload 的提速效果

**脚本**: `scripts/io_test_a_cold_hot.sh`

---

### 实验 B: MoE expert 读取粒度分析 (bpftrace biosnoop)

**目的**: 实测每次 MoE expert 读取的字节数和延迟

**步骤**:
1. 启动 ftllm, BIWIN 系统盘
2. 在另一个终端跑:
   ```bash
   bpftrace -e '
       kprobe:blk_mq_start_request {
           $s = (struct request *)arg0;
           $bytes = $s->__data_len;
           @start[arg0] = nsecs;
           @bytes[$bytes] = count();
       }
       kprobe:blk_mq_end_request /@start[arg0]/ {
           @lat_us = hist((nsecs - @start[arg0]) / 1000);
           delete(@start[arg0]);
       }
       END { print(@lat_us); clear(@lat_us); clear(@bytes); }' \
       > logs/biosnoop_disk.log
   ```
3. 同时跑 10 个请求
4. 分析: 读请求大小的分布, IO 延迟分布

**预期输出**:
- 大量 4-16 KB 请求 (页缓存未命中)
- 大量 64-256 KB 请求 (页缓存命中后合并读)
- 读延迟分布: 50-200μs (SSD 典型)

**脚本**: `scripts/io_test_b_biosnoop.sh`

---

### 实验 C: 各 SSD 的 4K 随机读 IOPS 实测 (fio)

**目的**: 把 SSD offload 结果与 SSD 硬件能力对比

**步骤**:
```bash
# 每块盘测一次,使用 direct IO 绕过 page cache
for dev in nvme0n1 nvme1n1 nvme2n1; do
    sudo fio --name=rand_read_4k \
        --filename=/dev/${dev} \
        --direct=1 --ioengine=libaio --iodepth=32 \
        --rw=randread --bs=4k --size=2G \
        --runtime=30 --time_based \
        --output=results/fio_${dev}_4k.json
done

# 同样测 128K 顺序读
for dev in nvme0n1 nvme1n1 nvme2n1; do
    sudo fio --name=seq_read_128k ... bs=128k rw=read
done
```

**预期输出**:
- 4K 随机读 IOPS: BIWIN 200K / ZHITAI 180K / WDC 100K / Seagate 80K
- 128K 顺序读带宽: BIWIN 5GB/s / ZHITAI 4.5GB/s / ...

**对比表**:

| 盘 | 4K IOPS | 128K 顺序读 | Offload TPS | TPS/IOPS 比 |
|---|---|---|---|---|
| BIWIN | 200K | 5 GB/s | 7.9 | 39.5 TPS/M |
| ... |

**脚本**: `scripts/io_test_c_fio.sh`

---

### 实验 D: GPU 显存层级分析 (nvidia-smi + nvprof-style)

**目的**: 量化 GPU 端的显存带宽占用

**步骤**:
```bash
# 后台采样 GPU 状态,频率 100ms
nvidia-smi dmon -s pucm -d 1 -c 600 > logs/gpu_dmon_pure_gpu.log &
nvidia-smi dmon -s pucm -d 1 -c 600 > logs/gpu_dmon_numa.log &
nvidia-smi dmon -s pucm -d 1 -c 600 > logs/gpu_dmon_disk.log &
```

**分析指标**:
- `sm` (SM 占用率): 推理时 30-80%
- `mem` (显存带宽): 推理时 200-1500 GB/s
- `fb` (framebuffer): 静态占用

**预期**:
- 纯 GPU: mem 800-1500 GB/s
- NUMA offload: mem 200-400 GB/s
- SSD offload: mem 100-200 GB/s

**脚本**: `scripts/io_test_d_gpu_dmon.sh`

---

### 实验 E: 多卡 PCIe 流量 (perf c2c 或 nsys)

**目的**: 量化 TP=2 时两卡之间的数据传输

**步骤**:
```bash
# llama-server 启动时设置
export CUDA_VISIBLE_DEVICES=0,1
# 用 nsys 抓 profile
nsys profile -o /tmp/qwen_tp2 \
    --trace=cuda,nvtx,osrt \
    --output=/tmp/qwen_tp2.qdrep \
    llama-server ... &

# 或者用 ncu (Nsight Compute)
ncu --target-processes all \
    --metrics gpu__time_duration.sum,sm__throughput.avg.pct_of_peak_sustained_elapsed \
    llama-server ...
```

**预期输出**:
- PCIe 4.0 x16: ~32 GB/s 理论带宽
- 实际 KV cache 同步流量: 1-5 GB/s (取决于 batch size)
- attention 算子的 cross-GPU 通信占比

**脚本**: `scripts/io_test_e_pcie.sh`

---

### 实验 F: 进程级 IO 累计值 (`/proc/<pid>/io`)

**目的**: 用最简单方法量化"总共读了 SSD 多少数据"

**步骤**:
```bash
# 启动 ftllm 后,采样 /proc/<pid>/io 每秒
PID=$(pgrep -f "ftllm server")
while kill -0 $PID 2>/dev/null; do
    grep -E "read_bytes|write_bytes" /proc/$PID/io
    sleep 1
done
```

**分析**:
- 总 read_bytes / 请求数 = 每请求 SSD 读字节数
- 总 read_bytes / (总 tokens × 30B) = expert 命中率倒数

**预期**:
- 每请求 SSD 读: 50-200 MB
- token 平均读: 0.5-2 MB/token

**脚本**: `scripts/io_test_f_proc_io.sh`

---

### 实验 G: 单请求 TPS 与 IO 关系 (时间序列对齐)

**目的**: 把 TPS 抖动和 IO 抖动对齐到时间轴

**步骤**:
1. 启动 ftllm + iostat (1秒采样) + nvidia-smi dmon (1秒采样)
2. 跑 10 个独立请求,**每个请求间隔 5 秒**
3. 把时间戳对齐到同一张图:
   - X 轴: 时间
   - Y 左: TPS (每秒)
   - Y 右: SSD rMB/s
   - 标注: 每次请求的起止时间

**分析**:
- 是否 IO 抖动 → TPS 抖动?
- 是 compute-bound 还是 IO-bound?
- 请求间是否有 warm-up 效应?

**预期输出**: `results/io_tps_timeseries.png` (matplotlib)

**脚本**: `scripts/io_test_g_timeseries.py`

---

### 实验 H: expert 命中模式分析 (bpftrace uprobes)

**目的**: 看 MoE 推理中每个 token 实际激活了哪些 expert

**步骤**:
1. 找到 ftllm 的 MoE routing 函数 (可能叫 `MoeRoute` 或类似)
2. 用 bpftrace uprobe 抓每次 routing 输出:
   ```bash
   bpftrace -e '
       uprobe:/home/ficus/llm/fast/.venv/lib/python*/site-packages/fastllm*:MoeRoute {
           $token_id = arg0;
           $expert_id = arg1;
           @expert_hit[$expert_id] = count();
       }
       END { for ($k in @expert_hit) { print($k, @expert_hit[$k]); } }' \
       > logs/expert_hits.log
   ```
3. 跑 100 个 token 生成,统计 expert 分布

**预期**:
- 128 个 expert 中,每次激活 8 个
- 是否均匀分布? (理想) 还是集中? (不理想,可能可优化)

**脚本**: `scripts/io_test_h_expert_hits.sh` (需要 ftllm debug build)

---

## 3. 优先级排序

| 优先级 | 实验 | 预计耗时 | 价值 |
|---|---|---|---|
| 🔴 P0 | A. 冷/热缓存分离 | 30 min | 立即量化 page cache 价值 |
| 🔴 P0 | C. fio 4K IOPS | 30 min | 把 TPS 与硬件能力直接关联 |
| 🟡 P1 | B. biosnoop | 1 hour | 解释"为何 SSD offload 是 7 tps" |
| 🟡 P1 | G. 时间序列对齐 | 1 hour | 区分 IO-bound vs compute-bound |
| 🟡 P1 | F. /proc/pid/io | 30 min | 量化总 IO 量 |
| 🟢 P2 | D. GPU dmon | 30 min | GPU 端带宽画像 |
| 🟢 P2 | E. PCIe 流量 | 2 hour | 多卡扩展性分析 |
| ⚪ P3 | H. expert 命中 | 4 hour | 需要重新编译 ftllm |

**最小可行分析包 (MVP)**: A + C + F + G,共 ~3 小时
**完整包**: 所有实验,共 ~10 小时

---

## 4. 输出物

1. **数据表**: `results/io_analysis_2026-06-23.json` - 所有 IO 指标汇总
2. **时间序列图**: `results/io_tps_timeseries.png` - 实验 G 的图
3. **直方图**: `results/io_latency_hist.png` - 实验 B 的 biosnoop 延迟分布
4. **专家命中热力图**: `results/expert_hits_heatmap.png` - 实验 H
5. **更新 REPORT.md**: 加入 IO 分析章节,回答"为何这个 TPS"

---

## 5. 风险与限制

- **bpftrace 需要 root**: sudo NOPASSWD 已配置
- **fio 需要直接访问块设备**: 有 NOPASSWD sudo
- **冷缓存测试要重启服务**: 每次都要重启 ftllm,耗时较长
- **TP=2 PCIe 流量分析依赖 nsys**: CUDA 13.3 自带 nsys,可用
- **MoE expert 命中率**: 需要 ftllm debug build,可能要花 1 小时重新编译

---

## 6. 建议下一步

按优先级顺序执行 P0 → P1 → P2 → P3。每个实验完成后:
1. 把数据存到 `results/io_*.json`
2. 更新本计划文件记录实际结果与预期对比
3. 实验 G (时间序列) 完成后立即出一张图

**结束条件**: 全部 P0+P1 完成 (约 3-4 小时),产出能回答"为什么 SSD offload 是 7 tps,不是 70 tps"的根因报告。
