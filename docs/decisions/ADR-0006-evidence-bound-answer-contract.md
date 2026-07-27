---
id: ADR-0006
title: Evidence-bound answer contract
status: accepted
date: 2026-07-27
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/answering.py
  - src/hearth/retrieval.py
  - model adapters
  - CLI answer rendering
related_decisions:
  - ADR-0001
  - ADR-0002
related_commits: []
related_issues: []
related_design_notes: []
---

# ADR-0006: Evidence-bound answer contract

## Context

Hearth must answer only from imported material and make uncertainty explicit.
Prompt instructions alone cannot guarantee that a model will cite only retrieved evidence or abstain when the evidence is insufficient.
The answer pipeline needs a contract that the application can validate independently of a model's fluency.

## Decision

Every non-abstained Hearth answer must include citations that reference only chunk IDs in that request's retrieved and approved evidence bundle.
The application validates citation IDs before rendering an answer.
An answer with no supporting evidence, invalid citations, or no citations is replaced with an explicit abstention.
Future local model adapters must produce structured answer data that preserves this evidence relationship.

## Alternatives considered

### Prompt-only citation instructions

Prompt instructions are simple but cannot reliably prevent unsupported claims or invented citations.

### Free-form answers with post-hoc citation matching

Post-hoc matching can attach evidence that was not actually used to form a claim and makes validation less precise.

### Permit uncited summaries

Uncited summaries improve apparent convenience but violate Hearth's evidence-first product promise.

## Consequences

The system can reject invalid citations deterministically and test abstention behavior.
Some plausible but unsupported answers will abstain instead of relying on model knowledge.
Model adapters and CLI rendering require a structured response schema rather than unconstrained plain text.
This rule validates source membership, not whether the source semantically supports every wording choice in a generated claim.

## Affected files and components

- `src/hearth/answering.py`
- `src/hearth/retrieval.py`
- Future local generator adapters
- CLI answer rendering
- Evaluation corpus and citation tests

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0001 and ADR-0002.
No related commits, issues, or design notes exist yet.
