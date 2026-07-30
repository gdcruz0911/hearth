---
id: ADR-0013
title: Source freshness attention
status: accepted
date: 2026-07-30
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/service.py
  - src/hearth/store.py
  - src/hearth/cli.py
  - local SQLite provenance store
related_decisions:
  - ADR-0002
  - ADR-0004
  - ADR-0012
related_commits: []
related_issues: []
related_design_notes:
  - docs/privacy.md
  - docs/architecture.md
---

# ADR-0013: Source freshness attention

## Context

Hearth treats the original local file as the source of truth under ADR-0002.
An external edit, replacement, deletion, or inaccessible source can leave locally stored chunks and citations out of date.
The collection health workflow must surface that state without silently changing a user file or stored evidence.

## Decision

At import and explicit reindex, Hearth records a SHA-256 fingerprint, byte size, and nanosecond modification time for each source in local SQLite.
It captures that state before extraction and checks it again after extraction.
If the source changes during extraction, the import fails before it replaces the existing provenance record.

`health` compares file size and modification time first.
It hashes only a source whose metadata changed, then reports one of three document-ID attention states when needed: source unavailable, source changed since import, or source needs baseline reindex.
The last state applies to records created before this decision because they have no fingerprint.

Health remains diagnostic.
It never reindexes, removes, moves, renames, restores, or otherwise repairs a source automatically.
The user must explicitly reindex a changed source or restore or remove an unavailable record.

Normal health output includes the document ID, display name, and status, but not the canonical source path or stored text.

## Alternatives considered

### Check only file metadata

Metadata can change when content does not and may not capture all content replacements reliably.
Fingerprint comparison after a metadata change confirms whether extraction provenance is stale.

### Hash every source on every health check

This would be simpler but makes the routine diagnostic command reread every local document.
Metadata-first comparison keeps a healthy collection fast while retaining content confirmation when a file appears to have changed.

### Automatically reindex changed files

Automatic reindexing could change answerable evidence without a conscious user action and might read a file while another application is still writing it.
Hearth reports the state and leaves repair explicit.

## Consequences

Collection health can distinguish a stale extraction from an unavailable file without copying original files into a managed library.
Existing collections require a one-time explicit reindex of each source to establish the first fingerprint.
An external move is reported as unavailable at its stored path rather than being searched for elsewhere on disk.

The fingerprint and file metadata are local runtime provenance data.
They are not transmitted, printed with source paths, or intended for version control.

## Reversibility and future change

Explicit reindex refreshes the stored fingerprint and extracted content for one source.
A future managed-library or watch-service proposal must define its own retention, user consent, repair, and deletion boundaries before changing this diagnostic behavior.

## Affected files and components

- `src/hearth/service.py`
- `src/hearth/store.py`
- `src/hearth/cli.py`
- SQLite document provenance
- Local source files during explicit import or reindex

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0002, ADR-0004, and ADR-0012.
Related design notes: `docs/privacy.md` and `docs/architecture.md`.
No related commits or issues exist yet.
