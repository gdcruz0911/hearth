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

## Runtime profile

A private runtime profile makes the normal command short while keeping model and database paths outside the repository.
Create it once at a location that is not committed to Git.

```bash
hearth profile create "$HOME/Library/Application Support/Hearth/hearth.json" \
  --database "$HOME/Library/Application Support/Hearth/hearth.sqlite" \
  --embedding-model "$HOME/Library/Application Support/Hearth/models/qwen3-embedding-0.6b-4bit-dwq" \
  --index-directory "$HOME/Library/Application Support/Hearth/indexes/main" \
  --relationship-minimum-score 0.72
```

Then start Hearth with that profile.

```bash
hearth --profile "$HOME/Library/Application Support/Hearth/hearth.json" web
```

The profile is JSON, contains private runtime paths, and is intentionally not created inside the repository.
Its database, local model, derived-index, OCR, generator, reranker, relationship-score, and connected-source-root settings are supported.
An explicit command-line option overrides the matching profile setting for a single run.
The relationship score is a cosine-similarity cutoff for map links, not a probability or factual-confidence value.

## Semantic map for an existing collection

If Hearth already has imported sources but the map says that the local index is not configured, restart the web app with the same database plus an existing local embedding model and private index directory.

```bash
.venv/bin/python -m hearth.cli \
  --database "$HOME/Library/Application Support/Hearth/hearth.sqlite" \
  --embedding-model "$HOME/Library/Application Support/Hearth/models/qwen3-embedding-0.6b-4bit-dwq" \
  --index-directory "$HOME/Library/Application Support/Hearth/indexes/main" \
  web
```

Then use the map's **Rebuild the semantic map** preview and apply it.
The action derives local vectors from evidence Hearth has already imported.
It does not modify or copy the original files.
The web interface runs this rebuild in the background in small batches, shows completed evidence units, and offers a cancel control.
Cancelling discards only the incomplete derived index and keeps any previously active index unchanged.
The overview renders only explainable links above the configured relationship threshold, and selecting a neighborhood reveals its source documents and the evidence for each link.

## Connected folders

Profiles connect Desktop, Documents, and Downloads by default.
Hearth does not scan them automatically.
Use the browser’s Review folders control or the CLI to inspect the eligible Markdown, text, and PDF files before importing them into the active knowledge base.

```bash
hearth --profile "$HOME/Library/Application Support/Hearth/hearth.json" sources preview
hearth --profile "$HOME/Library/Application Support/Hearth/hearth.json" sources import
```

To use another folder instead of the defaults, repeat `--source-root` when creating the profile.

```bash
hearth profile create "$HOME/Library/Application Support/Hearth/hearth.json" \
  --database "$HOME/Library/Application Support/Hearth/hearth.sqlite" \
  --source-root "$HOME/Documents" \
  --source-root "$HOME/Projects"
```

## Experimental reranking

Pass a pre-provisioned local reranker directory to score the retrieved candidates locally before Hearth selects its evidence bundle.

```bash
.venv/bin/python -m hearth.cli --reranker-model /path/to/local/reranker --database .hearth/hearth.sqlite search "your question"
```

The Qwen reranker scores each query-document pair as local yes/no relevance and deterministically breaks score ties by chunk ID.
It only reorders retrieved candidates and does not validate generated claims or bypass citation validation.
On 2026-07-30, the local reranker ranked a relevant synthetic archive passage above an unrelated passage on the current Apple Silicon Mac.
This is a narrow behavior check, not a selection decision or broader relevance evaluation.

## Experimental claim-support evaluation

Run the separate claim-support corpus with the pre-provisioned local generator.

```bash
.venv/bin/python -m hearth.cli --generator-model /path/to/local/model evaluate-claim-support tests/fixtures/public/claim-support-evaluation.json
```

The checker evaluates a question, proposed claim, and supplied evidence using a strict supported or unsupported JSON response.
It is evaluation-only and never runs during `search`, changes retrieval order, or relaxes the ADR-0010 verbatim cited-evidence contract.
On 2026-07-30, the Qwen3 8B 4-bit candidate passed five of eight public adversarial cases with the tightened prompt.
It still incorrectly supported the three overclaims involving an added duty or an unsupported qualifier.
It is therefore not suitable for a claim-validation gate or model-selection decision.

## Runtime storage

The current model root is `~/Library/Application Support/Hearth/models/`, where `~` means the current macOS user’s home directory.
Each model resides in a separate directory so that changing one model does not modify documents, indexes, or the source repository.
The current candidate directories are `qwen3-8b-4bit`, `qwen3-embedding-0.6b-4bit-dwq`, and `qwen3-reranker-0.6b-4bit`.

To remove a candidate, move only its exact model directory to the macOS Trash in Finder.
Do not remove the enclosing `Hearth` directory because it also holds the local model cache and may later hold other runtime data.

## Decision record

The local runtime and hardware profile are defined in [ADR-0005](decisions/ADR-0005-local-inference-runtime-and-hardware-profile.md).
