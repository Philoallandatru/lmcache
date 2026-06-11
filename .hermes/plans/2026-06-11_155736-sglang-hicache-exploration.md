# SGLang HiCache × AI SSD 预研实施计划

> **目标:** 用 SGLang HiCache 的 `file` storage backend,在 4 块候选 NVMe 上测真实 KV cache offload,
> 与已有 vLLM + LMCache 数据集形成双引擎交叉验证,补全 AI SSD 选型决策。
>
> **状态:** 探索阶段 — 本文档先做技术调研与方案设计,不直接执行。
>
> **关联仓库:**
> - `~/llm/infer/ai_ssd_prestudy/` (lmcache 数据,本计划落点)
> - `~/llm/storage/` (fio + kv-cache.py 合成负载数据,作 IO 模式参照基线)

---

## 0. TL;DR

| 维度 | LMCache (已做) | HiCache (本计划) | 关键差异 |
|---|---|---|---|
| 推理引擎 | vLLM 0.22.1 | **SGLang** (待定版本) | 不同调度器 |
| KV 后端 | `local_cpu` + `local_disk` | GPU L1 + DRAM L2 + **disk L3** | L2 大小显式控制 |
| 文件粒度 | chunk_size 256 token = 37.7 MB | **page_size 64 token** = 待测 | 文件数 × ~16 |
| O_DIRECT | 可选(关 NTFS 候选盘) | HiCacheFile 用 `open(rb)`,走 page cache | HiCache 无 O_DIRECT |
| 写策略 | 异步 store 后台线程 | write_through / write_back / write_through_selective | 显式可调 |
| IO 监测 | LMCache 报告 + iostat + bpftrace | HiCache metrics + iostat + bpftrace + L3 指标 | 更丰富 |
| 优势点 | 已实测,数据齐全 | HiCache 是 CPU L2 + disk L3,更接近真实分层 | 双引擎对比 |

**预期产出:** 在 4 盘上跑同模型 (Qwen3-4B / 8B) 同负载 (7000-token prefix + 多轮 warm),得到:
- IO 模式 (req size、IOPS、BW、await、queue depth)
- 冷热两档 TTFT 加速比
- GC / SLC cliff 表现
- 与 LMCache 数据交叉对比

---

## 1. 现状盘点 (Context)

### 1.1 已有的 AI SSD 预研数据 (`~/llm/infer/ai_ssd_prestudy/`)

| 资产 | 内容 | 用途 |
|---|---|---|
| `REPORT.md` | 4 轮 lmcache × 4 盘的 TTFT + IO 数据 | 选型基线 |
| `scripts/drive_rounds.sh` | 4 盘串行驱动脚本 | 模板可复用 |
| `scripts/serve_lmcache.sh` | vllm + lmcache 启动模板 | 改成 sglang + hicache |
| `scripts/lmcache_*.yaml` | 4 个盘的 lmcache 配置 | 改成 hicache 启动参数 |
| `scripts/load_test.py` | OpenAI-compat 客户端 (1 cold + N warm) | **直接复用**测 TTFT |
| `scripts/io_monitor.py` | iostat 1s 粒度采集 | 直接复用 |
| `scripts/blk_io_latency.bt` | bpftrace IO latency 直方图 | 直接复用 |
| `results/{baseline,ai_ssd{0,1,2}}_*/` | 4 盘的 iostat + load test 输出 | 作 LMCache 对照 |
| `lmcache_cache_baseline/` | 真实落盘的 `.pt` 文件 (37.7 MB / file) | 验证 LMCache IO 粒度 |

**结论**: 已有完整的 4 盘测试框架 + LMCache 基线数据,只需要把推理引擎和 KV 后端换成 SGLang HiCache。

### 1.2 已有的合成负载数据 (`~/llm/storage/`)

| 资产 | 内容 | 用途 |
|---|---|---|
| `kv_cache_benchmark/kv-cache.py` | 合成 8B/70B KV cache 压力工具 | **可参考但不可直接复用** |
| `results/cross_vendor_*` | 30+ 次 4 盘 fio + kv-cache.py 跑 | 合成 IO 模式参照 |
| `docs/kv-cache-final-selection-2026-06-10.md` | 选型主报告 | HiCache 实测后可追加 HiCache 章节 |

### 1.3 候选盘 (与 LMCache 实验一致)

| 设备 | 型号 | FW | 分区 | FS | 容量 | 备注 |
|---|---|---|---|---|---|---|
| nvme0n1 | WDC WDS960G2G0C | 231800WD | p2 | NTFS | 894G | SLC cache 最小(~2GB) |
| nvme1n1 | BIWIN X570 | BM555ALN | p3 | **ext4** | 384G(/) | 系统盘 baseline |
| nvme2n1 | ZHITAI Ti600 | ZTA23004 | p3 | NTFS | 931G | 写延迟最优(0.20ms) |
| nvme3n1 | Seagate ZP1000GV30012 | SUKSY000 | p2 | NTFS | 931G | 长稳态冠军 |

---

## 2. SGLang HiCache 技术调研 (已确认的事实)

### 2.1 架构概览

```
            L1 (GPU VRAM)        ← RadixAttention 已有
            │
            ↓ write-through / write-back
            L2 (Host DRAM, "mem_pool_host")  ← 新增,可配 hicache-ratio 或 hicache-size
            │
            ↓ write-back / async prefetch
            L3 (Storage backend)  ← 新增,可配 --hicache-storage-backend
            │
            ├── file      (本地文件系统,.bin pages)
            ├── mooncake  (RDMA,MoE 大模型用)
            ├── hf3fs     (DeepSeek 3FS,K8s)
            ├── nixl      (GDS / S3 / 各种 plugin)
            ├── aibrix    (生产级 KVCache 框架)
            ├── eic       (Intel EIC)
            └── lmcache   (与 LMCache 互操作)
```

**关键创新点(相比 LMCache):**
1. **显式 3 层** (L1/L2/L3) vs LMCache 的 L1=CPU + L2=disk
2. **page_size 可配** (默认 64 tokens),LMCache 固定 chunk_size 256
3. **写策略可配**:write_through / write_back / write_through_selective
4. **预取策略可配**:best_effort / wait_complete / **timeout**(生产推荐)
5. **零拷贝**:page_first_direct layout + kernel io backend,L2↔GPU 3x 加速
6. **运行时 attach/detach** L3 backend (HTTP API),无需重启

### 2.2 L3 = `file` 后端实现细节

源码: `python/sglang/srt/mem_cache/hicache_storage.py::HiCacheFile` (lines 319+)

| 属性 | 值 | 备注 |
|---|---|---|
| 文件命名 | `{key}{model_name}_{tp_rank}_{tp_size}.bin` | 每个 KV page 一个文件 |
| 写 | `open(path, "wb").write(tensor_bytes)` | 标准 buffered write,无 O_DIRECT |
| 读 | `open(path, "rb", buffering=0).readinto(torch_uint8_buf)` | **直接读入 torch tensor** |
| 缓存路径 | `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR` 环境变量 | 默认 `/tmp/hicache` |
| 淘汰策略 | `LRUFileEvictor` (按大小 + LRU) | 可配置 max_size |
| batch | `STORAGE_BATCH_SIZE = 128` pages | 单次 IO 最多 128 pages |

### 2.3 与 LMCache 关键差异表

| 维度 | LMCache | HiCache `file` |
|---|---|---|
| 文件粒度 | chunk_size 256 tokens = 37.7 MB | **page_size 64 tokens ≈ 9.4 MB** |
| 文件数 (per cold req) | 8 chunks × 1 file = 8 files | ~30 pages × 1 file ≈ 30 files (4x) |
| 写触发 | 后台 store 线程 | prefill 后 write-back / write-through |
| O_DIRECT | `use_odirect` 标志 (ext4 可,NTFS 不行) | **不支持,走 page cache** |
| 读路径 | load → CPU cache → GPU | L2 DRAM → GPU (直接 readinto) |
| 内存压力 | `max_local_cpu_size` 限制 L1 | `hicache-ratio` / `hicache-size` 限制 L2 |
| 数据布局 | layer first | **layer_first / page_first / page_first_direct** |

### 2.4 启动方式 (SGLang launch_server)

最小配置 (file 后端):
```bash
python3 -m sglang.launch_server \
    --model-path /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --port 30000 \
    --page-size 64 \
    --enable-hierarchical-cache \
    --hicache-ratio 2 \                          # L2 = 2x L1 (GPU KV cache)
    --hicache-size 0 \                           # 0 = 让 ratio 生效
    --hicache-mem-layout page_first_direct \     # 与 direct IO 配合最优
    --hicache-io-backend direct \                # 或 kernel (3x faster,需 page_first)
    --hicache-write-policy write_through \       # 与 LMCache store 行为最接近
    --hicache-storage-backend file \
    --hicache-storage-prefetch-policy timeout    # 生产推荐
```

运行时切换 (无需重启):
```bash
# 查询
curl -s http://127.0.0.1:30000/hicache/storage-backend

# 挂载 file 后端 (key=value 形式)
curl -X PUT http://127.0.0.1:30000/hicache/storage-backend \
    -H 'Content-Type: application/json' \
    -d '{"hicache_storage_backend":"file"}'

# 卸载
curl -X DELETE http://127.0.0.1:30000/hicache/storage-backend
```

---

## 3. 实验设计 (Proposal)

### 3.1 实验矩阵

按 **"盘 × 写策略 × 模型"** 三维变量:

| 维度 | 取值 |
|---|---|
| 盘 | nvme0n1(WDC) / nvme1n1(BIWIN,系统盘对照) / nvme2n1(ZHITAI) / nvme3n1(Seagate) |
| 写策略 | `write_through` (主) / `write_back` (副) |
| 模型 | Qwen3-4B-Instruct-2507 (与 LMCache 完全一致) |
| IO 负载 | 1 cold + 5 warm,prefix 7000 tokens (与 LMCache 一致) |

主测:`write_through` × 4 盘 (与 LMCache 最接近的对照)
副测:`write_back` × 2 盘 (BIWIN + Seagate,验证策略对盘差的影响)

### 3.2 每轮测试流程 (复用 LMCache 框架)

```
0. mount 候选盘 → /mnt/ai_ssdN (NTFS3,noatime)
1. 清理旧 cache: rm -rf $CACHE_DIR/*
2. 启动 iostat -xm 1 $DEV > /tmp/iostat_$round.log (后台)
3. 启动 bpftrace blk_io_latency.bt $DEV > /tmp/bpf_$round.log (后台)
4. 启动 SGLang (启用 HiCache,指向 $CACHE_DIR)
5. 等服务就绪 (curl http://127.0.0.1:30000/v1/models)
6. 跑 load_test.py: 1 cold + 5 warm 同 prompt (7000 tokens)
7. 记录 7 次 TTFT 到 /tmp/ttft_$round.jsonl
8. shutdown SGLang,等 iostat/bpftrace 退出
9. 收集: cache 文件数 + 总大小,生成 summary
```

### 3.3 监测点

| 指标 | 工具 | 频率 | 备注 |
|---|---|---|---|
| IOPS / BW / await / util / queue | iostat -xm 1 | 1s | 与 LMCache 一致,可直接复用 io_monitor.py |
| 单 IO latency 直方图 | bpftrace blk_io_latency.bt | 事件驱动 | 复用 `scripts/blk_io_latency.bt` |
| TTFT / ITL | load_test.py | 每请求 | 复用现有脚本,改 endpoint |
| HiCache 内部指标 | `curl /metrics` (Prometheus) | 请求级 | 看 hit rate + prefetch 时间 |
| cache 文件数 | `ls $CACHE_DIR \| wc -l` | 测前/后 | 与 LMCache 文件数对比 |

### 3.4 预期产出数据

| 文件 | 内容 |
|---|---|
| `results/hicache/baseline_ext4/` | BIWIN ext4 (系统盘) 数据 |
| `results/hicache/ai_ssd0_nvme0n1/` | WDC NTFS 数据 |
| `results/hicache/ai_ssd1_nvme2n1/` | ZHITAI NTFS 数据 |
| `results/hicache/ai_ssd2_nvme3n1/` | Seagate NTFS 数据 |
| `docs/hicache-{baseline,4disk}-{TTFT,io-pattern}-2026-06-XX.md` | 报告 |

---

## 4. 风险与不确定性

### 4.1 必须先解决的 blocker

| Blocker | 现状 | 解决方式 |
|---|---|---|
| SGLang 未安装 | `pip show sglang` 失败 | 装最新 stable:`pip install "sglang[all]"`,需 CUDA 12.x |
| 模型加载时间 | Qwen3-4B 7GB,首次加载 ~30s | 复用 `/home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507/` |
| GPU 占用冲突 | 与 LMCache 实验共享 GPU | 测试时停掉 LMCache vllm 进程 |
| NTFS + page cache | HiCache `file` 后端走 page cache,可能掩盖盘真实差 | 增加 `sync && echo 3 > /proc/sys/vm/drop_caches` 在每次 warm #1 之前 |

### 4.2 与 LMCache 对比时的"非公平因素"

1. **page cache**:HiCache `file` 走 page cache,内核会预读/合并写。LMCache `use_odirect=true` 走 O_DIRECT。
   - **修正**:HiCache 也用 `drop_caches` 后冷读,保持可比性
2. **文件粒度**:HiCache 64 tokens/page = 9.4 MB,LMCache 256 tokens/chunk = 37.7 MB
   - **影响**:HiCache IO 次数 4x,但单次更小,SLC cache 利用率不同
3. **写策略**:LMCache 是异步后台 store;HiCache `write_through` 是阻塞同步
   - **影响**:TTFT 会包含 L3 写时间,加速比看起来会"更小",但 IO 真实负载更大
4. **L2 内存**:HiCache 强制 L2 ≥ L1,LMCache L1 CPU 是可选
   - **影响**:HiCache warm #2/#3 命中 L2 (DRAM),LMCache warm #2/#3 命中 L1 (CPU cache) — 都不到 disk,真实 disk IO 只在 cold + warm #1 (drop_caches 后)

### 4.3 关键风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| SGLang 与现有 torch 2.11 ABI 冲突 | 中 | 高 | 准备单独 venv `~/llm/.venv-sglang/` |
| NTFS mount 失效(Windows 双系统不一致) | 中 | 中 | 每轮 mount 验证,加 fallback 到 ext4 |
| HiCache 监控指标不准(metrics 未上) | 低 | 中 | 用 iostat + bpftrace 主控,metrics 仅参考 |
| 4B 模型太轻,吃不满盘 | 高(已知) | 中 | 与 LMCache 一致不升级,加一个 8B 副测 |
| SGLang HiCache page_size 64 不支持某些模型 | 低 | 中 | 备选 page_size 16 / 32 |

---

## 5. 分阶段实施任务 (Bite-sized Tasks)

### Phase 0: 环境准备 (1-2 小时)

#### Task 0.1: 装 SGLang 到独立 venv

**Files:**
- Create: `~/llm/.venv-sglang/` (新 venv)
- Create: `scripts/setup_sglang.sh` (安装脚本)
- Modify: `~/llm/infer/ai_ssd_prestudy/.gitignore` (venv 不入 git)

**Step 1**: 写安装脚本
```bash
#!/bin/bash
# scripts/setup_sglang.sh
set -e
cd ~/llm
uv venv .venv-sglang --python 3.12
source .venv-sglang/bin/activate
uv pip install torch==2.7.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install "sglang[all]" --upgrade
# 验证
python -c "import sglang; print('sglang:', sglang.__version__); import torch; print('torch:', torch.__version__, 'cuda:', torch.cuda.is_available())"
```

**Step 2**: 跑通
```bash
bash scripts/setup_sglang.sh
# Expected: "sglang: 0.4.x" + "torch: 2.7.0+cu128 cuda: True"
```

**Step 3**: 冒烟 — 启动一个最小 sglang server,curl /v1/models
```bash
source ~/llm/.venv-sglang/bin/activate
python -m sglang.launch_server --model-path ~/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --port 30000 --max-model-len 8192 --mem-fraction-static 0.7 &
SERVER_PID=$!
sleep 60  # 加载时间
curl -s http://127.0.0.1:30000/v1/models | jq .
kill $SERVER_PID
```
Expected: 返回 `{"data":[{"id":"Qwen3-4B-Instruct-2507",...}]}`

#### Task 0.2: 验证 4 盘 mount 状态

**Step 1**: 写 `scripts/check_mounts.sh`
```bash
#!/bin/bash
# 验证 4 盘都挂好,fs 正确
for mp in /mnt/ai_ssd0 /mnt/ai_ssd1 /mnt/ai_ssd2; do
    ls -la $mp | head -3
    stat -f -c '%T' $mp  # 文件系统类型
done
# ext4 系统盘
df -h /home/ficus
```

**Step 2**: 跑通,记录到 REPORT.md "现状" 章节

#### Task 0.3: 复用 LMCache 监测脚本到新目录

**Files:**
- Copy: `scripts/{io_monitor.py,blk_io_latency.bt,blk_io_lat.sh}` → `scripts/hicache/`
- Create: `scripts/hicache/load_test_hicache.py` (改 endpoint 到 :30000)

### Phase 1: HiCache 单盘冒烟 (半天)

#### Task 1.1: 第一次 HiCache 启动 + 1 cold + 1 warm

**Files:**
- Create: `scripts/hicache/serve_hicache_baseline.sh`
- Create: `scripts/hicache/run_one_round.sh`

**Step 1**: 写启动脚本 (BIWIN 系统盘 baseline)
```bash
#!/bin/bash
# scripts/hicache/serve_hicache_baseline.sh
set -e
source ~/llm/.venv-sglang/bin/activate

export SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/home/ficus/llm/infer/ai_ssd_prestudy/cache_baseline

# 先清干净
rm -rf "$SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR"/*

python -m sglang.launch_server \
    --model-path /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --port 30000 \
    --max-model-len 8192 \
    --page-size 64 \
    --mem-fraction-static 0.7 \
    --enable-hierarchical-cache \
    --hicache-ratio 2 \
    --hicache-mem-layout page_first_direct \
    --hicache-io-backend direct \
    --hicache-write-policy write_through \
    --hicache-storage-backend file \
    --hicache-storage-prefetch-policy timeout
```

**Step 2**: 写单轮 driver
```bash
#!/bin/bash
# scripts/hicache/run_one_round.sh
ROUND=$1
DEV=$2
CACHE_DIR=$3
LOG_DIR=~/llm/infer/ai_ssd_prestudy/results/hicache/${ROUND}
mkdir -p $LOG_DIR

# 启动 iostat + bpftrace 后台
iostat -xm 1 $DEV > $LOG_DIR/iostat.log &
IOSTAT_PID=$!

# 启动 server (写入日志)
SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=$CACHE_DIR \
    bash scripts/hicache/serve_hicache_baseline.sh > $LOG_DIR/server.log 2>&1 &
SERVER_PID=$!

# 等就绪
for i in {1..120}; do
    if curl -s http://127.0.0.1:30000/v1/models > /dev/null 2>&1; then
        echo "server ready after ${i}s"
        break
    fi
    sleep 1
done

# 跑负载
python scripts/hicache/load_test_hicache.py --rounds 2 --warm 3 --prompt-tokens 7000 \
    --endpoint http://127.0.0.1:30000 > $LOG_DIR/load_test.log 2>&1

# 收尾
sync && echo 3 > /proc/sys/vm/drop_caches
kill $SERVER_PID
kill $IOSTAT_PID
sleep 5

# 收集 cache 文件信息
ls -la $CACHE_DIR | wc -l > $LOG_DIR/cache_file_count.txt
du -sh $CACHE_DIR > $LOG_DIR/cache_total_size.txt
```

**Step 3**: 跑 baseline round
```bash
bash scripts/hicache/run_one_round.sh baseline_biwin /dev/nvme1n1 \
    /home/ficus/llm/infer/ai_ssd_prestudy/cache_baseline
```

**Step 4**: 验证 L3 真的写了
```bash
ls -la ~/llm/infer/ai_ssd_prestudy/cache_baseline/ | head
du -sh ~/llm/infer/ai_ssd_prestudy/cache_baseline/
```
Expected: 看到 `.bin` 文件,总大小 ~ 30-100 MB (与 LMCache 0.95 GB cold 对比,HiCache 是 page 级,会更细)

#### Task 1.2: 调通 drop_caches + 真冷读

**Step 1**: 修改 `run_one_round.sh`,在 warm #1 之前插 `drop_caches`
```python
# load_test_hicache.py 增加 --drop-before-warm1 标志
# 触发: 在第 2 个请求前执行 (subprocess) "sync && sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches'"
```

**Step 2**: 验证 round 1 中 warm #1 的 iostat 出现真实 disk read IO (不为 0)

### Phase 2: 4 盘 × 1 轮 (半天)

#### Task 2.1: 4 盘串行 driver

**Files:**
- Create: `scripts/hicache/drive_4_rounds.sh`

**Step 1**: 复用 `scripts/drive_rounds.sh` 的结构,改成 HiCache
```bash
#!/bin/bash
# scripts/hicache/drive_4_rounds.sh
set -e

declare -A ROUNDS=(
    ["baseline_biwin_ext4"]="/dev/nvme1n1:/home/ficus/llm/infer/ai_ssd_prestudy/cache/baseline"
    ["ai_ssd0_wdc_ntfs"]="/dev/nvme0n1:/mnt/ai_ssd0/cache_hicache"
    ["ai_ssd1_zhitai_ntfs"]="/dev/nvme2n1:/mnt/ai_ssd1/cache_hicache"
    ["ai_ssd2_seagate_ntfs"]="/dev/nvme3n1:/mnt/ai_ssd2/cache_hicache"
)

for round in "${!ROUNDS[@]}"; do
    IFS=':' read -r dev cache_dir <<< "${ROUNDS[$round]}"
    echo "==== ROUND: $round on $dev ===="
    bash scripts/hicache/run_one_round.sh "$round" "$dev" "$cache_dir"
    echo "==== DONE: $round ===="
    sleep 30  # 盘冷却
done
```

**Step 2**: 跑全 4 轮,后台执行
```bash
cd ~/llm/infer/ai_ssd_prestudy
nohup bash scripts/hicache/drive_4_rounds.sh > /tmp/hicache_4rounds.log 2>&1 &
echo $! > /tmp/hicache_4rounds.pid
```

#### Task 2.2: 写 4 盘横向对比 report

**Files:**
- Create: `docs/hicache-4disk-headline-2026-06-XX.md`

模板 (复用 `kv-cache-4disk-K4-headline` 的结构):
```markdown
# SGLang HiCache 4 盘 KV Offload 横向对比

| 指标 | baseline (BIWIN ext4) | WDC NTFS | ZHITAI NTFS | Seagate NTFS |
|---|---|---|---|---|
| cold TTFT (s) | ... | ... | ... | ... |
| warm #1 TTFT (s, drop_caches 后) | ... | ... | ... | ... |
| 加速比 | ... | ... | ... | ... |
| L3 写带宽 (MB/s) | ... | ... | ... | ... |
| L3 读带宽 (MB/s, warm #1) | ... | ... | ... | ... |
| 写 await P50/P99 (ms) | ... | ... | ... | ... |
| 读 await P50/P99 (ms) | ... | ... | ... | ... |
| cache 文件数 | ... | ... | ... | ... |
| cache 平均文件大小 (MB) | ... | ... | ... | ... |
```

### Phase 3: 写策略对比 (半天,可选)

#### Task 3.1: write_back × 2 盘 (BIWIN + Seagate)

修改 `serve_hicache_baseline.sh` 加 `--hicache-write-policy` 参数,跑 2 个 sub-round:
```bash
# write_back 版
--hicache-write-policy write_back
```

**比较维度:**
- write_through 阻塞同步 vs write_back 异步延迟写
- 写带宽分布:write_back 突发 vs write_through 平稳

### Phase 4: 与 LMCache 交叉对比 (半天)

#### Task 4.1: 双引擎横向报告

**Files:**
- Create: `docs/hicache-vs-lmcache-cross-2026-06-XX.md`

**核心对比表:**
| 指标 | LMCache (BIWIN) | HiCache (BIWIN) | 差异来源 |
|---|---|---|---|
| 文件粒度 | 37.7 MB | ~9.4 MB | chunk_size vs page_size |
| cold TTFT | 0.785s | 待测 | LMCache 有 GPU prefix cache? |
| warm #1 TTFT (drop_caches) | 待 LMCache 重测 | 待测 | 都走真 disk reload |
| 写带宽峰值 | 977 MB/s (LMCache) / 6.7 GB/s (fio) | 待测 | 引擎调度 vs 盘能力 |
| IOPS 峰值 | 7921 | 待测 | 引擎 batch 大小 |

#### Task 4.2: 选型报告 v2

**Files:**
- Modify: `~/llm/storage/docs/kv-cache-final-selection-2026-06-10.md`
- 添加 §6 "SGLang HiCache 实测对照"

**关键结论形态:**
```
短突发 (<5min):
  - LMCache 数据: Biwin X570 胜 (3.14 GB/s)
  - HiCache 数据: 待填
  - 综合: 如果 HiCache 也 Biwin 胜 → 强化结论
        如果 HiCache Seagate 胜 → 说明引擎调度放大盘差

长稳态 (30min+):
  - LMCache: Seagate / Biwin 平手
  - HiCache: 待填
  - 综合: 引擎无关 → 推荐按价格/供应链
```

---

## 6. 成功标准

| 标准 | 测量方式 |
|---|---|
| 4 盘完整跑通 HiCache | `results/hicache/` 下 4 个子目录都有 iostat + load_test + cache 文件 |
| 与 LMCache 数据可比 | 同一模型同一 prompt,TTFT 差异在 ±20% 内 (引擎调度差异范围内) |
| IO 模式可解读 | 4 盘的 iostat + bpftrace 数据能讲清楚 SLC cache / GC 表现 |
| 双引擎交叉报告发布 | `docs/hicache-vs-lmcache-cross-2026-06-XX.md` 已 commit |
| AI SSD 选型 v2 已更新 | `~/llm/storage/docs/kv-cache-final-selection-*` 加入 HiCache 章节 |

---

## 7. 待用户决策的问题 (Open Questions)

在进入 Phase 1 之前,需要用户确认:

### Q1: SGLang 版本选择?

- **选项 A**: 最新 stable (推荐,可能有新功能) — `pip install "sglang[all]"`
- **选项 B**: 锁版本 (与某论文/对比基线一致) — 需指定版本号

**默认建议**: A (最新 stable)

### Q2: 模型选择?

- **选项 A**: Qwen3-4B (与 LMCache 完全一致,直接可比) — 推荐
- **选项 B**: 加跑 Qwen2.5-7B (验证"8B 模型吃满盘"的假设) — 加 1 轮时间

**默认建议**: A 为主,B 作为 Phase 5 (可选) 副测

### Q3: 写策略对比范围?

- **选项 A**: 只跑 `write_through` (与 LMCache 最接近,数据可比性最高)
- **选项 B**: 加跑 `write_back` × 2 盘 (验证策略影响,但数据不能与 LMCache 直接比)
- **选项 C**: A + B 全跑 (耗时 +50%)

**默认建议**: A 优先,B 作为补充分析 (Phase 3 可选)

### Q4: 报告输出位置?

- **选项 A**: 写到 `~/llm/infer/ai_ssd_prestudy/docs/` (与 LMCache 数据同目录)
- **选项 B**: 写到 `~/llm/storage/docs/` 与现有选型报告合并 (一处全览)

**默认建议**: A (新引擎独立目录),最终选型 v2 写到 B

### Q5: 时间投入预期?

| Phase | 工作量 | 产出 |
|---|---|---|
| 0 (环境) | 1-2 小时 | SGLang 装好,smoke 通过 |
| 1 (单盘冒烟) | 半天 | 1 盘完整 1 轮 |
| 2 (4 盘) | 半天 | 4 盘 headline 数据 |
| 3 (写策略) | 半天 (可选) | write_back 对照 |
| 4 (交叉对比) | 半天 | 双引擎选型 v2 |
| **总计** | **2-3 个工作日** | 完整双引擎 AI SSD 选型数据集 |

---

## 8. 决策记录 (Decision Log)

| 日期 | 决策 | 理由 |
|---|---|---|
| 2026-06-11 | 用 `file` 后端,不用 mooncake/hf3fs | mooncake 需 RDMA, hf3fs 需 K8s metadata server,本机 NVMe 直接 file 最简 |
| 2026-06-11 | 用 `page_size=64` (官方默认) | 与 LMCache chunk_size 256 对比,观察文件粒度影响 |
| 2026-06-11 | 用 `write_through` 主测 | 最接近 LMCache 的"每次 access 都写盘"语义 |
| 2026-06-11 | 用 `drop_caches` 强制冷读 | 让 warm #1 真实测 disk reload,与 LMCache 公平对比 |
| 2026-06-11 | 4B 模型,不复用 8B | 与 LMCache 完全一致,避免引入新变量 |

---

## 9. 关联文档

| 文档 | 位置 | 关系 |
|---|---|---|
| `LMCache 报告` | `~/llm/infer/ai_ssd_prestudy/REPORT.md` | 直接对比基线 |
| `kv-cache-final-selection` | `~/llm/storage/docs/kv-cache-final-selection-2026-06-10.md` | 最终目标:更新它 |
| `kv-cache-4disk-K4-headline` | `~/llm/storage/docs/kv-cache-4disk-K4-headline-2026-06-10.md` | 报告模板 |
| `io-pattern-analysis` | `~/llm/storage/docs/kv-cache-io-pattern-analysis-2026-06-10.md` | IO 分析方法复用 |
| `HiCache Design` | https://github.com/sgl-project/sglang/blob/main/docs/advanced_features/hicache_design.md | 架构参考 |
| `HiCache Best Practices` | https://github.com/sgl-project/sglang/blob/main/docs/advanced_features/hicache_best_practices.md | 配置参考 |
| `HiCache Runtime Attach/Detach` | https://github.com/sgl-project/sglang/blob/main/docs/advanced_features/hicache_storage_runtime_attach_detach.md | 切换 L3 backend |

---

## 10. 验收清单 (Definition of Done)

- [ ] Phase 0 完成,SGLang 0.4.x 装好,4B 模型冒烟通过
- [ ] Phase 1 完成,BIWIN 系统盘 baseline round 跑通,cache 目录有 `.bin` 文件
- [ ] Phase 2 完成,4 盘数据齐全,headline 报告写好
- [ ] (可选) Phase 3 完成,write_back × 2 盘对照跑完
- [ ] Phase 4 完成,双引擎交叉报告 + 选型 v2 已 commit + push
- [ ] 所有脚本在 `scripts/hicache/`,所有报告在 `docs/` 前缀 `hicache-`
- [ ] 所有 commit 通过 git push 备份
- [ ] memory 写入关键经验:HiCache vs LMCache IO 模式差异