# Semantic index rebuild responsiveness roadblock

**Date:** 2026-07-30  
**Status:** Addressed in the v1 implementation, with follow-up performance work deferred.

## What happened

The first real semantic-map rebuild ran as one synchronous web request over a personal collection containing hundreds of sources and thousands of evidence chunks.
The request occupied the local embedding runtime long enough that the browser appeared to time out and the Mac became noticeably less responsive.
The operation did not expose private content, but it gave no clear indication whether progress was being made, whether it was safe to stop, or whether the active semantic index would survive an interruption.

## Why this mattered

Hearth is local-first, so indexing intentionally uses the user's own hardware instead of sending document content to a remote service.
That privacy boundary does not excuse a workflow that feels stalled or makes the machine difficult to use.
An interrupted rebuild also must never leave the collection without its previously working semantic index.

## Changes made

- The web server starts rebuilds as background jobs instead of holding the request open.
- Index construction yields after small batches so the job can report progress and honor cancellation.
- The interface polls job state and shows completed evidence units, total units, and a cancel control.
- Each rebuild reports elapsed time, Hearth-process CPU time, peak process memory, and current evidence throughput without recording private collection data.
- Vectors are streamed into a staging index version instead of accumulating the entire collection in memory.
- Hearth activates a completed staging version atomically.
- Cancelling or failing a rebuild removes only the incomplete staging output and retains the previously active index.

## Validation

- The focused web tests verify that starting and cancelling a rebuild does not block the web server.
- The embedding tests verify that a cancelled rebuild retains the prior active index.
- The full automated suite passed with 89 tests and 4 optional skips after the background-job implementation.
- The local web interface was reviewed with a real connected collection without recording source names, file paths, or document content in version control.

## Remaining limitation

A large local rebuild still consumes CPU, memory, and local inference capacity while it runs.
Version one provides visible progress, local process measurements, and safe cancellation, but does not yet provide throttling, pause and resume, an estimated completion time, or persistent checkpointing across process restarts.

## GitHub issue draft

**Title:** Improve large-collection semantic index rebuild responsiveness and observability

**Suggested labels:** `performance`, `local-runtime`, `v1-follow-up`

### Problem

On a real personal collection with hundreds of sources and thousands of evidence chunks, a local semantic rebuild was perceived as a browser timeout and reduced overall Mac responsiveness.
The v1 fix moves the work into a cancellable background job and preserves the previous active index, but the runtime still needs measured performance and usability improvements for larger collections.

### Done so far

- Background rebuild jobs with progress reporting and cancellation.
- Atomic activation of completed index versions.
- Preservation of the prior active index after cancellation or failure.
- Streaming vector writes to avoid retaining the full rebuilt vector set in memory.

### Follow-up acceptance criteria

- Define a reproducible synthetic or public benchmark corpus that represents large local collections without committing private data.
- Record elapsed time, peak memory, and responsiveness metrics for supported local embedding configurations.
- Show a useful completion estimate only when it can be derived honestly from observed work.
- Offer a documented resource-use policy such as reduced-priority or user-selected throttling, if supported reliably on macOS.
- Evaluate resumable rebuilds or durable checkpoints without weakening atomic activation or leaving stale vectors active.
- Keep all content local and retain the ability to cancel safely at any point.

### Privacy note

Issue reports, benchmarks, and automated diagnostics must not include document text, source names, absolute paths, embeddings, prompts, or private runtime data.
