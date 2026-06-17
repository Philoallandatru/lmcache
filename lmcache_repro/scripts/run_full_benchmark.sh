#!/usr/bin/env bash
# Orchestrate the full LMCache benchmark:
#   1. baseline (LMCache disabled)
#   2. LMCache CPU backend
#   3. LMCache GPU backend
# Each with --num-trials trials.
#
# Usage: ./run_full_benchmark.sh [NUM_TRIALS]
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
mkdir -p results logs

NUM_TRIALS=${1:-3}
MODEL_PATH=${MODEL_PATH:-/home/ficus/llm/models/Qwen/Qwen2___5-14B-Instruct-AWQ}

# Use a SHORT tmp dir for LMCache local_cpu offload (ZMQ IPC path is
# limited to ~107 chars on Linux). /tmp paths are <80 chars.
LMCACHE_TMPDIR="/tmp/lmcache_tmp_$$"
mkdir -p "$LMCACHE_TMPDIR"
export TMPDIR="$LMCACHE_TMPDIR"

echo "=========================================="
echo "LMCache Reproducibility Benchmark"
echo "Model: $MODEL_PATH"
echo "Trials per backend: $NUM_TRIALS"
echo "Start: $(date)"
echo "=========================================="

run_backend() {
    local backend=$1
    local trial_results=()

    for ((trial=0; trial<NUM_TRIALS; trial++)); do
        local out="results/${backend}_trial${trial}.json"
        local log="logs/${backend}_trial${trial}.log"

        echo ""
        echo "[$backend] Trial $trial / $((NUM_TRIALS-1))..."
        echo "  Log: $log"

        if source ~/llm/.venv/bin/activate && \
           python3 scripts/lmcache_bench.py \
                --model "$MODEL_PATH" \
                --backend "$backend" \
                --num-trials "$NUM_TRIALS" \
                --trial-id "$trial" \
                --output "$out" 2>&1 | tee "$log"; then
            echo "[$backend] Trial $trial OK"
        else
            echo "[$backend] Trial $trial FAILED - see $log"
            return 1
        fi

        # Cooldown between trials
        sleep 5
    done
}

# Order: baseline first (no LMCache, fastest, validates pipeline)
echo ""
echo "### Phase 1: BASELINE (LMCache disabled) ###"
run_backend "disabled"

# CPU backend
echo ""
echo "### Phase 2: LMCache CPU backend ###"
run_backend "cpu"

# GPU backend
echo ""
echo "### Phase 3: LMCache GPU backend ###"
run_backend "gpu"

echo ""
echo "=========================================="
echo "All trials complete: $(date)"
echo "Results in: results/"
echo "Logs in: logs/"
echo "=========================================="