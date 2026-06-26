# AI SSD 预研报告

> **范围**: 整理当前目录下 `results/`、`logs/`、`docs/` 中的全部 AI SSD 相关测试  
> **时间**: 2026-06-09 ~ 2026-06-15  
> **结论先行**: 真正能区分 4 块盘的，是 **sglang HiCache 的 L2 miss + L3 reload 路径**；  
> L2 hit 场景下，盘差基本被 host DRAM / page cache 完全掩盖。  
> LMCache 早期测试证明了 KV Cache offload 的收益，但不作为最终盘型排名依据。

## 1. 测试目标

本轮预研回答 4 个问题：

1. AI SSD 在真实推理链路里，差异会不会被缓存层吃掉？
2. 盘的选择，应该看峰值带宽还是看 reload 延迟？
3. NTFS / ext4 / 盘型号，哪个因素更重要？
4. sglang HiCache 当前版本下，哪些测试结果能作为选型依据？

## 2. 测试矩阵

| 阶段 | 主题 | workload | 观测指标 | 结果用途 | 结论有效性 |
|---|---|---|---|---|---|
| Phase 0 | vLLM + LMCache 历史基线 | 7000 token prompt cold/warm | TTFT,hit tokens,落盘量 | 验证 offload 链路 | 参考 |
| Phase 1 | sglang HiCache smoke test | 单 prompt 启动和落盘 | 启动耗时,L3 file,path 行为 | 验证环境、参数、路径行为 | 有效 |
| Phase 2 | 4B write_through 4 盘 | 1 prompt × 6 rounds | cold/warm TTFT,iostat,L3 file | 看 L2 hit 下的 TTFT | L2-hit baseline |
| Phase 3 | write_through vs write_back | 4B 7K prompt,不同写策略 | cold/warm TTFT,L3 file | 看写策略差异 | 有效 |
| Phase 4 | 14B-AWQ 4 盘 | 14B-AWQ TP=2,7K prompt | cold/warm TTFT,page size | 更大模型下的 L2-hit 表现 | L2-hit baseline |
| Phase 5 | 4 client + drop_caches | 4 client 并发,每轮 drop | TTFT,iostat,L2 是否被清 | 验证并发与缓存行为 | 有效 |
| Phase 6 | fio direct=1 | 1MB seq,4K rand,seq4t | BW,IOPS,p50/p99/p99.9 | 测 raw disk 上限 | 有效 |
| Phase 7 | multiprompt + replay | 20 prompts × 7K + replay p0 | replay TTFT,L3 file,iostat | 强制 L2 miss，暴露盘差 | **核心选型依据** |
| G 多 run | Phase7 重复 6 次 | v3 + g1..g5 | mean,stdev,CV,burst/await | 判断稳定性和 tail | **核心选型依据** |
| v3 重跑 | mount 修正后复核 | Phase2/3/4/5/7 重跑 | TTFT spread,iostat 盘位 | 验证前期结论是否受 mount 事故影响 | 有效 |

被测盘和角色：

| 盘 | 文件系统/角色 | 解读边界 |
|---|---|---|
| BIWIN X570 | ext4,系统盘/root | replay 最快,但包含系统盘 page cache 优势 |
| WDC WDS960G2G0C | NTFS 数据盘 | 稳定居中,单轮最慢但多轮不是最差 |
| Seagate ZP1000GV30012 | NTFS 数据盘 | 多轮均值最慢,存在 bimodal 慢读 |
| ZHITAI Ti600 | NTFS 数据盘 | NTFS 三盘中多轮均值最好 |

## 3. 关键发现

### 3.1 LMCache 早期结果：证明 offload 方向成立

Phase 0 使用 vLLM + LMCache 验证 KV Cache local storage。  
同一个 7000 token prompt 下，cold 请求完整 prefill，warm 请求命中 LMCache。

| 指标 | 结果 |
|---|---:|
| Cold TTFT | 0.779-0.788s |
| Warm TTFT | 0.033-0.035s |
| TTFT 加速比 | 22.9-23.5× |
| LMCache hit tokens | 6912 / 7000 |
| 单 cold 请求落盘 | 约 0.95 GB |
| 单 chunk 文件 | 约 37.7 MB |

这组结果说明：KV Cache offload 对重复 prefix 场景确实有效，能把 TTFT 从约 0.78s 降到约 0.034s。

但它不能直接用于 AI SSD 排名，原因有三点：

- LMCache 默认有 CPU 内存层，warm 命中可能不再读盘
- 当时 3 块 NTFS 盘 mount 状态未完全确认
- 它更接近“缓存链路可用性验证”，不是“强制 L3 reload 盘差测试”

因此，LMCache 结果在本报告中的定位是 **路线有效性证明**，不是最终选型依据。

### 3.2 L2 hit 场景下，4 盘几乎没有差异

在 Phase 2 / 4 / 5 里，TTFT 的 4 盘 spread 都只有几毫秒到几十毫秒。
这说明：

- sglang HiCache 的 host DRAM L2 足够大时，L3 差异会被完全屏蔽
- `drop_caches` 只能清 OS page cache，清不掉 sglang 自己管理的 pinned host buffer
- 这类数据不能拿来做 AI SSD 排名

### 3.3 真正能看出盘差的是 L2 miss + L3 reload

Phase 7 用 20 个不同 prompt 把 L2 填满，再 replay p0，终于把盘差拉开。v3 单轮结果如下：

| 盘 | replay_p0 | 排名 |
|---|---:|---|
| BIWIN ext4 | **1.663s** | 1 |
| Seagate NTFS | 2.431s | 2 |
| ZHITAI NTFS | 2.545s | 3 |
| WDC NTFS | **2.643s** | 4 |

v3 单轮 4 盘 spread 为 **0.980s / 1.59×**。这证明 L3 reload 路径确实能暴露盘差。

但单轮不能直接做最终排序。后续 G 多 run 把 Phase7 跑到 6 次，排序变成：

| 盘 | replay_p0 均值 | stdev | CV | 结论 |
|---|---:|---:|---:|---|
| BIWIN ext4 | **1.620s** | 0.022s | 1.3% | 最快,但走系统盘 page cache |
| ZHITAI NTFS | **2.272s** | 0.174s | 7.7% | NTFS 三盘最好 |
| WDC NTFS | 2.651s | 0.159s | 6.0% | 稳定居中 |
| Seagate NTFS | **2.981s** | 0.540s | 18.1% | 均值最慢,tail 风险最大 |

因此最终选型不能只看 v3 单次 ranking。更可靠的结论是：**BIWIN 路径最快；独立 NTFS 数据盘里 ZHITAI 最好，WDC 次之，Seagate 需要警惕尾延迟**。

### 3.4 raw disk 上限远高于 sglang 实际利用率

fio 直接测盘得到的顺序读上限：

| 盘 | 1 thread 1MB seq |
|---|---:|
| BIWIN ext4 | **4765 MB/s** |
| ZHITAI NTFS | 3616 MB/s |
| Seagate NTFS | 3032 MB/s |
| WDC NTFS | 2632 MB/s |

而 sglang L3 reload 的有效吞吐只有大约 **1-2%**。  
说明瓶颈不在 SSD 峰值，而在 **sglang reader + 文件系统 + IO 组织方式**。

多 run iostat 进一步说明了原因：

| 盘 | active read mean | read peak | r_await mean | r_await p99 | avg req size | util active |
|---|---:|---:|---:|---:|---:|---:|
| BIWIN | 295 MB/s | 1177 MB/s | 0.14 ms | 0.33 ms | 53 KB | 23.7% |
| WDC | 270 MB/s | 775 MB/s | 0.53 ms | 2.29 ms | 96 KB | 32.5% |
| Seagate | 315 MB/s | 649 MB/s | 0.65 ms | 2.07 ms | 113 KB | 38.2% |
| ZHITAI | 278 MB/s | 824 MB/s | 0.42 ms | 1.06 ms | 98 KB | 19.5% |

HiCache replay 的读请求不是 1MB 顺序大读，而是约 60-125KB 的块读。盘也没有长期打满，差异主要来自读延迟和 tail，而不是 fio 峰值带宽。

### 3.5 write_back 只对 cold 有小收益

Phase 3 v3 显示：

- `write_back` 比 `write_through` 的 cold TTFT 快约 **37ms**
- 但 6 round 场景里，L3 worker 没有形成稳定落盘
- 对中短测试，write_back 不构成明显优势

## 4. 数据边界

### 4.1 可直接用于结论的测试

- Phase 6 fio
- Phase 7 multiprompt + replay
- Phase 2/3/4/5 的 v3 重跑
- Phase 1 smoke test

### 4.2 只能作为历史参考的测试

- Phase 0 LMCache baseline
- Phase 2/4/5 的 v2 原始数据

Phase 0 的价值是证明 LMCache offload 能显著降低重复请求 TTFT。  
Phase 2/4/5 v2 的价值是保留方法学和历史对照。

它们不用于最终盘排名。原因不是“数据全错”，而是它们主要反映 **缓存命中路径**，不能代表强制 L3 读盘性能。

### 4.3 mount 事故的真实影响

前期有 3 块 NTFS 盘没真正挂载，导致 iostat 和盘位映射出现误读。  
但 v3 重跑已经证明：

- TTFT spread 的大方向没变
- 结论不是 mount 事故伪造出来的
- 真正需要修正的是对 iostat 的解释

## 5. 结论

### 5.1 AI SSD 选型结论

| 场景 | 推荐 |
|---|---|
| 系统盘/root 路径 + 频繁 reload | **BIWIN ext4** |
| 独立 NTFS 数据盘 + 频繁 reload | **ZHITAI Ti600** |
| 容量优先且能接受中等 reload | **WDC** |
| tail latency 敏感 | 谨慎使用 **Seagate** |
| L2 hit 为主 | 盘型不敏感,优先加 host RAM |

### 5.2 最终排序

1. **BIWIN X570 (ext4)**
2. **ZHITAI Ti600 (NTFS)**
3. **WDC WDS960G2G0C (NTFS)**
4. **Seagate ZP1000GV30012 (NTFS)**

这个排序把 BIWIN 作为“系统盘 page-cache 路径”单独看。若只比较独立 NTFS 数据盘，排序是 **ZHITAI → WDC → Seagate**。

### 5.3 对 AI SSD 产品的反推

- 只堆峰值带宽不够
- 选型要看 **L3 reload latency**
- KV Cache offload 本身有明确收益，LMCache 已经测到约 23× TTFT 加速
- 文件系统实现和读路径组织方式，比 SSD 型号本身更容易决定体验
- HiCache 当前读形态更像 60-125KB 小块读 + prefetch,不是大顺序读
- 如果系统设计能尽量避免 L2 miss，盘差会被大幅弱化

## 6. 建议

1. 后续继续以 **Phase 7 这种 L2 miss 触发法** 作为标准测法
2. 把 LMCache 和 HiCache 统一到一个评测框架里：
   - 区分 CPU 命中、L2 hit、L3 reload
   - 分别记录 TTFT、落盘量、读盘量
   - 避免把内存命中误判成 SSD 性能
3. 如果要做产品对比，优先补：
   - 更大模型（32B 级）
   - 更长 prompt
   - 多 client 并发 replay
4. 如果要优化真实体验，优先看：
   - 文件系统
   - reader 并发
   - page size / IO 组织方式

## 7. 相关文件

- [主报告](./REPORT.md)
- [LMCache 历史基线](./REPORT_LMCACHE.md)
- [Phase 7 multiprompt 报告](./docs/hicache-multiprompt-l2fill-2026-06-14.md)
- [Phase 2/4/5 v3 重跑](./docs/hicache-v3-mount-fixed-2026-06-15.md)
- [Phase 3 v3 策略对比](./docs/hicache-v3-policy-2026-06-15.md)
- [fio 基线](./docs/l3-fio-bench-2026-06-13.md)
