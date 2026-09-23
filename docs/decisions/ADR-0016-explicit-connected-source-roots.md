---
id: ADR-0016
title: Explicit connected source roots
status: accepted
date: 2026-07-30
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/runtime.py
  - src/hearth/service.py
  - src/hearth/cli.py
  - src/hearth/web.py
related_decisions:
  - ADR-0002
  - ADR-0004
  - ADR-0013
  - ADR-0015
related_commits: []
related_issues: []
related_design_notes:
  - docs/privacy.md
  - docs/architecture.md
---

# ADR-0016: Explicit connected source roots

## Context

One personal Hearth profile should be able to grow a useful map from the owner’s everyday files without turning the application into an unrestricted filesystem crawler.
Desktop, Documents, and Downloads are useful initial roots on a personal Mac.
The application must not ingest those folders merely because a profile exists.

## Decision

The runtime profile owns an ordered `source_roots` list.
New and legacy profiles default to the current user’s Desktop, Documents, and Downloads folders.
Users can replace that list with repeated `--source-root` options when they create a profile or run a command.

`sources preview` and the local web interface scan only profile-approved roots when the user explicitly requests a review.
Discovery accepts UTF-8 Markdown, text, and PDF files.
It skips hidden files and directories, symlinks, `node_modules`, `__pycache__`, `.hearth`, unsupported files, and paths already imported into the active knowledge base.
Discovery reads filenames and file metadata only.
It does not extract document text, change a source file, or create a database record.

Import requires a second explicit action against the reviewed in-memory preview.
Hearth revalidates that each reviewed file remains inside a connected root before import.
Successful batch imports rebuild the optional semantic index once after the batch.
Files that cannot be imported are reported by display name and reason without cancelling other reviewed files.

The browser receives root display names, availability, counts, and candidate display names only.
Canonical source paths remain inside the local service boundary during source discovery and are not returned in normal source-root payloads.

## Consequences

A single profile can become the persistent map for the user’s normal working folders.
The first scan remains reviewable and reversible until the separate import action.
Existing imported-source freshness checks continue to operate on their stored source paths.

Source watching, automatic repair, automatic reindexing, folder mutation, and arbitrary full-disk crawling remain out of scope.
Adding a root later requires an explicit profile or command-line configuration change.

## Alternatives considered

### Scan the entire filesystem

An unrestricted scan would mix system data, application state, caches, backups, and personal working documents.
It would create too much noise and would not establish a clear consent boundary.

### Import on discovery

Scanning and importing in one action would make the first connection difficult to inspect and stop.
The preview-then-apply model matches Hearth’s existing safety pattern for local changes.

### Managed document copies

Copying files into a Hearth library would introduce retention, recovery, and deletion responsibilities.
Hearth continues to index approved original files in place.
