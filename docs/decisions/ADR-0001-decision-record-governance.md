---
id: ADR-0001
title: Decision record governance
status: accepted
date: 2026-07-27
supersedes: null
superseded_by: null
affected_components:
  - docs/decisions
related_decisions: []
related_commits: []
related_issues: []
related_design_notes: []
---

# ADR-0001: Decision record governance

## Context

Hearth will accumulate product and engineering choices whose rationale needs to remain understandable and indexable as part of the local knowledge base.
Material decisions should be recorded without turning routine implementation work into process overhead.

## Decision

Hearth will record each material, durable product or engineering decision in a standalone Markdown Architecture Decision Record under `docs/decisions/`.
Files use stable, zero-padded, sequential names in the form `ADR-0001-short-title.md`.
An ADR is proposed until the user explicitly approves it.
Only then may it be marked accepted.
The directory index at `docs/decisions/README.md` is maintained manually whenever an ADR is added or its status changes.
An ADR uses frontmatter that includes its ID, title, status, date, supersession fields, affected components, and relationship arrays.
Statuses are `proposed`, `accepted`, `rejected`, and `superseded`.
A changed decision requires a new ADR that names the earlier record in `supersedes`.
The earlier record is retained and marked `superseded` with its `superseded_by` field set.
Commit references must use an actual immutable commit SHA when one exists.

## Alternatives considered

### One growing decisions document

A single document is easy to start but becomes difficult to link, index, and preserve as individual historical records.

### Unstructured decision notes

Free-form notes are flexible but do not provide consistent metadata for later graph indexing or review.

### Automatically generated decision index

Automation could prevent manual index drift but would introduce tooling and validation requirements before the project needs them.
The index remains intentionally manual until such automation is justified and tested.

## Consequences

Material decisions receive a consistent, durable record with stable identifiers and explicit approval.
The decision history remains readable in a file browser and can later be indexed as graph nodes.
Maintaining the index manually adds a small review obligation when ADRs change.
Routine local implementation details do not receive ADRs.

## Threshold examples

ADR-worthy decisions include choosing SQLite versus another persistence system, selecting an inference runtime, selecting a retrieval/index strategy, defining the document-provenance model, or changing a local-only security boundary.
Non-ADR decisions include renaming a variable, placing a test fixture, formatting code, adding a one-call helper, or making a localized bug fix that does not change system behavior or a public contract.

## Affected files and components

- `docs/decisions/`
- Project decision-review workflow

## Related decisions, commits, issues, and design notes

No related records exist yet.
