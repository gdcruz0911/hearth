from __future__ import annotations

import argparse
from pathlib import Path

from .answering import LocalInferenceError, MLXLocalGenerator, StructuredGeneratorAnswerer
from .claim_support import StructuredClaimSupportChecker
from .domain import DocumentInspection, ImportedDocument, ImportError
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local-only, evidence-bound document chat.")
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
    inspector = subcommands.add_parser("inspect", help="Inspect one document's local provenance metadata.")
    inspector.add_argument("document_id", type=int)
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
            print(f"Imported document {service.import_document(args.path)}.")
        elif args.command == "reindex":
            print(f"Reindexed document {service.reindex_document(args.path)}.")
        elif args.command == "remove":
            print("Removed." if service.remove_document(args.path) else "No matching document found.")
        elif args.command == "list":
            _print_documents(service.list_documents())
        elif args.command == "inspect":
            inspection = service.inspect_document(args.document_id)
            if inspection is None:
                print(f"No imported document with ID {args.document_id}.")
                return 1
            _print_document_inspection(inspection)
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
                detail = "" if outcome.passed else f": expected {expected}, received {received}"
                print(f"{outcome.case_id}: {'PASS' if outcome.passed else 'FAIL'}{detail}")
            passed_count = sum(outcome.passed for outcome in outcomes)
            print(f"Summary: {passed_count}/{len(outcomes)} cases passed.")
            return 0 if passed_count == len(outcomes) else 1
        else:
            answer = service.answer(args.question)
            print(answer.text)
            for citation in answer.citations:
                section = f", section {citation.section}" if citation.section else ""
                extraction = f", extraction {citation.extraction_method}"
                confidence = (
                    f", OCR confidence {citation.ocr_confidence:.2f}"
                    if citation.ocr_confidence is not None
                    else ""
                )
                print(
                    f"[{citation.document_name}, page {citation.page_number}{section}, "
                    f"chunk {citation.chunk_id}{extraction}{confidence}] {citation.quote}"
                )
    except (
        EmbeddingError,
        EvaluationCorpusError,
        ImportError,
        IndexError,
        LocalInferenceError,
        RerankerError,
    ) as exc:
        parser.error(str(exc))
    finally:
        if service is not None:
            service.close()
    return 0


def _print_documents(documents: list[ImportedDocument]) -> None:
    if not documents:
        print("No imported documents.")
        return
    for document in documents:
        print(
            f"{document.id}: {document.name} "
            f"(pages: {document.page_count}, chunks: {document.chunk_count}, OCR pages: {document.ocr_page_count})"
        )


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
