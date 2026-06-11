#!/bin/bash
# scripts/hicache_bench_one_round.sh
# 用法: hicache_bench_one_round.sh <round_name> <device> <cache_dir> [write_policy]
#
# 一轮测试流程:
#   1. 启动 iostat 后台监测
#   2. 启动 sglang HiCache server (指向 cache_dir)
#   3. 等 server 就绪
#   4. 抓 /metrics baseline
#   5. 跑官方 bench_multiturn.py (1 client × 6 rounds = 1 cold + 5 warm)
#   6. 抓 /metrics after
#   7. drop_caches + 补一发 warm, 验证 disk read
#   8. 收集 L3 文件清单
#   9. 优雅关闭

set -e

ROUND=${1:?"round_name required (e.g. baseline_biwin_ext4)"}
DEV=${2:?"device required (e.g. nvme1n1)"}
CACHE_DIR=${3:?"cache_dir required"}
WRITE_POLICY=${4:-write_through}

cd /home/ficus/llm/infer/ai_ssd_prestudy
OUT=results/hicache/$ROUND
mkdir -p "$OUT"

source ~/llm/.venv/bin/activate

echo "==== ROUND START: $ROUND on $DEV (policy=$WRITE_POLICY) ===="

# 0a. 磁盘存在性检查
if [ ! -e "/sys/block/$DEV" ]; then
    echo "FATAL: device /sys/block/$DEV does not exist"
    ls /sys/block/ | grep nvme || true
    exit 1
fi
echo "[precheck] /sys/block/$DEV exists, model=$(cat /sys/block/$DEV/device/model 2>/dev/null | tr -d '\n' || echo 'unknown')"

# 0b. 缓存目录检查
if [ ! -d "$CACHE_DIR" ]; then
    echo "FATAL: cache_dir $CACHE_DIR does not exist"
    exit 1
fi
echo "[precheck] cache_dir=$CACHE_DIR mount=$(df --output=source,target $CACHE_DIR | tail -1)"

# 0c. 安全检查: 确保 :30000 没被旧 server 占用
if curl -s --max-time 2 http://127.0.0.1:30000/v1/models > /dev/null 2>&1; then
    echo "FATAL: port 30000 already has a server. Kill it first:"
    pgrep -af "sglang.launch_server" | grep -v bash | head -3
    exit 1
fi

# 1. 启动 iostat 后台
bash scripts/hicache_io_monitor.sh "$DEV" "$OUT" 1 &
IOSTAT_PID=$!
echo "iostat pid: $IOSTAT_PID"
sleep 1

# 2. 启动 sglang server
nohup bash scripts/hicache_serve.sh "$CACHE_DIR" "$WRITE_POLICY" > "$OUT/server.log" 2>&1 &
SERVER_PID=$!
echo "server pid: $SERVER_PID"

# cleanup hook — 任何失败时收尾
# 关键: sglang 启的 python -m sglang.launch_server 会 fork 出 scheduler/detokenizer
#       这些是 python 的子进程, 不是 bash 的子进程,
#       所以 pkill -P $SERVER_PID 找不到 → 必须用 pkill -f 全杀
cleanup() {
    echo "[cleanup] killing all sglang + iostat (sglang forks out of bash process tree)"
    # 1. 杀整个进程组 (nohup + 子进程)
    if [ -n "$SERVER_PID" ]; then
        kill -TERM -"$SERVER_PID" 2>/dev/null || true
        kill -TERM "$SERVER_PID" 2>/dev/null || true
    fi
    if [ -n "$IOSTAT_PID" ]; then
        kill -TERM "$IOSTAT_PID" 2>/dev/null || true
    fi
    sleep 3
    # 2. 兜底: 按命令行特征杀 — sglang 的所有 worker 都会被命中
    pkill -9 -f "sglang.launch_server" 2>/dev/null || true
    pkill -9 -f "sglang::scheduler" 2>/dev/null || true
    pkill -9 -f "sglang::detokenizer" 2>/dev/null || true
    pkill -9 -f "iostat -dx -m" 2>/dev/null || true
    # 3. 强杀端口 30000 上的 listener (fuser 不一定有, 用 lsof 兜底)
    fuser -k 30000/tcp 2>/dev/null || true
    sleep 2
    # 4. 验证端口真释放
    for i in {1..10}; do
        if ! curl -s --max-time 1 http://127.0.0.1:30000/v1/models > /dev/null 2>&1; then
            echo "[cleanup] port 30000 free after ${i}*1s"
            return 0
        fi
        sleep 1
    done
    echo "[cleanup] WARNING: port 30000 still occupied after 10s"
    pgrep -af "sglang" | head -5
}
trap cleanup EXIT TERM INT ERR

# 3. 等 server 就绪 (最长 180s)
READY=0
for i in {1..90}; do
    if curl -s --max-time 2 http://127.0.0.1:30000/v1/models > /dev/null 2>&1; then
        echo "server ready after ${i}*2s"
        READY=1
        break
    fi
    sleep 2
done
if [ "$READY" != "1" ]; then
    echo "FATAL: server failed to start within 180s"
    echo "--- server.log tail ---"
    tail -30 "$OUT/server.log"
    exit 1
fi
sleep 5  # 让 HiCache 内部线程完全初始化

# 4. /metrics baseline
echo "--- /metrics baseline ---"
curl -s http://127.0.0.1:30000/metrics > "$OUT/metrics_before.json" 2>/dev/null || true

# 5. 跑自定义 hicache_load_test.py (OpenAI client)
#    按 README L19: 1 client, 6 rounds = 1 cold + 5 warm (per 同 prompt)
#    prompt=7000 tokens (大 prefix, 逼出真 L3 offload, 与 LMCache REPORT 7000 对齐)
#    output=64 tokens (让 prefill 时间占比高, 加速比明显)
echo "--- hicache_load_test.py start ---"
python scripts/hicache_load_test.py \
    --endpoint http://127.0.0.1:30000/v1/chat/completions \
    --model-path /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --num-rounds 6 \
    --prompt-tokens 7000 \
    --output-tokens 64 \
    --request-rate 1.0 \
    --drop-caches-before-warm1 \
    --log-file "/home/ficus/llm/infer/ai_ssd_prestudy/$OUT/load_test.jsonl" \
    2>&1 | tee "/home/ficus/llm/infer/ai_ssd_prestudy/$OUT/load_test.log"
echo "--- hicache_load_test.py done ---"

# 6. /metrics after
curl -s http://127.0.0.1:30000/metrics > "$OUT/metrics_after.json" 2>/dev/null || true

# 7. drop_caches + 补一发 warm, 验证 disk read
echo "--- drop_caches + extra warm ---"
sync
sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>&1 || echo "drop_caches failed (no sudo or no perm)"

# 8. 收集 L3 文件清单
echo "--- L3 cache file list ---"
# 用 stat 而不是 ls -la (NTFS 上 ls -la 格式不输出 size 数值列)
python3 -c "
import os, sys
cache_dir = '$CACHE_DIR'
files = sorted(os.listdir(cache_dir))
total = 0
with open('$OUT/cache_file_list.txt', 'w') as f:
    for fname in files:
        p = os.path.join(cache_dir, fname)
        if os.path.isfile(p):
            sz = os.path.getsize(p)
            f.write(f'{sz} {fname}\n')
            total += sz
print(f'L3 file count: {len(files)}')
print(f'L3 total size: {total/1048576:.2f} MB')
"

# 9. 关 server + iostat
echo "--- cleanup ---"
# 显式调用 cleanup (不只靠 trap, 因为 tee 在 pipe 末端可能干扰 trap 触发)
cleanup

echo "==== ROUND DONE: $ROUND ===="
echo "iostat log:  $OUT/iostat_$DEV.log"
echo "server log:  $OUT/server.log"
echo "load test:   $OUT/load_test.jsonl"
echo "metrics:     $OUT/metrics_after.json"
echo "cache list:  $OUT/cache_file_list.txt"