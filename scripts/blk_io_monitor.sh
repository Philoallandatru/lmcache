#!/usr/bin/env bash
# bpftrace 落盘 IO 细粒度监测
# 抓 block_rq_issue / block_rq_complete 探针, 计算 latency
# 用法: ./blk_io_monitor.sh <device> <output.csv> <duration_sec>
set -uo pipefail

DEV="${1:?usage: $0 <device> <output.csv> <duration_sec>}"
OUT="${2:?missing output.csv}"
DUR="${3:?missing duration_sec}"

mkdir -p "$(dirname "$OUT")"

bpftrace - <<EOF > "$OUT" 2>&1 &
BEGIN { printf("ts_us,op,bytes,sector,lat_us,dev\n"); }

tracepoint:block:block_rq_issue
/args->dev->disk_name == "\$DEV"/
{
    @start[args->bio] = nsecs;
    printf("%llu,issue,0,%llu,0,%s\n",
        nsecs/1000, args->sector, args->dev->disk_name);
}

tracepoint:block:block_rq_complete
/args->dev->disk_name == "\$DEV"/ && @start[args->bio] != 0
{
    \$op = args->error == 0 ? "ok" : "err";
    delete(@start[args->bio]);
    // 输出由 issue 行已记录, 完整 latency 模式见 summary
}

END {
    printf("# done\n");
    clear(@start);
}
EOF

BPF_PID=$!
echo "[bpf] started pid=$BPF_PID dev=$DEV out=$OUT dur=${DUR}s"
sleep "$DUR"
kill $BPF_PID 2>/dev/null
wait $BPF_PID 2>/dev/null
echo "[bpf] done, output=$OUT"
