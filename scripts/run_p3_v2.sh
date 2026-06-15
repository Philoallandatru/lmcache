#!/bin/bash
# scripts/run_p3_v2.sh
#
# P3 v2: ZHITAI drop_caches 对照 (修正版)
# 简化设计: 只跑 2 个 run (A1, A2) — 干净对比
#   A1: drop_caches 后 干净状态
#   A2: 累积 A1 (不 drop) 测 page cache / 盘 state 累积影响
#
# 重要: 需要盘上 ≥20GB 空间 (在跑前确认)
# 数据: results/hicache_multiprompt_p3v2_{A1,A2}/

set -e

cd /home/ficus/llm/infer/ai_ssd_prestudy

# 预检
free_gb=$(df -BG --output=avail /mnt/ai_ssd2 | tail -1 | tr -dc '0-9')
if [ "$free_gb" -lt 25 ]; then
    echo "FATAL: /mnt/ai_ssd2 只有 ${free_gb}GB, 跑前需要 ≥25GB"
    exit 1
fi

run_zhitai() {
    local subdir=$1
    local cache_dir=/mnt/ai_ssd2/cache_multiprompt_p3v2_${subdir}_v3
    local round_name=ai_ssd2_zhitai_ntfs
    local dev=nvme3n1
    local port=$((30080 + RANDOM % 20))  # 避免冲突
    local out_subdir="hicache_multiprompt_p3v2_${subdir}"
    mkdir -p "$cache_dir"

    echo ""
    echo "=================================================="
    echo "#### P3v2 RUN: $subdir → $out_subdir (port=$port) $(date +%H:%M:%S)"
    echo "=================================================="

    OUT_DIR_SUBDIR="$out_subdir" \
    PORT=$port \
    NUM_PROMPTS=20 \
    REPLAY_PROMPT_ID=0 \
    bash scripts/hicache_bench_one_round.sh \
        "$round_name" "$dev" "$cache_dir" "write_through"
    rc=$?
    if [ "$rc" != "0" ]; then
        echo "FATAL: P3v2 $subdir exited with rc=$rc"
        exit "$rc"
    fi
    echo "#### P3v2 $subdir DONE $(date +%H:%M:%S)"
}

drop_caches() {
    echo ""
    echo "#### sync + drop_caches $(date +%H:%M:%S) ####"
    sync
    sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>&1 || echo "WARN: drop_caches failed"
    sleep 2
    free -m | head -2
}

# 起始: 干净
drop_caches

# A1: 干净
run_zhitai A1

# A2: 累积 A1 (不 drop)
run_zhitai A2

echo ""
echo "=================================================="
echo "#### P3v2 ALL DONE: A1 A2 (2 ZHITAI runs) $(date +%H:%M:%S)"
echo "=================================================="
