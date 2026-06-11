#!/bin/bash
# scripts/hicache_drive_4_rounds.sh
# 4 盘串行 driver (与 LMCache 实验同序)
# 用法: bash scripts/hicache_drive_4_rounds.sh

set -e

cd /home/ficus/llm/infer/ai_ssd_prestudy

# round_name : device : cache_dir
declare -a ROUNDS=(
    "baseline_biwin_ext4:nvme1n1:cache/baseline"
    "ai_ssd0_wdc_ntfs:nvme0n1:/mnt/ai_ssd0/cache_hicache"
    "ai_ssd1_zhitai_ntfs:nvme2n1:/mnt/ai_ssd1/cache_hicache"
    "ai_ssd2_seagate_ntfs:nvme3n1:/mnt/ai_ssd2/cache_hicache"
)

for entry in "${ROUNDS[@]}"; do
    IFS=':' read -r round_name dev cache_dir <<< "$entry"
    echo ""
    echo "########################################################"
    echo "#### ROUND: $round_name on $dev"
    echo "########################################################"

    # 强制清理前一轮残留 (兜底)
    pkill -9 -f "sglang.launch_server" 2>/dev/null || true
    pkill -9 -f "iostat -dx" 2>/dev/null || true
    pkill -9 -f "hicache_load_test" 2>/dev/null || true
    sleep 5

    # 检查 cache dir 可访问
    if [ ! -d "$(dirname "$cache_dir")" ]; then
        echo "FATAL: parent of cache_dir not mounted: $cache_dir"
        echo "  expected mount at: $(dirname "$cache_dir")"
        echo "  skip this round"
        continue
    fi

    bash scripts/hicache_bench_one_round.sh "$round_name" "$dev" "$cache_dir" write_through

    echo ""
    echo "==== DONE: $round_name ===="
    echo "Sleeping 30s to let disk settle..."
    sleep 30
done

echo ""
echo "########################################################"
echo "#### ALL 4 ROUNDS DONE"
echo "########################################################"
echo "Results: results/hicache/{baseline_biwin_ext4,ai_ssd0_wdc_ntfs,ai_ssd1_zhitai_ntfs,ai_ssd2_seagate_ntfs}/"