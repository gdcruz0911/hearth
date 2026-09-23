---
id: ADR-0019
title: Semantic retrieval as the default path
status: accepted
date: 2026-09-21
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/service.py
  - src/hearth/retrieval.py
  - src/hearth/cli.py
  - src/hearth/runtime.py
related_decisions:
  - ADR-0004
  - ADR-0008
  - ADR-0017
  - ADR-0020
related_commits: []
related_issues: []
related_design_notes:
  - docs/architecture.md
  - docs/local-inference.md
---

# ADR-0019: Semantic retrieval as the default path

## Context

The shipped default retrieval backend is `HashingVectorIndex`, a deterministic hashed bag-of-words scaffold with no semantic capability.
Semantic embeddings, the flat vector index, and the relationship map are opt-in and require a hand-provisioned local model directory.

`document_relationships` returns an empty list when no semantic index is configured.
The knowledge map is therefore empty on a fresh installation, and the map is the product.

Hearth's direction is a content-aware hub whose central capability is showing how a person's documents relate.
That capability cannot be optional.

## Decision

Semantic embedding and the flat vector index become the default retrieval and relationship path.
A new profile configures an embedding model and index directory without the person supplying paths.

`HashingVectorIndex` is retained only as the explicit no-model path and is no longer the default.
Consistent with ADR-0008, Hearth does not silently fall back to it when a semantic index is configured but missing or incompatible.

## Consequences

A fresh installation renders a populated map after import, without manual model provisioning.

Hearth acquires a model dependency at first run, governed by ADR-0020.

The default path now requires MLX, which restricts Hearth to Apple Silicon.
This is acceptable for a personal local application and forecloses deployment to other targets.
Replacing the default embedder with a portable CPU implementation is the upgrade path and is deliberately deferred to a separate decision, so that the platform constraint is chosen rather than inherited.

The evaluation corpus currently holds five semantic cases across two documents, which is insufficient to qualify a default model.
Expanding it is a prerequisite for any parameter or model change, not for this decision.

## Alternatives considered

### Keep semantic retrieval opt-in

The map, which is the reason to use Hearth, would remain empty for every new installation including the author's own next machine.

### Make the hashed scaffold semantic

The scaffold has no path to semantic quality; it is a lexical hash.

### Adopt a portable CPU embedder as the default now

This resolves the platform constraint but changes the default model before any evaluation corpus exists to compare candidates.
Sequencing it after the corpus keeps the choice measurable.
