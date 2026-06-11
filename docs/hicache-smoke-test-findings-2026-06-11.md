# SGLang HiCache × AI SSD 预研 — Smoke Test 发现 (Phase 1)

> **日期:** 2026-06-11
> **状态:** ✅ 冒烟测试通过,HiCache L3 file backend 端到端可用

---

## 1. 实际安装情况

**SGLang 已成功安装到 `~/llm/.venv/`**(与 vllm0.22.1 + lmcache0.4.6 共存)。

| 项目 | 值 |
|---|---|
| SGLang 版本 | **0.5.13** (PyPI 最新 stable) |
| 安装命令 | `pip install "sglang[all]==0.5.13" --upgrade` |
| 兼容 torch | **2.11.0+cu130** (与现有 vllm 时代环境完全匹配) |
| 安装耗时 | ~80s |
| 关键依赖 | `flashinfer_python[cu13]==0.6.12`, `cuda-python>=13.0`, `torch==2.11.0` |

**重要结论**: 不需要新建独立 venv。SGLang0.5.13 的官方依赖 `torch==2.11.0`
完美匹配现有 vllm 时代的 torch 2.11+cu130 环境,可以直接复用 `~/llm/.venv/`。

---

## 2. SGLang0.5.13 vs 官方文档的参数差异

读了官方 `hicache_best_practices.md` 的例子 (L80-98) 发现,**sglang 0.5.13 CLI 有改动**:

| 官方文档 (最佳实践) | sglang 0.5.13 实际 | 备注 |
|---|---|---|
| `--max-model-len 8192` | **`--context-length 8192`** | 改名 |
| `--hicache-storage-backend file` | 同 | ✅ |
| `--enable-hierarchical-cache` | 同 | ✅ |
| `--hicache-ratio 2` | 同 | ✅ |
| `--hicache-mem-layout page_first_direct` | 同 | ✅ |
| `--hicache-io-backend direct` | 同 | ✅ |
| `--hicache-write-policy write_through` | 同 | ✅ |
| `--hicache-storage-prefetch-policy timeout` | 同 | ✅ |

启动时间:Qwen3-4B 在 RTX 5080 上从冷启动到 `/v1/models` 就绪 = **12-32 秒** (典型 25 秒)。

---

## 3. 关键发现 — `file-storage-path` 不工作

启动时传 `--file-storage-path /path/to/cache`,**但实际 HiCacheFile 后端仍创建在默认 `/tmp/hicache`**。

**原因** (从源码 `hicache_storage.py::HiCacheFile.__init__` L344 看到):
```python
self.file_path = envs.SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR.get() or file_path
```
源码只读 `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR` 环境变量,**忽略 CLI 参数**。

**实测 server_args 里** `file_storage_path` 字段存了我们传的路径,但 HiCacheFile 不读它。
这是 0.5.13 的小 bug,已在启动 server 时通过环境变量设:
```bash
export SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/path/to/cache
```

---

## 4. 实测 L3 落盘数据 (冷请求 4515 tokens)

| 指标 | 实测值 |
|---|---|
| **L3 文件数** | **71 个** |
| **每个文件大小** | **9,437,184 bytes = 9.0 MB**(完全相等) |
| **L3 总大小** | **639 MB** |
| **L2 host_used tokens** | 4480 (per `sglang:hicache_host_used_tokens`) |
| **L2 host_total tokens** | 41024 (per `sglang:hicache_host_used_tokens`) |
| **backuped tokens (到 L3)** | 4544 (per `sglang:backuped_tokens_total{storage_backend="file"}`) |
| **文件命名格式** | `{64-char-hash}_-home-ficus-llm-models-Qwen-Qwen3-4B-Instruct-2507_0_1.bin` |
| **理论对照** | 71 pages × 64 tokens/page = 4544 tokens ✅ |

**对比 LMCache** (来自 `~/llm/infer/ai_ssd_prestudy/REPORT.md` §3.2):

| 引擎 | L3 总大小 | 文件数 | 单文件大小 |
|---|---|---|---|
| LMCache (vLLM 0.22) | **0.95 GB** (chunk=256 tok) | 8 | **37.7 MB** |
| **HiCache (sglang 0.5.13)** | **639 MB** (page=64 tok) | **71** | **9.0 MB** |

- HiCache 文件粒度比 LMCache 细 **4.2 倍** (9.0 vs 37.7 MB)
- 总大小接近 (0.64 vs 0.95 GB,差异因为请求 tokens 不同:LMCache 7000 tokens,HiCache 4515 tokens)

---

## 5. TTFT 加速比实测 (Qwen3-4B,prompt=4515 tokens)

| Phase | 延迟 | 加速比 | 备注 |
|---|---|---|---|
| Cold | **1.220s** | 1.0× | 4515 tokens 全 prefill + 异步 L3 store |
| Warm #1 | **1.087s** | 1.12× | L3 reload (从 page cache) |
| Warm #2 | **0.903s** | 1.35× | L2 DRAM hit |
| Warm #3 | **0.860s** | 1.42× | L2 DRAM hit |

**观察**:
- 加速比 **温和** (1.1-1.4×),而非 LMCache 报告的 ~23×
- 原因:**Qwen3-4B 模型太小,生成 100 tokens 的 decode 时间占比高**,纯 prefill 加速被稀释
- 加速比从 warm #1 → #2 跳了一档 (1.12→1.35×),说明 L2 命中(主机 DRAM)开始生效

**对比 LMCache**: LMCache 时代报告 ~23× 加速比,HiCache 只 ~1.4×
- 根本原因:**测的是同模型但不同负载**。LMCache 测 7000 tokens,这里 4515 tokens
- **下一步**: 用 7000 tokens + 大量 decode 长度 复测,才能公平对比

---

## 6. HiCache metrics 完整列表 (从 `/metrics` 抓取)

```prometheus
# L3 file backend 写入总 token 数
sglang:backuped_tokens_total{storage_backend="file"} 4544.0

# L2 host DRAM 当前使用 token 数
sglang:hicache_host_used_tokens 4480.0

# L2 host DRAM 总容量
sglang:hicache_host_total_tokens 41024.0
```

可观察的三层行为:
- L1 (GPU VRAM): 通过 `sglang:gpu_cache_usage_perc` 看
- L2 (host DRAM): `sglang:hicache_host_used_tokens / hicache_host_total_tokens`
- L3 (file backend): `sglang:backuped_tokens_total{storage_backend="file"}`

---

## 7. 关键结论与下一步

### ✅ 已验证
1. SGLang 0.5.13 与现有 torch2.11+cu130 环境**完全兼容**
2. HiCache `file` 后端**真的能写盘**(71 个 9MB 文件 = 639MB)
3. 文件命名格式严格遵循源码 `hicache_storage.py` L356-364 规范
4. 三层架构 metrics 完整可用
5. L2 → L3 hit 链路 warm 加速 1.4× (Qwen3-4B 4515 tokens)

### ⚠️ 待解决
1. `--file-storage-path` CLI 参数不生效,需用 `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR` 环境变量
2. TTFT 加速比不如 LMCache 报告的高,需 7000 tokens 长 prompt 复测
3. `enable_cache_report` 字段在 OpenAI completions endpoint 未暴露,需要用 `generate` endpoint

### 🎯 下一步 (Phase 2)
1. 写 `hicache_serve.sh` 用环境变量设 cache dir (避免 CLI bug)
2. 写 `hicache_io_monitor.sh` 启 iostat + bpftrace
3. 跑 BIWIN 系统盘 baseline round (用 7000 tokens prompt)
4. 4 盘串行 driver
5. headline 报告 + git commit

---

## 8. 调试笔记(可复用)

### 8.1 快速 smoke 命令
```bash
source ~/llm/.venv/bin/activate
export SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/tmp/hicache_test

python -m sglang.launch_server \
    --model-path /home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507 \
    --port 30000 \
    --context-length 8192 \
    --mem-fraction-static 0.7 \
    --page-size 64 \
    --enable-hierarchical-cache \
    --hicache-ratio 2 \
    --hicache-size 0 \
    --hicache-mem-layout page_first_direct \
    --hicache-io-backend direct \
    --hicache-write-policy write_through \
    --hicache-storage-backend file \
    --hicache-storage-prefetch-policy timeout \
    --enable-metrics \
    --enable-cache-report
```

### 8.2 关键 endpoints
- `curl http://127.0.0.1:30000/v1/models` — 就绪检查
- `curl http://127.0.0.1:30000/v1/chat/completions` — 推理
- `curl http://127.0.0.1:30000/metrics` — Prometheus metrics (含 hicache_*, backuped_tokens)
- `curl http://127.0.0.1:30000/get_server_info` — 完整 server args + 状态

### 8.3 L3 落盘验证
```bash
ls -la /tmp/hicache/ | head
du -sh /tmp/hicache/
# Expected: 71 个 9.0 MB 的 .bin 文件
```