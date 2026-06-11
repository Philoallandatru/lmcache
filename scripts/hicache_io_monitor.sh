#!/bin/bash
# scripts/hicache_io_monitor.sh
# 用法: hicache_io_monitor.sh <device> <output_dir> [interval_sec]
#   - 后台跑 iostat -dx -m <interval> 到 <output_dir>/iostat_<dev>.log
#   - 用 trap 收 SIGTERM/SIGINT 时优雅退出
#   - sysstat 12.7+ 22 列布局,用列名定位 (per iostat-dx-m-parser skill)

set -e

DEV=${1:?"device required (e.g. nvme0n1)"}
OUT=${2:?"output_dir required"}
INTERVAL=${3:-1}

mkdir -p "$OUT"
LOG="$OUT/iostat_${DEV}.log"

echo "[hicache_io_monitor] device=$DEV interval=${INTERVAL}s log=$LOG" >&2

# trap 优雅退出
cleanup() {
    if [ -n "$IOSTAT_PID" ] && kill -0 "$IOSTAT_PID" 2>/dev/null; then
        kill "$IOSTAT_PID" 2>/dev/null || true
        wait "$IOSTAT_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT TERM INT

iostat -dx -m "$INTERVAL" "$DEV" > "$LOG" &
IOSTAT_PID=$!

echo "[hicache_io_monitor] iostat pid=$IOSTAT_PID" >&2
# 等 iostat 自然退出 (会被 SIGTERM 杀)
wait "$IOSTAT_PID" || true
echo "[hicache_io_monitor] iostat exited" >&2