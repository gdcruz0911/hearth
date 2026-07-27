# Hearth next milestones

This roadmap describes known v1 limitations after the local foundation milestone.
It is not an accepted Architecture Decision Record and does not select implementation options that require approval.

## 1. Establish local PDF extraction verification

Install or otherwise provide the local Poppler tools required by the existing PDF extractor.
Add synthetic or public PDF fixtures under `tests/fixtures/public/`.
Verify page boundaries, native text extraction, extraction failure reporting, and citation page accuracy.

## 2. Connect local OCR fallback

Implement an OCRmyPDF-backed local OCR adapter for image-only PDF pages.
Preserve extraction method, OCR confidence or warnings, page identity, and original document provenance.
Add tests for native-text pages, OCR-required pages, OCR failure, and reindexing after OCR.

## 3. Select and integrate the production local retrieval index

Propose and approve an ADR for the embedding model, local vector-index format, index versioning, and rebuild strategy.
Replace the deterministic hashed-vector scaffold with the approved local embedding implementation.
Test deletion, reindexing, version mismatch handling, and retrieval quality against a local evaluation corpus.

## 4. Select and integrate local reranking and generation

Propose and approve an ADR for the local inference runtime and initial models.
Add model adapters that do not make network calls or fall back to a remote model.
Require structured responses that preserve evidence IDs for validation under ADR-0006.

## 5. Create the local evaluation corpus and release gate

Add synthetic or public fixtures and expected outcomes for supported answers, exact citations, abstentions, OCR warnings, deletion, and reindexing.
Record model, retrieval, and prompt configuration alongside each evaluation run without storing private prompts or responses in Git.

## 6. Prepare the private source repository baseline

Review the staged file list and diff for private documents, runtime data, credentials, absolute local paths, prompts, and responses.
Commit one coherent initial source-only milestone.
Connect the private GitHub repository and push only after that review.
