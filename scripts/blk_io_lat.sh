#!/usr/bin/env bash
# bpftrace: 抓 vllm/LMCache 进程 IO, 按 device 过滤
# 用法: ./blk_io_lat.sh <major:minor> <comm> <output_dir> <duration_sec>
set -uo pipefail

DEV_MM="${1:?usage: $0 <major:minor> <comm> <output_dir> <duration_sec>}"
COMM="${2:?missing comm filter}"
OUTDIR="${3:?missing outdir}"
DUR="${4:?missing dur}"
mkdir -p "$OUTDIR"
OUT="$OUTDIR/blk_io_${DEV_MM//:/_}_${COMM}.log"

MAJOR=$(echo "$DEV_MM" | cut -d: -f1)
MINOR=$(echo "$DEV_MM" | cut -d: -f2)
echo "[bpf] dev=$DEV_MM comm=$COMM dur=${DUR}s out=$OUT"

sudo -n bpftrace - <<EOF 2>&1 | tee "$OUT" &
tracepoint:block:block_rq_issue
/(args->dev >> 20) == $MAJOR && (args->dev & 0xFFFFF) == $MINOR && str(args->comm) == "$COMM"/
{
    @usecs[args->bio] = nsecs;
    @bytes[args->bio] = args->bytes;
    @op[args->bio] = args->rwbs;
    @io_count += 1;
    @io_bytes += args->bytes;
}

tracepoint:block:block_rq_complete
/(args->dev >> 20) == $MAJOR && (args->dev & 0xFFFFF) == $MINOR && str(args->comm) == "$COMM"/
{
    \$have = @usecs[args->bio] != 0;
    if (\$have) {
        \$lat_us = (nsecs - @usecs[args->bio]) / 1000;
        @latency_hist = lhist(\$lat_us, 1, 10, 1);
        @done_count += 1;
        delete(@usecs[args->bio]);
        delete(@bytes[args->bio]);
        delete(@op[args->bio]);
    }
}

interval:s:1
{
    printf("ts=%llu issued=%llu done=%llu bytes=%llu\n",
        nsecs, @io_count, @done_count, @io_bytes);
}

END
{
    printf("\n=== Latency (us) histogram ===\n");
    print(@latency_hist);
    printf("\n=== Totals ===\n");
    printf("total_issued=%llu total_done=%llu total_bytes=%llu\n",
        @io_count, @done_count, @io_bytes);
    clear(@latency_hist);
    clear(@io_count);
    clear(@io_bytes);
    clear(@done_count);
    clear(@usecs);
    clear(@bytes);
    clear(@op);
}
EOF

BPF_PID=$!
echo "[bpf] pid=$BPF_PID"
sleep "$DUR"
kill $BPF_PID 2>/dev/null
wait $BPF_PID 2>/dev/null
echo "[bpf] done"
