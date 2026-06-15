#!/bin/bash
# scripts/hicache_drive_4_rounds_32k_drop.sh
# 32K multiprompt + drop_caches 强制 cold-from-device 4 盘对比
# 设计: input_len=32768, N=20 prompts, L3 file 19.8 GB > page cache (~25 GB)
#       每 round 前 sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches' 清 page cache
#       L2 = host KV (sglang process heap) 不受 drop_caches 影响
#       L3 = page cache 文件 → drop_caches 后 L3 read 真从 NVMe
#
# 用法: hicache_drive_4_rounds_32k_drop.sh [out_dir_subdir]
#   out_dir_subdir 默认 hicache_32k_drop
# 环境变量:
#   DRIVE         - 测哪块盘 (默认循环 4 盘)
#   INPUT_LEN     - prompt 长度 (默认 32768)
#   NUM_PROMPTS   - 不同 prompt 数 (默认 20)
#   ROUNDS        - 跑几轮 (默认 4)
#   DROP_CACHES   - 是否每 round 前 drop_caches (默认 1)
#   OUT_DIR_SUBDIR - 结果子目录 (默认 hicache_32k_drop)
set -e

OUT_DIR_SUBDIR=${1:-hicache_32k_drop}
INPUT_LEN=${INPUT_LEN:-29000}
# Qwen3-4B chat template adds ~8 tokens overhead on top of input
# sglang CTX_LEN=35000 reserves ~5700 for chunked prefill + scheduling
# 29000 + 8 = 29008 < 29306 (sglang's effective max input)
NUM_PROMPTS=${NUM_PROMPTS:-20}
ROUNDS=${ROUNDS:-4}
DROP_CACHES=${DROP_CACHES:-1}

source ~/llm/.venv/bin/activate

CACHE_ROOT_BASE="/mnt"
# 4 盘映射 (跟 Phase7 一致, nvme1n1p3 是 BIWIN 系统盘)
if [ -n "$DRIVE" ]; then
    DRIVES_TO_TEST=("$DRIVE")
else
    DRIVES_TO_TEST=(ai_ssd0 ai_ssd1 ai_ssd2 ai_ssd1_root)
fi

RESULTS_BASE="$(pwd)/results/${OUT_DIR_SUBDIR}"
mkdir -p "$RESULTS_BASE"

for drive in "${DRIVES_TO_TEST[@]}"; do
    case $drive in
        ai_ssd0) NVME_DEV="/dev/nvme0n1p2"; DISK_NAME="WDC";;
        ai_ssd1) NVME_DEV="/dev/nvme2n1p2"; DISK_NAME="Seagate";;
        ai_ssd2) NVME_DEV="/dev/nvme3n1p3"; DISK_NAME="ZHITAI";;
        ai_ssd1_root) NVME_DEV="/dev/nvme1n1p3"; DISK_NAME="BIWIN";;
        *) echo "Unknown drive: $drive"; continue;;
    esac

    echo "==== Testing drive: $drive ($DISK_NAME, $NVME_DEV) ===="
    CACHE_DIR="/mnt/${drive}/cache_hicache_32k_drop"
    OUT_DIR="${RESULTS_BASE}/${drive}_${DISK_NAME,,}_drop"
    mkdir -p "$CACHE_DIR" "$OUT_DIR"
    rm -rf "$CACHE_DIR"/*

    # 重启 sglang
    pkill -f "sglang.launch_server" 2>/dev/null || true
    sleep 5
    CTX_LEN=50000 MEM_STATIC=0.9 nohup bash scripts/hicache_serve.sh "$CACHE_DIR" write_back > /tmp/sglang_${drive}.log 2>&1 &

    # 等 sglang 起来
    for i in {1..60}; do
        sleep 3
        if curl -sf http://127.0.0.1:30000/v1/models > /dev/null 2>&1; then
            echo "✓ sglang up on $CACHE_DIR (waited $((i*3))s)"
            break
        fi
    done
    if ! curl -sf http://127.0.0.1:30000/v1/models > /dev/null 2>&1; then
        echo "✗ sglang failed to start for $drive"
        tail -20 /tmp/sglang_${drive}.log
        continue
    fi

    # 跑 ROUNDS round
    for round in $(seq 1 $ROUNDS); do
        echo "--- $drive round $round/$ROUNDS ---"
        if [ "$DROP_CACHES" = "1" ] && [ "$round" -gt 1 ]; then
            sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches' && echo "  (drop_caches done)"
        fi

        # 跑 round (直接调 hicache_load_test.py)
        OUT_LOG="${OUT_DIR}/round_${round}.log"
        python scripts/hicache_load_test.py \
            --prompt-tokens $INPUT_LEN \
            --output-tokens 64 \
            --num-prompts $NUM_PROMPTS \
            --num-rounds 2 \
            --replay-prompt-id 0 \
            --log-file "$OUT_LOG" \
            2>&1 | tee -a "$OUT_LOG"

        # 记 iostat
        iostat -dxm 1 3 "$NVME_DEV" > "${OUT_DIR}/iostat_round_${round}.txt" 2>&1 || true
    done

    # 杀 sglang
    pkill -f "sglang.launch_server" 2>/dev/null || true
    sleep 5
done

echo "==== All drives done ===="
