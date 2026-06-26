# Mooncake SSD Offload 测试手册 — **实测修正 patch**

**适用文档**: `mooncake-ssd-offload-hardware-test-io-plan-2026-06-26.md`
**实测日期**: 2026-06-26 09:30-10:10
**实测结论**: 环境**基本就绪**,3 处文档假设需要修正,1 个新坑需要规避。

---

## 修正 1: GPU 准入 (§1 + §3.1)

### 文档原话 (§1 表格)
> "端到端 SGLang/Mooncake GPU 测试需先修复驱动"

### 实测结果
```
$ nvidia-smi -L
GPU 0: NVIDIA GeForce RTX 5080 (UUID: GPU-bf0ccb9a-...)
GPU 1: NVIDIA GeForce RTX 5060 Ti (UUID: GPU-6845fa38-...)
$ nvidia-smi --query-gpu=driver_version --format=csv
driver_version
595.71.05
```

**状态**: ✅ **可用**,无需修复。删除 §3.1 的"通过标准"中关于驱动修复的担忧。

---

## 修正 2: SSD 挂载状态 (§1 + §2.2 + §3.2)

### 文档原话 (§2.2 表格)
> `/mnt/ai_ssd0/1/2` NTFS/fuseblk **只读**

### 实测结果
```
$ findmnt -no SOURCE,FSTYPE,OPTIONS /mnt/ai_ssd0
/dev/nvme1n1p2 /mnt/ai_ssd0 fuseblk rw,relatime,user_id=0,group_id=0,default_permissions,allow_other,blksize=4096
$ dd if=/dev/zero of=/mnt/ai_ssd0/test bs=1M count=100 oflag=direct
100+0 records in / out
104857600 bytes (105 MB) copied, 0.0395842 s, **2.7 GB/s**
$ rm /mnt/ai_ssd0/test
$ df -h /mnt/ai_ssd0
/dev/nvme1n1p2  895G  596G  300G  67% /mnt/ai_ssd0
```

**状态**: ✅ **可写,2.7 GB/s,300GB 可用**。无需重挂为 ext4。

**§3.2 修正**:
```bash
# 原文档要求重挂 ext4, 改为直接在 NTFS 上建目录:
mkdir -p /mnt/ai_ssd0/mooncake_ssd0/file_storage
chown -R "$USER:$USER" /mnt/ai_ssd0/mooncake_ssd0

# 验证
test -w /mnt/ai_ssd0/mooncake_ssd0/file_storage && echo "OK"
df -h /mnt/ai_ssd0/mooncake_ssd0
```

---

## 修正 3: venv + LD_LIBRARY_PATH (§3.3 新增)

### 问题
- `mooncake_master / mooncake_client / mooncake_http_metadata_server` 都在 `/home/ficus/llm/.venv/bin/`,**不在默认 PATH**
- `mooncake_transfer_engine-0.3.11.post1` 链接 `libcudart.so.12`,但 venv 默认只有 `cu13/libcudart.so.13`

### 修复 (已永久写入)
```bash
# 必须在跑任何 mooncake 命令前激活
source /home/ficus/llm/.venv/bin/activate

# 验证
which mooncake_master  # /home/ficus/llm/.venv/bin/mooncake_master
echo $LD_LIBRARY_PATH | grep cuda_runtime && echo "✅ libcudart.so.12 可加载"
```

**已在 `/home/ficus/llm/.venv/bin/activate` 末尾追加 LD_LIBRARY_PATH 自动设置**,激活即生效。

---

## 修正 4: §5.5 master 启动命令 (核心改动)

### 文档原命令 (会失败)
```bash
mooncake_master \
  -http_metadata_server_port=8081 \   # ← HTTP server 1 秒后自动停
  -metrics_port=9004 \
  -logtostderr
```

### 实测日志 (失败)
```
HTTP metadata server started on 0.0.0.0:8081
C++ HTTP metadata server started successfully
HTTP metadata server stopped   ← 1 秒后停了
```

**根因**: 单机模式下,`enable_ha=0` + `etcd_endpoints=` 没设时,HTTP metadata server **自动退出**(等不到 HA 后端)。

### 修正命令 (实测通过)
```bash
mooncake_master \
  -metrics_port=9004 \
  -logtostderr
```

实测:
```
Master service started on port 50051, max_threads=4, ...
Task cleanup thread started
Master admin server started on port 9004
```
50051 (RPC) + 9004 (metrics) **稳定监听**。

---

## 修正 5: §5.6 client 启动命令 (用 TCP 替代 RDMA)

### 文档原命令 (失败,本机无 RDMA)
```bash
mooncake_client \
  --protocol=rdma \                     # ← 本机没 RDMA 设备
  --device_names=<rdma_device_names> \  # ← 无设备名可填
  ...
```

### 修正命令 (实测通过)
```bash
# Master 用上一步的 background 命令跑

# Client 用 P2PHANDSHAKE + TCP
export MOONCAKE_OFFLOAD_FILE_STORAGE_PATH="/mnt/ai_ssd0/mooncake_ssd0/file_storage"
export MOONCAKE_OFFLOAD_LOCAL_BUFFER_SIZE_BYTES=8589934592

mooncake_client \
  --host=127.0.0.1 \
  --global_segment_size=8GB \          # 起步用 8GB, 稳定后改 32GB
  --master_server_address=127.0.0.1:50051 \
  --metadata_server=P2PHANDSHAKE \     # 单机 P2P 直连
  --protocol=tcp \                     # ← 改 TCP, 不是 RDMA
  --port=50052 \
  --logtostderr
```

实测:
```
Transfer Engine RPC using P2P handshake, listening on 127.0.0.1:16671
Successfully created client on port 14031 after 1 attempt(s)   ← 1 次就连上 master
Mounting segment: 8589934592 bytes (8GB 成功)
Starting real client service on 127.0.0.1:50052
```

---

## 修正 6: §5.6 SSD client 命令 (合并到 §5.6)

**原 §5.6 的 SSD 启动命令** 与非 SSD 几乎一样,只是加 `--enable_offload=true` 和 offload env vars。实测一样能用 TCP。

### 完整 SSD 配置命令
```bash
# 1. Master (与 §5.5 修正版相同)
mooncake_master \
  -metrics_port=9004 \
  -logtostderr &

# 2. Client (带 offload)
export MOONCAKE_OFFLOAD_FILE_STORAGE_PATH="/mnt/ai_ssd0/mooncake_ssd0/file_storage"
export MOONCAKE_OFFLOAD_LOCAL_BUFFER_SIZE_BYTES=8589934592
export MOONCAKE_OFFLOAD_USE_URING=1

mooncake_client \
  --host=127.0.0.1 \
  --global_segment_size=8GB \
  --master_server_address=127.0.0.1:50051 \
  --metadata_server=P2PHANDSHAKE \
  --protocol=tcp \
  --enable_offload=true \
  --port=50052 \
  --logtostderr &

# 3. SGLang server (与文档相同)
MOONCAKE_MASTER="127.0.0.1:50051" \
MOONCAKE_GLOBAL_SEGMENT_SIZE=0 \
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
  2>&1 | tee "$BENCH_OUT/server_mooncake_ssd.log"
```

---

## 实测没改但需要确认的点

| 项 | 文档假设 | 实测状态 | 备注 |
|---|---|---|---|
| §2.1 CPU/内存 | Ultra 7 270K Plus / 83GiB | ✅ 一致 | |
| §2.1 max_sectors_kb | 128 | ✅ 一致 | |
| §2.2 NVMe 容量 | 各 ~1TB | ✅ 一致 | |
| §3.3 sglang | 0.5.13 | ✅ 实测 0.5.13 | |
| §3.3 lmcache | (隐含) | ✅ 实测 0.4.6 | |
| §3.3 BurstGPT trace | /datasets/BurstGPT/... | ✅ 文件存在 | |
| §4 模型 Qwen3-8B | 建议值 | ❌ 没 8B,有 4B-Instruct-2507 和 14B | **改用 Qwen3-4B** |
| §4 TP=1 | 建议值 | ✅ sglang 0.5.13 `--tp` alias 仍可用 | |
| §6 IO 采集 8 层 | 完整 | ✅ iostat/bpftrace/perf/fio 都已装 | |

---

## 总结: 实测修正清单

| # | 改动 | 影响 |
|---|---|---|
| 1 | §1/§3.1 删除 "驱动不可用" | 直接跳到准入通过 |
| 2 | §2.2/§3.2 把 "NTFS 只读" 改 "可写" | 不需要重挂 |
| 3 | §3.3 新增 venv 激活步骤 | 必须,否则 mooncake 找不到 |
| 4 | §5.5 master 命令去掉 -http_metadata_server_port | 单机不需要 HTTP metadata |
| 5 | §5.5/§5.6 全部命令把 rdma 改成 tcp | 本机没 RDMA |
| 6 | §4 模型改 Qwen3-4B (没 Qwen3-8B) | 16GB 卡显存限制 |
| 7 | §5.5 sglang 的 `MOONCAKE_GLOBAL_SEGMENT_SIZE` 必须非 0 | 为 0 时 warmup 失败 (sglang 内置 client) |
| 8 | §3.3 activate 追加 `${LD_LIBRARY_PATH:-}` 防 `set -u` | bash 脚本 `set -u` 下 activate 会报未绑定变量 |

### 主测 (§4.2) 阶段实际参数

| 参数 | 计划值 | 实际值 | 说明 |
|---|---|---|---|
| clients | 8/12/16 三档 | 8 | 减少维度 |
| rounds | 8-10 | 6 | 
| duration | 600s bench | 300s | 每配置约 5-8 分钟 |
| request_length | 4096 | 4096 | 一致 |
| 配置数 | 4 (GPU/HiCache/Mooncake/+SSD) | 4 | 一致 |

### 主测结果

| 配置 | avg TTFT | P99 TTFT | Input tput | 总体 Cache% |
|---|---|---|---:|---:|---:|---:|
| GPU only | 5.166s | 12.431s | 3652 tok/s | 4.2% |
| HiCache L1+L2 | 4.939s | 12.763s | 3744 tok/s | 14.3% |
| +Mooncake (no SSD) | **4.550s** | 12.698s | 3966 tok/s | **19.7%** |
| +Mooncake +SSD | 4.546s | 12.693s | 3969 tok/s | 19.6% |

**关键发现**:
- Mooncake R4/R5 维持 20%/10% cache hit, HiCache 只剩 4%/0% → **Mooncake DRAM pool 有效**
- Mooncake+SSD 与 Mooncake 完全一致 → **SSD offload 路径没被触发** (env var 没传进 sglang 内置 client)
- GPU 是限制因素: 所有配置 TTFT R0~0.7s → R5~11.6s

### 当前阻塞的最后问题

**SSD offload env var 不生效**: sglang 0.5.13 内置 mooncake client 在启动时不继承 `MOONCAKE_OFFLOAD_FILE_STORAGE_PATH`,导致 offload 目录为空。需要:
1. 在 sglang server 进程环境显式 export
2. 或改用独立 `mooncake_client` 服务
