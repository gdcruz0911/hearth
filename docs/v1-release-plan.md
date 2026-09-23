# Hearth v1 completion plan

## Purpose

Hearth v1 is a single-user, local-first document search application.
Its direction was re-scoped on 2026-09-21 to a personal knowledge hub; see [CONTEXT.md](../CONTEXT.md).
The command-line foundation is complete, and the local web interface is the final product layer.
The interface is a control surface over `HearthService`, not a second storage system or a cloud application.

## Shipped v1 workflow

1. Choose one local Markdown, text, or PDF file through the native macOS dialog.
2. Hearth indexes that original file in place and records page-level provenance in local SQLite.
3. Ask a question and receive retrieved evidence with citations, or an explicit abstention.
4. Review collection health, document metadata, page metadata, and OCR warnings without exposing document text or canonical paths by default.
5. Preview and explicitly apply a one-file rename, move, reindex, relink, or local-record removal.

## Interface boundary

`hearth web` starts the interface on `127.0.0.1` only.
Each launch creates a random capability URL and opens that URL in the default browser unless `--no-open` is supplied.
The browser contains no file upload input, account flow, telemetry, CORS policy, or remote asset.
Native selection returns a path only to the local Python process.
Import keeps the selected original in place rather than copying it into a managed library.
Canonical paths appear in the interface only when a user has explicitly requested an organization or relink preview.

## v1 acceptance criteria

- A new empty collection can import a public test note through the local chooser and render its metadata.
- Supported answers include source, page, chunk, extraction method, and evidence text.
- Unsupported questions abstain without an invented answer or citation.
- OCR evidence is marked for review.
- Health attention identifies unavailable and changed sources without scanning the filesystem.
- Rename, move, reindex, relink, and remove require a preview followed by a separate apply action.
- Remove changes only Hearth's local record and derived index data, never the source file.
- The server listens only on `127.0.0.1` and requires its per-launch capability URL.
- The default test suite, focused web suite, public evaluation corpus, and manual browser smoke test pass.
- The release diff contains no private documents, SQLite data, local paths, model files, prompts, responses, or secrets.

## Release procedure

1. Install the package in a clean local virtual environment.
2. Run the full unit and integration suite using the public fixtures.
3. Run the deterministic public evaluation corpus.
4. Start `hearth web`, confirm the listener is loopback-only, and exercise import, search, inspection, one preview/apply action, and removal with temporary public data.
5. Stop the server and verify that the temporary source file remains after removal.
6. Review `git status`, the staged diff, and runtime directories before committing or distributing the source.

## Explicitly deferred

Managed library copies, browser uploads, cloud synchronization, multi-user access, conversational memory, tags, collections, source watching, automatic repair, bulk actions, source-file deletion, cross-volume moves, undo history, and a production model-default decision are outside v1.
Each future expansion must preserve the local provenance and evidence contract or be governed by a new ADR.
