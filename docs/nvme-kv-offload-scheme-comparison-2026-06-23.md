# NVMe SSD KV Cache 下放方案梳理

**日期**: 2026-06-23
**范围**: vLLM + LMCache、SGLang + LMCache、SGLang + HiCache、SGLang/vLLM + Mooncake Store 在 NVMe SSD 下放/溢出 KV cache 场景的工程对比。

> 说明: 用户口径里的 "MoonCache" 在当前公开资料和本机安装包里没有独立项目命名。本文按 **Mooncake / Mooncake Store** 理解: 即 Mooncake transfer engine / Mooncake Store 作为 KV transfer 或 L2/L3 远端存储后端。

## 1. 一句话结论

| 方案 | 当前判断 | 适合做什么 | 不适合做什么 |
|---|---|---|---|
| **vLLM + LMCache local disk** | 最成熟、我们已跑通 | 单机 NVMe 真 offload baseline、和已有 MLPerf/LMCache 报告对齐 | 直接暴露盘差,因为 `local_cpu` 和 page cache 很容易把 SSD 读屏蔽掉 |
| **SGLang + LMCache local disk** | 代码路径存在,但本仓未系统验证 | 想保留 LMCache 生态,同时切到 SGLang 调度/serve | 和 HiCache 同时开启;两套 prefix cache 语义会混在一起 |
| **SGLang + HiCache file backend** | 当前最适合 AI SSD 选型 | SGLang 原生分层 KV cache,直接测 L3 file reload latency、write policy、盘间 tail | 小 prompt/单 prompt 测盘;L2 host DRAM 会把 L3 读完全挡住 |
| **SGLang/vLLM + Mooncake Store** | 更像分布式 KV/PD cache,不是单机 NVMe 首选 | 多机、多实例、prefill/decode 分离、跨节点 KV 复用 | 单机 4 块消费级 NVMe 选型 baseline |

推荐路线:

1. **单机 NVMe 选型**: 以 `SGLang + HiCache(file)` 为主线,用 `vLLM + LMCache(local_disk)` 做历史对照。
2. **vLLM 产品线兼容**: 保留 `vLLM + LMCache` 作为可交付路径,补一组 4 盘 cold reload / long steady 测试。
3. **SGLang 产品线**: 优先 HiCache,只在必须复用 LMCache 生态时验证 `SGLang + LMCache`。
4. **Mooncake Store**: 放在集群/远端缓存预研,不要和本轮 "NVMe SSD 下放" 混成一个单机方案。

## 2. 四套方案分解

### 2.1 vLLM + LMCache

**链路**

```text
vLLM KV connector
  -> LMCache engine
     -> local_cpu L1
     -> local_disk L2 (file:// /mnt/<ssd>/lmcache)
```

**本仓状态**

- 已跑通: `REPORT_LMCACHE.md`
- 配置: `scripts/lmcache_baseline.yaml`, `scripts/lmcache_ai_ssd*.yaml`
- vLLM 启动核心参数:

```bash
LMCACHE_CONFIG_FILE=scripts/lmcache_baseline.yaml \
vllm serve /path/to/model \
  --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'
```

**已知数据**

- 7000 tokens prompt, cold store 单请求约 **0.95 GB** KV 到 disk。
- warm 命中 **6912 / 7000 tokens**。
- cold -> warm TTFT 约 **0.779s -> 0.035s**,加速约 **22x**。
- LMCache chunk 粒度为 `chunk_size=256`,单文件约 **37.7 MB**。

**对 NVMe 的意义**

- 优点: 端到端真实推理链路,能回答 "LMCache local_disk 是否可用"。
- 问题: `local_cpu: true` 会让 warm 很快变成 CPU 内存命中;Linux page cache 也会污染磁盘读。
- 所以它适合做 **functional baseline**,不适合作为唯一的 SSD 排名依据。

**要测出真盘差的条件**

- 每轮清空 cache dir。
- cold store 后重启进程或清 LMCache CPU tier。
- warm reload 前 `sync && echo 3 > /proc/sys/vm/drop_caches`。
- ext4 上启用 `extra_config.use_odirect: true`;NTFS 不要拿 O_DIRECT 数字直接和 ext4 比。
- 用 14B/32B 或多 prompt,让 KV 数据超过 CPU tier 和 page cache 的舒适区。

### 2.2 SGLang + LMCache

**链路**

```text
SGLang RadixCache replacement
  -> LMCache SGLang adapter
     -> LMCache local_cpu / local_disk / remote backend
```

**本机代码证据**

- `lmcache.integration.sglang.sglang_adapter.LMCacheConnector` 存在。
- SGLang cache registry 在 `server_args.enable_lmcache` 时创建 `LMCRadixCache`。
- `init_lmcache_engine(...)` 接受 `config_file`,说明 SGLang 路径也走 LMCache YAML。

**工程判断**

- 它是可行路线,但本仓现有实测主要集中在 vLLM+LMCache 和 SGLang+HiCache。
- 对 NVMe 行为而言,底层仍是 LMCache 的 `local_disk` 语义,所以风险点和 vLLM+LMCache 类似: CPU tier/page cache 会隐藏真实盘读。
- 不建议和 HiCache 同时开。SGLang registry 先判断 `enable_hierarchical_cache`,再判断 `enable_lmcache`;同时开会让实验语义不干净。

**适用场景**

- 需要 SGLang server 能力,但希望继续使用 LMCache 的存储后端、Mooncake Store adapter 或已有 LMCache 配置。
- 对比 "SGLang scheduler 差异" 而不是 "HiCache vs LMCache 实现差异"。

**建议验证**

```bash
python -m sglang.launch_server \
  --model-path /path/to/model \
  --enable-lmcache \
  --lmcache-config-file scripts/lmcache_baseline.yaml
```

验证顺序:

1. 单 prompt cold/warm 看 LMCache store/load log。
2. 关闭 HiCache,确认只走 `LMCRadixCache`。
3. 复用 LMCache 4 盘矩阵,单独比较 SGLang vs vLLM 的 TTFT 和 I/O。

### 2.3 SGLang + HiCache

**链路**

```text
SGLang radix cache
  -> L1 GPU KV pool
  -> L2 host DRAM hierarchical cache
  -> L3 file backend on NVMe SSD
```

**本仓状态**

- 已跑通多轮: `docs/hicache-phase7-v3-validation-2026-06-15.md`, `docs/hicache-phase7-g-multirun-validation-2026-06-15.md`
- 启动脚本: `scripts/hicache_serve.sh`
- 负载脚本: `scripts/hicache_bench_one_round.sh`

核心参数:

```bash
export SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/mnt/ai_ssd0/cache_hicache

python -m sglang.launch_server \
  --enable-hierarchical-cache \
  --hicache-ratio 2 \
  --hicache-size 0 \
  --hicache-mem-layout page_first_direct \
  --hicache-io-backend direct \
  --hicache-write-policy write_through \
  --hicache-storage-backend file \
  --hicache-storage-prefetch-policy timeout
```

**已知数据**

- HiCache page 粒度: `page_size=64`,L3 文件约 **9 MB/page**。
- 20 prompts x 7K tokens 会写出约 **19 GB L3 files**。
- multiprompt replay 能暴露 L3 真读盘,NTFS 三盘 mean:
  - ZHITAI: **2.272s**,CV 7.7%
  - WDC: **2.651s**,CV 6.0%
  - Seagate: **2.981s**,CV 18.1%,存在 bimodal 慢读
- iostat 看到请求大小主要在 **60-125 KB**,盘未打满,差异主要来自 r_await/tail latency。

**对 NVMe 的意义**

- 这是目前最像 "AI SSD KV cache 下放" 的路径: page file 多、随机-ish 读、能暴露 p99/tail。
- HiCache 的 L2 host DRAM 很强,单 prompt 或小数据集会 100% L2 hit,看不到盘。
- 要暴露盘差,必须做 multiprompt/L2 eviction/drop-caches。

**write policy**

| policy | 结论 |
|---|---|
| `write_through` | 推荐作为选型基线;能保证 L3 落盘,数据可解释 |
| `write_back` | cold 少约 2-3% 阻塞,但短测里 L3 可能还没落盘;适合长稳态预研 |
| `write_through_selective` | 本仓测试里几乎不写盘但 TTFT 反增,暂不推荐 |

**当前坑点**

- SGLang 0.5.13 中 `--file-storage-path` 曾不生效,本仓用 `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR` 指定 L3 目录。
- BIWIN 写系统盘根目录时,page cache 影响很重,不能当纯硬件盘性能。
- 监控盘位必须用 `lsblk -d -o NAME,MODEL` 每次确认,避免 nvme 编号漂移。

### 2.4 SGLang/vLLM + Mooncake Store

**链路 A: vLLM Mooncake connector**

```text
vLLM KV transfer
  -> MooncakeConnector / MooncakeStoreConnector
     -> Mooncake transfer engine / store
```

本机 vLLM connector factory 注册了:

- `MooncakeConnector`
- `MooncakeStoreConnector`

**链路 B: LMCache Mooncake Store L2 adapter**

```text
vLLM or SGLang
  -> LMCache
     -> mooncake_store L2 adapter
        -> Mooncake Store
```

本机 LMCache 注册了 `mooncake_store` L2 adapter。该 adapter 把大部分配置原样转发给 Mooncake SDK;如果使用 RDMA,还需要 L1 memory descriptor 做 preregistration。

**工程判断**

- Mooncake Store 的定位是 **远端/分布式 KV store 或 PD disaggregation transfer**,不是 "把 KV 文件写到本机 `/mnt/nvme`" 的最短路径。
- 它可能最终也落到某种后端存储,但那已经是 Mooncake Store 的服务端部署问题,不是简单 local NVMe 文件下放。
- 对单机 4 块消费级 SSD 的选型,直接上 Mooncake 会引入太多变量:网络/RDMA、metadata、client worker、store 服务端、memory registration。

**适用场景**

- 多台推理实例共享 prefix cache。
- prefill/decode 分离,KV 跨进程/跨节点传输。
- 需要把 KV cache 做成服务化资源,而不是每个推理进程独占本地目录。

**不建议当前优先做的原因**

- 本仓尚无 Mooncake Store 部署脚本和数据。
- 对 "NVMe SSD 盘差" 不够直接,会把传输层和存储层混在一起。
- 如果目标是 AI SSD 选型,HiCache file backend 和 LMCache local_disk 更干净。

## 3. 横向指标对比

| 维度 | vLLM+LMCache | SGLang+LMCache | SGLang+HiCache | Mooncake Store |
|---|---|---|---|---|
| 本仓验证 | 已验证 | 未系统验证 | 已验证最多 | 未验证 |
| 单机 NVMe 直接性 | 高 | 高 | 最高 | 低/中 |
| 分布式能力 | 依赖 LMCache remote | 依赖 LMCache remote | SGLang 生态内扩展 | 强 |
| 文件粒度 | ~37.7 MB chunk | 同 LMCache | ~9 MB page | 取决于 store |
| 默认是否易被内存挡住 | 是 | 是 | 是,L2 更明显 | 取决于部署 |
| 盘差暴露难度 | 中 | 中 | 中,但可控 | 高 |
| 推荐优先级 | P1 | P2 | P0 | P3 |

## 4. 测试矩阵建议

### P0: SGLang + HiCache 作为主线

目标: 给 AI SSD 选型一个可解释排序。

- 模型: Qwen3-4B multiprompt 作为稳定小模型;再补 14B-AWQ。
- 负载: 20 prompts x 7K tokens + replay p0。
- 参数: `write_through`, `hicache-ratio` 降低或用 multiprompt 挤出 L2。
- 指标: replay TTFT、r_await p95/p99、read burst、L3 file count/size、CV。
- 输出: 每盘至少 5 runs,run 间 drop page cache。

### P1: vLLM + LMCache 作为兼容 baseline

目标: 验证 vLLM 产品线也能使用 NVMe local_disk。

- 复用 `scripts/lmcache_*.yaml`。
- 加入进程重启或 CPU tier 清理,避免 warm 只测 CPU。
- 增加 4 盘 cold reload 对比,不要只看 cold store。
- 补 20-30 分钟 steady state,看 GC/tail。

### P2: SGLang + LMCache 作为桥接路线

目标: 判断是否有必要维护 "SGLang server + LMCache backend"。

- 只开 `--enable-lmcache`,不开 `--enable-hierarchical-cache`。
- 用同一 LMCache YAML 对比 vLLM 和 SGLang。
- 如果 TTFT/I/O 没有明显收益,不建议作为主线。

### P3: Mooncake Store 作为集群预研

目标: 判断远端共享 KV 是否值得做。

- 先跑单机 loopback,确认 connector 可用。
- 再拆成 prefill/decode 或 producer/consumer。
- 最后再讨论 store server 的 NVMe 后端和多盘布局。
- 指标从 "SSD 排名" 改为 "跨实例命中率、传输延迟、服务端 tail、失效/一致性"。

## 5. 决策建议

短期交付:

- 报告主线用 **SGLang + HiCache(file)**。
- 保留 **vLLM + LMCache(local_disk)** 作为历史 baseline 和 vLLM 兼容证明。
- 暂不把 Mooncake Store 写成 "NVMe SSD 下放方案已验证";只列为分布式缓存预研项。

中期补测:

- `SGLang + LMCache` 跑一轮 smoke + 4 盘短矩阵,确认是否值得保留。
- `vLLM + LMCache` 补 cold reload/drop-caches/long steady,解决当前 "local_cpu 屏蔽盘读" 的解释缺口。
- HiCache 重跑 5-run 时每 run 间 drop page cache,减少 ZHITAI 单调变快这类 OS cache 干扰。

最终选型口径:

```text
如果目标是单机 AI SSD 选型:
  SGLang + HiCache(file) > vLLM + LMCache(local_disk) > SGLang + LMCache > Mooncake Store

如果目标是 vLLM 产品兼容:
  vLLM + LMCache(local_disk) 是主线;HiCache 只作为 SGLang 对照。

如果目标是多机共享 KV / PD 分离:
  Mooncake Store 进入主线;本地 NVMe file backend 降级为单机 baseline。
```

## 6. 参考材料

本仓材料:

- `REPORT_LMCACHE.md`
- `docs/hicache-phase7-v3-validation-2026-06-15.md`
- `docs/hicache-phase7-g-multirun-validation-2026-06-15.md`
- `docs/hicache-writeback-vs-writethrough-2026-06-13.md`
- `reports/ai-ssd-real-offloading-investigation-report-2026-06-17.md`
- `scripts/lmcache_baseline.yaml`
- `scripts/hicache_serve.sh`

外部/安装包依据:

- LMCache docs: https://docs.lmcache.ai/
- SGLang HiCache docs: https://docs.sglang.io/docs/advanced_features/hicache
- vLLM connector registry in installed package: `vllm/distributed/kv_transfer/kv_connector/factory.py`
- LMCache SGLang adapter in installed package: `lmcache/integration/sglang/sglang_adapter.py`
- LMCache Mooncake Store adapter in installed package: `lmcache/v1/distributed/l2_adapters/mooncake_store_l2_adapter.py`
