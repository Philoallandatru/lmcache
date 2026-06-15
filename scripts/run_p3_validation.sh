#!/bin/bash
# scripts/run_p3_validation.sh
#
# P3 验证: ZHITAI 跨 run 持续变快 (2.545→2.058s, 19%↓) 根因
#
# 关键观察: v3 / g1 / g2 / g3 / g4 / g5 ZHITAI replay:
#   2.545, 2.385, 2.280, 2.212, 2.151, 2.058
# 单调下降 19%。每个 run 写不同 L3 file (不同 prompt 序列),
# 所以 page cache 不能跨 run 复用 (新文件不命中旧 cache)。
# 但 ZHITAI 盘本身的 metadata / FS state / disk controller cache / sglang runtime
# 都可能跨 run 累积或 warmup。
#
# 设计: 跑 4 个 ZHITAI run, 测 3 种状态:
#   A1 = clean (刚 drop_caches, 跟 v3 等价)
#   A2 = follow A1 (不 drop, 模拟 g1 的"累积"效应)
#   A3 = follow A2 (不 drop, 模拟 g2 的"累积"效应)
#   B1 = drop_caches (清掉 A1-A3 的 page cache, 跟 A1 对比)
#
# 如果 A2 < A1, B1 ≈ A1 → page cache 累积假设成立
# 如果 A2 ≈ A1, B1 ≈ A1 → filesystem/disk 内部 warmup 假设成立
# 如果 A2 < A1, B1 < A1 → 两者都贡献
#
# 用法: bash scripts/run_p3_validation.sh
# 数据: results/hicache_multiprompt_p3_{A1,A2,A3,B1}/

set -e

cd /home/ficus/llm/infer/ai_ssd_prestudy

run_zhitai() {
    local subdir=$1
    local cache_dir=/mnt/ai_ssd2/cache_multiprompt_p3_${subdir}_v3
    local round_name=ai_ssd2_zhitai_ntfs
    local dev=nvme3n1
    local port=$((30030 + RANDOM % 50))  # 30030-30079
    local out_subdir="hicache_multiprompt_p3_${subdir}"
    mkdir -p "$cache_dir"

    echo ""
    echo "=================================================="
    echo "#### P3 RUN: $subdir → $out_subdir (port=$port) $(date +%H:%M:%S)"
    echo "=================================================="

    OUT_DIR_SUBDIR="$out_subdir" \
    PORT=$port \
    NUM_PROMPTS=20 \
    REPLAY_PROMPT_ID=0 \
    bash scripts/hicache_bench_one_round.sh \
        "$round_name" "$dev" "$cache_dir" "write_through"
    rc=$?
    if [ "$rc" != "0" ]; then
        echo "FATAL: P3 $subdir exited with rc=$rc"
        exit "$rc"
    fi
    echo "#### P3 $subdir DONE $(date +%H:%M:%S)"
}

drop_caches() {
    echo ""
    echo "#### sync + drop_caches $(date +%H:%M:%S) ####"
    sync
    sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>&1 || echo "WARN: drop_caches failed"
    sleep 2
    free -m | head -2
}

# 起始: 全清 page cache (跟 v3 跑前状态对齐)
drop_caches

# Phase A1: 干净状态
run_zhitai A1

# Phase A2: 不 drop, 跟随 A1 (测 page cache / 盘内 state 累积)
run_zhitai A2

# Phase A3: 不 drop, 跟随 A2 (看趋势是否跟 g1-g5 一致)
run_zhitai A3

# Phase B1: drop 后 (清掉 A1-A3 的累积), 看是否回到 A1 baseline
drop_caches
run_zhitai B1

echo ""
echo "=================================================="
echo "#### P3 ALL DONE: A1 A2 A3 B1 (4 ZHITAI runs) $(date +%H:%M:%S)"
echo "=================================================="
