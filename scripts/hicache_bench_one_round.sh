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

# 模型 + 部署 env vars (跟 serve.sh 同步, 都可选, 默认 4B Phase2 配)
MODEL_PATH=${MODEL_PATH:-/home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507}
TP_SIZE=${TP_SIZE:-1}
PORT=${PORT:-30000}
CTX_LEN=${CTX_LEN:-8192}
MEM_STATIC=${MEM_STATIC:-0.7}
WATCHDOG_TIMEOUT=${WATCHDOG_TIMEOUT:-}
# 负载模式 (Phase5 多并发测):
#   CONCURRENT_CLIENTS=1 + DROP_EVERY_ROUND=0 → 1 client 串行, warm_1 前 drop (默认)
#   CONCURRENT_CLIENTS=4 + DROP_EVERY_ROUND=1 → 4 client 并发, 每轮前 drop, 真 L3 读盘
CONCURRENT_CLIENTS=${CONCURRENT_CLIENTS:-1}
DROP_EVERY_ROUND=${DROP_EVERY_ROUND:-0}
# Phase6 大 prompt 测: PROMPT_TOKENS=30000 让 L2 装不下
PROMPT_TOKENS=${PROMPT_TOKENS:-7000}

cd /home/ficus/llm/infer/ai_ssd_prestudy
# OUT_DIR_SUBDIR 允许 driver 把不同 policy / model 的数据放到不同子目录
# 默认 hicache, write_back 时 driver 会传 "hicache_writeback"
# 不同 model (如 14B-AWQ) driver 会传 "hicache_14b_awq"
OUT_DIR_SUBDIR=${OUT_DIR_SUBDIR:-hicache}
OUT="results/${OUT_DIR_SUBDIR}/$ROUND"
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

# 0c. 安全检查: 确保 :$PORT 没被旧 server 占用
if curl -s --max-time 2 http://127.0.0.1:$PORT/v1/models > /dev/null 2>&1; then
    echo "FATAL: port $PORT already has a server. Kill it first:"
    pgrep -af "sglang.launch_server" | grep -v bash | head -3
    exit 1
fi

# 1. 启动 iostat 后台
bash scripts/hicache_io_monitor.sh "$DEV" "$OUT" 1 &
IOSTAT_PID=$!
echo "iostat pid: $IOSTAT_PID"
sleep 1

# 2. 启动 sglang server (透传 model + 部署 env vars)
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
    # 3. 强杀端口 $PORT 上的 listener (fuser 不一定有, 用 lsof 兜底)
    fuser -k $PORT/tcp 2>/dev/null || true
    sleep 2
    # 4. 验证端口真释放
    for i in {1..10}; do
        if ! curl -s --max-time 1 http://127.0.0.1:$PORT/v1/models > /dev/null 2>&1; then
            echo "[cleanup] port $PORT free after ${i}*1s"
            return 0
        fi
        sleep 1
    done
    echo "[cleanup] WARNING: port $PORT still occupied after 10s"
    pgrep -af "sglang" | head -5
}
trap cleanup EXIT TERM INT ERR

# 3. 等 server 就绪 (最长 180s)
READY=0
for i in {1..90}; do
    if curl -s --max-time 2 http://127.0.0.1:$PORT/v1/models > /dev/null 2>&1; then
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
curl -s http://127.0.0.1:$PORT/metrics > "$OUT/metrics_before.json" 2>/dev/null || true

# 5. 跑自定义 hicache_load_test.py (OpenAI client)
#    按 README L19: 1 client, 6 rounds = 1 cold + 5 warm (per 同 prompt)
#    prompt=7000 tokens (大 prefix, 逼出真 L3 offload, 与 LMCache REPORT 7000 对齐)
#    output=64 tokens (让 prefill 时间占比高, 加速比明显)
#    NOTE: --model-path 必须跟 server 启动的 model 一致(否则 tokenize 失败)
#    Phase5 多并发模式 (env: CONCURRENT_CLIENTS=4 DROP_EVERY_ROUND=1):
#      强制每轮前 drop_caches + 4 client 同时发, 暴露 N 路 L3 真读盘延迟
echo "--- hicache_load_test.py start (clients=$CONCURRENT_CLIENTS drop_every=$DROP_EVERY_ROUND prompt=$PROMPT_TOKENS) ---"
LOAD_TEST_FLAGS="--num-rounds 6 --prompt-tokens $PROMPT_TOKENS --output-tokens 64 --request-rate 1.0"
if [ "$DROP_EVERY_ROUND" = "1" ]; then
    LOAD_TEST_FLAGS="$LOAD_TEST_FLAGS --drop-caches-every-round"
else
    LOAD_TEST_FLAGS="$LOAD_TEST_FLAGS --drop-caches-before-warm1"
fi
if [ "$CONCURRENT_CLIENTS" -gt 1 ]; then
    LOAD_TEST_FLAGS="$LOAD_TEST_FLAGS --concurrent-clients $CONCURRENT_CLIENTS"
fi
python scripts/hicache_load_test.py \
    --endpoint http://127.0.0.1:$PORT/v1/chat/completions \
    --model-path "$MODEL_PATH" \
    $LOAD_TEST_FLAGS \
    --log-file "/home/ficus/llm/infer/ai_ssd_prestudy/$OUT/load_test.jsonl" \
    2>&1 | tee "/home/ficus/llm/infer/ai_ssd_prestudy/$OUT/load_test.log"
echo "--- hicache_load_test.py done ---"

# 6. /metrics after
curl -s http://127.0.0.1:$PORT/metrics > "$OUT/metrics_after.json" 2>/dev/null || true

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