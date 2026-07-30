---
id: ADR-0011
title: Transient OCR artifact lifecycle
status: accepted
date: 2026-07-29
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/extraction.py
  - src/hearth/service.py
  - src/hearth/cli.py
  - local OCR output
related_decisions:
  - ADR-0002
  - ADR-0003
related_commits: []
related_issues: []
related_design_notes:
  - docs/pdf-and-ocr.md
  - docs/privacy.md
  - docs/evaluation.md
---

# ADR-0011: Transient OCR artifact lifecycle

## Context

ADR-0003 requires OCR output to remain local and notes that it needs lifecycle management with its source document.
Before this decision, every successful OCR run created a UUID-named derived PDF in the caller-configured output directory.
Reindexing therefore accumulated private copies even though Hearth only needs the extracted page text in its local provenance store.
The original PDF remains the source of truth under ADR-0002.

## Decision

The caller-configured OCR output directory is temporary private scratch space by default.
After local extraction reads the OCR-derived PDF, `OCRmyPDFFallback` deletes that file before returning extracted pages to the import path.
If deletion fails, the import fails before the SQLite store can replace document provenance.
The original PDF is never modified.
The extracted text, page metadata, and extraction method continue to be stored only in local SQLite.

Users may explicitly retain OCR-derived PDFs for inspection with `--retain-ocr-output` and `--ocr-output-directory`.
Retained PDFs remain in that caller-managed private directory until manually removed.
Hearth does not provide a broad cleanup command because the directory can contain files Hearth does not own.

## Alternatives considered

### Retain every OCR PDF and add a cleanup command

Retaining all artifacts increases private disk use by default.
A broad cleanup command could delete unrelated files because the output directory is caller-owned and has no ownership manifest.

### Delete every retained OCR PDF on document removal

Hearth does not record a durable mapping from a retained UUID-named OCR PDF to a source document.
Deleting by filename patterns would be unsafe and could remove a file retained intentionally for inspection.

### Keep only the original PDF and never offer retention

This minimizes disk use but prevents a user from inspecting the local OCR-enhanced document when source quality is in doubt.

## Consequences

Default OCR import and reindexing do not accumulate derived private PDFs.
An undeletable temporary output fails closed and requires manual removal before retrying.
Explicit retention is a conscious local privacy and disk-use tradeoff.
The output directory can remain outside source control without becoming persistent by default.
A future managed cleanup feature requires a separate ADR that defines artifact ownership, retention, user confirmation, deletion failure handling, and migration of previously retained files.

## Reversibility and future change

The retention default can change only through a new ADR because it affects private-data persistence.
A future manifest-backed retention manager may safely identify Hearth-owned artifacts and offer targeted cleanup.
That change must preserve the original PDF, avoid deleting user files, and define behavior for artifacts created before the manager exists.

## Affected files and components

- `src/hearth/extraction.py`
- `src/hearth/service.py`
- `src/hearth/cli.py`
- Local OCR output directory
- OCR unit and integration tests

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0002 and ADR-0003.
Related design notes: `docs/pdf-and-ocr.md`, `docs/privacy.md`, and `docs/evaluation.md`.
No related commits or issues exist yet.
