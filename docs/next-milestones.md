# Hearth next milestones

The v1 completion scope is recorded in [the v1 release plan](v1-release-plan.md).
This roadmap describes the work deliberately deferred beyond that local product baseline.
It is not an accepted Architecture Decision Record and does not select implementation options that require approval.

## 1. Improve claim support and broader reranking quality

The optional local generator and reranker pass narrow public evidence and abstention checks, while the tightened local claim-support checker passed five of eight adversarial public cases and still made three false approvals.
Reranking only selects evidence and cannot validate generated factual wording.
Evaluate prompt or model candidates against a larger adversarial corpus before proposing any claim-support ADR.
Keep the checker outside the answer path until a decision record sets an explicit threshold, fallback, and rollback behavior.

## 2. Create the local evaluation corpus and release gate

The baseline harness covers one supported citation and one abstention using only synthetic public material.
The MVP workflow now covers collection health, citation-first output, reindexing, and removal through the CLI.
Expand the public corpus with expected outcomes for additional OCR warning, deletion, and source-freshness cases before selecting a production model.
Record model, retrieval, and prompt configuration alongside each evaluation run without storing private prompts or responses in Git.

## 3. Maintain the private source repository boundary

Review the staged file list and diff for private documents, runtime data, credentials, absolute local paths, prompts, and responses.
Commit and push only coherent source-only milestones after that review.

## 4. Build a local model-evaluation leaderboard

Evaluate pre-provisioned local generators, embedding models, and rerankers against a fixed synthetic or public corpus.
Record model identity and fingerprint, chunking version, retrieval configuration, citation and abstention outcomes, malformed-response rate, latency, and peak memory.
Do not use private documents, prompts, responses, or runtime paths in version-controlled leaderboard data.
Do not automatically select a model from leaderboard results.
Use the results to propose an explicit ADR for a default model configuration.

## 5. Improve claim support and memory boundaries

ADR-0010 requires generated factual text to be a normalized verbatim span from cited evidence.
Evaluate a local claim-support checker separately from reranking because better retrieval alone cannot validate generated wording.
Any future claim-support mechanism must supersede or supplement ADR-0010 through a new ADR with explicit evaluation thresholds and rollback behavior.
Hearth currently has no conversational memory and answers only from the currently imported collection.
If a future feature persists conversation state, define its local storage, deletion, retention, and citation boundaries in a new ADR.

## 6. Extend the local organization workflow carefully

The v1 `organize` command supports one explicit same-volume move or extension-preserving rename with preview and no overwrite behavior.
The v1 `relink` command supports one explicit fingerprint-verified rebinding of an unavailable source without moving or reindexing it.
Add tags, collections, bulk operations, deletion, cross-volume moves, or undo history only with a new ADR that defines confirmation, collision, rollback, and recovery behavior.

## 7. Decide whether to add a local source watcher

The current `health` command diagnoses source changes only when the user runs it and never repairs them.
Consider a local-only watcher only if regular manual health checks become inadequate.
Any watcher must preserve explicit reindexing, avoid copying sources into a managed library, and define consent, resource use, and notification behavior in a new ADR.

## 8. Measure and improve semantic-index rebuild throughput

The v1 implementation now runs semantic rebuilds as cancellable background jobs and preserves the previous active index on interruption.
Use only synthetic or public benchmarks to measure elapsed time, peak memory, and responsiveness for larger collections.
Consider honest completion estimates, resource throttling, and resumable rebuilds only if they preserve local-only processing and atomic index activation.
The observed constraint and the prepared external issue draft are recorded in [the semantic-index rebuild responsiveness roadblock note](semantic-index-rebuild-responsiveness.md).
