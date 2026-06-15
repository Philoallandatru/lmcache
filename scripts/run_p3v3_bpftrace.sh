#!/bin/bash
# scripts/run_p3v3_bpftrace.sh
#
# P3v3: ZHITAI 单独跑 + bpftrace blk-mq bio 延迟跟踪
# 目标: 解释 P3v2 A1 (4.013s) vs g1-g5 ZHITAI (2.058s) 慢 ~2s 的根因
#
# bpftrace 探针:
#   blk_mq_start_request:  bio 入队
#   blk_mq_end_request:    bio 完成 (携带延迟)
#   tracepoint: block:block_rq_issue / block_rq_complete
#
# 数据: results/hicache_multiprompt_p3v3_A1/ai_ssd2_zhitai_ntfs/

set -e

cd /home/ficus/llm/infer/ai_ssd_prestudy

# 预检
free_gb=$(df -BG --output=avail /mnt/ai_ssd2 | tail -1 | tr -dc '0-9')
if [ "$free_gb" -lt 25 ]; then
    echo "FATAL: /mnt/ai_ssd2 只有 ${free_gb}GB"
    exit 1
fi

# drop_caches + start bpftrace 后台
sync
sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>&1 || true
sleep 2

RESULT_DIR=results/hicache_multiprompt_p3v3_A1/ai_ssd2_zhitai_ntfs
mkdir -p "$RESULT_DIR"

# 启动 bpftrace: 跟踪 nvme3n1 (ZHITAI) 所有 bio 延迟
BPFTRACE_OUT="$RESULT_DIR/bpftrace_nvme3n1.log"
rm -f "$BPFTRACE_OUT"

cat > /tmp/bpftrace_blk_mq.bt << 'EOF'
#include <linux/blk_types.h>
#include <linux/blkdev.h>

BEGIN {
    @start_time = nsecs;
    @total_ios = 0;
    @total_read_ios = 0;
    @total_write_ios = 0;
    @read_lat_total = 0;
    @write_lat_total = 0;
    @read_lat_max = 0;
    @read_lat_min = 99999999999;
    @slow_ios_1ms = 0;
    @slow_ios_10ms = 0;
    @slow_ios_100ms = 0;
    @slow_ios_1s = 0;
    @slow_ios_4s = 0;
    printf("=== bpftrace blk-mq latency tracking start ===\n");
}

kprobe:blk_mq_start_request {
    $rq = (struct request *)arg0;
    $dev = $rq->q->disk->disk_name;
    if ($dev == "nvme3n1") {
        @start[$rq] = nsecs;
    }
}

kprobe:blk_mq_end_request {
    $rq = (struct request *)arg0;
    $dev = $rq->q->disk->disk_name;
    if ($dev == "nvme3n1" && @start[$rq] != 0) {
        $lat_us = (nsecs - @start[$rq]) / 1000;
        $is_write = ($rq->cmd_flags & REQ_WRITE) != 0;
        @total_ios += 1;
        if ($is_write) {
            @total_write_ios += 1;
            @write_lat_total += $lat_us;
        } else {
            @total_read_ios += 1;
            @read_lat_total += $lat_us;
            if ($lat_us > @read_lat_max) { @read_lat_max = $lat_us; }
            if ($lat_us < @read_lat_min && $lat_us > 0) { @read_lat_min = $lat_us; }
            if ($lat_us > 1000)   { @slow_ios_1ms += 1; }
            if ($lat_us > 10000)  { @slow_ios_10ms += 1; }
            if ($lat_us > 100000) { @slow_ios_100ms += 1; }
            if ($lat_us > 1000000) { @slow_ios_1s += 1; }
            if ($lat_us > 4000000) { @slow_ios_4s += 1; }
        }
        delete(@start[$rq]);
    }
}

END {
    $elapsed_s = (nsecs - @start_time) / 1000000000;
    printf("\n=== bpftrace blk-mq latency summary (nvme3n1 = ZHITAI) ===\n");
    printf("elapsed: %d s\n", $elapsed_s);
    printf("total IO: %lld (read=%lld write=%lld)\n", @total_ios, @total_read_ios, @total_write_ios);
    if (@total_read_ios > 0) {
        printf("read avg lat: %lld us\n", @read_lat_total / @total_read_ios);
        printf("read max lat: %lld us (%.2f ms)\n", @read_lat_max, @read_lat_max/1000.0);
        printf("read min lat: %lld us (%.2f ms)\n", @read_lat_min, @read_lat_min/1000.0);
    }
    if (@total_write_ios > 0) {
        printf("write avg lat: %lld us\n", @write_lat_total / @total_write_ios);
    }
    printf("slow IO (>1ms):    %lld\n", @slow_ios_1ms);
    printf("slow IO (>10ms):   %lld\n", @slow_ios_10ms);
    printf("slow IO (>100ms):  %lld\n", @slow_ios_100ms);
    printf("slow IO (>1s):     %lld\n", @slow_ios_1s);
    printf("slow IO (>4s):     %lld (跟 g1-g5 慢读现象对照)\n", @slow_ios_4s);
}
EOF

# 启动 bpftrace 后台
sudo -n bpftrace /tmp/bpftrace_blk_mq.bt > "$BPFTRACE_OUT" 2>&1 &
BPFTRACE_PID=$!
echo "bpftrace pid: $BPFTRACE_PID (log: $BPFTRACE_OUT)"
sleep 2

# 跑 P3v2 A1
echo "==== P3v3 A1 (drop_caches + 干净) start $(date +%H:%M:%S) ===="
mkdir -p /mnt/ai_ssd2/cache_multiprompt_p3v3_A1_v3
OUT_DIR_SUBDIR=hicache_multiprompt_p3v3_A1 \
PORT=30095 \
NUM_PROMPTS=20 \
REPLAY_PROMPT_ID=0 \
bash scripts/hicache_bench_one_round.sh \
    ai_ssd2_zhitai_ntfs nvme3n1 \
    /mnt/ai_ssd2/cache_multiprompt_p3v3_A1_v3 \
    write_through
rc=$?
if [ "$rc" != "0" ]; then
    echo "FATAL: A1 failed rc=$rc"
    kill $BPFTRACE_PID 2>/dev/null
    exit "$rc"
fi
echo "==== P3v3 A1 DONE $(date +%H:%M:%S) ===="

# 停 bpftrace
sleep 2
kill $BPFTRACE_PID 2>/dev/null
sleep 2
echo "==== bpftrace output ===="
cat "$BPFTRACE_OUT"
echo ""
echo "==== P3v3 全部完成 $(date +%H:%M:%S) ===="
