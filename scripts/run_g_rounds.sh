#!/bin/bash
# scripts/run_g_rounds.sh
# 跑 G 任务多 run (Phase7 v3 multiprompt × N run)
#
# 用法: bash scripts/run_g_rounds.sh <start_run> <end_run>
#   默认: 1..5
#
# 串行跑,每次一个新 RUN_ID, 数据写到 hicache_multiprompt_g{N}/
# 完成后用 scripts/analyze_io_pattern.py 汇总

set -e

START=${1:-1}
END=${2:-5}

cd /home/ficus/llm/infer/ai_ssd_prestudy

for N in $(seq $START $END); do
    echo ""
    echo "=================================================="
    echo "#### RUN $N / $END  (start at $(date +%H:%M:%S))"
    echo "=================================================="
    RUN_ID=$N bash scripts/hicache_drive_4_rounds_model.sh qwen3_4b_multiprompt_run
    rc=$?
    if [ "$rc" != "0" ]; then
        echo "FATAL: RUN $N exited with rc=$rc"
        exit "$rc"
    fi
    echo ""
    echo "#### RUN $N DONE (end at $(date +%H:%M:%S))"
done

echo ""
echo "=================================================="
echo "#### ALL RUNS DONE: $START..$END"
echo "=================================================="
