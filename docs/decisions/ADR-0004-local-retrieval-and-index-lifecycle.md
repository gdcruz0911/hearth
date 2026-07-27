---
id: ADR-0004
title: Local retrieval and index lifecycle
status: accepted
date: 2026-07-27
supersedes: null
superseded_by: null
affected_components:
  - retrieval index
  - embedding configuration
  - reranking pipeline
  - SQLite provenance store
related_decisions:
  - ADR-0001
  - ADR-0002
  - ADR-0003
  - ADR-0006
related_commits: []
related_issues: []
related_design_notes: []
---

# ADR-0004: Local retrieval and index lifecycle

## Context

Hearth needs semantic retrieval that remains local, can be rebuilt safely, and does not weaken citation provenance.
SQLite is already the authoritative provenance store under ADR-0002.
The current deterministic hashed-vector scaffold is suitable for early tests but is not the production retrieval implementation.

## Decision

Hearth will maintain a separate, local, rebuildable on-disk vector index containing derived embeddings only.
SQLite remains authoritative for document, page, chunk, and citation metadata.
Each index version records the embedding-model identity, vector dimension, normalization method, chunking configuration, and index format.
Reindexing builds a new index version before activating it, so a failed build does not replace the active retrieval path.
Retrieval produces the top 20 local candidates.
A future local reranker selects the best 4 to 6 evidence chunks for the answer pipeline.
Hearth will not use a hosted vector service, cloud embedding API, or remote fallback.
The local embedding model and index backend are deferred to ADR-0005.

## Alternatives considered

### Brute-force local cosine search

Brute-force search is simple and transparent but becomes slower as the local collection grows.

### SQLite as both provenance store and vector index

One storage system would simplify deployment but couples authoritative provenance records to a retrieval implementation that needs independent rebuild and version handling.

### A hosted vector service

Hosted retrieval would reduce local index management but conflicts with the local-first privacy boundary.

## Consequences

Index artifacts require additional local disk space and lifecycle management.
Version metadata makes retrieval behavior reproducible and supports safe model or chunking changes.
The index backend can be changed later without migrating authoritative source metadata.
Reindexing needs enough temporary space to build a new version alongside the active one.

## Affected files and components

- Future local vector-index implementation
- Embedding and reranking adapters
- Retrieval and reindexing workflow
- SQLite index-version metadata
- Local evaluation corpus

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0001, ADR-0002, ADR-0003, and ADR-0006.
No related commits, issues, or design notes exist yet.
