---
id: ADR-0021
title: Evidence-only answers
status: accepted
date: 2026-09-23
supersedes:
  - ADR-0007
  - ADR-0010
superseded_by: null
affected_components:
  - src/hearth/answering.py
  - src/hearth/cli.py
  - src/hearth/evaluation.py
  - src/hearth/runtime.py
related_decisions:
  - ADR-0006
  - ADR-0009
related_commits: []
related_issues: []
related_design_notes:
  - CONTEXT.md
---

# ADR-0021: Evidence-only answers

## Context

The 2026-09-21 re-scope made Hearth a knowledge hub whose product is the relationship map, not chat.
ADR-0010 already required generated factual text to be a verbatim span of cited evidence, so the opt-in generator could only choose which evidence to quote.
The default path already returns cited evidence directly.
The experimental claim-support checker existed only to validate generated wording and approved three of eight adversarial public claims.

## Decision

Hearth answers a search with retrieved evidence and its citations, or abstains. It generates no prose.
The MLX generator adapter, the structured generator response protocol, the claim-support checker, its corpus and CLI command, and the generator benchmark are removed.
ADR-0006, which binds every answer to cited evidence, is unchanged.
Runtime profiles that still contain `generator_model` load and ignore it.

## Consequences

One model, one CLI option, one evaluation command, and their tests and fixtures leave the codebase.
Paraphrase and multi-source synthesis are not available, as was already true in practice under ADR-0010.
Reintroducing generation requires a new ADR, and the removed code remains in Git history.

## Alternatives considered

### Keep the generator opt-in and frozen

It costs little while unused, but it keeps a model, a roadmap line, and a validation problem alive for a feature outside the product.
