from __future__ import annotations

import argparse
from pathlib import Path

from .answering import LocalInferenceError, MLXLocalGenerator, StructuredGeneratorAnswerer
from .domain import ImportError
from .embedding import EmbeddingError, FlatVectorIndex, IndexError, MLXEmbedder
from .evaluation import EvaluationCorpusError, evaluate_corpus, load_evaluation_corpus
from .service import HearthService


def main() -> int:
    parser = argparse.ArgumentParser(description="Local-only, evidence-bound document chat.")
    parser.add_argument("--database", type=Path, default=Path(".hearth/hearth.sqlite"))
    parser.add_argument(
        "--ocr-output-directory",
        type=Path,
        help="Private local directory for OCR-derived PDFs. Enables local OCRmyPDF fallback.",
    )
    parser.add_argument(
        "--generator-model",
        type=Path,
        help="Pre-provisioned local MLX model directory. Enables generated evidence-bound answers.",
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
    subcommands = parser.add_subparsers(dest="command", required=True)
    importer = subcommands.add_parser("import", help="Import a local note or configured PDF.")
    importer.add_argument("path")
    search = subcommands.add_parser("search", help="Answer from retrieved evidence or abstain.")
    search.add_argument("question")
    reindexer = subcommands.add_parser("reindex", help="Re-extract and replace one document's derived index.")
    reindexer.add_argument("path")
    remover = subcommands.add_parser("remove", help="Remove a document and its derived records.")
    remover.add_argument("path")
    evaluator = subcommands.add_parser("evaluate", help="Run a local synthetic or public evaluation corpus.")
    evaluator.add_argument("corpus", type=Path)
    args = parser.parse_args()
    if (args.embedding_model is None) != (args.index_directory is None):
        parser.error("--embedding-model and --index-directory must be provided together.")
    service: HearthService | None = None
    try:
        answerer = (
            StructuredGeneratorAnswerer(MLXLocalGenerator(args.generator_model))
            if args.generator_model is not None
            else None
        )
        semantic_index = (
            FlatVectorIndex(args.index_directory, MLXEmbedder(args.embedding_model))
            if args.embedding_model is not None
            else None
        )
        service = HearthService(
            args.database,
            answerer=answerer,
            semantic_index=semantic_index,
            ocr_output_directory=args.ocr_output_directory,
        )
        if args.command == "import":
            print(f"Imported document {service.import_document(args.path)}.")
        elif args.command == "reindex":
            print(f"Reindexed document {service.reindex_document(args.path)}.")
        elif args.command == "remove":
            print("Removed." if service.remove_document(args.path) else "No matching document found.")
        elif args.command == "evaluate":
            outcomes = evaluate_corpus(service, load_evaluation_corpus(args.corpus))
            for outcome in outcomes:
                detail = "" if outcome.passed else f": {'; '.join(outcome.errors)}"
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
    except (EmbeddingError, EvaluationCorpusError, ImportError, IndexError, LocalInferenceError) as exc:
        parser.error(str(exc))
    finally:
        if service is not None:
            service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
