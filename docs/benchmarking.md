# Benchmarking

## Purpose

The host-side benchmark measures whether a pre-provisioned local generator can run interactively on the current Mac.
It is a performance and memory check, not a document-chat quality evaluation.

## Prerequisites

Use an interactive macOS terminal with Apple Silicon GPU access.
Install the local MLX dependency and provision the generator model before running the script.
The script does not download a model.

```bash
scripts/benchmark_qwen3_8b.sh
```

The runner validates the local model weight file, passes the local filesystem model path to MLX, and sets `HF_HUB_OFFLINE=1` for the Hugging Face Hub client.
It has no model download command.
The environment setting prevents Hugging Face Hub client downloads but is not equivalent to a network firewall for every dependency in the process.

## Method

The runner first generates up to eight tokens from a public smoke-test prompt to verify local model loading.
It then warms up the model and executes three batch-size-one timing trials with 512 prompt tokens and 128 generation tokens.
MLX reports prompt throughput, generation throughput, and peak model memory for every trial and their average.

## Current result

The current 16 GB Apple Silicon Mac produced the following result for the Qwen3 8B 4-bit candidate.

| Metric | Result |
| --- | ---: |
| Average prompt throughput | 213.905 tokens/sec |
| Average generation throughput | 20.218 tokens/sec |
| MLX peak model memory | 5.123 GB |
| Trial total time | 8.436 to 9.360 sec |

This establishes that the candidate can run interactively in the benchmark configuration.
It does not establish whole-system memory headroom, retrieval quality, citation accuracy, claim support, or unsupported-question abstention.

## Next evaluation

The evidence-bound generator adapter now receives only approved retrieved chunks and returns structured citation IDs.
The application validates those IDs under [ADR-0006](decisions/ADR-0006-evidence-bound-answer-contract.md) and [ADR-0007](decisions/ADR-0007-structured-local-generator-response.md).
The candidate cannot become Hearth’s default generator until a synthetic or public corpus covers supported answers, citations, and abstention through the host-side adapter.
