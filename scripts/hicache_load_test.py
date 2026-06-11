#!/usr/bin/env python3
"""scripts/hicache_load_test.py
简化的 HiCache 多轮负载测试 client (按 benchmark/hicache/README.md L11-22 精神简化).

核心模式: 1 client × N rounds = 1 cold + (N-1) warm,同 prompt,测 TTFT 加速比.
- request-length 7000 tokens (与 LMCache REPORT 7000 对齐)
- output-length 64 tokens (突出 prefill 占比)
- 输出到 --log-file 的 JSONL

用法: python hicache_load_test.py --endpoint http://127.0.0.1:30000 \
        --num-rounds 6 --prompt-tokens 7000 --output-tokens 64
"""
import argparse
import json
import os
import time
import urllib.request
import urllib.error


def gen_prompt_tokens(tokenizer, target_len):
    """生成 ~target_len 个随机 token ids 的 prompt."""
    import random
    random.seed(42)
    vocab_size = tokenizer.vocab_size if hasattr(tokenizer, 'vocab_size') else 100000
    # 用普通 ASCII token 范围,避免触发 tokenizer 特殊路径
    ids = [random.randint(100, min(vocab_size - 100, 50000)) for _ in range(target_len)]
    return ids


def build_chat_payload(prompt_text, output_tokens, model_path):
    """OpenAI chat completions payload (sglang 0.5.13 supports this endpoint)."""
    return {
        "model": model_path,
        "messages": [{"role": "user", "content": prompt_text}],
        "max_tokens": output_tokens,
        "temperature": 0.7,
    }


def build_generate_payload(prompt_text, output_tokens):
    """sglang native /generate payload."""
    return {
        "text": prompt_text,
        "sampling_params": {
            "max_new_tokens": output_tokens,
            "temperature": 0.7,
        }
    }


def detect_endpoint(ep):
    """决定用 /v1/chat/completions 还是 /generate (sglang native)."""
    return ep.rstrip('/')


def call_endpoint(endpoint, payload, timeout=300):
    """发请求, 返回 (latency_s, usage, content_or_error)."""
    url = endpoint
    headers = {"Content-Type": "application/json"}
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        latency = time.time() - t0
        # 提取 usage 和 content
        usage = body.get('usage', {}) or {}
        content = ''
        if 'choices' in body and body['choices']:
            choice = body['choices'][0]
            if 'message' in choice:
                content = choice['message'].get('content', '')
            elif 'text' in choice:
                content = choice['text']
        return latency, usage, content, None
    except urllib.error.HTTPError as e:
        return time.time() - t0, {}, '', f'HTTPError: {e.code} {e.reason}'
    except urllib.error.URLError as e:
        return time.time() - t0, {}, '', f'URLError: {e.reason}'
    except Exception as e:
        return time.time() - t0, {}, '', f'Error: {type(e).__name__}: {e}'


def try_real_tokens(model_path, target_len):
    """用 transformers tokenizer 生成精确 token 长度的 prompt."""
    try:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        # 用一段重复的中文文本,确保 tokenizer 能精确产生 target_len
        base_text = "请详细回答以下问题。" * 100  # ~100 中文字符 ≈ 100-200 tokens
        # 计算需要重复多少次
        sample_ids = tok.encode(base_text)
        repeat = max(1, target_len // len(sample_ids))
        full_text = (base_text * repeat)[:5000]  # cap at 5000 chars
        ids = tok.encode(full_text)
        if len(ids) > target_len:
            ids = ids[:target_len]
        elif len(ids) < target_len:
            # 补足
            extra_text = "请继续。" * (target_len - len(ids))
            ids += tok.encode(extra_text)[:target_len - len(ids)]
        return tok.decode(ids)
    except Exception as e:
        print(f"[warn] tokenizer 失败: {e}, 用简单字符串 fallback")
        return "请详细描述以下技术主题 " * 7000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://127.0.0.1:30000/v1/chat/completions",
                    help="sglang endpoint")
    ap.add_argument("--model-path", default="/home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507")
    ap.add_argument("--num-rounds", type=int, default=6, help="1 cold + (N-1) warm")
    ap.add_argument("--prompt-tokens", type=int, default=7000)
    ap.add_argument("--output-tokens", type=int, default=64)
    ap.add_argument("--request-rate", type=float, default=1.0)
    ap.add_argument("--drop-caches-before-warm1", action="store_true",
                    help="在第 2 个请求前 sudo drop_caches")
    ap.add_argument("--log-file", required=True, help="输出 JSONL")
    args = ap.parse_args()

    print(f"[hicache_load_test] endpoint={args.endpoint}")
    print(f"[hicache_load_test] num_rounds={args.num_rounds} prompt_tokens={args.prompt_tokens}")
    print(f"[hicache_load_test] output_tokens={args.output_tokens} rate={args.request_rate}")

    # 生成固定 prompt (所有 round 用同一 prompt)
    prompt_text = try_real_tokens(args.model_path, args.prompt_tokens)
    print(f"[hicache_load_test] prompt char count: {len(prompt_text)}")

    # 决定 payload 类型
    is_chat = '/chat/completions' in args.endpoint
    if is_chat:
        payload_fn = lambda: build_chat_payload(prompt_text, args.output_tokens, args.model_path)
    else:
        payload_fn = lambda: build_generate_payload(prompt_text, args.output_tokens)

    # 跑 N rounds
    os.makedirs(os.path.dirname(args.log_file) or '.', exist_ok=True)
    results = []
    with open(args.log_file, 'w') as fout:
        for r in range(args.num_rounds):
            label = "cold" if r == 0 else f"warm_{r}"
            print(f"\n[{r+1}/{args.num_rounds}] {label} request")

            # round 2 (warm_1) 之前 drop_caches
            if r == 1 and args.drop_caches_before_warm1:
                print(f"[{label}] drop_caches...")
                os.system("sync && sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>&1")

            payload = payload_fn()
            latency, usage, content, err = call_endpoint(args.endpoint, payload)
            if err:
                print(f"[{label}] ERROR: {err}")
                entry = {"round": r, "label": label, "error": err}
            else:
                pt = usage.get('prompt_tokens', -1)
                ct = usage.get('completion_tokens', -1)
                ttft_proxy = latency  # 用全 latency 作 TTFT proxy (非流式)
                print(f"[{label}] latency={latency:.3f}s prompt_tokens={pt} "
                      f"completion_tokens={ct}")
                entry = {
                    "round": r,
                    "label": label,
                    "latency_s": latency,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "content_preview": content[:80],
                }
            fout.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fout.flush()
            results.append(entry)

            if r < args.num_rounds - 1 and args.request_rate > 0:
                sleep_s = 1.0 / args.request_rate
                time.sleep(sleep_s)

    # 总结
    print("\n========== Summary ==========")
    cold = results[0]
    print(f"Cold ({cold['label']}): {cold['latency_s']:.3f}s")
    for r in results[1:]:
        if 'latency_s' in r:
            speedup = cold['latency_s'] / r['latency_s']
            print(f"{r['label']}: {r['latency_s']:.3f}s (speedup {speedup:.2f}x)")
    print(f"Results written to: {args.log_file}")


if __name__ == "__main__":
    main()