---
id: ADR-0008
title: Local embedding and flat vector index
status: accepted
date: 2026-07-27
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/embedding.py
  - src/hearth/retrieval.py
  - src/hearth/service.py
  - local semantic index
  - local evaluation corpus
related_decisions:
  - ADR-0002
  - ADR-0004
  - ADR-0005
related_commits: []
related_issues: []
related_design_notes:
  - docs/semantic-index-rebuild-responsiveness.md
---

# ADR-0008: Local embedding and flat vector index

## Context

ADR-0004 requires a separate local, rebuildable vector index while SQLite remains authoritative for provenance.
The current hashed-vector scaffold is deterministic but not semantic.
The local Qwen3 Embedding 0.6B 4-bit MLX candidate is provisioned and needs a persistent index format with safe deletion and reindexing behavior.

## Decision

Hearth uses the pre-provisioned local Qwen3 Embedding 0.6B 4-bit MLX candidate for semantic embeddings.
The first production retrieval backend is a versioned local flat index containing normalized float32 vectors and ordered chunk IDs only.
Hearth L2-normalizes every stored and query vector, rejects zero or non-finite vectors, and uses dot product as cosine similarity.
Equal scores sort by ascending chunk ID.

Every index manifest records a stable model name and weight fingerprint, dimension, pooling strategy, normalization, chunking version, vector format, and the exact ordered chunk-ID sequence.
Search rejects an index whose manifest does not match the configured embedding model or current chunks.
Hearth never silently falls back to the hashed index when a semantic index is configured but missing or incompatible.

Import, removal, and reindexing build a complete new index version before atomically activating it.
After successful activation, Hearth removes the prior derived version so removed documents do not remain in the active or retained semantic index.
SQLite remains the authoritative provenance store.

## Alternatives considered

### SQLite embedding blobs

Storing vectors in SQLite reduces the number of local files but couples authoritative provenance to a derived retrieval implementation.

### Approximate nearest-neighbor index

An HNSW or equivalent index improves search speed at larger scale but adds native dependency, persistence, and deletion complexity before collection-size evidence requires it.

### Continue using the hashed-vector scaffold

The scaffold is dependency-free but does not provide semantic retrieval quality.

## Consequences

Rebuilds use temporary local disk space and exact search becomes slower as the collection grows.
The flat format is simple to inspect, test, and replace later without migrating SQLite provenance.
Changing the embedding model, pooling, normalization, chunking version, or index format requires a rebuild rather than risking stale retrieval results.

## Affected files and components

- `src/hearth/embedding.py`
- `src/hearth/retrieval.py`
- `src/hearth/service.py`
- Local MLX embedding adapter
- Versioned local vector index
- Retrieval evaluation corpus

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0002, ADR-0004, and ADR-0005.
No related commits or issues exist yet.
Related design note: [Semantic index rebuild responsiveness roadblock](../semantic-index-rebuild-responsiveness.md).
