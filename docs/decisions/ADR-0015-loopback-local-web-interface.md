---
id: ADR-0015
title: Loopback local web interface
status: accepted
date: 2026-07-30
supersedes: null
superseded_by: null
affected_components:
  - src/hearth/web.py
  - src/hearth/web_assets/
  - src/hearth/cli.py
  - src/hearth/service.py
related_decisions:
  - ADR-0002
  - ADR-0004
  - ADR-0006
  - ADR-0012
  - ADR-0013
  - ADR-0014
related_commits: []
related_issues: []
related_design_notes:
  - docs/privacy.md
  - docs/v1-release-plan.md
---

# ADR-0015: Loopback local web interface

## Context

Hearth's CLI workflow now supports import, evidence-bound search, collection health, metadata inspection, explicit organization, and source relinking.
A visual v1 interface should expose those stable operations without introducing a second document model, a hosted service, browser-upload copies, or implicit file changes.
The interface handles private local documents and must preserve the path and provenance boundaries of prior decisions.

## Decision

Hearth provides `hearth web` as a local browser interface that reuses `HearthService` directly.
The process binds only to `127.0.0.1` and has no command-line option to bind to another host.
Each process launch creates a random capability token that is required in the initial URL and every local asset or API route.
The server rejects other host headers, sends no CORS response, disables caching, and sets a restrictive same-origin content security policy.

The first interface uses Python standard-library HTTP primitives and packaged static HTML, CSS, and JavaScript.
It has no external CDN, analytics, account system, telemetry, cloud API, browser upload field, or separate persistence layer.
The only persisted product data remains the local SQLite provenance store and existing derived index directories.

The browser invokes native macOS selection through the local backend for import, move-target selection, and relink selection.
Import indexes the selected original in place and does not retain an upload or managed copy.
The selected import path remains in the local process and is not rendered to the browser.
Canonical paths are rendered only within an explicit organization or relink preview.

Mutating interface actions use an in-memory, single-use preview identifier.
The user first requests a preview, then separately applies that exact preview before it expires.
The service revalidates the same existing safeguards at apply time.
Reindex and record removal use the selected document ID so their canonical path remains inside the service boundary.

## Alternatives considered

### Browser file uploads

Browser uploads conceal the original Finder path and create a temporary or managed copy.
That changes Hearth from an in-place index and organizer into a managed library, which needs retention, recovery, ownership, and removal decisions that are outside v1.

### A frontend framework and separate API service

A second runtime and data boundary would add deployment and privacy surface without adding v1 product capability.
The existing local Python package can serve this focused interface directly.

### A native SwiftUI application

SwiftUI remains a viable future surface for deeper macOS integration.
It would require a new application package and native service boundary.
The loopback interface is the smaller v1 layer over the already verified core.

### A LAN or public server

Network exposure would require authentication, origin policy, transport security, and a broader privacy review.
Hearth v1 is deliberately single-user and loopback-only.

## Consequences

Users can interact with the complete personal collection workflow from a browser while data processing stays on the local Mac.
The UI keeps source paths out of normal health, list, inspection, import, and search views.
The random URL is a local process capability, not an account or a substitute for operating-system access control.

The local web server must run while the interface is in use.
Native chooser operation is macOS-specific.
An ordinary browser still displays answer evidence and explicitly requested path previews, so users should treat browser history, screenshots, and screen sharing as local privacy surfaces.

## Reversibility and future change

The web server can be stopped without changing the collection.
The source and service layers remain callable through the CLI, so a future native interface can replace the browser surface without migrating document data.
Managed copies, browser uploads, external access, source watching, bulk actions, or automatic repair require a new ADR with explicit storage, consent, authentication, recovery, and deletion rules.

## Affected files and components

- `src/hearth/web.py`
- `src/hearth/web_assets/`
- `src/hearth/cli.py`
- `src/hearth/service.py`
- Local loopback listener and browser process

## Related decisions, commits, issues, and design notes

Related decisions: ADR-0002, ADR-0004, ADR-0006, ADR-0012, ADR-0013, and ADR-0014.
Related design notes: `docs/privacy.md` and `docs/v1-release-plan.md`.
No related commits or issues exist yet.
