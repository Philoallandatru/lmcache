#!/bin/bash
# scripts/hicache_drive_4_rounds_model.sh
# 4 盘串行 driver, model 可配置 (4B / 14B-AWQ)
# 用法: hicache_drive_4_rounds_model.sh <model_key>
#
# 例:
#   bash scripts/hicache_drive_4_rounds_model.sh qwen3_4b          # 4B Phase2 重跑
#   bash scripts/hicache_drive_4_rounds_model.sh qwen3_14b_awq     # 14B AWQ Phase4
#
# 设计: 与 hicache_drive_4_rounds_policy.sh (policy driver) 完全独立,
#       通过 OUT_DIR_SUBDIR 把不同 model 的数据放到不同子目录:
#         qwen3_4b      -> results/hicache/{round}/
#         qwen3_14b_awq -> results/hicache_14b_awq/{round}/
#       cache_dir 也加 model 后缀, 避免不同 model 互相污染 L3 文件:
#         qwen3_4b      -> /mnt/ai_ssd0/cache_hicache/
#         qwen3_14b_awq -> /mnt/ai_ssd0/cache_14b_awq/
#
# 关键:每个 model key 显式设 MODEL_PATH / TP_SIZE / CTX_LEN / MEM_STATIC / WATCHDOG_TIMEOUT / PORT
#      这些 env vars 被 hicache_serve.sh 和 hicache_bench_one_round.sh 读取

set -e

MODEL_KEY=${1:?"model_key required (qwen3_4b | qwen3_14b_awq)"}

cd /home/ficus/llm/infer/ai_ssd_prestudy

# Model preset registry
# 模型: 路径 | TP | ctx_len | mem_static | watchdog_timeout | port | OUT_DIR_SUBDIR | cache_subdir
case "$MODEL_KEY" in
    qwen3_4b)
        export MODEL_PATH=/home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507
        export TP_SIZE=1
        export CTX_LEN=8192
        export MEM_STATIC=0.7
        export WATCHDOG_TIMEOUT=""
        export PORT=30000
        SUBDIR=hicache
        CACHE_SUBDIR=cache_hicache
        ;;
    qwen3_4b_multiclient)
        # Phase5: 4B + 4 client 并发 + drop_caches every round
        # 数据写到独立子目录避免污染 Phase2 hicache/
        # 依赖 env vars: CONCURRENT_CLIENTS=4 DROP_EVERY_ROUND=1
        export MODEL_PATH=/home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507
        export TP_SIZE=1
        export CTX_LEN=8192
        export MEM_STATIC=0.7
        export WATCHDOG_TIMEOUT=1800
        export PORT=30002
        SUBDIR=hicache_multiclient
        CACHE_SUBDIR=cache_multiclient
        ;;
    qwen3_14b_awq)
        export MODEL_PATH=/home/ficus/llm/models/Qwen/Qwen3-14B-AWQ
        export TP_SIZE=2
        export CTX_LEN=12288
        export MEM_STATIC=0.85
        export WATCHDOG_TIMEOUT=1800
        export PORT=30001
        SUBDIR=hicache_14b_awq
        CACHE_SUBDIR=cache_14b_awq
        ;;
    *)
        echo "FATAL: unknown model_key '$MODEL_KEY'"
        echo "  supported: qwen3_4b | qwen3_4b_multiclient | qwen3_14b_awq"
        exit 1
        ;;
esac

echo "########################################################"
echo "#### MODEL DRIVER: $MODEL_KEY"
echo "####   MODEL_PATH=$MODEL_PATH"
echo "####   TP_SIZE=$TP_SIZE  CTX_LEN=$CTX_LEN  MEM_STATIC=$MEM_STATIC"
echo "####   PORT=$PORT  WATCHDOG=$WATCHDOG_TIMEOUT"
echo "####   OUT_DIR_SUBDIR=$SUBDIR  CACHE_SUBDIR=$CACHE_SUBDIR"
echo "########################################################"

# round_name : device : cache_dir
# CACHE_SUBDIR 跟 4B 数据隔离(避免 14B L3 写到 4B 测试用的 cache_hicache 目录)
declare -a ROUNDS=(
    "baseline_biwin_ext4:nvme1n1:cache/${CACHE_SUBDIR}"
    "ai_ssd0_wdc_ntfs:nvme0n1:/mnt/ai_ssd0/${CACHE_SUBDIR}"
    "ai_ssd1_zhitai_ntfs:nvme2n1:/mnt/ai_ssd1/${CACHE_SUBDIR}"
    "ai_ssd2_seagate_ntfs:nvme3n1:/mnt/ai_ssd2/${CACHE_SUBDIR}"
)

for entry in "${ROUNDS[@]}"; do
    IFS=':' read -r round_name dev cache_dir <<< "$entry"
    echo ""
    echo "########################################################"
    echo "#### ROUND: $round_name on $dev (model=$MODEL_KEY)"
    echo "########################################################"

    # 强制清理前一轮残留 (兜底)
    pkill -9 -f "sglang.launch_server" 2>/dev/null || true
    pkill -9 -f "sglang::" 2>/dev/null || true
    pkill -9 -f "iostat -dx" 2>/dev/null || true
    pkill -9 -f "hicache_load_test" 2>/dev/null || true
    sleep 5

    # 检查 cache dir 可访问, 不存在则创建
    if [ ! -d "$(dirname "$cache_dir")" ]; then
        echo "FATAL: parent of cache_dir not mounted: $cache_dir"
        echo "  expected mount at: $(dirname "$cache_dir")"
        echo "  skip this round"
        continue
    fi
    if [ ! -d "$cache_dir" ]; then
        echo "[create] cache_dir $cache_dir does not exist, creating"
        mkdir -p "$cache_dir"
    fi

    # 复用已有 bench_one_round.sh, 但把结果写到 SUBDIR 目录
    # 通过 OUT_DIR_SUBDIR + 透传所有 model env vars
    OUT_DIR_SUBDIR="$SUBDIR" \
    bash scripts/hicache_bench_one_round.sh \
        "$round_name" "$dev" "$cache_dir" "write_through"

    rc=$?
    if [ "$rc" != "0" ]; then
        echo "FATAL: round $round_name exited with rc=$rc, aborting"
        exit "$rc"
    fi

    # 验证本轮数据完整性 (jsonl 在 SUBDIR 子目录下)
    jsonl="results/$SUBDIR/$round_name/load_test.jsonl"
    if [ ! -f "$jsonl" ]; then
        echo "FATAL: $jsonl missing"
        exit 1
    fi
    nlines=$(wc -l < "$jsonl")
    if [ "$nlines" -lt 6 ]; then
        echo "FATAL: $jsonl has only $nlines lines (expected >= 6)"
        exit 1
    fi
    cold_ttft=$(jq -r 'select(.label=="cold") | .latency_s' "$jsonl" 2>/dev/null | head -1)
    echo "[verify] $round_name: cold TTFT=$cold_ttft  lines=$nlines"
    cold_ms=$(awk -v t="$cold_ttft" 'BEGIN { printf "%d", t*1000 }')
    if [ -z "$cold_ttft" ] || [ "$cold_ttft" = "null" ]; then
        echo "FATAL: could not parse cold TTFT from $jsonl"
        head -2 "$jsonl"
        exit 1
    fi
    # 14B-AWQ cold TTFT 期望 ~4.6s (vs 4B ~1.4s), 阈值设 2500ms
    # 4B cold 期望 ~1.4s, 阈值 1400ms
    # 4B multiclient cold N=4 期望 ~1.7s (BIWIN ext4), 阈值 1600ms (NTFS 期望 >2s)
    case "$MODEL_KEY" in
        qwen3_4b)              MIN_COLD_MS=1400 ;;
        qwen3_4b_multiclient)  MIN_COLD_MS=1600 ;;
        qwen3_14b_awq)         MIN_COLD_MS=2500 ;;
    esac
    if [ "$cold_ms" -lt "$MIN_COLD_MS" ]; then
        echo "FATAL: cold TTFT=$cold_ttft s ($cold_ms ms) < ${MIN_COLD_MS}ms (model=$MODEL_KEY), likely cached"
        exit 1
    fi
    echo "[verify] $round_name: cold TTFT=$cold_ms ms >= ${MIN_COLD_MS}ms, OK"

    echo ""
    echo "==== DONE: $round_name ===="
    echo "Sleeping 30s to let disk settle..."
    sleep 30
done

echo ""
echo "########################################################"
echo "#### ALL 4 ROUNDS DONE (model=$MODEL_KEY)"
echo "########################################################"
echo "Results: results/$SUBDIR/{baseline_biwin_ext4,ai_ssd0_wdc_ntfs,ai_ssd1_zhitai_ntfs,ai_ssd2_seagate_ntfs}/"