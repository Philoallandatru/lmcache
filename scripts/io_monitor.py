"""
IO 监测 - 同步采集 iostat + pidstat + per-process IO counter
输出 CSV, 后续可画时序图
"""
import os
import sys
import time
import csv
import subprocess
import signal
from datetime import datetime

DURATION = int(os.environ.get("IO_DURATION", "180"))  # 秒
INTERVAL = float(os.environ.get("IO_INTERVAL", "0.5"))
TAG = sys.argv[1] if len(sys.argv) > 1 else "monitor"
OUT_DIR = f"/home/ficus/llm/infer/ai_ssd_prestudy/results/{TAG}"
os.makedirs(OUT_DIR, exist_ok=True)

IOSTAT_CSV = f"{OUT_DIR}/iostat.csv"
PIDSTAT_CSV = f"{OUT_DIR}/pidstat.csv"
DEVICES = ["nvme0n1", "nvme1n1", "nvme2n1", "nvme3n1"]


def find_vllm_pid():
    out = subprocess.run(["pgrep", "-f", "vllm.*serve"], capture_output=True, text=True).stdout
    pids = [int(p) for p in out.split() if p.isdigit()]
    return pids[0] if pids else None


def iostat_loop(stop_at):
    """iostat -xm 1 每秒一行, 直接 tee 到 csv 文件"""
    cmd = ["iostat", "-xm", "1", str(DURATION)]
    with open(IOSTAT_CSV, "w") as f:
        f.write(f"# started at {datetime.now().isoformat()}\n")
        f.write("timestamp,device,rrqm_s,wrqm_s,r_s,w_s,rMB_s,wMB_s,avgrq-sz,avgqu-sz,await,r_await,w_await,svctm,%util\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in proc.stdout:
            f.write(f"{datetime.now().isoformat()},{line.strip()}\n")
            f.flush()
            if time.time() >= stop_at:
                break
        proc.terminate()


def pidstat_loop(vllm_pid, stop_at):
    """pidstat -d 1 -p $vllm_pid"""
    cmd = ["pidstat", "-d", "1", "-p", str(vllm_pid)]
    with open(PIDSTAT_CSV, "w") as f:
        f.write(f"# vllm_pid={vllm_pid} started at {datetime.now().isoformat()}\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in proc.stdout:
            f.write(line)
            f.flush()
            if time.time() >= stop_at:
                break
        proc.terminate()


def main():
    print(f"[io] duration={DURATION}s interval={INTERVAL}s tag={TAG}")
    print(f"[io] out={OUT_DIR}")
    vllm_pid = find_vllm_pid()
    print(f"[io] vllm_pid={vllm_pid}")
    if not vllm_pid:
        print("WARN: vllm PID not found, skip pidstat", file=sys.stderr)
    stop_at = time.time() + DURATION
    if vllm_pid:
        pidstat_loop(vllm_pid, stop_at)
    else:
        iostat_loop(stop_at)


if __name__ == "__main__":
    main()
