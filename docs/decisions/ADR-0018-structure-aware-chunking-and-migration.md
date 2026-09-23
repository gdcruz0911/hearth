---
id: ADR-0018
title: Structure-aware chunking and chunk version migration
status: accepted
date: 2026-09-21
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/chunking.py
  - src/hearth/store.py
  - src/hearth/service.py
related_decisions:
  - ADR-0002
  - ADR-0008
  - ADR-0013
  - ADR-0017
related_commits: []
related_issues: []
related_design_notes:
  - docs/architecture.md
---

# ADR-0018: Structure-aware chunking and chunk version migration

## Context

`chunk_page` split text only between sentences detected by terminal punctuation.
Markdown headings and list items carry no terminal punctuation, so a bullet list was recognized as one sentence and emitted as one chunk regardless of length.
A 200-item list produced a single 1603-word chunk, while equivalent prose produced six chunks of 400 words.
Notes in Markdown are Hearth's primary corpus, so the failure applied to the common case.

`target_tokens` counts whitespace-separated words, not model tokens.
The Qwen3 tokenizer produces roughly 1.3 to 1.5 tokens per English word, so the configured 400 corresponds to approximately 550 model tokens.

Changing chunking has a migration cost that the current design hides.
`reindex_document` re-extracts from the original source file, so a document whose source is unavailable under ADR-0013 cannot be rechunked.
`CHUNKING_VERSION` is recorded only in the semantic index manifest and never per document in SQLite.
A rebuild therefore stamps the manifest with the current constant regardless of which chunker produced the chunks in the database, so a partially reindexed collection reports a version it does not have.

## Decision

Chunking recognizes document structure in addition to sentence boundaries.
Markdown headings, list items, and blank lines are chunk boundaries.
A single unit that exceeds the target is split rather than emitted whole, so no input can produce an unbounded chunk.

The word-count estimate is retained and the parameter is renamed to state that it counts words, not tokens.
Chunking is deliberately not coupled to a model tokenizer.

SQLite records the chunking version per document.
`CollectionHealth` reports documents whose stored chunking version differs from the current one, as an Attention state requiring an explicit reindex.
The semantic index manifest records the chunking version only when every indexed document agrees.

## Consequences

Markdown notes produce chunks that follow their structure instead of one chunk per list.
No chunk can exceed the configured bound.

Existing collections hold chunks from the previous chunker and must be reindexed document by document.
Health makes that visible rather than silent.
A document whose source has moved cannot be reindexed until it is relinked or restored, and remains at its old chunking version until then.
This is reported, not repaired, consistent with the analog principle.

## Alternatives considered

### Hard-split oversized sentences only

This bounds chunk length without addressing the cause.
A bullet list would be cut into arbitrary word-count blocks mid-item rather than grouped by item.

### Use the embedding model's tokenizer for exact token counts

Accurate counts would couple `CHUNKING_VERSION` to the model identity.
Every future embedding-model change would then force a full re-extraction of every document from source, and any document with an unavailable source would become permanently unreindexable.
Hearth is about to evaluate new embedding models, so this cost would be paid repeatedly.

### Record no chunking version per document

The existing manifest field would continue to report a version the collection does not uniformly hold.
