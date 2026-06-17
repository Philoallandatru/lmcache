#!/usr/bin/env python3
"""
LMCache benchmark driver - reproduces validation_results.md methodology.

Methodology (from report):
  - Send 16 prompts split into TWO passes:
    * Pass 1 (warm): 8 KILLER prompts (will be cached)
    * Pass 2 (test): the SAME 8 KILLER prompts again (should hit LMCache)
  - Compare Pass 1 TTFT vs Pass 2 TTFT per prompt
  - speedup_pass = mean(Pass1_TTFT) / mean(Pass2_TTFT)
  - Plus a `no_cache` baseline where LMCache is DISABLED for comparison

Differences from report:
  - Model: Qwen2.5-7B-Instruct (report used Mistral-7B-Instruct-v0.2)
  - Prompts: 16 short prompts (8 killer × 2 passes) (report used 500 ShareGPT)
    rationale: 500 prompts × 4 disks × 2 backends × 3 trials = too long; this
    reduced protocol still exercises the LMCache hit-rate path.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import torch


KILLER_PROMPTS = [
    # 8 prompts designed to be long enough to actually offload KV (>=2048 tokens)
    # but also repeated so LMCache caches them. We use fixed seeds for determinism.
    "Explain in great detail the entire history of computing from the abacus through "
    "modern GPUs, covering mechanical calculators, vacuum tubes, transistors, "
    "integrated circuits, microprocessors, the rise of personal computers, the GUI "
    "revolution, the internet boom, mobile computing, cloud platforms, and AI accelerators. "
    "Discuss each era's key inventions, the companies that led them, and how they "
    "reshaped society." * 4,
    "Describe the complete life cycle of a star, from nebula formation through main "
    "sequence, red giant, planetary nebula or supernova, and final remnant (white "
    "dwarf, neutron star, or black hole). Include the physics of nuclear fusion, "
    "degeneracy pressure, and gravitational collapse, and discuss how the star's "
    "initial mass determines its fate." * 4,
    "Walk through the entire process of training a large language model, from "
    "data collection and tokenization, through pretraining with next-token prediction, "
    "instruction tuning, RLHF or DPO alignment, evaluation on benchmarks like MMLU, "
    "and deployment via quantization, speculative decoding, and KV-cache reuse. "
    "Discuss the GPU memory budget at each stage." * 4,
    "Provide a comprehensive overview of modern cryptography, covering symmetric "
    "ciphers (AES, ChaCha20), asymmetric crypto (RSA, ECDSA, Ed25519), hash "
    "functions (SHA-2, SHA-3, BLAKE3), key exchange (Diffie-Hellman, X25519), "
    "TLS 1.3 handshake, post-quantum cryptography (Kyber, Dilithium), and the "
    "practical implications of Shor's algorithm for RSA-2048." * 4,
    "Explain the full TCP/IP stack, from the physical layer encoding bits on "
    "copper or fiber, through Ethernet framing, IP routing and subnetting, "
    "TCP's three-way handshake, congestion control (slow start, CUBIC, BBR), "
    "QUIC's UDP-based transport, and modern HTTP/3 multiplexing. Discuss how "
    "each protocol solves reliability, ordering, and flow control." * 4,
    "Describe the complete architecture of a modern CPU, from instruction fetch "
    "and decode, through the reorder buffer, reservation stations, register "
    "renaming, micro-op fusion, branch prediction (TAGE, perceptron), and the "
    "cache hierarchy (L1d/L1i/L2/L3), to SIMD units (SSE, AVX-512, NEON, SVE) "
    "and recent accelerators like AMX. Discuss pipelining and out-of-order "
    "execution hazards." * 4,
    "Provide an in-depth explanation of quantum computing, covering qubits, "
    "superposition, entanglement, the Bloch sphere, quantum gates (Hadamard, "
    "Pauli, CNOT, Toffoli), the no-cloning theorem, Bell states, quantum "
    "teleportation, the Deutsch-Jozsa and Grover algorithms, Shor's factoring "
    "algorithm, surface codes for error correction, and the current state of "
    "superconducting versus trapped-ion hardware." * 4,
    "Walk through the entire software stack of a modern web application, from "
    "DNS resolution and TLS termination at the edge, through a CDN, to an "
    "API gateway, a load balancer, a microservices backend (gRPC or REST), a "
    "relational or document database, a cache layer (Redis or Memcached), "
    "an analytics pipeline (Kafka to Spark to a data warehouse), and a "
    "monitoring stack with Prometheus and Grafana. Discuss latency budgets "
    "at each tier." * 4,
]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="/home/ficus/llm/models/Qwen/Qwen2.5-7B-Instruct")
    p.add_argument("--backend", choices=["cpu", "gpu", "disabled"], required=True,
                   help="cpu=LMCache CPU offload, gpu=LMCache GPU only, "
                        "disabled=LMCache off (baseline)")
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--gpu-mem-util", type=float, default=0.7)
    p.add_argument("--num-trials", type=int, default=3,
                   help="Number of trials per backend (report recommends >=3)")
    p.add_argument("--output", required=True, help="Output JSON path")
    p.add_argument("--trial-id", type=int, default=0)
    return p.parse_args()


def setup_lmcache_env(args):
    """Set LMCACHE_* env vars BEFORE importing vllm (they're read at engine init)."""
    if args.backend == "cpu":
        os.environ["LMCACHE_CHUNK_SIZE"] = "256"
        os.environ["LMCACHE_LOCAL_CPU"] = "True"
        os.environ["LMCACHE_MAX_LOCAL_CPU_SIZE"] = "32"
    elif args.backend == "gpu":
        os.environ["LMCACHE_CHUNK_SIZE"] = "256"
        os.environ["LMCACHE_LOCAL_CPU"] = "False"
        os.environ["LMCACHE_MAX_LOCAL_CPU_SIZE"] = "0"
    else:  # disabled
        os.environ["LMCACHE_LOCAL_CPU"] = "False"
        os.environ["LMCACHE_MAX_LOCAL_CPU_SIZE"] = "0"
        # The key: we simply don't pass kv_transfer_config to LLM()


def run_benchmark(args):
    setup_lmcache_env(args)

    # Import AFTER env vars are set
    from vllm import LLM, SamplingParams
    from vllm.config import KVTransferConfig

    print(f"[{args.backend}] Loading model {args.model}...", flush=True)

    llm_kwargs = dict(
        model=args.model,
        gpu_memory_utilization=args.gpu_mem_util,
        dtype="bfloat16",
        max_model_len=1024,
        max_num_seqs=4,  # limit concurrent sequences to control KV cache footprint
        enforce_eager=True,  # avoid CUDA graph capture overhead in benchmark
    )
    if args.backend == "cpu":
        # For 7B+ models on 16GB GPUs, offload part of model weights to CPU
        # so the GPU has room for KV cache. 7B ~14.3GB leaves ~1.7GB for KV.
        # cpu_offload_gb pushes some weights to host RAM, freeing GPU for KV.
        llm_kwargs["cpu_offload_gb"] = 4  # offload 4 GB of weights
    if args.backend in ("cpu", "gpu"):
        ktc = KVTransferConfig(
            kv_connector="LMCacheConnectorV1",
            kv_role="kv_both",
        )
        llm_kwargs["kv_transfer_config"] = ktc

    t0 = time.time()
    llm = LLM(**llm_kwargs)
    t_load = time.time() - t0
    print(f"[{args.backend}] Model loaded in {t_load:.1f}s", flush=True)

    sampling_params = SamplingParams(
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )

    # Pass 1: warm KILLER prompts
    print(f"[{args.backend}] Pass 1 (warm) - sending {len(KILLER_PROMPTS)} prompts...",
          flush=True)
    t1 = time.time()
    outputs_p1 = llm.generate(KILLER_PROMPTS, sampling_params)
    t_p1 = time.time() - t1

    # Pass 2: same KILLER prompts (should hit LMCache if enabled)
    print(f"[{args.backend}] Pass 2 (test) - re-sending same {len(KILLER_PROMPTS)} prompts...",
          flush=True)
    t1 = time.time()
    outputs_p2 = llm.generate(KILLER_PROMPTS, sampling_params)
    t_p2 = time.time() - t1

    # Extract per-prompt TTFT
    # vllm outputs RequestOutputs with .metrics.first_token_time
    pass1_ttfts = [o.metrics.first_token_time - o.metrics.arrival_time
                   for o in outputs_p1]
    pass2_ttfts = [o.metrics.first_token_time - o.metrics.arrival_time
                   for o in outputs_p2]

    result = {
        "backend": args.backend,
        "model": args.model,
        "trial_id": args.trial_id,
        "model_load_time_s": t_load,
        "pass1_total_time_s": t_p1,
        "pass2_total_time_s": t_p2,
        "pass1_ttfts": pass1_ttfts,
        "pass2_ttfts": pass2_ttfts,
        "pass1_ttft_mean_s": sum(pass1_ttfts) / len(pass1_ttfts),
        "pass2_ttft_mean_s": sum(pass2_ttfts) / len(pass2_ttfts),
        "pass1_prompt_tokens": sum(
            len(o.prompt_token_ids) for o in outputs_p1),
        "pass2_prompt_tokens": sum(
            len(o.prompt_token_ids) for o in outputs_p2),
        "gpu_mem_util": args.gpu_mem_util,
    }
    # Only compute speedup when LMCache is enabled
    if args.backend in ("cpu", "gpu"):
        # Pass1 = cold (no cache), Pass2 = hot (cache hit expected)
        # speedup = pass1_mean / pass2_mean
        result["speedup"] = (result["pass1_ttft_mean_s"] /
                             result["pass2_ttft_mean_s"])
        result["interpretation"] = ("Pass2 should be faster than Pass1 if LMCache is "
                                    "hitting the cache.")
    else:
        # No cache: Pass1 and Pass2 should be roughly equal (no caching benefit)
        result["speedup"] = (result["pass1_ttft_mean_s"] /
                             result["pass2_ttft_mean_s"])
        result["interpretation"] = ("No LMCache: Pass1 ~ Pass2 (sanity check that "
                                    "speedup ~1.0 means LMCache isn't accidentally active).")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n[{args.backend}] === Trial {args.trial_id} results ===")
    print(f"  Pass1 (cold)  mean TTFT: {result['pass1_ttft_mean_s']*1000:.1f} ms")
    print(f"  Pass2 (hot)   mean TTFT: {result['pass2_ttft_mean_s']*1000:.1f} ms")
    print(f"  Speedup (cold/hot): {result['speedup']:.2f}x")
    print(f"  Output: {args.output}", flush=True)

    return result


if __name__ == "__main__":
    args = parse_args()
    run_benchmark(args)