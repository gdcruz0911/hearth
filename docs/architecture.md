# Architecture

Hearth is a single-user local application with explicit boundaries for extraction, retrieval, answering, and persistence.

```text
Local note or PDF
  -> local extraction and optional local OCR
  -> page-aware chunking
  -> SQLite provenance store
  -> deterministic local retrieval scaffold or optional local flat semantic index
  -> local reranking boundary
  -> evidence-bound answer rendering
```

## Current components

`TextNoteExtractor` imports UTF-8 text and Markdown files as one logical page.
`PopplerPdfExtractor` extracts digital PDF text one page at a time through local Poppler commands.
`OCRmyPDFFallback` is an optional local extension that fills only pages with no native text and deletes its derived PDF after extraction unless retention is explicitly configured.
`chunk_page` preserves page boundaries and stores character offsets for each chunk.
`SQLiteStore` is the authoritative store for document identities, page metadata, chunks, source fingerprints, and provenance.
`CollectionHealth` summarizes local collection counts, OCR-review needs, source freshness attention, and derived-index state without including stored document text or canonical source paths.
`HearthService` can preview or apply one explicit local rename or same-volume move while updating the existing document source binding and preserving chunks and citations.
`HashingVectorIndex` is an in-memory deterministic retrieval scaffold used when no semantic index is configured.
`FlatVectorIndex` stores derived normalized float32 vectors and chunk IDs in a versioned local index directory when a semantic index is configured.
`IdentityReranker` preserves candidate order when no local reranker is configured.
`MLXLocalReranker` scores already-retrieved query-document pairs locally and reorders the evidence bundle without changing citation or claim-validation rules.
`EvidenceAnswerer` returns retrieved evidence directly when no generator is configured.
`StructuredGeneratorAnswerer` accepts only strict JSON from an opt-in local generator, resolves citation metadata from the approved evidence bundle, and rejects generated factual text that is not a normalized contiguous span of a cited quote.
`MLXLocalGenerator` loads a pre-provisioned local model directory and does not accept a model repository identifier.
`StructuredClaimSupportChecker` is an evaluation-only local adapter that scores a supplied claim against supplied evidence and is not connected to the answer path.
`validate_answer` rejects a non-abstained answer whose citations are absent or reference chunks outside the approved evidence bundle.
`has_lexical_support` adds the ADR-0009 fail-closed check that a supported answer's cited evidence shares a meaningful term with the question.

## Data boundaries

Original documents remain at their local source paths and are not stored as document bytes in SQLite.
SQLite records canonical source paths, display names, source fingerprints, source size and modification time, pages, extraction metadata, sections, chunks, and character offsets.
The `health` command checks source-file availability and freshness locally and reports only document IDs, display names, and attention states, not canonical paths or stored document text.
The `organize` command displays an explicitly selected source and target only in the local interactive command and never uses that output as a replacement for provenance storage.
OCR-derived PDFs are transient private scratch artifacts by default and remain only when a user explicitly opts in to retention for inspection.
The deterministic hashed index is derived in memory when no semantic index is configured.
When configured, `FlatVectorIndex` stores its derived vectors and manifest in a local versioned directory that can be rebuilt from source documents and SQLite provenance.

## Design constraints

The application must preserve document, page, section, extraction method, OCR confidence when available, and chunk identity through citation rendering.
Local adapters must not introduce cloud fallback, telemetry, or document transmission.
Generated answers must follow the evidence-bound contract in [ADR-0006](decisions/ADR-0006-evidence-bound-answer-contract.md).
The structured generator response protocol is defined in [ADR-0007](decisions/ADR-0007-structured-local-generator-response.md).
The semantic embedding and index format are defined in [ADR-0008](decisions/ADR-0008-local-embedding-and-flat-vector-index.md).
The lexical evidence-sufficiency gate is defined in [ADR-0009](decisions/ADR-0009-lexical-evidence-sufficiency-gate.md).
The verbatim evidence answer contract is defined in [ADR-0010](decisions/ADR-0010-verbatim-evidence-answer-contract.md).
The OCR artifact lifecycle is defined in [ADR-0011](decisions/ADR-0011-transient-ocr-artifact-lifecycle.md).
Explicit local source-file organization is defined in [ADR-0012](decisions/ADR-0012-explicit-local-file-organization.md).
Source freshness attention is defined in [ADR-0013](decisions/ADR-0013-source-freshness-attention.md).

## Related decisions

- [ADR-0002: Authoritative provenance store](decisions/ADR-0002-authoritative-provenance-store.md)
- [ADR-0003: Local PDF and OCR ingestion](decisions/ADR-0003-local-pdf-and-ocr-ingestion.md)
- [ADR-0004: Local retrieval and index lifecycle](decisions/ADR-0004-local-retrieval-and-index-lifecycle.md)
- [ADR-0006: Evidence-bound answer contract](decisions/ADR-0006-evidence-bound-answer-contract.md)
- [ADR-0007: Structured local generator response](decisions/ADR-0007-structured-local-generator-response.md)
- [ADR-0008: Local embedding and flat vector index](decisions/ADR-0008-local-embedding-and-flat-vector-index.md)
- [ADR-0009: Lexical evidence sufficiency gate](decisions/ADR-0009-lexical-evidence-sufficiency-gate.md)
- [ADR-0010: Verbatim evidence answer contract](decisions/ADR-0010-verbatim-evidence-answer-contract.md)
- [ADR-0011: Transient OCR artifact lifecycle](decisions/ADR-0011-transient-ocr-artifact-lifecycle.md)
- [ADR-0012: Explicit local file organization](decisions/ADR-0012-explicit-local-file-organization.md)
- [ADR-0013: Source freshness attention](decisions/ADR-0013-source-freshness-attention.md)
