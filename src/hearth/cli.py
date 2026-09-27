from __future__ import annotations

import argparse
from pathlib import Path

from .domain import (
    CollectionHealth,
    DocumentInspection,
    ImportedDocument,
    ImportError,
    ImportSummary,
    SourceImportPlan,
    SourceImportResult,
)
from .embedding import EmbeddingError, FlatVectorIndex, IndexError, MLXEmbedder
from .evaluation import (
    EvaluationCorpusError,
    evaluate_corpus,
    load_evaluation_corpus,
)
from .retrieval import MLXLocalReranker, RerankerError
from .runtime import (
    RuntimeProfile,
    RuntimeProfileError,
    default_source_roots,
    load_runtime_profile,
    write_runtime_profile,
)
from .service import HearthService
from .web import HearthWebServer
from .workbench import tasks, usage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local-only, evidence-bound knowledge hub.",
        epilog=(
            "Common workflow:\n"
            "  import <local-file>  Add one note or PDF.\n"
            "  health               Check collection attention items.\n"
            "  search <question>    Answer from cited evidence.\n"
            "  list                 Find document IDs to inspect.\n"
            "Run a command with --help to see its arguments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--profile", type=Path, help="Private JSON runtime profile. Explicit options override it.")
    parser.add_argument("--database", type=Path, help="Private SQLite provenance database.")
    parser.add_argument(
        "--source-root",
        type=Path,
        action="append",
        help="Connected local folder to scan after explicit preview and approval. May be repeated.",
    )
    parser.add_argument(
        "--ocr-output-directory",
        type=Path,
        help="Private local scratch directory for OCRmyPDF. Enables local OCR fallback.",
    )
    parser.add_argument(
        "--retain-ocr-output",
        action="store_true",
        help="Keep OCR-derived PDFs in --ocr-output-directory for local inspection.",
    )
    parser.add_argument(
        "--embedding-model",
        type=Path,
        help="Pre-provisioned local MLX embedding model directory. Requires --index-directory.",
    )
    parser.add_argument(
        "--index-directory",
        type=Path,
        help="Private local directory for the derived semantic index. Requires --embedding-model.",
    )
    parser.add_argument(
        "--reranker-model",
        type=Path,
        help="Pre-provisioned local MLX reranker directory. Enables reranking retrieved evidence.",
    )
    parser.add_argument(
        "--relationship-minimum-score",
        type=_relationship_minimum_score,
        help="Minimum cosine similarity for a semantic map relationship, from 0 to 1.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    importer = subcommands.add_parser("import", help="Import a local note or configured PDF.")
    importer.add_argument("path")
    search = subcommands.add_parser("search", help="Answer from retrieved evidence or abstain.")
    search.add_argument("question")
    reindexer = subcommands.add_parser("reindex", help="Re-extract and replace one document's derived index.")
    reindexer.add_argument("path")
    remover = subcommands.add_parser("remove", help="Remove a document and its derived records.")
    remover.add_argument("path")
    usage.add_parser(subcommands)
    tasks.add_parser(subcommands)
    subcommands.add_parser("list", help="List imported documents without document text or source paths.")
    subcommands.add_parser("health", help="Summarize private collection health without document text or source paths.")
    inspector = subcommands.add_parser("inspect", help="Inspect one document's local provenance metadata.")
    inspector.add_argument("document_id", type=int)
    sources = subcommands.add_parser("sources", help="Preview or import supported files from connected local folders.")
    sources.add_argument("action", choices=("preview", "import"))
    web = subcommands.add_parser("web", help="Run the local Hearth web interface on this Mac only.")
    web.add_argument("--port", type=_port, default=8765, help="Loopback port to use (default: 8765).")
    web.add_argument("--no-open", action="store_true", help="Do not open the local interface in the default browser.")
    evaluator = subcommands.add_parser("evaluate", help="Run a local synthetic or public evaluation corpus.")
    evaluator.add_argument("corpus", type=Path)
    profile = subcommands.add_parser("profile", help="Create one private runtime profile for repeatable Hearth commands.")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_create = profile_commands.add_parser("create", help="Create a new profile without overwriting an existing file.")
    profile_create.add_argument("path", type=Path)
    profile_create.add_argument("--database", type=Path, required=True)
    profile_create.add_argument("--embedding-model", type=Path)
    profile_create.add_argument("--index-directory", type=Path)
    profile_create.add_argument("--reranker-model", type=Path)
    profile_create.add_argument("--ocr-output-directory", type=Path)
    profile_create.add_argument("--retain-ocr-output", action="store_true")
    profile_create.add_argument("--relationship-minimum-score", type=_relationship_minimum_score, default=0.72)
    profile_create.add_argument(
        "--source-root",
        type=Path,
        action="append",
        help="Connected local folder. Defaults to Desktop, Documents, and Downloads when omitted.",
    )
    args = parser.parse_args(argv)
    if args.command == "usage":
        return usage.run(args)
    if args.command == "task":
        return tasks.run(args)
    if args.command == "profile":
        if (args.embedding_model is None) != (args.index_directory is None):
            parser.error("--embedding-model and --index-directory must be provided together.")
        if args.retain_ocr_output and args.ocr_output_directory is None:
            parser.error("--retain-ocr-output requires --ocr-output-directory.")
        try:
            profile_path = write_runtime_profile(
                args.path,
                RuntimeProfile(
                    database=args.database,
                    embedding_model=args.embedding_model,
                    index_directory=args.index_directory,
                    reranker_model=args.reranker_model,
                    ocr_output_directory=args.ocr_output_directory,
                    retain_ocr_output=args.retain_ocr_output,
                    relationship_minimum_score=args.relationship_minimum_score,
                    source_roots=tuple(args.source_root) if args.source_root else default_source_roots(),
                ),
            )
        except RuntimeProfileError as exc:
            parser.error(str(exc))
        print(f"Created private runtime profile: {profile_path}")
        return 0
    try:
        profile = load_runtime_profile(args.profile) if args.profile is not None else RuntimeProfile()
    except RuntimeProfileError as exc:
        parser.error(str(exc))
    args.database = args.database or profile.database or Path(".hearth/hearth.sqlite")
    args.source_roots = tuple(args.source_root) if args.source_root else profile.source_roots
    args.embedding_model = args.embedding_model or profile.embedding_model
    args.index_directory = args.index_directory or profile.index_directory
    args.reranker_model = args.reranker_model or profile.reranker_model
    args.ocr_output_directory = args.ocr_output_directory or profile.ocr_output_directory
    args.retain_ocr_output = args.retain_ocr_output or profile.retain_ocr_output
    args.relationship_minimum_score = (
        args.relationship_minimum_score
        if args.relationship_minimum_score is not None
        else profile.relationship_minimum_score
    )
    if (args.embedding_model is None) != (args.index_directory is None):
        parser.error("--embedding-model and --index-directory must be provided together.")
    if args.retain_ocr_output and args.ocr_output_directory is None:
        parser.error("--retain-ocr-output requires --ocr-output-directory.")
    service: HearthService | None = None
    try:
        semantic_index = (
            FlatVectorIndex(args.index_directory, MLXEmbedder(args.embedding_model))
            if args.embedding_model is not None
            else None
        )
        reranker = MLXLocalReranker(args.reranker_model) if args.reranker_model is not None else None
        service = HearthService(
            args.database,
            reranker=reranker,
            semantic_index=semantic_index,
            ocr_output_directory=args.ocr_output_directory,
            retain_ocr_output=args.retain_ocr_output,
            relationship_minimum_score=args.relationship_minimum_score,
        )
        if args.command == "import":
            _print_import_summary("Imported", service.import_with_summary(args.path))
        elif args.command == "reindex":
            _print_import_summary("Reindexed", service.reindex_with_summary(args.path))
        elif args.command == "remove":
            if service.remove_document(args.path):
                print("Removed.")
            else:
                print("No matching document found.")
                print("Next: run list to review imported documents and their IDs.")
        elif args.command == "list":
            _print_documents(service.list_documents())
        elif args.command == "health":
            _print_collection_health(service.collection_health())
        elif args.command == "inspect":
            inspection = service.inspect_document(args.document_id)
            if inspection is None:
                print(f"No imported document with ID {args.document_id}.")
                print("Next: run list to review imported documents and their IDs.")
                return 1
            _print_document_inspection(inspection)
        elif args.command == "sources":
            plan = service.plan_source_import(args.source_roots)
            if args.action == "preview":
                _print_source_import_plan(plan)
            else:
                _print_source_import_result(service.import_source_plan(plan))
        elif args.command == "web":
            server = HearthWebServer(service, port=args.port, source_roots=args.source_roots)
            print(f"Hearth is running locally at {server.url}", flush=True)
            print("It is bound to 127.0.0.1 only. Press Ctrl+C to stop it.", flush=True)
            if not args.no_open:
                server.open_browser()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nHearth web interface stopped.")
            finally:
                server.close()
        elif args.command == "evaluate":
            outcomes = evaluate_corpus(service, load_evaluation_corpus(args.corpus))
            for outcome in outcomes:
                detail = "" if outcome.passed else f": {'; '.join(outcome.errors)}"
                print(f"{outcome.case_id}: {'PASS' if outcome.passed else 'FAIL'}{detail}")
            passed_count = sum(outcome.passed for outcome in outcomes)
            print(f"Summary: {passed_count}/{len(outcomes)} cases passed.")
            return 0 if passed_count == len(outcomes) else 1
        elif args.command == "search":
            answer = service.answer(args.question)
            _print_answer(answer)
    except (
        EmbeddingError,
        EvaluationCorpusError,
        ImportError,
        IndexError,
        RerankerError,
        RuntimeProfileError,
    ) as exc:
        parser.error(str(exc))
    finally:
        if service is not None:
            service.close()
    return 0


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("The web port must be a number from 1 to 65535.") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("The web port must be a number from 1 to 65535.")
    return port


def _relationship_minimum_score(value: str) -> float:
    try:
        score = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Relationship minimum score must be a number from 0 to 1.") from exc
    if not 0 <= score <= 1:
        raise argparse.ArgumentTypeError("Relationship minimum score must be a number from 0 to 1.")
    return score


def _print_documents(documents: list[ImportedDocument]) -> None:
    if not documents:
        print("No imported documents.")
        print("Next: import <local-file>, then run health or search.")
        return
    for document in documents:
        print(
            f"{document.id}: {document.name} "
            f"(pages: {document.page_count}, chunks: {document.chunk_count}, OCR pages: {document.ocr_page_count})"
        )
    print("Next: inspect <document-id>, or search.")


def _print_import_summary(action: str, summary: ImportSummary) -> None:
    document = summary.document
    print(f"{action} document {document.id}: {document.name}")
    print(f"Extracted: {document.page_count} pages, {document.chunk_count} chunks, {document.ocr_page_count} OCR pages.")
    print(f"Semantic index: {summary.semantic_index_status}.")
    print(f"OCR artifacts: {summary.ocr_artifact_status}.")


def _print_source_import_plan(plan: SourceImportPlan) -> None:
    print("Connected folders")
    for root in plan.roots:
        if root.status == "ready":
            print(f"- {root.name}: {root.candidate_count} new supported files, {root.imported_count} already imported.")
        else:
            print(f"- {root.name}: unavailable.")
    if not plan.candidates:
        print("No new supported files are ready to import.")
        return
    print(f"Ready to import: {len(plan.candidates)} files.")
    for candidate in plan.candidates[:20]:
        print(f"- {candidate.path.name} ({candidate.source_root})")
    if len(plan.candidates) > 20:
        print(f"- {len(plan.candidates) - 20} additional files")
    print("Next: run sources import to index the currently eligible files.")


def _print_source_import_result(result: SourceImportResult) -> None:
    print(f"Imported: {len(result.imported)} files.")
    if result.failures:
        print(f"Needs review: {len(result.failures)} files could not be imported.")
        for failure in result.failures[:20]:
            print(f"- {failure.name}: {failure.message}")
        if len(result.failures) > 20:
            print(f"- {len(result.failures) - 20} additional files")
    print("Next: run health, open web, or preview sources again.")


def _print_collection_health(health: CollectionHealth) -> None:
    print("Collection health")
    print(f"Documents: {health.document_count}")
    print(f"Pages: {health.page_count}")
    print(f"Chunks: {health.chunk_count}")
    print(f"OCR pages needing review: {health.ocr_page_count}")
    print(f"Source files unavailable: {health.unavailable_source_count}")
    print(f"Sources changed since import: {health.changed_source_count}")
    print(f"Sources requiring baseline reindex: {health.baseline_reindex_count}")
    print(f"Sources chunked by an older version: {health.stale_chunking_count}")
    print(f"Semantic index: {health.semantic_index_status}")
    if health.source_attention:
        print("Needs attention")
        for attention in health.source_attention:
            print(f"- Document {attention.document_id}: {attention.document_name} - {attention.status}")
            if attention.status == "source unavailable":
                print(
                    "  Next: restore the source file, or remove its stale collection record "
                    "and import the file from its new location."
                )
            elif attention.status == "chunking outdated":
                print(
                    "  Next: reindex this document so its chunks match the current chunker. "
                    "Search and the semantic map stay usable until you do."
                )
            else:
                print("  Next: reindex the source file when you are ready to refresh its extracted content.")
    if health.ocr_page_count:
        print("Next: run list, then inspect <document-id> to review OCR provenance.")
    if health.semantic_index_status == "needs reindex":
        print("Next: reindex the affected source document before searching semantically.")
    if not (health.ocr_page_count or health.source_attention or health.semantic_index_status == "needs reindex"):
        print("Next: import a document, or search the current collection.")


def _print_answer(answer) -> None:
    print("Answer")
    print(answer.text)
    if not answer.citations:
        print("No evidence-bound answer was available from the imported documents.")
        print("Next: try different terms, run health for source attention, or import another local document.")
        return
    print("Sources")
    for citation in answer.citations:
        source = f"- {citation.document_name}, page {citation.page_number}"
        if citation.section:
            source += f", section {citation.section}"
        source += f", chunk {citation.chunk_id}, {citation.extraction_method}"
        print(source)
        if citation.extraction_method == "ocr":
            confidence = f"{citation.ocr_confidence:.2f}" if citation.ocr_confidence is not None else "unavailable"
            print(f"  OCR warning: verify against the original document (confidence: {confidence}).")
        print(f"  Evidence: {citation.quote}")


def _print_document_inspection(inspection: DocumentInspection) -> None:
    document = inspection.document
    print(f"Document {document.id}: {document.name}")
    for page in inspection.pages:
        metadata = [
            f"section: {page.section or 'none'}",
            f"extraction: {page.extraction_method}",
            f"chunks: {len(page.chunks)}",
        ]
        if page.extraction_method == "ocr":
            confidence = f"{page.ocr_confidence:.2f}" if page.ocr_confidence is not None else "unavailable"
            metadata.extend((f"OCR confidence: {confidence}", "OCR warning: verify against the original document"))
        print(f"Page {page.page_number} ({', '.join(metadata)})")
        for chunk in page.chunks:
            print(f"  Chunk {chunk.id} (characters: {chunk.char_start}-{chunk.char_end})")


if __name__ == "__main__":
    raise SystemExit(main())
