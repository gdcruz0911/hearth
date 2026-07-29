---
id: ADR-0010
title: Verbatim evidence answer contract
status: accepted
date: 2026-07-29
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/answering.py
  - structured generator prompt
  - generator evaluation corpus
  - CLI answer rendering
related_decisions:
  - ADR-0006
  - ADR-0007
  - ADR-0009
related_commits: []
related_issues: []
related_design_notes:
  - docs/evaluation.md
  - docs/next-milestones.md
---

# ADR-0010: Verbatim evidence answer contract

## Context

ADR-0006 validates that citations come from the approved evidence bundle.
ADR-0009 rejects answers supported only by unrelated retrieved evidence.
Neither rule proves that a local generator's factual wording is present in a cited source.
The generator could otherwise paraphrase or invent a claim while returning a valid citation ID.

## Decision

For v1, a supported answer produced by `StructuredGeneratorAnswerer` must be a non-empty contiguous span from one of its cited evidence quotes after deterministic normalization.
Normalization applies Unicode NFC, case folding, and whitespace collapsing only.
The application abstains if the generated answer is not a normalized contiguous span of a cited quote.
The generator prompt instructs the model to select a source excerpt rather than paraphrase, combine excerpts, or add factual words.
Direct evidence rendering remains unchanged because it already returns source text.

## Alternatives considered

### Keep the lexical evidence-sufficiency gate alone

The lexical gate detects unrelated evidence but can accept an invented answer that shares a few words with a cited quote.

### Use a local reranker as claim verification

A reranker can improve evidence selection but does not deterministically establish that a generated claim is entailed by a source passage.

### Use a local model to self-verify claims

Model self-verification adds another probabilistic model judgment without an independent deterministic boundary.

### Permit unrestricted generated summaries

Unrestricted prose is more conversational but violates Hearth's fail-closed evidence-first v1 goal.

## Consequences

Generated answers cannot introduce factual words absent from the cited passage.
The application can safely reject a model response even when its citation IDs are syntactically valid.
Valid paraphrases, cross-document synthesis, translation, and grammatical rewriting can abstain because they are not verbatim evidence spans.
A short verbatim span can omit a source qualifier or negate context, so this policy does not prove full semantic entailment.
Reranking remains a retrieval concern rather than a claim-validation mechanism.

## Reversibility and future change

This policy is intentionally conservative and reversible.
A future version may replace or supplement it with a locally evaluated claim-support mechanism only through a new ADR that defines the model, evidence corpus, acceptance thresholds, failure handling, privacy boundary, and rollback path.
That later ADR must supersede ADR-0010 rather than rewriting this decision.
Until then, the application favors abstention over generated paraphrase.

## Affected files and components

- `src/hearth/answering.py`
- Structured generator prompt and parser
- Generator evaluation corpus
- CLI answer rendering

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0006, ADR-0007, and ADR-0009.
Related design notes: `docs/evaluation.md` and `docs/next-milestones.md`.
No related commits or issues exist yet.
