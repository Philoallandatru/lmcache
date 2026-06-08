"""
压测客户端: 触发 LMCache 真实 KV cache offload + reload
关键设计:
  - 长上下文(~7000 tokens) 逼出 KV cache 大小, 单请求 ~50-200MB chunk 量级
  - 第一轮 cold: LMCache miss -> store 到 disk 触发写入
  - 第二轮 warm: LMCache hit -> load from disk 触发读取
  - 测 TTFT 加速比 (这是 LMCache 文档标准 demo)
  - 同时发多个不同前缀的请求, 让 offload 流量更真实
"""
import time
import json
import os
import sys
import random
import string
from openai import OpenAI
from transformers import AutoTokenizer

BASE_URL = "http://localhost:8000/v1"
MODEL_NAME = "Qwen3-4B-Instruct-2507"
TOKENIZER_DIR = "/home/ficus/llm/models/Qwen/Qwen3-4B-Instruct-2507"

# 输出目录
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "/home/ficus/llm/infer/ai_ssd_prestudy/results/cold"
os.makedirs(OUT_DIR, exist_ok=True)
LOG_FILE = os.path.join(OUT_DIR, "ttft_log.jsonl")

client = OpenAI(api_key="dummy", base_url=BASE_URL)
tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_DIR)


def random_chunk(n: int) -> str:
    """生成随机文本, 模拟长上下文"""
    alphabet = string.ascii_letters + string.digits + " "
    return "".join(random.choices(alphabet, k=n))


def build_prompt(target_tokens: int = 7000) -> tuple[str, str]:
    """构造 prompt, 返回 (full_prompt, prefix_id)
    prefix_id 用来标识同一上下文, 同一 prefix 才能命中 LMCache"""
    prefix_id = "ctx_" + "".join(random.choices(string.ascii_lowercase, k=8))
    body = random_chunk(target_tokens * 5)  # 字符数 5x
    # 截到目标 token 数
    ids = tokenizer.encode(body)
    body = tokenizer.decode(ids[:target_tokens])
    question = "\n\n请用两句话总结上述文字的核心内容."
    return prefix_id, body + question


def query_and_measure(prompt: str) -> dict:
    """发请求, 测 TTFT, 返回 {ttft, total, prompt_tokens, completion_tokens}"""
    t0 = time.perf_counter()
    ttft = None
    completion_text = ""
    stream = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=128,
        temperature=0.0,
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta is not None:
            if ttft is None:
                ttft = time.perf_counter() - t0
            completion_text += delta
    total = time.perf_counter() - t0
    return {
        "ttft": ttft,
        "total": total,
        "completion_tokens": len(tokenizer.encode(completion_text)),
    }


def run_round(round_name: str, num_warm: int = 3, num_replays: int = 3):
    """跑一轮: 1 cold + N warm
    关键: cold 和 warm 必须用**完全相同的 prompt** 才能触发 LMCache prefix cache hit
    第一次 LMCache miss -> 完整计算 KV cache 并 offload 到 disk
    后续 LMCache hit -> 从 disk reload KV cache, 跳过 prefill, TTFT 应大幅下降
    """
    print(f"\n=== Round: {round_name} ===", flush=True)
    results = []
    # 同一个 prompt, 跑 num_warm+1 次
    prefix_id, prompt = build_prompt(target_tokens=7000)
    prompt_tokens = len(tokenizer.encode(prompt))
    for i in range(num_warm + 1):  # 第一个是 cold miss, 之后是 warm hit
        phase = "cold" if i == 0 else "warm"
        print(f"  [{phase}] req{i}: prefix={prefix_id} prompt_tokens={prompt_tokens}", flush=True)
        rec = query_and_measure(prompt)
        rec["round"] = round_name
        rec["phase"] = phase
        rec["req_idx"] = i
        rec["prefix_id"] = prefix_id
        rec["prompt_tokens"] = prompt_tokens
        rec["timestamp"] = time.time()
        print(f"    ttft={rec['ttft']:.3f}s total={rec['total']:.3f}s", flush=True)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
        results.append(rec)
    return results


if __name__ == "__main__":
    # 等 vllm 就绪
    print("Waiting for vllm...", flush=True)
    for _ in range(120):
        try:
            client.models.list()
            break
        except Exception:
            time.sleep(1)
    else:
        print("vllm 启动超时", file=sys.stderr)
        sys.exit(1)
    print("vllm ready", flush=True)

    # 跑轮次(可由参数控制)
    rounds = sys.argv[2:] if len(sys.argv) > 2 else ["r1"]
    for r in rounds:
        run_round(r)
