---
id: ADR-0007
title: Structured local generator response
status: accepted
date: 2026-07-27
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/answering.py
  - src/hearth/service.py
  - src/hearth/cli.py
  - local generator adapter
  - evaluation corpus
related_decisions:
  - ADR-0005
  - ADR-0006
related_commits: []
related_issues: []
related_design_notes: []
---

# ADR-0007: Structured local generator response

## Context

ADR-0006 requires generated answers to identify only chunks from the request’s approved evidence bundle.
The local generator is untrusted output even when it runs on-device.
The application needs a response protocol that it can validate before rendering an answer.

## Decision

The local generator returns exactly one JSON object with the keys `status`, `answer`, and `citation_chunk_ids`.
Supported answers use `status` `supported`, a non-empty answer string, and one or more unique integer chunk IDs.
Abstentions use `status` `abstained`, an empty answer string, and an empty citation ID list.
The application rejects malformed JSON, unknown keys, invalid schema, duplicate IDs, and IDs outside the approved evidence bundle by returning an explicit abstention.
The application resolves document names, pages, sections, quotes, extraction metadata, and OCR metadata from the original evidence rather than trusting model-generated citation text.

## Alternatives considered

### Free-form text with inline citations

Free-form output is easier for a model to produce but cannot be safely validated as a machine-readable evidence relationship.

### Grammar-constrained JSON decoding

Grammar-constrained decoding could provide stronger syntax guarantees but adds model and runtime complexity before benchmark evidence shows that it is needed.

### Continue returning direct retrieved evidence

Direct evidence is safe and remains the default path without a configured generator, but it does not provide generated document-chat answers.

## Consequences

Malformed but otherwise useful model answers will abstain.
The response format is moderately reversible because generated answers are not persisted, but changing it requires adapter, evaluation, and test updates.
The protocol checks evidence membership and schema validity but does not prove that every generated claim is semantically entailed by its citations.

## Affected files and components

- `src/hearth/answering.py`
- `src/hearth/service.py`
- `src/hearth/cli.py`
- Local MLX generator adapter
- Evaluation corpus and citation tests

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0005 and ADR-0006.
No related commits, issues, or design notes exist yet.
