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
    rc=$?
    if [ "$rc" != "0" ]; then
        echo "FATAL: round $round_name exited with rc=$rc, aborting"
        exit "$rc"
    fi

    # 验证本轮数据完整性: load_test.jsonl 应该有 6 行 + cold TTFT > 1.4s
    if [ ! -f "results/hicache/$round_name/load_test.jsonl" ]; then
        echo "FATAL: results/hicache/$round_name/load_test.jsonl missing"
        exit 1
    fi
    nlines=$(wc -l < "results/hicache/$round_name/load_test.jsonl")
    if [ "$nlines" -lt 6 ]; then
        echo "FATAL: load_test.jsonl has only $nlines lines (expected >= 6)"
        exit 1
    fi
    # cold TTFT 应 > 1.4s (4 盘 7000-token 模型 cold 都 ~1.43-1.44s)
    # jsonl 字段是 label (cold/warm_1/...), 不是 phase
    cold_ttft=$(jq -r 'select(.label=="cold") | .latency_s' "results/hicache/$round_name/load_test.jsonl" 2>/dev/null | head -1)
    echo "[verify] $round_name: cold TTFT=$cold_ttft  lines=$nlines"
    cold_ms=$(awk -v t="$cold_ttft" 'BEGIN { printf "%d", t*1000 }')
    if [ -z "$cold_ttft" ] || [ "$cold_ttft" = "null" ]; then
        echo "FATAL: could not parse cold TTFT from load_test.jsonl"
        head -2 "results/hicache/$round_name/load_test.jsonl"
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
echo "#### ALL 4 ROUNDS DONE"
echo "########################################################"
echo "Results: results/hicache/{baseline_biwin_ext4,ai_ssd0_wdc_ntfs,ai_ssd1_zhitai_ntfs,ai_ssd2_seagate_ntfs}/"