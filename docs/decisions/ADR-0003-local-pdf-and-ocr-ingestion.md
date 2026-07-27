---
id: ADR-0003
title: Local PDF and OCR ingestion
status: accepted
date: 2026-07-27
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/extraction.py
  - src/hearth/service.py
  - src/hearth/cli.py
  - local OCR output
related_decisions:
  - ADR-0001
  - ADR-0002
related_commits: []
related_issues: []
related_design_notes: []
---

# ADR-0003: Local PDF and OCR ingestion

## Context

Hearth must import both digital and scanned PDFs while preserving exact page-level citation provenance.
Digital PDFs should not be OCRed when usable native text already exists.
Scanned or image-only pages need an on-device fallback that does not upload documents or create a cloud dependency.

## Decision

Hearth extracts native text per PDF page first.
When native extraction returns no usable text for one or more pages, Hearth may run a caller-configured local OCRmyPDF fallback once for the document and substitute OCR text only for those blank pages.
Every extracted page records whether its text came from native extraction or OCR.
OCR output is a derived local artifact that must be written to a caller-configured private directory outside version control.
The original PDF remains unchanged.

## Alternatives considered

### OCR every PDF page

This would make one pipeline handle every PDF but wastes work, may degrade usable digital text, and obscures the original extraction method.

### Require users to OCR PDFs before importing

This reduces application complexity but makes Hearth less useful for scanned documents and loses consistent provenance handling.

### Use a cloud OCR service

Cloud OCR would conflict with the local-first promise and expose private document contents to an external service.

## Consequences

The PDF pipeline depends on local Poppler and, when OCR is enabled, local OCRmyPDF tooling.
OCR output consumes local disk space and needs lifecycle management with its source document.
Page citations remain stable because original page numbers are preserved.
OCR confidence may remain unavailable when the underlying local tool does not expose a usable page-level confidence value.

## Affected files and components

- `src/hearth/extraction.py`
- `src/hearth/service.py`
- `src/hearth/cli.py`
- Local OCR output directory
- PDF ingestion tests

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0001 and ADR-0002.
No related commits, issues, or design notes exist yet.
