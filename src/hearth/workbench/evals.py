"""Measure reviewers on seeded changes whose planted bugs the tests do not catch (TEST-5)."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from .tasks import REVIEW_MODELS, REVIEW_PROMPT, REVIEWERS, _git, _home, _now, _run, _verdict, refused_inside_task


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    parser = subcommands.add_parser("review-eval", help="Score one reviewer on seeded changes, some with planted bugs.")
    parser.add_argument("cases", type=Path, help="A directory with base/ and one folder per case, such as tests/fixtures/public/reviews.")
    parser.add_argument("--reviewer", required=True, choices=sorted(REVIEWERS))
    parser.add_argument("--model", help="Reviewer model. Defaults to the one hearth loop uses.")
    parser.add_argument("--effort", choices=("low", "medium", "high"), help="Reviewer effort. Defaults to the review role's.")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds before each review is stopped. Defaults to 600.")


def run(args: argparse.Namespace) -> int:
    if refused_inside_task():
        return 1
    model = args.model or REVIEW_MODELS.get(args.reviewer)
    root = _home() / "evals/runs" / time.strftime("%Y%m%d-%H%M%S")
    rows = []
    for case_dir in sorted(path.parent for path in args.cases.glob("*/case.json")):
        case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        work = root / case_dir.name
        repo = work / "repo"
        shutil.copytree(args.cases / "base", repo)
        _git(repo, "init", "-q")
        _commit(repo, "base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        shutil.copytree(case_dir / "after", repo, dirs_exist_ok=True)
        _commit(repo, case["goal"])
        task = {"id": f"eval-{case_dir.name}", "goal": case["goal"], "base": base, "worktree": str(repo), "runs": [], "status": "running"}
        prompt = REVIEW_PROMPT.format(id=task["id"], goal=case["goal"], base=base[:12], diff=_git(repo, "diff", f"{base}..HEAD"), messages="", verification="")
        started = time.monotonic()
        result = _run(task, work, "review", args.reviewer, REVIEWERS[args.reviewer], prompt, model, args.effort, args.timeout, "")
        record = task["runs"][-1]
        verdict = None if result["stop"] else _verdict(result["final"])
        rows.append({"at": _now(), "reviewer": args.reviewer, "model": record["model_used"], "effort": record["effort"], "case": case_dir.name, "expect": case["expect"],
                     "verdict": verdict and verdict["verdict"], "correct": bool(verdict) and verdict["verdict"] == case["expect"],
                     "seconds": round(time.monotonic() - started), "stop": result["stop"], "usage": result["usage"]})
        print(f"{case_dir.name:<14} expected {case['expect']:<8} got {rows[-1]['verdict'] or result['stop'] or 'unreadable':<11} {'ok' if rows[-1]['correct'] else 'MISS'}")

    log = _home() / "evals/reviews.jsonl"
    with log.open("a", encoding="utf-8") as file:
        file.writelines(json.dumps(row) + "\n" for row in rows)
    for expect, label in (("changes", "planted bugs caught"), ("approve", "clean changes approved")):
        scored = [row for row in rows if row["expect"] == expect]
        print(f"{label}: {sum(row['correct'] for row in scored)} of {len(scored)}")
    print(f"Receipts in {root}; every run is appended to {log}.")
    return 0


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Hearth", "-c", "user.email=hearth@localhost", "commit", "-q", "-m", message)
