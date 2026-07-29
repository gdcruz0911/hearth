# Hearth next milestones

This roadmap describes known v1 limitations after the local foundation milestone.
It is not an accepted Architecture Decision Record and does not select implementation options that require approval.

## 1. Expand local PDF extraction verification

Add synthetic or public PDF fixtures under `tests/fixtures/public/`.
Expand coverage beyond the existing page-boundary, native-text, and citation checks.

## 2. Expand local OCR fallback verification

The `OCRmyPDFFallback` adapter is configured through a caller-supplied output directory.
Add public or synthetic fixtures for OCR failure and reindexing after OCR.

## 3. Evaluate the local semantic retrieval index

Run the MLX embedding and flat-index path against public or synthetic retrieval cases from an interactive macOS terminal.
Test deletion, reindexing, version mismatch handling, and retrieval quality before making semantic retrieval the default path.

## 4. Integrate local reranking and generation

Evaluate the local generator adapter against citations and abstention after the successful host-side performance benchmark.
Expand the public or synthetic corpus to cover malformed output, unsupported questions, and source-bound generated answers.
Evaluate whether a local reranker or claim-support checker should replace or supplement the conservative lexical evidence-sufficiency gate from ADR-0009.

## 5. Create the local evaluation corpus and release gate

The baseline harness covers one supported citation and one abstention using only synthetic public material.
Expand it with expected outcomes for OCR warnings, deletion, and reindexing before selecting a production model.
Record model, retrieval, and prompt configuration alongside each evaluation run without storing private prompts or responses in Git.

## 6. Maintain the private source repository boundary

Review the staged file list and diff for private documents, runtime data, credentials, absolute local paths, prompts, and responses.
Commit and push only coherent source-only milestones after that review.

## 7. Build a local model-evaluation leaderboard

Evaluate pre-provisioned local generators, embedding models, and rerankers against a fixed synthetic or public corpus.
Record model identity and fingerprint, chunking version, retrieval configuration, citation and abstention outcomes, malformed-response rate, latency, and peak memory.
Do not use private documents, prompts, responses, or runtime paths in version-controlled leaderboard data.
Do not automatically select a model from leaderboard results.
Use the results to propose an explicit ADR for a default model configuration.

## 8. Improve claim support and memory boundaries

The current lexical gate rejects unrelated cited evidence but cannot prove that every generated claim is entailed by its citation.
Evaluate a local claim-support checker separately from reranking because better retrieval alone cannot validate generated wording.
Hearth currently has no conversational memory and answers only from the currently imported collection.
If a future feature persists conversation state, define its local storage, deletion, retention, and citation boundaries in a new ADR.

## 9. Add collection inspection commands

Add a local CLI command to list imported documents and inspect page, chunk, extraction-method, and OCR-warning metadata.
Avoid exporting document text by default.
