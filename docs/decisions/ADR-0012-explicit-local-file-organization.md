---
id: ADR-0012
title: Explicit local file organization
status: accepted
date: 2026-07-30
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/service.py
  - src/hearth/store.py
  - src/hearth/cli.py
  - local source files
related_decisions:
  - ADR-0002
  - ADR-0004
related_commits: []
related_issues: []
related_design_notes:
  - docs/privacy.md
  - docs/architecture.md
---

# ADR-0012: Explicit local file organization

## Context

Hearth stores canonical local source paths to preserve document provenance under ADR-0002.
Personal collection use benefits from renaming and moving a small number of original files without first navigating Finder.
Those operations alter the source of truth and can leave provenance stale if the filesystem and SQLite update diverge.

## Decision

Hearth provides explicit single-file rename and move actions for already imported documents.
Each action is addressed by document ID and requires a no-write `organize preview` command or an explicit `organize apply` command.
The apply action repeats all validation immediately before changing the filesystem.

The first version permits only same-volume moves into an existing local directory and renames that preserve the source extension.
It refuses existing target paths, including broken symlinks, and never overwrites a file.
It does not support deletion, bulk actions, wildcard targets, cross-volume copy-and-delete moves, or automatic organization from tags or health findings.

Hearth uses a same-volume non-overwriting hard-link and unlink sequence, then updates the authoritative canonical path and display name for the same document record.
Because the file bytes do not change, existing pages, chunks, derived vectors, and citation chunk IDs remain valid without reindexing.
If the SQLite update fails, Hearth attempts to rename the file back before reporting failure.

## Alternatives considered

### Store tags and collections only inside Hearth

An overlay library is safer and remains a likely future feature.
It does not meet the current goal of organizing the original local files from Hearth.

### Allow automatic repair or organization from `health`

Automatic actions could unexpectedly move or rename source files.
Health remains diagnostic and only points to explicit follow-up actions.

### Support bulk, deletion, or cross-volume moves immediately

Those operations require richer confirmation, collision, rollback, recovery, and audit behavior.
They are deferred until a later decision record defines a managed file-operation lifecycle.

## Consequences

The local collection can follow an explicitly renamed or moved original file without losing its document identity or citations.
Users remain responsible for choosing the exact target and applying the operation.
The preview displays local source and target paths only in the interactive command the user invoked.
Hearth does not send, store in version control, or log those paths remotely.

An interruption between filesystem movement and database update can still require manual recovery if the compensating move also fails.
The command reports that exceptional state clearly rather than silently continuing with stale provenance.

## Reversibility and future change

A user can explicitly rename or move the same file again through Hearth or Finder.
A future tag and collection overlay can coexist without changing original paths.
Deletion, bulk organization, cross-volume moves, undo history, and automatic remediation require a new ADR with confirmation, rollback, and recovery requirements.

## Affected files and components

- `src/hearth/service.py`
- `src/hearth/store.py`
- `src/hearth/cli.py`
- SQLite document source bindings
- Explicitly selected local source files

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0002 and ADR-0004.
Related design notes: `docs/privacy.md` and `docs/architecture.md`.
No related commits or issues exist yet.
