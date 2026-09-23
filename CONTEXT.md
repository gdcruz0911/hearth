# Hearth

Hearth is one person's local knowledge hub: a hearth is the centre of a home, and this is
the centre of a machine's documents. It indexes files you already own, in place, and
shows how they relate — with evidence you can point at, or nothing at all.

## Governing principle

**Analog**:
Hearth never acts without you. Every action that changes a file, a record, or a derived
index is previewed and then separately applied by a person. There is no watcher, no
background repair, no automatic reindex, no daemon. When Hearth notices something is
wrong it tells you and waits.
_Avoid_: Automatic, smart, seamless, background sync.

## Language

### Files and records

**Source**:
An original file on disk that a person chose to connect. Hearth reads it and never moves,
copies, rewrites, or deletes it except through an explicit organize action.
_Avoid_: Upload, attachment, asset.

**Document**:
Hearth's record of one Source — its pages, chunks, and provenance. Distinct from the
Source itself: removing a Document deletes the record, never the file.
_Avoid_: File, item, entry.

**Collection**:
Every Document in one database. The full extent of what Hearth can answer from.
_Avoid_: Library, corpus, workspace, vault.

**Source root**:
A folder a person has explicitly connected, making its supported files eligible for
review. Connecting a root grants permission to scan, not to import.
_Avoid_: Watched folder, scanned directory.

**Profile**:
A saved set of runtime paths — database, models, index, source roots — so a person need
not retype them. Configuration only; it holds no documents.

### Provenance

**Page**:
One unit of a Source as its extractor found it. A note is one page; a PDF has the pages it
has. Pages are never merged.

**Chunk**:
A contiguous span of one Page's text, stored with its character offsets. The smallest unit
Hearth retrieves, and an internal artifact — a person never asks for a chunk by name.
_Avoid_: Passage, segment, node.

**Provenance**:
The unbroken chain Document → Page → Chunk → Citation. Every claim Hearth makes must be
traceable back along it. This chain is the asset; everything else is replaceable.

**Extraction method**:
How a Page's text was obtained — native, or OCR. Carried through to Citation, because
OCR text deserves less trust and the reader should see that.

### Answering

**Evidence**:
A Chunk paired with a relevance score, retrieved for one question and not yet shown to
anyone. The input to answering.

**Citation**:
The rendered provenance of a Chunk that supported an answer — source, page, section,
quote, extraction method. What the person actually sees.
_Avoid_: Reference, footnote, link.

**Abstention**:
Hearth's refusal to answer when no retrieved Evidence supports one. A first-class,
correct outcome — never a failure or an empty state.
_Avoid_: No results, not found, error.

### Relationships

**Semantic index**:
Derived vectors for every Chunk in a Collection, stored in a versioned local directory.
Always rebuildable from Sources and provenance; never authoritative.
_Avoid_: Vector database, embeddings store.

**Relationship**:
A link between two Documents, carrying the specific cross-document Chunk pair that
explains it. An edge without its explaining pair is not a Relationship.
_Avoid_: Similarity, match, connection.

**Neighborhood**:
A cluster of Documents connected by Relationships. What the map draws.
_Avoid_: Cluster, group, topic.

**Map**:
The visual graph of Documents and their Relationships. An overview of evidence-backed
structure, not a picture of every file.
_Avoid_: Graph view, canvas.

### Attention

**Attention**:
Hearth's report that a Source needs a person's judgement — unavailable, changed, or
awaiting OCR review. Attention describes; it never repairs.
_Avoid_: Alert, warning, issue, error.

**Relink**:
Rebinding a Document to a replacement Source that matches its stored fingerprint exactly.
Recovers a binding; never searches the filesystem, moves, or re-extracts.

**Organize**:
One explicit same-volume rename or move of a Source, preserving the Document and its
Citations.
