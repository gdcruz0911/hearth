# Privacy

## Local runtime boundary

Imported documents, SQLite databases, retrieval artifacts, local model weights, prompts, and responses are runtime data that remain on the user’s Mac.
Derived OCR PDFs are deleted after local text extraction by default and remain runtime data only when explicitly retained for inspection.
SQLite stores a local SHA-256 source fingerprint with file size and modification time to diagnose stale extraction without retaining document bytes.
The current ingestion, retrieval, and answer paths use local files, SQLite, and local command-line tools.
The current application code does not configure telemetry, a cloud database, a hosted vector service, a cloud model API, or a cloud fallback.
The optional `hearth web` interface binds to `127.0.0.1` only and serves only first-party packaged assets with no CORS policy or external resources.
Each launch uses a random local capability URL and does not log that URL through the HTTP request handler.

This is not a claim that every operation associated with development is offline.
Installing Python packages and downloading a model are explicit provisioning operations that can contact external registries.
The optional benchmark itself uses a pre-provisioned filesystem model path and sets Hugging Face Hub offline mode, but that environment setting is not a whole-process network firewall.

## Source control boundary

GitHub may store source code, ADRs, tests, documentation, configuration examples, and synthetic or public fixtures.
GitHub must not store real PDFs, private notes, OCR text from private documents, SQLite databases, indexes, model weights, credentials, prompts, responses, or absolute local paths.

The repository `.gitignore` excludes common runtime directories, database formats, model formats, credentials, and PDFs by default.
Public or synthetic PDF fixtures are allowed only under `tests/fixtures/public/`.

## Operational guidance

Keep any explicitly retained OCR output and local databases outside version control.
Review `git status` and the staged diff before every commit.
Use only synthetic or public data in tests and version-controlled evaluation corpora.
Treat any file containing canonical source paths as private runtime data because paths can reveal user names and directory structure.
Source fingerprints and file metadata are also private runtime provenance data and must remain outside version control.
The explicit `organize` command may display the source and target path you selected in your local terminal so you can confirm the operation.
It does not transmit those paths or add them to version-controlled output.
The explicit `relink` command may display an unavailable source path and replacement path you selected in your local terminal.
It validates the replacement locally and does not scan unrelated directories or modify either file.
The `health` command reports source attention by document ID and display name without printing canonical paths or stored document text.
The web interface uses a native macOS chooser for imports rather than a browser upload, so selected original files remain in place and their paths stay inside the local backend.
The interface displays canonical paths only in a user-requested organization or relink preview.
Connected source roots are private runtime configuration that may include absolute paths.
The source-root review scans only folders named in that profile and sends the browser folder names, availability, counts, and candidate file names without canonical source paths.
It does not extract file text until the user approves the reviewed import plan.

## Release verification

Before declaring a release local-only, verify that the installed runtime has no unexpected outbound network behavior while importing, indexing, answering, browsing the local interface, and benchmarking with pre-provisioned models.
Confirm that `hearth web` listens only on `127.0.0.1` during that check.
