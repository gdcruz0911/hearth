---
id: ADR-0014
title: Explicit source relink
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
  - ADR-0012
  - ADR-0013
related_commits: []
related_issues: []
related_design_notes:
  - docs/privacy.md
  - docs/architecture.md
---

# ADR-0014: Explicit source relink

## Context

An imported source moved outside Hearth is unavailable at its recorded canonical path even when an identical local file remains elsewhere.
Reindexing an explicitly selected replacement would work, but it unnecessarily recreates chunks and citations when the bytes are unchanged.
Hearth must not automatically scan a user’s filesystem, infer a replacement, or change a binding without user approval.

## Decision

Hearth provides `relink preview <document-id> <replacement-path>` and `relink apply <document-id> <replacement-path>`.
Relink operates only on an unavailable document with a stored SHA-256 source fingerprint.
It resolves and hashes the user-selected replacement file and refuses it unless its fingerprint matches exactly.

Preview makes no change and displays the two paths only in the local command the user invoked.
Apply repeats validation immediately before atomically updating the existing document record’s canonical path, display name, and source metadata.
It does not move, copy, delete, extract, index, or modify either source file.

Because a matching fingerprint proves the source bytes are unchanged, existing pages, chunks, citation chunk IDs, and semantic vectors remain valid.
Records without a fingerprint must be restored and explicitly reindexed before they can be relinked.
Changed or still-available sources also require explicit reindexing rather than relinking.

## Alternatives considered

### Automatically search the filesystem

Disk-wide search is slow, may expose unrelated private files to the runtime, can produce ambiguous candidates, and could make a wrong binding appear valid to the user.
Hearth never searches outside the exact replacement path the user provides.

### Reindex every replacement path

Reindexing is correct when source bytes changed.
It unnecessarily changes document and chunk identities for an exact moved-file recovery case.

### Bind a replacement by name, size, or modification time

These signals can collide or be stale.
A content fingerprint is required before preserving existing provenance and citations.

## Consequences

Users can recover an unchanged externally moved file through a short explicit workflow without Finder navigation or a managed private library.
The canonical source binding can change while the document ID, stored text, chunks, citations, and derived index stay stable.

Relink does not help locate a file.
The user chooses the candidate path, and a mismatch fails closed with guidance to reindex.
The selected paths and source fingerprint remain local runtime data and are not sent or added to version control.

## Reversibility and future change

A user can relink the same unavailable document again if a later selected replacement matches its fingerprint.
An explicit reindex adopts changed source content and establishes a new fingerprint.
A future search or watcher feature requires a new ADR that defines scope, user consent, ambiguity handling, resource limits, and privacy protections.

## Affected files and components

- `src/hearth/service.py`
- `src/hearth/store.py`
- `src/hearth/cli.py`
- SQLite document source bindings
- Explicitly selected local replacement files

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0002, ADR-0012, and ADR-0013.
Related design notes: `docs/privacy.md` and `docs/architecture.md`.
No related commits or issues exist yet.
