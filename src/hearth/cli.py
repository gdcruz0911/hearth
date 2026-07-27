from __future__ import annotations

import argparse
from pathlib import Path

from .domain import ImportError
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
    service = HearthService(args.database, ocr_output_directory=args.ocr_output_directory)
    try:
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
    except (EvaluationCorpusError, ImportError) as exc:
        parser.error(str(exc))
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
