#!/bin/bash
# scripts/run_p5_policy_matrix.sh
#
# P5: 3 write_policy × 4 盘 = 12 run 矩阵
#   POLICY_ID=1 → write_through
#   POLICY_ID=2 → write_back
#   POLICY_ID=3 → write_through_selective
#   4 盘串行 (BIWIN / WDC / Seagate / ZHITAI)
#
# 用法: bash scripts/run_p5_policy_matrix.sh
#   默认: 3 policy × 4 盘 = 12 run
#
# 数据: results/hicache_multiprompt_p5_p{1,2,3}_{wt,wb,wts}/{round}/
# 分析: scripts/analyze_metrics.py + scripts/analyze_io_pattern.py
#
# 时间估算: 4 盘 × ~3-4 min/run × 3 policy = ~45-60 min

set -e

cd /home/ficus/llm/infer/ai_ssd_prestudy

# 预创建 cache dirs (12 套 × 4 盘位置 = 48 目录, 但 cache_dir 复用 v3 命名)
# 直接让 hicache_drive_4_rounds_model.sh 创建
mkdir -p results 2>/dev/null || true

POLICIES=(1 2 3)
# 跑过的子目录会按 POLICY_ID 独立,不会互相覆盖
for POLICY_ID in "${POLICIES[@]}"; do
    echo ""
    echo "######################################################"
    echo "#### POLICY_ID=$POLICY_ID  (1=write_through 2=write_back 3=write_through_selective)"
    echo "####   start at $(date +%H:%M:%S)"
    echo "######################################################"

    # 强制清理残留 sglang 进程
    pkill -9 -f "sglang.launch_server" 2>/dev/null || true
    pkill -9 -f "sglang::" 2>/dev/null || true
    pkill -9 -f "iostat -dx" 2>/dev/null || true
    sleep 5

    POLICY_ID=$POLICY_ID bash scripts/hicache_drive_4_rounds_model.sh qwen3_4b_multiprompt_policy
    rc=$?
    if [ "$rc" != "0" ]; then
        echo "FATAL: POLICY_ID=$POLICY_ID exited with rc=$rc"
        exit "$rc"
    fi

    echo "#### POLICY_ID=$POLICY_ID DONE  (end at $(date +%H:%M:%S))"
    echo ""
done

echo ""
echo "######################################################"
echo "#### P5 ALL 12 RUNS DONE"
echo "####   end at $(date +%H:%M:%S)"
echo "######################################################"
echo ""
echo "Results:"
echo "  results/hicache_multiprompt_p5_p1_wt/{baseline_biwin_ext4,ai_ssd0_wdc_ntfs,ai_ssd1_seagate_ntfs,ai_ssd2_zhitai_ntfs}/"
echo "  results/hicache_multiprompt_p5_p2_wb/{baseline_biwin_ext4,ai_ssd0_wdc_ntfs,ai_ssd1_seagate_ntfs,ai_ssd2_zhitai_ntfs}/"
echo "  results/hicache_multiprompt_p5_p3_wts/{baseline_biwin_ext4,ai_ssd0_wdc_ntfs,ai_ssd1_seagate_ntfs,ai_ssd2_zhitai_ntfs}/"
