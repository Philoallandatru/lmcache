#!/usr/bin/env bash
# SLC cache 探测 (简单可靠版)
# 原理: 跑 N 个连续短测试, 每次写 chunk_size_MB 数据, 测平均 BW
# SLC cache 满后 BW 会跌, 断崖点 = 之前累计写量
#
# 用法: ./slc_probe.sh <mount_path> <tag> <chunk_size_MB> <num_chunks>
# 例: ./slc_probe.sh /mnt/ssd ai_ssd 1024 12  # 12 段 × 1GB = 12GB 总写
set -uo pipefail

MOUNT="${1:?usage: $0 <mount_path> <tag> <chunk_MB> <num_chunks>}"
TAG="${2:?missing tag}"
CHUNK_MB="${3:-1024}"
NUM="${4:-12}"

OUTDIR="/home/ficus/llm/infer/ai_ssd_prestudy/results/slc_probe_${TAG}"
mkdir -p "$OUTDIR"
SUMMARY="$OUTDIR/summary.json"
LOG="$OUTDIR/all.log"
: > "$LOG"

echo "[slc] mount=$MOUNT tag=$TAG chunk=${CHUNK_MB}MB chunks=$NUM  total=$((CHUNK_MB*NUM))MB"

# 同步 + drop_caches
sync
echo 3 | sudo -n tee /proc/sys/vm/drop_caches > /dev/null 2>&1 || true

for i in $(seq 1 $NUM); do
    FIO_JOB="$OUTDIR/chunk_${i}.fio"
    cat > "$FIO_JOB" <<EOF
[global]
ioengine=libaio
direct=1
bs=128k
iodepth=32
group_reporting=1
filename=${MOUNT}/.slc_${i}.dat
size=${CHUNK_MB}M
stonewall

[chunk]
rw=write
EOF
    echo "--- chunk $i ---" >> "$LOG"
    fio "$FIO_JOB" --output-format=normal 2>&1 | tee -a "$LOG" | tail -8
    rm -f "$MOUNT/.slc_${i}.dat"
done

# python 解析整个 log
/home/ficus/llm/.venv/bin/python <<PYEOF
import re, json
text = open("$LOG").read()

# 按 "--- chunk N ---" 切分
chunks = re.split(r'--- chunk \d+ ---', text)
chunks = [c for c in chunks if 'WRITE:' in c]

samples = []
for idx, c in enumerate(chunks, start=1):
    # 找 WRITE: bw=...MiB/s (X MB/s), ..., io=YMiB (ZMB), ...
    m = re.search(r'WRITE:\s*bw=([0-9.]+)MiB/s\s*\(([0-9.]+)MB/s\)[^,]*,\s*[0-9.]+MiB/s-[0-9.]+MiB/s[^,]*,\s*io=([0-9.]+)(MiB|MB|kB)\s*\(([0-9.]+)MB\)', c)
    if not m:
        # fallback: 简单模式
        m = re.search(r'WRITE:\s*bw=([0-9.]+)MiB/s', c)
        if not m: continue
        bw_mib = float(m.group(1))
        bw_mb = bw_mib * 1.048576
        iomb = None
    else:
        bw_mib = float(m.group(1))
        bw_mb = float(m.group(2))
        iomb = float(m.group(3))
    
    # p99 延迟
    p99_m = re.search(r'99\.00th=\[\s*([0-9.]+)\s*\]', c)
    p99_us = float(p99_m.group(1)) if p99_m else None
    # p99 unit 转换
    p99_unit_m = re.search(r'99\.00th=\[\s*[0-9.]+\s*(\w+)\s*\]', c)
    p99_unit = p99_unit_m.group(1) if p99_unit_m else 'us'
    if p99_us is not None:
        if p99_unit == 'msec' or p99_unit == 'ms': p99_us *= 1000
        elif p99_unit == 'nsec' or p99_unit == 'ns': p99_us /= 1000
    
    cum_mb = idx * $CHUNK_MB
    samples.append({
        'idx': idx,
        'cum_MB': cum_mb,
        'bw_MiBps': round(bw_mib, 1),
        'bw_MBps': round(bw_mb, 1),
        'io_MB': round(iomb, 1) if iomb else None,
        'p99_us': round(p99_us, 1) if p99_us else None,
    })

# 断崖检测
out = {
    'tag': '$TAG', 'mount': '$MOUNT', 'chunk_MB': $CHUNK_MB, 'num_chunks': $NUM,
    'samples': samples,
    'slc_cliff': None,
}

if samples:
    # 找第一段后续 BW 跌到上一段 60% 以下
    for i in range(1, len(samples)):
        cur = samples[i]['bw_MiBps']
        prev = samples[i-1]['bw_MiBps']
        if prev > 100 and cur < prev * 0.6:
            cap_gb = samples[i-1]['cum_MB'] / 1024
            out['slc_cliff'] = {
                'after_chunk': i,
                'cumulative_MB_before': samples[i-1]['cum_MB'],
                'estimated_SLC_capacity_GB': round(cap_gb, 2),
                'bw_pre_MiBps': prev,
                'bw_post_MiBps': cur,
                'drop_ratio': round(cur/prev, 2),
            }
            break
    if not out['slc_cliff']:
        # 全程稳态, peak 是 cache 内速度
        out['note'] = f'全程 BW 稳定, SLC cache >= $((CHUNK_MB*NUM/1024))GB, 未触发跌速'
    
    out['bw_peak_MiBps'] = max(s['bw_MiBps'] for s in samples)
    out['bw_first_MiBps'] = samples[0]['bw_MiBps']
    out['bw_last_MiBps'] = samples[-1]['bw_MiBps']

print(json.dumps(out, indent=2, ensure_ascii=False))
open("$SUMMARY", 'w').write(json.dumps(out, indent=2, ensure_ascii=False))
PYEOF
