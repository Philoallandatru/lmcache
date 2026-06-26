# Mooncake SSD Offload 当前硬件测试手册与 IO 分析计划

**日期**: 2026-06-26  
**目标**: 把 DGX 上的 Mooncake SSD Offload 多轮对话 benchmark 改造成适合当前工作站硬件的可执行测试手册,并建立一套能解释 TTFT / input throughput / cache hit rate / NVMe IO 行为之间关系的分析流程。

---

## 1. 背景与结论先行

原始 benchmark 的关键结论是:

- 多轮对话不断追加历史上下文,会让 KV cache 容量压力逐轮上升。
- 当 Mooncake 80GB DRAM pool 足够容纳 KV 时,`Mooncake` 和 `Mooncake + SSD` 表现一致。
- 当第 7 轮开始超过 DRAM pool 后,未启用 SSD offload 的 Mooncake 需要丢弃 KV 并重算,cache hit rate 从约 83% 掉到约 36%,TTFT 从约 6s 上升到约 16s。
- 启用 SSD offload 后,被 DRAM pool 淘汰的 KV 仍可从 NVMe 读回,第 8 轮 hit rate 仍高于 84%,TTFT 保持在约 9.4s。

当前机器不能直接复刻这组 DGX 结果。当前环境更适合作为 **单机 NVMe offload 行为验证平台**:

| 项 | DGX 原始环境 | 当前工作站观测值 | 对测试设计的影响 |
|---|---|---|---|
|| GPU | 8 x A100-SXM4-40GB | NVIDIA GeForce RTX 5080 + 5060 Ti,driver 595.71.05 | ✅ GPU 可用,无需修复驱动 ||
|| CPU | DGX server class | Intel Core Ultra 7 270K Plus,24 CPU,单 NUMA | 可做 Mooncake/IO 控制面与 fio 基线,但不能代表 DGX CPU/NUMA ||
|| DRAM | DGX 大内存 | 83GiB total,约 72GiB available | Mooncake global segment 不应设 80GB,建议 32-48GB ||
|| RDMA | 双 HDR 200Gb/s RNIC | 无 RDMA 设备 | 全部用 localhost/TCP 单机验证 ||
|| SSD | 5 x Samsung NVMe RAID0,约 27GB/s seq read | 4 块 NVMe,NTFS/fuseblk 可写 (2.7 GB/s direct IO) | 可直接用 NTFS 做 offload 目录,不需要改 ext4/xfs ||
|| 文件系统 | `/mnt/data` 可写 RAID0 | `/mnt/ai_ssd0/1/2` fuseblk `rw`;根分区 ext4 剩约 44GB | NTFS 可写,直接在其上建 offload 目录即可 ||

因此本手册分两层:

1. **当前硬件准入与 IO 基线**: 先确认 GPU、文件系统、NVMe、page cache、direct IO 是否满足 offload 测试条件。
2. **Mooncake SSD Offload 端到端复测**: 条件满足后,用缩放后的 memory pool 和并发参数复现“DRAM pool 耗尽后 SSD 是否避免性能断崖”。

---

## 2. 当前硬件盘点

### 2.1 CPU / 内存

```text
CPU: Intel Core Ultra 7 270K Plus
CPU(s): 24
NUMA node(s): 1
Memory: 83GiB total,72GiB available at inspection time
Swap: 8GiB
Kernel: Linux 7.0.0-22-generic
```

测试含义:

- 单 NUMA 环境便于解释 CPU-side Mooncake worker 和 IO 线程行为。
- DRAM 总量只有 83GiB,不能照抄 `--global_segment_size=80GB` 加 `20GB` offload buffer。建议先用:
  - `MOONCAKE_POOL=32GB`
  - `MOONCAKE_OFFLOAD_LOCAL_BUFFER_SIZE_BYTES=8589934592` (8GiB)
  - 确认稳定后再升到 48GB + 12GiB buffer。

### 2.2 NVMe 与挂载状态

| 设备 | 型号 | 容量 | 当前挂载 | 当前状态 | 用途建议 |
|---|---|---:|---|---|---|
| `nvme0n1` | BIWIN X570 1TB | 953.9G | `/` ext4 | 工作区所在盘,剩约 76GB | 只做小规模 smoke;不要做大 offload 主盘 |
| `nvme1n1` | WDC WDS960G2G0C | 894.3G | `/mnt/ai_ssd0` NTFS/fuseblk | 只读 | 需重挂为可写 ext4/xfs 后测试 |
| `nvme2n1` | Seagate ZP1000GV30012 | 931.5G | `/mnt/ai_ssd1` NTFS/fuseblk | 只读 | 需重挂为可写 ext4/xfs 后测试 |
| `nvme3n1` | ZHITAI Ti600 1TB | 931.5G | `/mnt/ai_ssd2` NTFS/fuseblk | 只读 | 需重挂为可写 ext4/xfs 后测试 |

块层参数:

```text
scheduler: none
nr_requests: 1023
max_sectors_kb: 128
logical/physical block size: 512/512
```

测试含义:

- `max_sectors_kb=128` 会把较大的读写拆成 128KiB 级别设备请求,这与既有 HiCache/fastllm 观察到的 60-256KiB KV IO 粒度一致。
- NTFS/fuseblk 不适合作为 Mooncake SSD offload 性能基线。它会引入 FUSE/NTFS 驱动开销,且当前还是只读。
- 单盘测试优先使用 ext4/xfs,多盘吞吐测试再考虑 mdraid0 或 fio 多文件并发。

---

## 3. 准入检查

端到端 Mooncake SSD offload 测试开始前,必须先通过以下检查。

### 3.1 GPU 驱动

```bash
nvidia-smi -L
nvidia-smi
```

通过标准:

- 能列出目标 GPU。
- SGLang 能加载目标模型。
- `nvidia-smi dmon` 可正常采样。

当前状态:

```text
GPU 0: NVIDIA GeForce RTX 5080 (UUID: GPU-bf0ccb9a-...)
GPU 1: NVIDIA GeForce RTX 5060 Ti (UUID: GPU-6845fa38-...)
driver_version: 595.71.05
Memory: 16GB (RTX 5080) + 16GB (RTX 5060 Ti)
```

结论: ✅ GPU 驱动正常,端到端推理 benchmark 可执行。SGLang 可与 Mooncake 一起跑出 TTFT / cache hit rate。注意单卡 16GB 显存限制,建议模型 ≤7B。

### 3.2 SSD offload 目录

Mooncake SSD offload 需要可写目录。可直接使用现有 NTFS 挂载点:

```bash
# 直接在 NTFS 上创建 offload 目录 (NTFS 可写,实测 2.7 GB/s direct IO)
MOONCAKE_OFFLOAD_DIR=/mnt/ai_ssd0/mooncake_ssd0/file_storage
mkdir -p "$MOONCAKE_OFFLOAD_DIR"
chown -R "$USER:$USER" "$(dirname "$MOONCAKE_OFFLOAD_DIR")"
test -w "$MOONCAKE_OFFLOAD_DIR" && echo "✅ offload directory writable"
df -h "$MOONCAKE_OFFLOAD_DIR"
```

通过标准:

- `test -w "$MOONCAKE_OFFLOAD_DIR"` 返回 0。
- offload 目录至少有 `POOL_SIZE + BUFFER_SIZE + 2x run output` 的可用空间。NTFS 盘通常有 250-300GB 可用,足够。
- 需要确认 Mooncake client/sglang 能正确继承 `MOONCAKE_OFFLOAD_FILE_STORAGE_PATH` 环境变量 (见 §5.6 的 env var 传递问题)。

不建议:

- 不要把根分区 44GiB 空间用于 32-48GB pool 的长测 (根分区已不够)。
- 使用 NTFS 时注意: Mooncake offload 写 NTFS 可能产生 defrag/碎片,但实测单次 2.7 GB/s direct IO,短期测试无影响。

### 3.3 工具链

当前已发现:

```text
fio: /usr/bin/fio
iostat: /usr/bin/iostat
bpftrace: /usr/bin/bpftrace
perf: /usr/bin/perf
mooncake_master: /home/ficus/llm/.venv/bin/mooncake_master
mooncake_client: /home/ficus/llm/.venv/bin/mooncake_client
sglang: /home/ficus/llm/.venv/bin/sglang (0.5.13)
lmcache: 0.4.6
mooncake_transfer_engine: 0.3.11.post1 (CUDA 12 binary)
torch: 2.11.0+cu130
uv: /home/ficus/.local/bin/uv
```

还需要确认:

```bash
source /home/ficus/llm/.venv/bin/activate
which mooncake_master  # 必须在 venv/bin/ 下
mooncake_client --help
python3 -c "import sglang; print(sglang.__version__)"
python3 benchmark/hicache/bench_multiturn.py --help
```

通过标准:

- `mooncake_master` 和 `mooncake_client` 在 `~/llm/.venv/bin/` 下,必须 source 激活 venv 后运行。
- LD_LIBRARY_PATH 已自动包含 `nvidia/cuda_runtime/lib`(已在 activate 末尾追加),否则 `mooncake_transfer_engine` 会因找不到 `libcudart.so.12` 报错。
- SGLang benchmark 脚本存在且参数与本文命令兼容。
- `bpftrace` 运行权限可用;若内核限制,至少保留 iostat + `/proc/<pid>/io`。

---

## 4. 测试矩阵

### 4.1 最小 smoke 矩阵

目标: 确认服务能启动、请求能完成、metrics 能采集。

| 配置 | pool | offload buffer | clients | rounds | request length | output | 预期 |
|---|---:|---:|---:|---:|---:|---:|---|
| GPU only | N/A | N/A | 2 | 3 | 1024 | 1 | TTFT 有效 |
| HiCache L1+L2 | N/A | N/A | 2 | 3 | 1024 | 1 | hit rate 上升 |
| Mooncake | 16GB | 0 | 2 | 4 | 2048 | 1 | memory pool 命中 |
| Mooncake+SSD | 16GB | 4GB | 2 | 4 | 2048 | 1 | offload 文件有写入 |

### 4.2 当前工作站主测试矩阵

目标: 在 83GiB DRAM 的限制下,人为缩小 Mooncake pool,让第 5-8 轮出现 DRAM pool 耗尽,观察 SSD offload 是否避免 TTFT 断崖。

| 变量 | 建议值 |
|---|---|
| Model | Qwen3-4B-Instruct-2507 (HF,7.6GB,Qwen3-14B 也可用但显存不够) |
| TP | 1,除非本机 GPU 拓扑确认适合 TP>1 |
| Mooncake pool | 32GB 起步,稳定后 48GB |
| SSD buffer | 8GB 起步,稳定后 12GB |
| clients | 8,12,16 三档 |
| rounds | 8 或 10 |
| request length | 4096 起步;若无法触发淘汰,升到 6144/8192 |
| output length | 1 |
| max parallel | 2 或 4 |
| request rate | 8 或 16 |
| ready queue policy | random |
| round barrier | enabled |

### 4.3 四配置对比

| 配置 | 目的 | 主要指标 |
|---|---|---|
| GPU only | 无缓存扩展 baseline | TTFT,input throughput,GPU memory |
| HiCache L1+L2 | host DRAM cache baseline | TTFT,host hit rate,DRAM 压力 |
| HiCache L1+L2+Mooncake | DRAM pool 扩展 baseline | TTFT,Mooncake hit rate,eviction 点 |
| HiCache L1+L2+Mooncake+SSD | SSD offload 核心配置 | TTFT,hit rate,SSD read/write,tail latency |

---

## 5. 运行手册

以下命令以当前工作站缩放参数为例。DGX 复刻时可把 pool 改回 80GB,buffer 改回 20GB,clients 改回 20。

### 5.1 通用环境变量

```bash
export MODEL_PATH=/path/to/Qwen3-8B
export PORT=8189
export BENCH_OUT=results/mooncake_ssd_$(date +%Y%m%d_%H%M%S)
mkdir -p "$BENCH_OUT"
```

### 5.2 Benchmark 命令

```bash
python3 benchmark/hicache/bench_multiturn.py \
  --model-path "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --disable-random-sample \
  --output-length 1 \
  --request-length 4096 \
  --num-clients 8 \
  --num-rounds 8 \
  --max-parallel 2 \
  --request-rate 8 \
  --ready-queue-policy random \
  --disable-auto-run \
  --enable-round-barrier \
  2>&1 | tee "$BENCH_OUT/bench.log"
```

如果前 8 轮没有触发 Mooncake pool 淘汰,按顺序增加:

1. `--num-clients 12`
2. `--request-length 6144`
3. `--num-rounds 10`
4. `MOONCAKE_POOL=24GB`

调整原则: 优先缩小 pool 和增加 rounds,不要一开始把 clients 拉满,否则容易把 GPU 调度和 IO 竞争混在一起。

### 5.3 GPU only

```bash
python3 -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --tp 1 \
  --page-size 64 \
  --attention-backend triton \
  2>&1 | tee "$BENCH_OUT/server_gpu_only.log"
```

采集:

```bash
nvidia-smi dmon -s pucm -d 1 > "$BENCH_OUT/gpu_only.dmon" &
iostat -dxm 1 > "$BENCH_OUT/gpu_only.iostat" &
```

### 5.4 HiCache L1+L2

```bash
python3 -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --tp 1 \
  --page-size 64 \
  --attention-backend triton \
  --enable-hierarchical-cache \
  --hicache-ratio 2 \
  2>&1 | tee "$BENCH_OUT/server_hicache_l1_l2.log"
```

### 5.5 HiCache L1+L2+Mooncake

启动 Mooncake master:

```bash
mooncake_master \
  -metrics_port=9004 \
  -logtostderr \
  2>&1 | tee "$BENCH_OUT/mooncake_master.log"
```

启动 Mooncake client。当前工作站**没有 RDMA**,全部用 TCP + P2PHANDSHAKE:

```bash
mooncake_client \
  --host=127.0.0.1 \
  --global_segment_size=32GB \
  --master_server_address=localhost:50051 \
  --metadata_server=P2PHANDSHAKE \
  --protocol=tcp \
  --port=50052 \
  --logtostderr \
  2>&1 | tee "$BENCH_OUT/mooncake_client.log"
```

启动 SGLang (内置 Mooncake client,必须设非零 segment size):

```bash
MOONCAKE_MASTER="127.0.0.1:50051" \
MOONCAKE_GLOBAL_SEGMENT_SIZE=8589934592 \
MOONCAKE_PROTOCOL="tcp" \
python3 -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --tp 1 \
  --page-size 64 \
  --attention-backend triton \
  --enable-hierarchical-cache \
  --hicache-ratio 2 \
  --hicache-storage-prefetch-policy wait_complete \
  --hicache-mem-layout page_first_direct \
  --hicache-storage-backend mooncake \
  2>&1 | tee "$BENCH_OUT/server_mooncake.log"
```

### 5.6 HiCache L1+L2+Mooncake+SSD

准入条件:

```bash
export MOONCAKE_OFFLOAD_DIR=/mnt/ai_ssd0/mooncake_ssd0/file_storage
test -w "$MOONCAKE_OFFLOAD_DIR"
df -h "$MOONCAKE_OFFLOAD_DIR"
```

启动 master:

```bash
mooncake_master \
  -enable_offload=true \
  -metrics_port=9004 \
  -logtostderr \
  2>&1 | tee "$BENCH_OUT/mooncake_master_ssd.log"
```

启动 client (TCP + 非 0 segment size):

```bash
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH="$MOONCAKE_OFFLOAD_DIR" \
MOONCAKE_OFFLOAD_LOCAL_BUFFER_SIZE_BYTES=8589934592 \
MOONCAKE_OFFLOAD_USE_URING=1 \
mooncake_client \
  --host=127.0.0.1 \
  --global_segment_size=32GB \
  --master_server_address=localhost:50051 \
  --metadata_server=P2PHANDSHAKE \
  --protocol=tcp \
  --enable_offload=true \
  --port=50052 \
  --logtostderr \
  2>&1 | tee "$BENCH_OUT/mooncake_client_ssd.log"
```

SGLang server 命令与无 SSD 的 Mooncake 配置保持一致,但需要额外显式传递 offload env var:

```bash
MOONCAKE_MASTER="127.0.0.1:50051" \
MOONCAKE_GLOBAL_SEGMENT_SIZE=8589934592 \
MOONCAKE_PROTOCOL="tcp" \
MOONCAKE_OFFLOAD_FILE_STORAGE_PATH="$MOONCAKE_OFFLOAD_DIR" \
MOONCAKE_OFFLOAD_LOCAL_BUFFER_SIZE_BYTES=8589934592 \
python3 -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --tp 1 \
  --page-size 64 \
  --attention-backend triton \
  --enable-hierarchical-cache \
  --hicache-ratio 2 \
  --hicache-storage-prefetch-policy wait_complete \
  --hicache-mem-layout page_first_direct \
  --hicache-storage-backend mooncake \
  2>&1 | tee "$BENCH_OUT/server_mooncake_ssd.log"
```

> ⚠️ **注意**: sglang 0.5.13 内置 mooncake client 启动时可能不继承 `MOONCAKE_OFFLOAD_FILE_STORAGE_PATH` 环境变量 (实测在 sglang 进程环境已有该变量但内置 client 仍报告 "Storage root directory is not set")。如果后续版本未修复,需要:
> 1. 改用独立的 `mooncake_client` 服务 (不依赖 sglang 内置 client)
> 2. 或者在 sglang/mooncake 的配置文件中指定 offload 路径

---

## 6. IO 采集计划

### 6.1 每轮必须采集

| 层 | 工具 | 文件 | 用途 |
|---|---|---|---|
| benchmark | bench log/json | `bench.log` | per-turn TTFT,input throughput |
| SGLang | server log | `server_*.log` | cache hit/miss,错误,OOM |
| Mooncake | master/client log | `mooncake_*.log` | offload enable,eviction,load/store |
| Mooncake metrics | HTTP metrics | `mooncake_metrics_*.txt` | pool 使用量,offload 计数 |
| block device | iostat | `*.iostat` | rMB/s,wMB/s,await,util,rareq-sz |
| process IO | `/proc/<pid>/io` | `proc_io_*.tsv` | 进程真实 read_bytes/write_bytes |
| GPU | nvidia-smi dmon | `*.dmon` | GPU util,mem util,power |
| file inventory | find/du | `offload_files.txt` | offload 文件数量和大小 |

### 6.2 iostat

```bash
iostat -dxm 1 nvme0n1 nvme1n1 nvme2n1 nvme3n1 \
  > "$BENCH_OUT/mooncake_ssd.iostat" &
```

关注字段:

- `r/s`, `w/s`: IO 请求率。
- `rMB/s`, `wMB/s`: 设备吞吐。
- `rareq-sz`, `wareq-sz`: 平均请求大小。若长期在 64-128KiB,说明 KV offload 是小块读写,不能用 1MiB 顺序读峰值解释。
- `r_await`, `w_await`: 块层排队+设备延迟。
- `%util`: 是否打满设备。若 `%util < 60%` 且 TTFT 高,瓶颈通常在软件栈/同步等待/命中组织,不是 SSD 峰值带宽。

### 6.3 进程级 IO

```bash
PID=$(pgrep -n -f "mooncake_client")
while kill -0 "$PID" 2>/dev/null; do
  ts=$(date +%s.%N)
  awk -v ts="$ts" '
    /read_bytes|write_bytes|cancelled_write_bytes/ {print ts "\t" $1 "\t" $2}
  ' /proc/$PID/io
  sleep 1
done > "$BENCH_OUT/mooncake_client.proc_io.tsv"
```

用途:

- 区分“应用请求从 page cache 读到”与“块设备真实读到”。
- 计算每轮 read_bytes/write_bytes 增量,对齐 TTFT。

### 6.4 bpftrace 块层延迟

只在有 root/bpf 权限时运行:

```bash
sudo bpftrace -e '
kprobe:blk_mq_start_request {
  @start[arg0] = nsecs;
  @bytes[((struct request *)arg0)->__data_len] = count();
}
kprobe:blk_mq_end_request /@start[arg0]/ {
  @lat_us = hist((nsecs - @start[arg0]) / 1000);
  delete(@start[arg0]);
}
interval:s:10 {
  print(@bytes);
  print(@lat_us);
  clear(@bytes);
  clear(@lat_us);
}' > "$BENCH_OUT/blk_latency.btlog"
```

关注:

- 读写请求大小是否集中在 64/128/256KiB。
- p50/p95/p99 设备完成时间。
- `iostat r_await` 明显高于 bpftrace d2c 延迟时,说明排队或上层同步等待占主导。

### 6.5 fio 硬件基线

每块候选盘至少跑三组。不要在包含重要数据的裸设备上直接 destructive fio;优先使用挂载目录里的测试文件。

```bash
fio --name=randread_4k_qd32 \
  --directory=/mnt/mooncake_ssd0/fio \
  --filename=randread_4k_qd32.dat \
  --size=32G \
  --direct=1 \
  --ioengine=libaio \
  --iodepth=32 \
  --rw=randread \
  --bs=4k \
  --runtime=60 \
  --time_based \
  --group_reporting \
  --output="$BENCH_OUT/fio_randread_4k_qd32.json" \
  --output-format=json

fio --name=randread_128k_qd32 \
  --directory=/mnt/mooncake_ssd0/fio \
  --filename=randread_128k_qd32.dat \
  --size=32G \
  --direct=1 \
  --ioengine=libaio \
  --iodepth=32 \
  --rw=randread \
  --bs=128k \
  --runtime=60 \
  --time_based \
  --group_reporting \
  --output="$BENCH_OUT/fio_randread_128k_qd32.json" \
  --output-format=json

fio --name=write_128k_qd32 \
  --directory=/mnt/mooncake_ssd0/fio \
  --filename=write_128k_qd32.dat \
  --size=32G \
  --direct=1 \
  --ioengine=libaio \
  --iodepth=32 \
  --rw=write \
  --bs=128k \
  --runtime=60 \
  --time_based \
  --group_reporting \
  --output="$BENCH_OUT/fio_write_128k_qd32.json" \
  --output-format=json
```

判读:

- Mooncake offload 若主要 128KiB read,优先看 128KiB randread/seqread 的 p95/p99 latency。
- 若端到端吞吐远低于 fio 能力,不要直接下结论“SSD 慢”;需要看 Mooncake worker、SGLang cache hit、同步 prefetch policy。

---

## 7. 分析方法

### 7.1 核心图表

每组配置输出以下图表:

1. `per_turn_ttft.png`: X=round,Y=avg/p50/p95 TTFT,四配置同图。
2. `per_turn_hit_rate.png`: X=round,Y=cache hit rate,标记 pool 耗尽点。
3. `ttft_vs_hit_rate.png`: X=hit rate,Y=TTFT,看 hit 下降是否解释 TTFT 上升。
4. `io_timeline.png`: X=time,Y=rMB/s/wMB/s/%util,叠加 benchmark round boundary。
5. `read_latency_hist.png`: bpftrace 或 fio 的 read latency histogram。
6. `offload_bytes_by_round.png`: 每轮 Mooncake/client write_bytes/read_bytes 增量。

### 7.2 关键派生指标

| 指标 | 计算 | 意义 |
|---|---|---|
| SSD offload TTFT gain | `(TTFT_mooncake - TTFT_mooncake_ssd) / TTFT_mooncake` | DRAM pool 耗尽后 SSD 的收益 |
| GPU-only TTFT gain | `(TTFT_gpu_only - TTFT_mooncake_ssd) / TTFT_gpu_only` | 完整 cache hierarchy 相对无 offload 的收益 |
| Throughput speedup | `throughput_ssd / throughput_gpu_only` | 输入 token 吞吐提升 |
| Pool cliff round | 第一轮 hit rate 急降或 TTFT 急升的 round | 判断 pool 是否被压爆 |
| Real disk read ratio | `block_device_read_bytes / process_read_bytes` | page cache 是否隐藏真盘读 |
| IO cost per token | `device_read_bytes / input_tokens` | 每 token SSD 读取成本 |
| Tail amplification | `p99_TTFT / p50_TTFT` | 多客户端下 tail 是否恶化 |

### 7.3 判读规则

有效复现 Mooncake SSD offload 收益需要同时满足:

- 无 SSD 的 Mooncake 配置在中后段 round 出现 hit rate 下跌。
- SSD 配置在同一 round 的 hit rate 明显更高。
- SSD 配置的 TTFT 明显低于无 SSD Mooncake。
- `mooncake_client` 有 write_bytes/read_bytes 增量,offload 目录文件大小增长。
- iostat 或 bpftrace 能看到目标 NVMe 在对应 round 出现读写活动。

以下情况不能作为 SSD offload 成功证据:

- 只有 TTFT 下降,但 offload 目录没有写入。
- hit rate 没变,TTFT 小幅变化。
- iostat 目标盘没有 IO,说明数据可能仍在 DRAM/page cache。
- GPU only、HiCache L1+L2、Mooncake、Mooncake+SSD 四配置没有触发容量压力,全部 round 表现接近。

---

## 8. 当前机器推荐执行顺序

### 阶段 A: 准入条件 (已通过,无需重复)

1. ✅ GPU 驱动 595.71.05,`nvidia-smi -L` 正常。
2. ✅ NTFS offload 目录已创建:`/mnt/ai_ssd0/mooncake_ssd0/file_storage` 可写。
3. ✅ Mooncake master/client 可运行 (venv 激活后)。
4. ✅ SGLang benchmark 脚本存在并能连到 server。
5. ✅ LD_LIBRARY_PATH 已包含 CUDA 12 runtime (已在 activate 追加)。

### 阶段 B: Smoke 测试 (已完成)

1. ✅ 四配置全部跑通 (GPU only / HiCache L1+L2 / Mooncake only / Mooncake+SSD)。
2. ✅ 测试参数: 4 clients × 3 rounds, 3KB prompt, 1 token output。
3. ✅ Mooncake 8GB DRAM segment + 8GB host HiCache, TCP localhost。
4. ✅ 全部 12 requests 完成,TTFT 有效。

### 阶段 C: 容量压力主测 (已完成)

1. `pool=32GB`, `buffer=8GB`, `clients=8`, `rounds=8`, `request_length=4096`。
2. 如果没有 pool cliff,逐步升 `request_length` 和 `clients`,或降 pool 到 24GB。
3. 每组至少重复 3 次,报告 mean/p50/p95/CV。

### 阶段 E: IO 归因

1. 对 cliff round 前后做 per-round IO 增量。
2. 对比 Mooncake vs Mooncake+SSD 的 hit rate 和 read_bytes。
3. 若 SSD 配置 TTFT 仍高,用 bpftrace 区分设备延迟、队列等待、软件等待。

---

## 9. 报告模板

最终报告按以下结构输出:

```text
1. 摘要
   - 当前硬件
   - 是否复现 pool cliff
   - SSD offload TTFT gain
   - input throughput speedup

2. 测试环境
   - GPU/CPU/内存/SSD/文件系统/Mooncake/SGLang 版本

3. 测试矩阵
   - 四配置参数
   - clients/rounds/request_length/pool/buffer

4. 端到端结果
   - overall TTFT
   - input throughput
   - per-turn TTFT
   - per-turn hit rate

5. IO 分析
   - offload 文件写入量
   - process read/write bytes
   - iostat timeline
   - bpftrace latency/request size

6. 结论
   - SSD 是否避免 DRAM pool 耗尽后的性能断崖
   - 当前瓶颈是 SSD、Mooncake worker、SGLang prefetch 还是 GPU compute
   - 下一轮参数建议
```

---

## 10. 风险与注意事项

- ✅ `nvidia-smi` 可用,可以报告 TTFT 性能结论。注意单卡 16GB 显存限制,模型 ≤7B。
- ✅ `/mnt/ai_ssd0/1/2` 是 NTFS/fuseblk 可写 (实测 2.7 GB/s direct IO),可以直接作为 Mooncake SSD offload 目标。NTFS 碎片不影响短期测试。
- 83GiB DRAM 不支持 80GB pool + 20GB buffer;照抄 DGX 参数会导致 OOM 或 swap 污染。
- 单机 localhost/TCP 测试不能代表 DGX 双 HDR RDMA 传输开销。
- page cache 会让 SSD 读看起来过快;必须用 iostat/bpftrace/proc IO 交叉验证。
- fio 峰值不能直接解释 KV offload 性能;Mooncake/SGLang 的 IO 粒度、同步策略、hit rate 才决定 TTFT。
- `MOONCAKE_OFFLOAD_FILE_STORAGE_PATH` 环境变量在 sglang 0.5.13 内置 mooncake client 中可能不被继承 (实测问题)。如果正式测试中 offload 目录没有写入,需要检查 sglang 进程环境是否包含该变量,或改用独立 mooncake_client 方案。
