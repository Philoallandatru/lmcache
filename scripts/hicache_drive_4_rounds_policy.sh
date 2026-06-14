#!/bin/bash
# scripts/hicache_drive_4_rounds_policy.sh
# 4 盘串行 driver, policy 可配置 (write_through / write_back)
# 用法: hicache_drive_4_rounds_policy.sh <policy> <results_subdir>
#
# 例:
#   bash scripts/hicache_drive_4_rounds_policy.sh write_through hicache
#   bash scripts/hicache_drive_4_rounds_policy.sh write_back hicache_writeback
#
# 设计: 与 hicache_drive_4_rounds.sh (legacy, hard-coded write_through) 完全独立,
#       通过 round name 加后缀避免目录撞车:
#         write_through -> baseline_biwin_ext4 / ai_ssd0_wdc_ntfs / ...
#         write_back    -> baseline_biwin_ext4_wb / ai_ssd0_wdc_ntfs_wb / ...

set -e

POLICY=${1:?policy required (write_through | write_back | write_through_selective)}
SUBDIR=${2:?results_subdir required (e.g. hicache_writeback)}
SUFFIX=${3:-}  # round name suffix (e.g. "_wb")

cd /home/ficus/llm/infer/ai_ssd_prestudy

# round_name : device : cache_dir
# 加 SUFFIX 避免与 write_through 数据共存时 verify 逻辑撞车
# v3 (mount-fixed): 盘映射修正 (nvme2n1=Seagate, nvme3n1=ZHITAI), v3 SUBDIR 隔离 v2 老数据
declare -a ROUNDS=(
    "baseline_biwin_ext4${SUFFIX}:nvme1n1:cache/baseline${SUFFIX}_v3"
    "ai_ssd0_wdc_ntfs${SUFFIX}:nvme0n1:/mnt/ai_ssd0/cache_hicache${SUFFIX}_v3"
    "ai_ssd1_seagate_ntfs${SUFFIX}:nvme2n1:/mnt/ai_ssd1/cache_hicache${SUFFIX}_v3"
    "ai_ssd2_zhitai_ntfs${SUFFIX}:nvme3n1:/mnt/ai_ssd2/cache_hicache${SUFFIX}_v3"
)

# 预创建所有 cache_dir (避免 bench_one_round.sh precheck fail)
mkdir -p "cache/baseline${SUFFIX}_v3" 2>/dev/null || true
for mount_dir in /mnt/ai_ssd0 /mnt/ai_ssd1 /mnt/ai_ssd2; do
    mkdir -p "$mount_dir/cache_hicache${SUFFIX}_v3" 2>/dev/null || echo "WARN: cannot create $mount_dir/cache_hicache${SUFFIX}_v3"
done

for entry in "${ROUNDS[@]}"; do
    IFS=':' read -r round_name dev cache_dir <<< "$entry"
    echo ""
    echo "########################################################"
    echo "#### ROUND: $round_name on $dev (policy=$POLICY)"
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
    # 通过 OUT_DIR 环境变量 (在 bench_one_round.sh 顶部支持)
    OUT_DIR_SUBDIR="$SUBDIR" bash scripts/hicache_bench_one_round.sh \
        "$round_name" "$dev" "$cache_dir" "$POLICY"

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
    if [ "$cold_ms" -lt 1400 ]; then
        echo "FATAL: cold TTFT=$cold_ttft s ($cold_ms ms) is too low, likely cached"
        exit 1
    fi
    echo "[verify] $round_name: cold TTFT=$cold_ms ms >= 1400 ms, OK"

    echo ""
    echo "==== DONE: $round_name ===="
    echo "Sleeping 30s to let disk settle..."
    sleep 30
done

echo ""
echo "########################################################"
echo "#### ALL 4 ROUNDS DONE (policy=$POLICY)"
echo "########################################################"
echo "Results: results/$SUBDIR/{baseline_biwin_ext4${SUFFIX},ai_ssd0_wdc_ntfs${SUFFIX},ai_ssd1_zhitai_ntfs${SUFFIX},ai_ssd2_seagate_ntfs${SUFFIX}}/"