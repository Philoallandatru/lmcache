# lmcache

> vLLM + LMCache 真实 KV-Cache Offloading 在 4 块 NVMe 上的 IO 特征预研

## 目录

- [REPORT.md](./REPORT.md) — 主报告 (方案 / 测量 / 启示 / 后续)
- [scripts/](./scripts/) — 启动 / 压测 / IO 监测 / 驱动器
- [results/](./results/) — 4 轮的 TTFT + 测点 JSONL
- [logs/](./logs/) — iostat + 驱动器 + 压测 + bpftrace 原始输出

## 速览

| 指标 | baseline (BIWIN ext4) | nvme0 WDC (NTFS) | nvme2 致钛 (NTFS) | nvme3 Seagate (NTFS) |
|---|---|---|---|---|
| cold TTFT | 0.785s | 0.787s | 0.788s | 0.787s |
| warm TTFT | 0.033s | 0.034s | 0.034s | 0.034s |
| 加速比 | 23.5× | 23.5× | 22.9× | 22.9× |
| 写带宽峰值 | 977 MB/s | 643 MB/s | 684 MB/s | 648 MB/s |
| w_await 峰值 | 10.69 ms | 1.10 ms | 0.20 ms | 17.00 ms |
| util 峰值 | 13.5% | 30.5% | 20.4% | 16.1% |

**结论**: 4 块盘均不是 IO 瓶颈。**致钛 nvme2** 写延迟最低 (0.20ms) + 高带宽 (684 MB/s)，是 AI-SSD 最优候选。

## 重现

```bash
# 1) 装环境
cd ~/llm && source .venv/bin/activate   # vllm 0.22.1 + lmcache 0.4.6 + torch 2.11
# 2) 拉模型
python -c "from modelscope import snapshot_download; snapshot_download('Qwen/Qwen3-4B-Instruct-2507', cache_dir='/home/ficus/llm/models')"
# 3) 挂载候选盘
sudo -n mount -t ntfs3 -o noatime,nodiratime,uid=1000,gid=1000 /dev/nvme0n1p2 /mnt/ai_ssd0
sudo -n mount -t ntfs3 -o noatime,nodiratime,uid=1000,gid=1000 /dev/nvme2n1p3 /mnt/ai_ssd1
sudo -n mount -t ntfs3 -o noatime,nodiratime,uid=1000,gid=1000 /dev/nvme3n1p2 /mnt/ai_ssd2
# 4) 跑 4 轮
bash scripts/drive_rounds.sh
```

## 关键依赖

- vllm 0.22.1 (cu130)
- lmcache 0.4.6
- torch 2.11.0+cu130
- transformers 5.10.2
- sysstat 12.7.7 (iostat/pidstat)
- bpftrace 0.25.0

## 注意事项

- torch 2.12 与 vllm 0.22.1 ABI 不兼容, 必须用 2.11
- torchvision 必须 0.26.0+cu130 (跟 torch 2.11 配)
- NTFS 在 Linux O_DIRECT 行为不一致, 候选盘必须 `use_odirect: false`
- LMCache `local_cpu: true` 会让 warm 命中走内存, 测盘要 drop_caches
