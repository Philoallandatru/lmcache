# AI SSD 预研项目总结

## 📊 实验概览

本项目对 AI SSD 在大模型推理场景下的性能进行了全面评估，涵盖了 4 个主要实验方向：

### 1. LMCache KV 缓存验证
- **模型**: Qwen3-4B-Instruct
- **配置**: GPU / CPU offload
- **结果**: CPU offload 达到 6379 tok/s，验证了 KV cache 离线存储的可行性

### 2. FastLLM Qwen3-30B 端到端推理
- **纯GPU双卡**: 184 tok/s ⭐ (最优方案)
- **CPU RAM offload**: 33 tok/s (5.5x 慢)
- **SSD offload**: 6.5-7.9 tok/s (23-28x 慢)

### 3. SGlang 多磁盘性能对比
- **BIWIN**: 8.1 tok/s (最快)
- **WDC**: 5.7 tok/s
- **Seagate**: 7.8 tok/s
- **ZHITAI**: 7.7 tok/s

### 4. IO 模式深度分析
- **读取**: BIWIN 38GB, WDC 13GB, 其他盘 ~6GB
- **写入**: 所有盘 ~100GB (接近)
- **Burst 分析**: WDC 最多 (23次), BIWIN 次之 (26次)
- **延迟**: 全部 <1ms (磁盘性能未饱和)

---

## 🎯 核心发现

### 1. SSD 性能严重浪费 ⚠️
- FIO 基准测试: **290K IOPS** (BIWIN)
- 实际推理使用: **7-8 tok/s** (仅 1-2% 利用率)
- **瓶颈**: 应用层 MoE expert 加载策略，而非磁盘硬件

### 2. 纯 GPU 方案压倒性优势 ✅
```
纯GPU:    184 tok/s  ████████████████████████
CPU RAM:   33 tok/s  ████
SSD:      6.5 tok/s  █
```

### 3. 磁盘间性能差异小 (<20%)
- 主要受文件系统影响 (ext4 vs NTFS)
- Page cache 掩盖了磁盘硬件差异
- NTFS 冷启动退化严重 (1.4-1.8x)

### 4. LMCache 技术验证成功 ✅
- CPU offload 保持高吞吐 (6400 tok/s)
- KV cache 离线存储方案可行

---

## 📈 可视化图表

本项目生成了 17 个分析图表：

### 新生成 (2026-06-24)
1. **lmcache_comparison.png** - LMCache 吞吐量与延迟对比
2. **io_patterns_complete.png** - IO 模式完整分析 (6子图)
3. **fastllm_comparison.png** - FastLLM 性能对比
4. **sglang_metrics.png** - SGlang 多磁盘指标

### 历史图表 (2026-06-15)
- FIO 带宽/IOPS/延迟基准
- HiCache 冷热启动对比
- IO 时序分析
- Burst 模式分析
- 决策雷达图
- 等共 13 个图表

所有图表位于: `results/plots/`

---

## 📁 项目结构

```
ai_ssd_prestudy/
├── reports/
│   ├── complete_analysis_report_20260624_073928.md  (完整报告)
│   └── ai-ssd-real-offloading-investigation-report-2026-06-17.md
├── results/
│   ├── lmcache_validation/          (LMCache 实验数据)
│   ├── fastllm-2026-06-23/          (FastLLM 对比数据)
│   ├── plots/                       (17 个图表)
│   ├── sglang_metrics_summary.json
│   └── io_pattern_analysis.json
├── scripts/
│   ├── generate_complete_report.py  (报告生成器)
│   ├── run_lmcache_validation.py    (LMCache 基准测试)
│   └── [其他 30+ 脚本]
└── lmcache_repro/logs/              (原始日志)
```

---

## 🚀 快速开始

### 查看完整报告
```bash
cat reports/complete_analysis_report_20260624_073928.md
```

### 重新生成报告
```bash
/home/ficus/llm/.venv/bin/python3 scripts/generate_complete_report.py
```

### 运行 LMCache 验证
```bash
python3 scripts/run_lmcache_validation.py --only lmcache_cpu
```

---

## 💡 建议与结论

### 生产环境推荐方案
1. **首选**: 纯 GPU 方案 (双卡/多卡)
   - 性能最优 (184 tok/s)
   - 成本效益最好
   
2. **备选**: CPU RAM offload
   - 适合内存充足场景
   - 性能可接受 (33 tok/s)

3. **不推荐**: SSD offload
   - 性能差 (6.5-7.9 tok/s)
   - 浪费 SSD 硬件能力
   - 瓶颈在软件层

### 优化方向
- 优化 MoE expert 加载策略 (批量 IO)
- 改进 page cache 利用
- 考虑 Direct IO + 预加载
- 评估 GPU Direct Storage

---

## 📝 相关文档

- [完整分析报告](reports/complete_analysis_report_20260624_073928.md)
- [AI SSD 调研报告](reports/ai-ssd-real-offloading-investigation-report-2026-06-17.md)
- [FastLLM 对比数据](results/fastllm-2026-06-23/)
- [LMCache 验证数据](results/lmcache_validation/)

---

**最后更新**: 2026-06-24  
**项目状态**: ✅ 完成  
**Git 提交**: d5c2260
