---
id: ADR-0017
title: NumPy dependency and chunk-level document relationships
status: accepted
date: 2026-09-21
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/embedding.py
  - pyproject.toml
related_decisions:
  - ADR-0004
  - ADR-0008
related_commits: []
related_issues: []
related_design_notes:
  - docs/architecture.md
---

# ADR-0017: NumPy dependency and chunk-level document relationships

## Context

The semantic map gated document relationships on the cosine similarity of document centroids, then displayed a different score derived from the strongest cross-document chunk pair.
Averaging every chunk vector in a document discards the signal the map exists to surface.
Two documents that share one strongly related section have dissimilar centroids and were filtered out before their chunk pair was ever examined.
Long multi-topic documents drift toward the corpus mean and appeared spuriously similar to each other.

The centroid gate was not a modelling choice.
Hearth declared no required dependencies, so every similarity was a pure-Python loop.
Comparing documents instead of chunks reduced the comparison count enough to stay interactive.
A measured comparison of 7500 chunks at 1024 dimensions takes 0.18 seconds with NumPy and approximately twelve minutes in pure Python.

## Decision

NumPy becomes a required runtime dependency.

Document relationships are computed from chunk-level cosine similarity.
An edge between two documents is admitted when a cross-document chunk pair is each other's strongest cross-document match and scores at or above the configured minimum.
The admitted score and the displayed score are the same quantity.
Document centroids are removed.

Mutual best-match admission replaces the centroid gate as the defence against hub documents.
A large multi-topic document contains some chunk close to almost anything, so a one-sided threshold would connect it to the entire collection.
`_distributed_relationship_candidates` continues to spread the bounded visible set across neighborhoods after admission.

SQLite remains the authoritative provenance store, and the derived index remains rebuildable.

## Consequences

The map surfaces documents that share one section, which the centroid gate made structurally impossible.
The relationship score shown to a person is the score that admitted the edge.
Hand-rolled vector arithmetic in `embedding.py` is deleted rather than ported.

Hearth is no longer installable without a compiled dependency.
NumPy was already present in any environment with the optional MLX extra; it is now declared rather than inherited.

The similarity computation is quadratic in chunk count and materializes a full matrix, which is 225 MB at 7500 chunks.
This is acceptable for a personal collection and will not survive connecting several large folders.
Blocked top-k computation is the upgrade path and is deferred until a measurement requires it.

## Alternatives considered

### Keep the centroid gate and tune its threshold

No threshold on an averaged vector recovers two documents that share a single section.
The false negative is structural, not a matter of calibration.

### Make NumPy optional and keep the centroid gate as a fallback

Two similarity implementations means the tested path and the executed path differ by environment, and the incorrect one would be maintained indefinitely.

### Hand-write an approximate nearest-neighbor index

This reimplements a solved problem to preserve a zero-dependency property that delivers no benefit to a single local user.
