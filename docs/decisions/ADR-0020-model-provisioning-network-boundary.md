---
id: ADR-0020
title: Model provisioning network boundary
status: accepted
date: 2026-09-21
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/embedding.py
  - src/hearth/runtime.py
  - src/hearth/cli.py
  - src/hearth/web.py
related_decisions:
  - ADR-0005
  - ADR-0008
  - ADR-0019
related_commits: []
related_issues: []
related_design_notes:
  - docs/privacy.md
---

# ADR-0020: Model provisioning network boundary

## Context

ADR-0019 makes semantic retrieval the default, which requires an embedding model to be present at first run.
`MLXEmbedder` sets `HF_HUB_OFFLINE=1` and rejects a model repository identifier, so Hearth cannot obtain a model itself.
Provisioning is currently a manual step performed outside the application.

Hearth's privacy documentation states that the application has no configured cloud service, telemetry, or cloud-model fallback.
That statement conflates two different promises: that a person's documents never leave the machine, and that the application never makes a network request.
Only the first is a guarantee a person depends on.
The second is incidental strictness that has been preserved without being named or examined.

## Decision

Hearth states two separate promises.

The document boundary is absolute.
Document text, extracted pages, chunks, embeddings, prompts, questions, answers, filenames, and source paths are never transmitted anywhere, under any configuration.
No setting relaxes this.

The network boundary is bounded and explicit.
Hearth may perform exactly one class of network request: downloading a named model to the local model directory, after a person explicitly consents to that specific download in that moment.
The prompt names the model and its size.
Declining leaves Hearth functional on the explicit no-model path.
Hearth performs no other request, including update checks, telemetry, and error reporting.

Downloaded weights are verified against the expected fingerprint before use.
`HF_HUB_OFFLINE=1` remains set for all inference.

`docs/privacy.md` presents these as two separate guarantees rather than one combined claim.

## Consequences

A person can install Hearth and reach a populated map without provisioning a model by hand.

Hearth can no longer claim it never touches the network.
It can claim, more precisely and more usefully, that documents never leave the machine.

Consent is per-download and not remembered as a general permission.

## Alternatives considered

### Bundle the model weights in the distribution

335 MB cannot reasonably be version-controlled, and the figure grows with each additional model.
The absolute no-network property it preserves is not one a person benefits from.

### Keep manual provisioning

Zero implementation cost, and the map stays empty for every new installation.
This contradicts ADR-0019 directly.

### Treat model download as covered by the existing privacy statement

The existing statement would become inaccurate without being corrected, which is worse than narrowing it deliberately.
