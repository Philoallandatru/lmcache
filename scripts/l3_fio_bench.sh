#!/bin/bash
# scripts/l3_fio_bench.sh
# 用 fio 直接测 4 盘 L3 file 读性能,绕开 sglang HiCache 行为
#
# 目的:展示"如果 sglang 真从 L3 读盘,4 盘差距多少"——为产品选型提供参考
# 测试模式:
#   1) Single file sequential read (1×9MB,模拟单 page reload)
#   2) 4K random read (模拟 kernel 实际 page cache 行为)
#   3) Concurrent 4 thread sequential read (模拟多并发 L3 reload)
#
# 用法: bash scripts/l3_fio_bench.sh

set -e

cd /home/ficus/llm/infer/ai_ssd_prestudy

# 4 盘的 L3 目录(每个盘都有同样的 9MB×N files)
declare -A DISKS=(
    ["BIWIN_ext4"]="/home/ficus/llm/infer/ai_ssd_prestudy/cache/hicache_fio"
    ["WDC_NTFS"]="/mnt/ai_ssd0/cache_hicache"
    ["ZHITAI_NTFS"]="/mnt/ai_ssd1/cache_hicache"
    ["Seagate_NTFS"]="/mnt/ai_ssd2/cache_hicache_fio"
)

# 输出目录
mkdir -p results/l3_fio

echo "============================================================"
echo "L3 File Read Benchmark - 4 Disks"
echo "  Goal: Measure raw disk read speed on L3 KV cache files"
echo "  Mode: 9MB page files (Qwen3-4B L3 page size)"
echo "============================================================"

for label in "${!DISKS[@]}"; do
    dir="${DISKS[$label]}"
    if [ ! -d "$dir" ]; then
        echo "FATAL: $dir missing, skip $label"
        continue
    fi
    # 取第一个文件作为 single-file test target
    first_file=$(ls "$dir" | head -1)
    if [ -z "$first_file" ]; then
        echo "FATAL: $dir empty, skip $label"
        continue
    fi
    target="$dir/$first_file"
    file_size_mb=$(($(stat -c%s "$target") / 1048576))
    echo ""
    echo "########################################################"
    echo "#### Disk: $label"
    echo "#### Dir: $dir"
    echo "#### First file: $first_file (${file_size_mb} MB)"
    echo "########################################################"

    out_base="results/l3_fio/${label}"

    # ========= Test 1: Single file sequential read (1 thread) =========
    # 模拟 sglang HiCache 从 L3 读一个 9MB page
    echo ""
    echo "--- Test 1: Single file sequential read (1 thread, 1×9MB) ---"
    fio --name=seq1t \
        --filename="$target" \
        --rw=read \
        --bs=1M \
        --ioengine=libaio \
        --direct=1 \
        --numjobs=1 \
        --runtime=5 \
        --time_based \
        --output-format=normal \
        --output="${out_base}_test1_seq1t.txt" 2>&1 | tail -20 || true

    # ========= Test 2: 4K random read (单 thread, 模拟 kernel 实际 IO) =========
    echo ""
    echo "--- Test 2: 4K random read (1 thread, 5s) ---"
    fio --name=rand4k_1t \
        --filename="$target" \
        --rw=randread \
        --bs=4k \
        --ioengine=libaio \
        --direct=1 \
        --numjobs=1 \
        --runtime=5 \
        --time_based \
        --output-format=normal \
        --output="${out_base}_test2_rand4k_1t.txt" 2>&1 | tail -20 || true

    # ========= Test 3: 4 concurrent threads sequential read =========
    # 模拟 4 client 同时触发 L3 reload (同 1 file 4 jobs)
    echo ""
    echo "--- Test 3: 4 thread concurrent sequential read (1 file × 4 jobs) ---"
    fio --name=seq4t \
        --filename="$target" \
        --rw=read \
        --bs=1M \
        --ioengine=libaio \
        --direct=1 \
        --numjobs=4 \
        --runtime=5 \
        --time_based \
        --group_reporting \
        --output-format=normal \
        --output="${out_base}_test3_seq4t.txt" 2>&1 | tail -20 || true
done

echo ""
echo "============================================================"
echo "ALL DISKS DONE"
echo "Results: results/l3_fio/<disk>_test{1,2,3}_*.txt"
echo "============================================================"