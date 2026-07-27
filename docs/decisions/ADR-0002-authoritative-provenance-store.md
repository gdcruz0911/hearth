---
id: ADR-0002
title: Authoritative provenance store
status: accepted
date: 2026-07-27
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/store.py
  - src/hearth/service.py
  - document metadata
  - derived indexes
related_decisions:
  - ADR-0001
related_commits: []
related_issues: []
related_design_notes: []
---

# ADR-0002: Authoritative provenance store

## Context

Hearth must preserve document, page, section, extraction, chunk, and citation provenance locally.
Indexes must be removable and rebuildable without becoming the only record of where an answer came from.
The v1 product promise excludes hosted databases and cloud document storage.

## Decision

SQLite is Hearth's authoritative local store for imported-document metadata and provenance.
The database stores document identities and paths, pages, extraction metadata, sections, chunks, character offsets, and citation lookup data.
Vector indexes, embeddings, and other retrieval artifacts are derived local data that can be rebuilt from the authoritative records and source documents.
SQLite is not the authoritative store for original document bytes.

## Alternatives considered

### Files and JSON as the authoritative store

Files and JSON have low setup cost but make transactional replacement, referential integrity, queries, and deletion handling more difficult.

### A local embedded search or vector index as the authoritative store

An index can support retrieval but is not designed to be the durable, provenance-rich record for deletion, reindexing, and citation lookup.

### A hosted relational or vector database

Hosted storage adds operational and privacy risks that conflict with Hearth's local-first product promise.

## Consequences

Schema changes require versioned migrations and integrity checks.
Document removal and reindexing must update authoritative records transactionally before or alongside any derived index update.
The system can rebuild retrieval artifacts while retaining stable citation metadata.
SQLite remains suitable for a single-user local v1 but is not a multi-user synchronization solution.

## Affected files and components

- `src/hearth/store.py`
- `src/hearth/service.py`
- SQLite document, page, and chunk schema
- Local retrieval-index lifecycle

## Related decisions, commits, issues, and design notes

Related decision: ADR-0001.
No related commits, issues, or design notes exist yet.
