#!/usr/bin/env bash
# 4 轮 offload 驱动器
# 每轮:
#   1) 清 cache dir, 起 vllm (用对应 LMCache yaml)
#   2) 等 vllm ready
#   3) 启 iostat -xm 1 (粗粒度) + bpftrace (细粒度, 每块盘)
#   4) 跑压测 (1 cold + 3 warm, 固定 prompt)
#   5) 多跑 1 轮同 prefix (测 LMCache 多轮命中)
#   6) 杀 vllm, 留 logs
# 下一轮

set -uo pipefail

ROUNDS=(
    "baseline|259:13|/home/ficus/llm/infer/ai_ssd_prestudy/lmcache_cache_baseline|/home/ficus/llm/infer/ai_ssd_prestudy/scripts/lmcache_baseline.yaml"
    "ai_ssd0_nvme0n1|259:2|/mnt/ai_ssd0/lmcache|/home/ficus/llm/infer/ai_ssd_prestudy/scripts/lmcache_ai_ssd0_nvme0n1.yaml"
    "ai_ssd1_nvme2n1|259:9|/mnt/ai_ssd1/lmcache|/home/ficus/llm/infer/ai_ssd_prestudy/scripts/lmcache_ai_ssd1_nvme2n1.yaml"
    "ai_ssd2_nvme3n1|259:5|/mnt/ai_ssd2/lmcache|/home/ficus/llm/infer/ai_ssd_prestudy/scripts/lmcache_ai_ssd2_nvme3n1.yaml"
)

DRIVE_DIR="/home/ficus/llm/infer/ai_ssd_prestudy"
LOG_DIR="$DRIVE_DIR/logs"
RESULT_DIR="$DRIVE_DIR/results"
SCRIPT_DIR="$DRIVE_DIR/scripts"
mkdir -p "$LOG_DIR" "$RESULT_DIR"

# 杀残留
pkill -9 -f 'vllm serve' 2>/dev/null || true
pkill -9 -f 'VLLM::EngineCore' 2>/dev/null || true
pkill -9 -f 'io_monitor' 2>/dev/null || true
pkill -9 -f 'blk_io_lat' 2>/dev/null || true
sleep 3

for ROUND in "${ROUNDS[@]}"; do
    TAG=$(echo "$ROUND" | cut -d'|' -f1)
    DEV_MM=$(echo "$ROUND" | cut -d'|' -f2)
    CACHE_DIR=$(echo "$ROUND" | cut -d'|' -f3)
    YAML=$(echo "$ROUND" | cut -d'|' -f4)
    echo ""
    echo "=========================================="
    echo "Round: $TAG (dev=$DEV_MM)"
    echo "  cache: $CACHE_DIR"
    echo "  yaml:  $YAML"
    echo "=========================================="

    # 1) 起 vllm
    bash "$SCRIPT_DIR/serve_lmcache.sh" "$CACHE_DIR" "$YAML" "$TAG" &
    VLLM_PID=$!
    echo "[drive] vllm bash pid=$VLLM_PID"

    # 2) 等 vllm ready
    echo "[drive] waiting for vllm..."
    for i in $(seq 1 90); do
        if curl -s http://localhost:8000/v1/models >/dev/null 2>&1; then
            echo "[drive] vllm ready after ${i}s"
            break
        fi
        sleep 1
    done
    if ! curl -s http://localhost:8000/v1/models >/dev/null 2>&1; then
        echo "[drive] ERROR: vllm failed, tail log:"
        tail -30 "$LOG_DIR/vllm_${TAG}.log"
        pkill -9 -f 'vllm serve' 2>/dev/null
        pkill -9 -f 'EngineCore' 2>/dev/null
        continue
    fi

    # 3) 启动 iostat (1s 粒度) + bpftrace (细粒度)
    iostat -xm 1 90 > "$LOG_DIR/iostat_${TAG}.log" 2>&1 &
    IOSTAT_PID=$!

    # bpftrace: 只看 vllm python3 进程对该盘的 IO
    bash "$SCRIPT_DIR/blk_io_lat.sh" "$DEV_MM" "python3" "$LOG_DIR" 90 > "$LOG_DIR/bpf_${TAG}.log" 2>&1 &
    BPF_PID=$!

    sleep 3   # 留 3s 给 bpftrace 启动并稳

    # 4) 跑压测 - 1 cold + 3 warm (同 prefix 触发 LMCache hit reload)
    cd "$DRIVE_DIR"
    source ~/llm/.venv/bin/activate
    python "$SCRIPT_DIR/load_test.py" "$RESULT_DIR/$TAG" "${TAG}_r1" > "$LOG_DIR/load_${TAG}.log" 2>&1
    LOAD_RC=$?

    sleep 2

    # 5) 多跑 1 轮同 prefix (二次验证 LMCache hit 稳定)
    python "$SCRIPT_DIR/load_test.py" "$RESULT_DIR/$TAG" "${TAG}_r2" >> "$LOG_DIR/load_${TAG}.log" 2>&1

    # 6) 留 5s 让 iostat/bpftrace 收尾
    sleep 5

    # 7) 停所有
    kill $IOSTAT_PID 2>/dev/null
    kill $BPF_PID 2>/dev/null
    pkill -9 -f 'vllm serve' 2>/dev/null
    pkill -9 -f 'EngineCore' 2>/dev/null
    pkill -9 -f 'blk_io_lat' 2>/dev/null
    sleep 3

    echo "[drive] round $TAG summary (load_rc=$LOAD_RC):"
    if [ -f "$RESULT_DIR/$TAG/ttft_log.jsonl" ]; then
        python -c "
import json
recs = [json.loads(l) for l in open('$RESULT_DIR/$TAG/ttft_log.jsonl')]
cold = [r for r in recs if r['phase']=='cold']
warm = [r for r in recs if r['phase']=='warm']
if cold and warm:
    cm = sum(r['ttft'] for r in cold)/len(cold)
    wm = sum(r['ttft'] for r in warm)/len(warm)
    print(f'  cold_mean_ttft = {cm:.3f}s  ({len(cold)} reqs)')
    print(f'  warm_mean_ttft = {wm:.3f}s  ({len(warm)} reqs)')
    print(f'  speedup        = {cm/wm:.1f}x')
"
    fi
    if [ -d "$CACHE_DIR" ]; then
        echo "  cache_size: $(du -sh "$CACHE_DIR" 2>/dev/null | head -1 | cut -f1)  files: $(find "$CACHE_DIR" -name '*.pt' 2>/dev/null | wc -l)"
    fi
done

echo ""
echo "[drive] all rounds done"
