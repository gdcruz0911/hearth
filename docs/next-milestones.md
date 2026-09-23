# Hearth next milestones

The v1 completion scope is recorded in [the v1 release plan](v1-release-plan.md).
The 2026-09-21 re-scope in [CONTEXT.md](../CONTEXT.md) makes the relationship map the product.
Hearth has one user and one machine; milestones are ordered by what most reduces the risk that the map is not worth having.
This roadmap is not an Architecture Decision Record.

## 1. Remove the relationship-matrix ceiling

`_mutual_best_chunk_pairs` in `embedding.py` materializes an N by N similarity matrix, which stops being reasonable near 15000 chunks.
Replace it with blocked top-k so memory stays bounded, and verify on a synthetic collection sized like the author's real one.

## 2. Validate the map on a real collection

Run the map against the author's own documents with an already provisioned embedding model before building provisioning.
While reviewing, label document pairs that should and should not be related.
Keep those labels private and outside Git, and derive a small synthetic public mirror for the repository.
Decide from this whether the map earns its place.

## 3. Implement semantic retrieval as the default with one-command model provisioning

Choose the default embedding model during milestone 2, then implement ADR-0019 and ADR-0020 once for that model.
Provisioning is a single explicit CLI command that names the model and size, asks for consent, downloads, verifies the fingerprint, and writes the profile.

## 4. Tune relationship parameters against labeled pairs

`_MUTUAL_NEIGHBORS` and `relationship_minimum_score` have no measurement behind them.
Tune them only against the labeled relationship corpus from milestone 2.

## 5. Overhaul the web interface

Redesign the interface once the map shows real relationships, so layout decisions are made against real data.

## 6. Measure and improve semantic-index rebuild throughput

Semantic rebuilds run as cancellable background jobs and preserve the previous active index on interruption.
Use only synthetic or public benchmarks to measure elapsed time, peak memory, and responsiveness for larger collections.
The observed constraint is recorded in [the semantic-index rebuild responsiveness roadblock note](semantic-index-rebuild-responsiveness.md).

## Out of scope

Generated answers and claim support (ADR-0021), source watchers and background repair (the analog principle in CONTEXT.md), storage hygiene, cloud services, telemetry, and model leaderboards.
Tags, bulk operations, deletion, cross-volume moves, and undo for organize require a new ADR if they are ever needed.
