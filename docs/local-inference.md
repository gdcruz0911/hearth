# Local inference

## Status

MLX is installed as an optional local runtime for host-side benchmarking on this Apple Silicon Mac.
The current Hearth application does not yet load an MLX embedding model, reranker, or generator during document chat.
The deterministic retrieval and direct-evidence answer path remain the active implementation.

## Candidate models

The provisioned candidates are a Qwen3 8B 4-bit generator, a Qwen3 Embedding 0.6B 4-bit model, and a Qwen3 Reranker 0.6B 4-bit model.
They are evaluation candidates, not product defaults.
The generator candidate has passed a basic host-side throughput and MLX peak-memory measurement.
It still requires evidence-bound retrieval, citation, and abstention evaluation before a default can be accepted.

## Installation

Install the optional local inference dependencies in the project virtual environment.

```bash
.venv/bin/python -m pip install ".[local-inference]"
```

Model provisioning is intentionally separate from the application runtime.
It requires a local model directory before the benchmark can run and may use a model registry during that explicit provisioning step.

## Runtime storage

The current model root is `~/Library/Application Support/Hearth/models/`, where `~` means the current macOS user’s home directory.
Each model resides in a separate directory so that changing one model does not modify documents, indexes, or the source repository.
The current candidate directories are `qwen3-8b-4bit`, `qwen3-embedding-0.6b-4bit-dwq`, and `qwen3-reranker-0.6b-4bit`.

To remove a candidate, move only its exact model directory to the macOS Trash in Finder.
Do not remove the enclosing `Hearth` directory because it also holds the local model cache and may later hold other runtime data.

## Decision record

The local runtime and hardware profile are defined in [ADR-0005](decisions/ADR-0005-local-inference-runtime-and-hardware-profile.md).
