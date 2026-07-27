---
id: ADR-0005
title: Local inference runtime and hardware profile
status: accepted
date: 2026-07-27
supersedes: null
superseded_by: null
affected_components:
  - local model runtime
  - embedding adapter
  - reranker adapter
  - generator adapter
  - local evaluation corpus
related_decisions:
  - ADR-0001
  - ADR-0004
  - ADR-0006
related_commits: []
related_issues: []
related_design_notes: []
---

# ADR-0005: Local inference runtime and hardware profile

## Context

Hearth requires entirely local embedding, reranking, and generation.
The original product outline described 24 GB and 48 GB+ hardware tiers.
The current development Mac has 16 GB unified memory, which is below the outlined 24 GB tier.
No MLX runtime or local model is installed yet.

## Decision

Hearth will use MLX as its local inference runtime.
The current 16 GB Mac is a distinct hardware tier that requires local benchmark evidence before a generator model becomes the product default.
The first implementation installs the runtime and evaluates a smaller candidate generator, embedding model, and reranker against the local evaluation corpus.
An 8B 4-bit generator remains an optional experiment and cannot become the default unless it passes defined resident-memory, latency, citation, and abstention checks on this hardware.
Hearth will not use remote models, cloud APIs, or model fallback.
Specific model identities and benchmark thresholds will be recorded with the resulting evaluated configuration.

## Alternatives considered

### Require 24 GB or greater hardware

This would retain the original 8B default but would make the current development hardware unsuitable for the product baseline.

### Use an 8B 4-bit generator by default without local measurement

This would align with the original 24 GB recommendation but risks memory pressure and poor responsiveness on the current 16 GB tier.

### Use a remote model fallback for constrained hardware

Remote fallback would simplify model-size constraints but violates Hearth's local-first privacy promise.

## Consequences

Model selection is evidence-driven rather than assumed from a hardware tier that does not match the current machine.
The first local-model milestone requires a synthetic or public evaluation corpus and defined acceptance thresholds.
Model artifacts remain local runtime data and are excluded from version control.
The selected model configuration can later be superseded with benchmark evidence without changing the local-only boundary.

## Affected files and components

- Future MLX runtime integration
- Embedding, reranking, and generator adapters
- Local benchmark and evaluation configuration
- Runtime-model storage and ignore rules

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0001, ADR-0004, and ADR-0006.
No related commits, issues, or design notes exist yet.
