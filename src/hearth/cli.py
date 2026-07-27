from __future__ import annotations

import argparse
from pathlib import Path

from .domain import ImportError
from .service import HearthService


def main() -> int:
    parser = argparse.ArgumentParser(description="Local-only, evidence-bound document chat.")
    parser.add_argument("--database", type=Path, default=Path(".hearth/hearth.sqlite"))
    subcommands = parser.add_subparsers(dest="command", required=True)
    importer = subcommands.add_parser("import", help="Import a local note or configured PDF.")
    importer.add_argument("path")
    search = subcommands.add_parser("search", help="Answer from retrieved evidence or abstain.")
    search.add_argument("question")
    reindexer = subcommands.add_parser("reindex", help="Re-extract and replace one document's derived index.")
    reindexer.add_argument("path")
    remover = subcommands.add_parser("remove", help="Remove a document and its derived records.")
    remover.add_argument("path")
    args = parser.parse_args()
    service = HearthService(args.database)
    try:
        if args.command == "import":
            print(f"Imported document {service.import_document(args.path)}.")
        elif args.command == "reindex":
            print(f"Reindexed document {service.reindex_document(args.path)}.")
        elif args.command == "remove":
            print("Removed." if service.remove_document(args.path) else "No matching document found.")
        else:
            answer = service.answer(args.question)
            print(answer.text)
            for citation in answer.citations:
                section = f", section {citation.section}" if citation.section else ""
                print(f"[{citation.document_name}, page {citation.page_number}{section}, chunk {citation.chunk_id}] {citation.quote}")
    except ImportError as exc:
        parser.error(str(exc))
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
