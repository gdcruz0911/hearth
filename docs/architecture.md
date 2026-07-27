# Architecture

Hearth is a single-user local application with explicit boundaries for extraction, retrieval, answering, and persistence.

```text
Local note or PDF
  -> local extraction and optional local OCR
  -> page-aware chunking
  -> SQLite provenance store
  -> deterministic local retrieval scaffold
  -> local reranking boundary
  -> evidence-bound answer rendering
```

## Current components

`TextNoteExtractor` imports UTF-8 text and Markdown files as one logical page.
`PopplerPdfExtractor` extracts digital PDF text one page at a time through local Poppler commands.
`OCRmyPDFFallback` is an optional local extension that fills only pages with no native text.
`chunk_page` preserves page boundaries and stores character offsets for each chunk.
`SQLiteStore` is the authoritative store for document identities, page metadata, chunks, and provenance.
`HashingVectorIndex` is an in-memory deterministic retrieval scaffold built from stored chunks for the current query.
`IdentityReranker` preserves candidate order until a local reranker is integrated.
`EvidenceAnswerer` returns retrieved evidence directly when no generator is configured.
`StructuredGeneratorAnswerer` accepts only strict JSON from an opt-in local generator and resolves citation metadata from the approved evidence bundle.
`MLXLocalGenerator` loads a pre-provisioned local model directory and does not accept a model repository identifier.
`validate_answer` rejects a non-abstained answer whose citations are absent or reference chunks outside the approved evidence bundle.

## Data boundaries

Original documents remain at their local source paths and are not stored as document bytes in SQLite.
SQLite records canonical source paths, display names, pages, extraction metadata, sections, chunks, and character offsets.
The current deterministic index is derived in memory and is not a durable retrieval artifact.
Future embeddings and vector indexes must remain derived local data that can be rebuilt from source documents and SQLite provenance.

## Design constraints

The application must preserve document, page, section, extraction method, OCR confidence when available, and chunk identity through citation rendering.
Local adapters must not introduce cloud fallback, telemetry, or document transmission.
Generated answers must follow the evidence-bound contract in [ADR-0006](decisions/ADR-0006-evidence-bound-answer-contract.md).
The structured generator response protocol is defined in [ADR-0007](decisions/ADR-0007-structured-local-generator-response.md).

## Related decisions

- [ADR-0002: Authoritative provenance store](decisions/ADR-0002-authoritative-provenance-store.md)
- [ADR-0003: Local PDF and OCR ingestion](decisions/ADR-0003-local-pdf-and-ocr-ingestion.md)
- [ADR-0004: Local retrieval and index lifecycle](decisions/ADR-0004-local-retrieval-and-index-lifecycle.md)
- [ADR-0006: Evidence-bound answer contract](decisions/ADR-0006-evidence-bound-answer-contract.md)
- [ADR-0007: Structured local generator response](decisions/ADR-0007-structured-local-generator-response.md)
