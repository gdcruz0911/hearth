# Hearth next milestones

This roadmap describes known v1 limitations after the local foundation milestone.
It is not an accepted Architecture Decision Record and does not select implementation options that require approval.

## 1. Expand local PDF extraction verification

Add synthetic or public PDF fixtures under `tests/fixtures/public/`.
Expand coverage beyond the existing page-boundary, native-text, and citation checks.

## 2. Expand local OCR fallback verification

The `OCRmyPDFFallback` adapter is configured through a caller-supplied output directory.
Add public or synthetic fixtures for OCR failure and reindexing after OCR.

## 3. Integrate the production local retrieval index

Replace the deterministic hashed-vector scaffold with the approved local embedding implementation.
Test deletion, reindexing, version mismatch handling, and retrieval quality against a local evaluation corpus.

## 4. Integrate local reranking and generation

Evaluate the local generator adapter against citations and abstention after the successful host-side performance benchmark.
Expand the public or synthetic corpus to cover malformed output, unsupported questions, and source-bound generated answers.

## 5. Create the local evaluation corpus and release gate

The baseline harness covers one supported citation and one abstention using only synthetic public material.
Expand it with expected outcomes for OCR warnings, deletion, and reindexing before selecting a production model.
Record model, retrieval, and prompt configuration alongside each evaluation run without storing private prompts or responses in Git.

## 6. Maintain the private source repository boundary

Review the staged file list and diff for private documents, runtime data, credentials, absolute local paths, prompts, and responses.
Commit and push only coherent source-only milestones after that review.
