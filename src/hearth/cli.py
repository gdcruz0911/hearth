from __future__ import annotations

import argparse
from pathlib import Path

from .answering import LocalInferenceError, MLXLocalGenerator, StructuredGeneratorAnswerer
from .claim_support import StructuredClaimSupportChecker
from .domain import (
    CollectionHealth,
    DocumentInspection,
    FileOrganizationError,
    FileOrganizationPlan,
    ImportedDocument,
    ImportError,
    ImportSummary,
    SourceRelinkError,
    SourceRelinkPlan,
)
from .embedding import EmbeddingError, FlatVectorIndex, IndexError, MLXEmbedder
from .evaluation import (
    EvaluationCorpusError,
    evaluate_claim_support_corpus,
    evaluate_corpus,
    load_claim_support_corpus,
    load_evaluation_corpus,
)
from .retrieval import MLXLocalReranker, RerankerError
from .service import HearthService
from .web import HearthWebServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local-only, evidence-bound document chat.",
        epilog=(
            "Common workflow:\n"
            "  import <local-file>  Add one note or PDF.\n"
            "  health               Check collection attention items.\n"
            "  search <question>    Answer from cited evidence.\n"
            "  list                 Find document IDs for inspect, organize, or relink.\n"
            "Run a command with --help to see its arguments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--database", type=Path, default=Path(".hearth/hearth.sqlite"))
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
        "--generator-model",
        type=Path,
        help="Pre-provisioned local MLX model directory. Enables generated answers and claim-support evaluation.",
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
    subcommands = parser.add_subparsers(dest="command", required=True)
    importer = subcommands.add_parser("import", help="Import a local note or configured PDF.")
    importer.add_argument("path")
    search = subcommands.add_parser("search", help="Answer from retrieved evidence or abstain.")
    search.add_argument("question")
    reindexer = subcommands.add_parser("reindex", help="Re-extract and replace one document's derived index.")
    reindexer.add_argument("path")
    remover = subcommands.add_parser("remove", help="Remove a document and its derived records.")
    remover.add_argument("path")
    subcommands.add_parser("list", help="List imported documents without document text or source paths.")
    subcommands.add_parser("health", help="Summarize private collection health without document text or source paths.")
    inspector = subcommands.add_parser("inspect", help="Inspect one document's local provenance metadata.")
    inspector.add_argument("document_id", type=int)
    organizer = subcommands.add_parser("organize", help="Preview or apply one explicit local file move or rename.")
    organizer.add_argument("action", choices=("preview", "apply"))
    organizer.add_argument("document_id", type=int)
    organization_target = organizer.add_mutually_exclusive_group(required=True)
    organization_target.add_argument("--move-to", type=Path, help="Existing local directory to receive the file.")
    organization_target.add_argument("--rename", help="New file name that preserves the existing extension.")
    relinker = subcommands.add_parser("relink", help="Preview or apply an explicit source binding for an unavailable document.")
    relinker.add_argument("action", choices=("preview", "apply"))
    relinker.add_argument("document_id", type=int)
    relinker.add_argument("replacement_path", type=Path, help="Existing local file with identical imported contents.")
    web = subcommands.add_parser("web", help="Run the local Hearth web interface on this Mac only.")
    web.add_argument("--port", type=_port, default=8765, help="Loopback port to use (default: 8765).")
    web.add_argument("--no-open", action="store_true", help="Do not open the local interface in the default browser.")
    evaluator = subcommands.add_parser("evaluate", help="Run a local synthetic or public evaluation corpus.")
    evaluator.add_argument("corpus", type=Path)
    claim_evaluator = subcommands.add_parser(
        "evaluate-claim-support", help="Run an experimental local claim-support corpus."
    )
    claim_evaluator.add_argument("corpus", type=Path)
    args = parser.parse_args(argv)
    if (args.embedding_model is None) != (args.index_directory is None):
        parser.error("--embedding-model and --index-directory must be provided together.")
    if args.retain_ocr_output and args.ocr_output_directory is None:
        parser.error("--retain-ocr-output requires --ocr-output-directory.")
    if args.command == "evaluate-claim-support" and args.generator_model is None:
        parser.error("evaluate-claim-support requires --generator-model.")
    service: HearthService | None = None
    try:
        generator = MLXLocalGenerator(args.generator_model) if args.generator_model is not None else None
        answerer = StructuredGeneratorAnswerer(generator) if generator is not None else None
        semantic_index = (
            FlatVectorIndex(args.index_directory, MLXEmbedder(args.embedding_model))
            if args.embedding_model is not None
            else None
        )
        reranker = MLXLocalReranker(args.reranker_model) if args.reranker_model is not None else None
        service = HearthService(
            args.database,
            answerer=answerer,
            reranker=reranker,
            semantic_index=semantic_index,
            ocr_output_directory=args.ocr_output_directory,
            retain_ocr_output=args.retain_ocr_output,
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
        elif args.command == "organize":
            plan = service.plan_organization(args.document_id, move_to=args.move_to, rename=args.rename)
            if args.action == "preview":
                _print_organization_preview(plan)
            else:
                plan = service.apply_organization(args.document_id, move_to=args.move_to, rename=args.rename)
                _print_organization_applied(plan)
        elif args.command == "relink":
            plan = service.plan_relink(args.document_id, args.replacement_path)
            if args.action == "preview":
                _print_relink_preview(plan)
            else:
                plan = service.apply_relink(args.document_id, args.replacement_path)
                _print_relink_applied(plan)
        elif args.command == "web":
            server = HearthWebServer(service, port=args.port)
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
        elif args.command == "evaluate-claim-support":
            checker = StructuredClaimSupportChecker(generator)
            outcomes = evaluate_claim_support_corpus(checker, load_claim_support_corpus(args.corpus))
            for outcome in outcomes:
                expected = "supported" if outcome.expected_supported else "unsupported"
                received = "supported" if outcome.received_supported else "unsupported"
                detail = "" if outcome.passed else f": {outcome.category}; expected {expected}, received {received}"
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
        FileOrganizationError,
        ImportError,
        IndexError,
        LocalInferenceError,
        RerankerError,
        SourceRelinkError,
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
    print("Next: inspect <document-id>, organize preview <document-id>, relink an unavailable source, or search.")


def _print_import_summary(action: str, summary: ImportSummary) -> None:
    document = summary.document
    print(f"{action} document {document.id}: {document.name}")
    print(f"Extracted: {document.page_count} pages, {document.chunk_count} chunks, {document.ocr_page_count} OCR pages.")
    print(f"Semantic index: {summary.semantic_index_status}.")
    print(f"OCR artifacts: {summary.ocr_artifact_status}.")


def _print_collection_health(health: CollectionHealth) -> None:
    print("Collection health")
    print(f"Documents: {health.document_count}")
    print(f"Pages: {health.page_count}")
    print(f"Chunks: {health.chunk_count}")
    print(f"OCR pages needing review: {health.ocr_page_count}")
    print(f"Source files unavailable: {health.unavailable_source_count}")
    print(f"Sources changed since import: {health.changed_source_count}")
    print(f"Sources requiring baseline reindex: {health.baseline_reindex_count}")
    print(f"Semantic index: {health.semantic_index_status}")
    if health.source_attention:
        print("Needs attention")
        for attention in health.source_attention:
            print(f"- Document {attention.document_id}: {attention.document_name} - {attention.status}")
            if attention.status == "source unavailable":
                print(
                    "  Next: restore the source file, relink it with "
                    "relink preview <document-id> <replacement-path>, or remove its stale collection record."
                )
            else:
                print("  Next: reindex the source file when you are ready to refresh its extracted content.")
    if health.ocr_page_count:
        print("Next: run list, then inspect <document-id> to review OCR provenance.")
    if health.semantic_index_status == "needs reindex":
        print("Next: reindex the affected source document before searching semantically.")
    if not (health.ocr_page_count or health.source_attention or health.semantic_index_status == "needs reindex"):
        print("Next: import a document, or search the current collection.")


def _print_organization_preview(plan: FileOrganizationPlan) -> None:
    print(f"Preview: {plan.operation} document {plan.document.id}: {plan.document.name}")
    print(f"Source: {plan.source_path}")
    print(f"Target: {plan.target_path}")
    print("No changes made. Run the same command with organize apply to proceed.")


def _print_organization_applied(plan: FileOrganizationPlan) -> None:
    print(f"Applied: {plan.operation} document {plan.document.id}: {plan.source_path.name} -> {plan.target_path.name}")
    print("Source binding updated. Existing extracted text and citation chunk IDs were preserved.")


def _print_relink_preview(plan: SourceRelinkPlan) -> None:
    print(f"Preview: relink document {plan.document.id}: {plan.document.name}")
    print(f"Previous source (unavailable): {plan.previous_source_path}")
    print(f"Replacement source: {plan.replacement_source_path}")
    print("No changes made. Run the same command with relink apply to proceed.")


def _print_relink_applied(plan: SourceRelinkPlan) -> None:
    print(
        f"Applied: relink document {plan.document.id}: "
        f"{plan.previous_source_path.name} -> {plan.replacement_source_path.name}"
    )
    print("Source binding updated. Existing extracted text, citation chunk IDs, and semantic index were preserved.")


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
