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

| 阶段 | 主题 | 结果用途 | 结论有效性 |
|---|---|---|---|
| Phase 0 | vLLM + LMCache 历史基线 | 验证 offload 链路 | 参考 |
| Phase 1 | sglang HiCache smoke test | 验证环境、参数、路径行为 | 有效 |
| Phase 2 | 4B write_through 4 盘 | 看 L2 hit 下的 TTFT | 仅作 L2-hit baseline |
| Phase 3 | write_through vs write_back | 看写策略差异 | 有效 |
| Phase 4 | 14B-AWQ 4 盘 | 更大模型下的 L2-hit 表现 | 仅作 L2-hit baseline |
| Phase 5 | 4 client + drop_caches | 验证并发与缓存行为 | 有效 |
| Phase 6 | fio direct=1 | 测 raw disk 上限 | 有效 |
| Phase 7 | multiprompt + replay | 强制 L2 miss，暴露盘差 | **核心选型依据** |
| v3 重跑 | mount 修正后复核 | 验证前期结论是否受 mount 事故影响 | 有效 |

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

Phase 7 用 20 个不同 prompt 把 L2 填满，再 replay p0，终于把盘差拉开：

| 盘 | replay_p0 | 排名 |
|---|---:|---|
| BIWIN ext4 | **1.718s** | 1 |
| ZHITAI NTFS | 2.677s | 2 |
| Seagate NTFS | 2.773s | 3 |
| WDC NTFS | **3.816s** | 4 |

4 盘 spread 达到 **2.098s**。这是本次预研最重要的数据。

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
| 单盘 + 频繁 reload | **BIWIN ext4** |
| 多盘 + 容量优先 | **ZHITAI / Seagate** |
| 大 L3、低延迟敏感 | 避免 **WDC** |

### 5.2 最终排序

1. **BIWIN X570 (ext4)**
2. **ZHITAI Ti600 (NTFS)**
3. **Seagate ZP1000GV30012 (NTFS)**
4. **WDC WDS960G2G0C (NTFS)**

### 5.3 对 AI SSD 产品的反推

- 只堆峰值带宽不够
- 选型要看 **L3 reload latency**
- KV Cache offload 本身有明确收益，LMCache 已经测到约 23× TTFT 加速
- 文件系统实现和读路径组织方式，比 SSD 型号本身更容易决定体验
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
