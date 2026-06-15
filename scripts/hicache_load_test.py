#!/usr/bin/env python3
"""scripts/hicache_load_test.py
简化的 HiCache 多轮负载测试 client (按 benchmark/hicache/README.md L11-22 精神简化).

核心模式: 1 client × N rounds = 1 cold + (N-1) warm,同 prompt,测 TTFT 加速比.
- request-length 7000 tokens (与 LMCache REPORT 7000 对齐)
- output-length 64 tokens (突出 prefill 占比)
- 输出到 --log-file 的 JSONL

用法: python hicache_load_test.py --endpoint http://127.0.0.1:30000 \\
        --num-rounds 6 --prompt-tokens 7000 --output-tokens 64

并发模式: python hicache_load_test.py --endpoint ... \\
        --concurrent-clients 4 --drop-caches-every-round \\
        --num-rounds 3 \\
        # 4 client 同时发同一 prompt, 每轮前 drop_caches, 强制 N 路 L3 真读盘
        # 共 3 rounds × 4 client = 12 个请求, 评估多并发 L3 reload 吞吐
"""
import argparse
import json
import os
import time
import urllib.request
import urllib.error
import concurrent.futures


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
        try:
            body = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            body = '<no body>'
        return time.time() - t0, {}, '', f'HTTPError: {e.code} {e.reason} | body={body}'
    except urllib.error.URLError as e:
        return time.time() - t0, {}, '', f'URLError: {e.reason}'
    except Exception as e:
        return time.time() - t0, {}, '', f'Error: {type(e).__name__}: {e}'


def try_real_tokens(model_path, target_len, seed=42):
    """用 transformers tokenizer 生成精确 token 长度的 prompt.
    seed 不同 → 加不同前缀, 生成不同 token 序列 (但长度一致).
    """
    try:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        # 用一段重复的中文文本,确保 tokenizer 能精确产生 target_len
        base_text = "请详细回答以下问题。" * 100  # ~100 中文字符 ≈ 100-200 tokens
        # seed 不同 → 用不同 prefix 让 token 序列不一致
        # (tokenizer 对相同输入产生相同输出, 加 prefix 让 prefix 变化)
        prefix_text = f"[prompt-{seed:08d}] "
        full_prefix = prefix_text * 20  # ~600 chars
        # 计算需要重复多少次
        sample_ids = tok.encode(base_text)
        repeat = max(1, target_len // len(sample_ids))
        full_text = (base_text * repeat)[:5000]  # cap at 5000 chars
        # prefix 加在前面, 让每个 prompt token 序列不同
        full_text = (full_prefix + full_text)[:5000]
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
        return f"[seed{seed}] " + ("请详细描述以下技术主题 " * 7000)


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
    ap.add_argument("--drop-caches-every-round", action="store_true",
                    help="每轮请求前 sudo drop_caches (强制 L2 miss, 暴露真 L3 读盘延迟)")
    ap.add_argument("--concurrent-clients", type=int, default=1,
                    help="每轮发 N 个并发请求 (N=1 即单 client 串行; N>1 模拟多路 L3 reload)")
    # Phase7 多 prompt 累积测 (L2 filling + replay):
    #   --num-prompts N: 跑 N 个不同 prompt (每个 prompt-tokens 长), 不再重复同 prompt
    #   --replay-prompt-id 0: 在 N 个 unique prompt 跑完后, 再回放 prompt 0 (测 L2 miss → L3 read)
    #   与 --num-rounds 互斥 (用 --num-prompts 时 --num-rounds 被忽略)
    ap.add_argument("--num-prompts", type=int, default=0,
                    help="跑 N 个不同 prompt (默认 0 = 用 --num-rounds 同 prompt 模式)")
    ap.add_argument("--replay-prompt-id", type=int, default=-1,
                    help="多 prompt 模式: 在 unique 跑完后, 回放指定 ID 的 prompt (默认 -1 = 不回放)")
    ap.add_argument("--log-file", required=True, help="输出 JSONL")
    args = ap.parse_args()

    print(f"[hicache_load_test] endpoint={args.endpoint}")
    print(f"[hicache_load_test] num_rounds={args.num_rounds} prompt_tokens={args.prompt_tokens}")
    print(f"[hicache_load_test] output_tokens={args.output_tokens} rate={args.request_rate}")
    if args.num_prompts > 0:
        print(f"[hicache_load_test] MULTI-PROMPT MODE: num_prompts={args.num_prompts} replay_id={args.replay_prompt_id}")

    # 决定 payload 类型 (chat vs generate)
    is_chat = '/chat/completions' in args.endpoint

    # 生成 prompt 列表:
    #   - 默认 (num_prompts=0): 1 个固定 prompt, 跑 num_rounds 轮
    #   - num_prompts>0: num_prompts 个不同 prompt, 各跑 1 次
    #     - replay_prompt_id >= 0: 在 N 个跑完后, 再回放该 ID 的 prompt
    prompts = []  # list of (prompt_id, prompt_text, label)
    if args.num_prompts > 0:
        for pid in range(args.num_prompts):
            p = try_real_tokens(args.model_path, args.prompt_tokens, seed=42 + pid)
            prompts.append((pid, p, f"p{pid}"))
        if args.replay_prompt_id >= 0 and args.replay_prompt_id < len(prompts):
            rid, rp, _ = prompts[args.replay_prompt_id]
            prompts.append((rid, rp, f"replay_p{rid}"))
        print(f"[hicache_load_test] generated {args.num_prompts} unique prompts"
              f"{'+ 1 replay' if args.replay_prompt_id >= 0 else ''}, "
              f"each {args.prompt_tokens} tokens")
    else:
        prompt_text = try_real_tokens(args.model_path, args.prompt_tokens, seed=42)
        prompts.append((-1, prompt_text, "cold") if False else (-1, prompt_text, None))  # None label 由 round 决定
        # 旧模式: 单 prompt 跑 num_rounds 轮, round 0 = cold, round 1+ = warm_N
        # 把 round labels 展开成 prompts
        round_prompts = []
        for r in range(args.num_rounds):
            label = "cold" if r == 0 else f"warm_{r}"
            round_prompts.append((r, prompt_text, label))
        prompts = round_prompts
        print(f"[hicache_load_test] single prompt x {args.num_rounds} rounds mode")

    def make_payload(prompt_text):
        if is_chat:
            return build_chat_payload(prompt_text, args.output_tokens, args.model_path)
        return build_generate_payload(prompt_text, args.output_tokens)

    # 跑 requests: 每个 prompt 1 次 (multi-prompt) 或 同 prompt N rounds
    os.makedirs(os.path.dirname(args.log_file) or '.', exist_ok=True)
    results = []
    n_clients = max(1, args.concurrent_clients)
    with open(args.log_file, 'w') as fout:
        for (idx, prompt_text, label) in prompts:
            if label is None:
                # 旧 num-rounds 模式: round 0 = cold, 1+ = warm_N
                label = "cold" if idx == 0 else f"warm_{idx}"
            print(f"\n[{idx+1}/{len(prompts)}] prompt_id={idx} label={label} request (clients={n_clients})")

            # drop_caches 逻辑:
            #   - 默认 (无 flag): 完全不 drop
            #   - --drop-caches-before-warm1: 只在 idx=1 前 drop (旧 num-rounds 模式)
            #   - --drop-caches-every-round: 每轮 idx=0..N-1 前都 drop
            #   - multi-prompt 模式: 始终不在 unique prompt 前 drop (那是 cold L1 命中)
            #                          只在 replay 前 drop (逼 L2 读 L3)
            should_drop = (
                (idx == 1 and args.drop_caches_before_warm1) or
                (args.drop_caches_every_round) or
                (label and label.startswith('replay_'))
            )
            if should_drop:
                print(f"[{label}] drop_caches...")
                os.system("sync && sudo -n sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>&1")

            if n_clients == 1:
                # 单 client 串行模式 (原行为)
                payload = make_payload(prompt_text)
                latency, usage, content, err = call_endpoint(args.endpoint, payload)
                if err:
                    print(f"[{label}] ERROR: {err}")
                    entry = {"round": idx, "prompt_id": idx, "label": label, "client_id": 0, "error": err}
                else:
                    pt = usage.get('prompt_tokens', -1)
                    ct = usage.get('completion_tokens', -1)
                    ttft_proxy = latency
                    print(f"[{label}] latency={latency:.3f}s prompt_tokens={pt} "
                          f"completion_tokens={ct}")
                    entry = {
                        "round": idx,
                        "prompt_id": idx,
                        "label": label,
                        "client_id": 0,
                        "latency_s": latency,
                        "prompt_tokens": pt,
                        "completion_tokens": ct,
                        "content_preview": content[:80],
                    }
                fout.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fout.flush()
                results.append(entry)
            else:
                # N client 并发模式: 用 ThreadPoolExecutor 同时发 n_clients 个请求
                #   - 全部用同一 prompt (同 KV cache, 才能测 N 路并发 reload)
                #   - 收集所有 client 的 latency 后再进下一 round
                with concurrent.futures.ThreadPoolExecutor(max_workers=n_clients) as ex:
                    futures = [
                        ex.submit(call_endpoint, args.endpoint, make_payload(prompt_text))
                        for _ in range(n_clients)
                    ]
                    round_entries = []
                    for cid, fut in enumerate(concurrent.futures.as_completed(futures)):
                        latency, usage, content, err = fut.result()
                        if err:
                            print(f"[{label} cid={cid}] ERROR: {err}")
                            entry = {"round": idx, "prompt_id": idx, "label": label, "client_id": cid, "error": err}
                        else:
                            pt = usage.get('prompt_tokens', -1)
                            ct = usage.get('completion_tokens', -1)
                            print(f"[{label} cid={cid}] latency={latency:.3f}s "
                                  f"prompt_tokens={pt} completion_tokens={ct}")
                            entry = {
                                "round": idx,
                                "prompt_id": idx,
                                "label": label,
                                "client_id": cid,
                                "latency_s": latency,
                                "prompt_tokens": pt,
                                "completion_tokens": ct,
                                "content_preview": content[:80],
                            }
                        fout.write(json.dumps(entry, ensure_ascii=False) + "\n")
                        fout.flush()
                        round_entries.append(entry)
                    # 用 round 内最大 latency 作该 round 的代表值 (最慢的 client 决定吞吐)
                    valid = [e['latency_s'] for e in round_entries if 'latency_s' in e]
                    if valid:
                        agg_entry = {
                            "round": idx,
                            "prompt_id": idx,
                            "label": label,
                            "client_id": -1,  # 标记这是 round 聚合
                            "is_aggregate": True,
                            "n_clients": len(valid),
                            "latency_s": max(valid),       # 关键: max = 最慢 client
                            "latency_mean_s": sum(valid)/len(valid),
                            "latency_min_s": min(valid),
                            "latency_max_s": max(valid),
                        }
                        fout.write(json.dumps(agg_entry, ensure_ascii=False) + "\n")
                        fout.flush()
                        round_entries.append(agg_entry)
                    results.extend(round_entries)

            if idx < len(prompts) - 1 and args.request_rate > 0:
                sleep_s = 1.0 / args.request_rate
                time.sleep(sleep_s)

    # 总结
    #   - 单 client 模式: 简单列出每个 result (label = cold/warm_N/pN/replay_pN)
    #   - 多 client 模式: 列出每个 round 的 aggregate entry
    #   - multi-prompt 模式: 单独打印 cold / 每个 pN / replay 三段对比
    print("\n========== Summary ==========")
    if n_clients == 1:
        for i, r in enumerate(results):
            if 'latency_s' in r:
                print(f"  [{i+1}/{len(results)}] {r['label']}: {r['latency_s']:.3f}s")
        # multi-prompt 模式额外对比: cold (p0) vs 各 pN vs replay_p0
        if args.num_prompts > 0:
            print("\n--- Multi-prompt analysis ---")
            p0 = next((r for r in results if r.get('label') == 'p0'), None)
            replay = next((r for r in results if r.get('label', '').startswith('replay_')), None)
            if p0 and replay:
                ratio = replay['latency_s'] / p0['latency_s']
                print(f"  p0 (cold fill):    {p0['latency_s']:.3f}s")
                print(f"  replay_{args.replay_prompt_id} (L2 miss→L3):  {replay['latency_s']:.3f}s")
                print(f"  replay/cold ratio: {ratio:.2f}x  (1.0 = same; >1.0 = L3 reload 慢)")
    else:
        # 取每个 round 的 aggregate entry (client_id == -1)
        agg_per_round = [e for e in results if e.get('is_aggregate')]
        if not agg_per_round:
            print("(no aggregate entries found)")
        else:
            for i, r in enumerate(agg_per_round):
                print(f"  [{i+1}/{len(agg_per_round)}] {r['label']} (N={r['n_clients']}): "
                      f"max={r['latency_max_s']:.3f}s mean={r['latency_mean_s']:.3f}s")
    print(f"Results written to: {args.log_file}")


if __name__ == "__main__":
    main()