# Local inference

## Status

MLX is installed as an optional local runtime for host-side benchmarking on this Apple Silicon Mac.
Hearth includes an opt-in MLX generator adapter for document chat when a pre-provisioned local model directory is explicitly configured.
The deterministic retrieval and direct-evidence answer path remain the default implementation.
An opt-in MLX embedding and flat-index path is implemented.
An opt-in MLX reranker is implemented behind `--reranker-model`.

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

## Experimental generator

Pass an existing local model directory to the CLI to enable generated answers.

```bash
.venv/bin/python -m hearth.cli --generator-model /path/to/local/model --database .hearth/hearth.sqlite search "your question"
```

The adapter rejects a directory without `model.safetensors` and does not accept a model repository identifier.
It sets Hugging Face Hub offline mode before loading MLX and does not configure a remote fallback.
Malformed model output becomes an explicit abstention under [ADR-0007](decisions/ADR-0007-structured-local-generator-response.md).

## Experimental semantic retrieval

Pass a pre-provisioned local embedding model directory and a private index directory together to enable semantic retrieval.

```bash
.venv/bin/python -m hearth.cli --embedding-model /path/to/local/embedding-model --index-directory /path/to/private/index --database .hearth/hearth.sqlite import path/to/note.md
```

The flat index stores only derived vectors and chunk IDs outside SQLite.
It validates model fingerprint, dimension, normalization, chunking version, and ordered chunk IDs before search.
It rebuilds on document import, removal, and reindexing rather than silently using stale vectors.

## Experimental reranking

Pass a pre-provisioned local reranker directory to score the retrieved candidates locally before Hearth selects its evidence bundle.

```bash
.venv/bin/python -m hearth.cli --reranker-model /path/to/local/reranker --database .hearth/hearth.sqlite search "your question"
```

The Qwen reranker scores each query-document pair as local yes/no relevance and deterministically breaks score ties by chunk ID.
It only reorders retrieved candidates and does not validate generated claims or bypass citation validation.
On 2026-07-30, the local reranker ranked a relevant synthetic archive passage above an unrelated passage on the current Apple Silicon Mac.
This is a narrow behavior check, not a selection decision or broader relevance evaluation.

## Runtime storage

The current model root is `~/Library/Application Support/Hearth/models/`, where `~` means the current macOS user’s home directory.
Each model resides in a separate directory so that changing one model does not modify documents, indexes, or the source repository.
The current candidate directories are `qwen3-8b-4bit`, `qwen3-embedding-0.6b-4bit-dwq`, and `qwen3-reranker-0.6b-4bit`.

To remove a candidate, move only its exact model directory to the macOS Trash in Finder.
Do not remove the enclosing `Hearth` directory because it also holds the local model cache and may later hold other runtime data.

## Decision record

The local runtime and hardware profile are defined in [ADR-0005](decisions/ADR-0005-local-inference-runtime-and-hardware-profile.md).
