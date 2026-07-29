---
id: ADR-0009
title: Lexical evidence sufficiency gate
status: accepted
date: 2026-07-29
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/retrieval.py
  - src/hearth/service.py
  - evaluation corpus
related_decisions:
  - ADR-0006
  - ADR-0008
related_commits: []
related_issues: []
related_design_notes:
  - docs/next-milestones.md
---

# ADR-0009: Lexical evidence sufficiency gate

## Context

The first host-side semantic-index evaluation retrieved a document chunk for an unsupported annual-budget question.
The preconfigured direct evidence answerer treated any retrieved chunk as sufficient and returned a cited answer.
ADR-0006 validates citation membership but deliberately does not establish semantic support for a claim.
Hearth needs a conservative local check before rendering a supported answer.

## Decision

For this v1 milestone, Hearth returns a supported answer only when at least one cited evidence quote shares a non-stopword term with the question.
If no cited quote passes this check, Hearth returns its standard abstention with no citations.
The check runs after citation membership validation and applies to both direct-evidence and generated answers.

## Alternatives considered

### Fixed embedding-score threshold

A score threshold can reject weak retrieval but needs calibration across model versions, documents, and query styles.
The observed result alone does not provide sufficient evaluation data to choose a robust threshold.

### Local reranker or claim-support checker

A local cross-encoder or entailment-style checker could handle paraphrases more effectively.
It introduces a model-dependent decision and needs a broader evaluation corpus before it can be relied upon as the product boundary.

### Accept any retrieved evidence

This is simple but permits the observed unsupported answer and violates Hearth's explicit-abstention promise.

## Consequences

The gate is deterministic, local, and straightforward to test.
It prevents answers when semantic retrieval returns unrelated chunks without relying on an uncalibrated similarity score.
It is intentionally conservative and can abstain when a valid paraphrased question shares no terms with its cited evidence.
The project roadmap requires evaluating a local reranker or claim-support checker to replace or supplement this gate.

## Affected files and components

- `src/hearth/retrieval.py`
- `src/hearth/service.py`
- Semantic retrieval evaluation
- Source-bound answer validation

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0006 and ADR-0008.
Related design note: `docs/next-milestones.md`.
No related commits or issues exist yet.
