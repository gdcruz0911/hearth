"""Measure reviewers and verifiers on seeded changes whose problems the tests do not catch (TEST-5)."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from .tasks import (BUILTIN_REVIEW, REVIEW_PROMPT, REVIEWERS, VERIFIERS, VERIFY_PROMPT, _evidence_problems, _git, _home, _now, _run, _verdict,
                    refused_inside_task)

# The seeded project is a small Python parser, so its verify instructions are short and live here, not in the fixtures.
SEEDED_VERIFY = """# Verifying the duration parser

Run the parser on the inputs the goal names, for example:

    python3 -c "from durations import parse; print(parse('2d'))"

Save each output to its own file in .hearth/evidence/, and claim only what that output shows.
"""
SEEDED_CHECK = "python3 -m unittest -q"


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    for command, role, choices in (("review-eval", "reviewer", REVIEWERS), ("verify-eval", "verifier", VERIFIERS)):
        parser = subcommands.add_parser(command, help=f"Score one {role} on seeded changes, some with planted problems.")
        parser.add_argument("cases", type=Path, help="A directory with base/ and one folder per case, such as tests/fixtures/public/reviews.")
        parser.add_argument(f"--{role}", required=True, choices=sorted(choices))
        parser.add_argument("--model", help=f"{role.capitalize()} model. Defaults to the one hearth loop uses.")
        parser.add_argument("--effort", choices=("low", "medium", "high"), help=f"{role.capitalize()} effort. Defaults to the role's.")
        parser.add_argument("--timeout", type=int, default=600, help="Seconds before each run is stopped. Defaults to 600.")


def run(args: argparse.Namespace) -> int:
    if refused_inside_task():
        return 1
    verifying = args.command == "verify-eval"
    root = _home() / "evals/runs" / time.strftime("%Y%m%d-%H%M%S")
    rows = []
    for case_dir in sorted(path.parent for path in args.cases.glob("*/case.json")):
        case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
        work, repo, base = _seed(args.cases, case_dir, root, case["goal"], {"VERIFY.md": SEEDED_VERIFY} if verifying else {})
        task = {"id": f"eval-{case_dir.name}", "goal": case["goal"], "base": base, "worktree": str(repo), "runs": [], "status": "running", "copied": []}
        diff = _git(repo, "diff", f"{base}..HEAD")
        started = time.monotonic()
        if verifying:
            prompt = VERIFY_PROMPT.format(id=task["id"], goal=case["goal"], base=base[:12], diff=diff, check=SEEDED_CHECK, verify="python3 -c", messages="")
            result = _run(task, work, "verify", args.verifier, VERIFIERS[args.verifier], prompt, args.model, args.effort, args.timeout, SEEDED_CHECK, "python3 -c")
            run_dir = work / "runs" / task["runs"][-1]["dir"]
            if (repo / ".hearth/evidence").exists():
                shutil.move(repo / ".hearth/evidence", run_dir / "evidence")
            verdict = None if result["stop"] else _verdict(result["final"], ("verified", "failed"), "claims")
            # A verdict whose evidence does not hold is rejected, which is never the right answer.
            outcome = "rejected" if verdict is None or _evidence_problems(run_dir, verdict) else verdict["verdict"]
            expect = case["verify_expect"]
        else:
            prompt = REVIEW_PROMPT.format(id=task["id"], goal=case["goal"], base=base[:12], diff=diff, guidance=BUILTIN_REVIEW, messages="", verification="")
            result = _run(task, work, "review", args.reviewer, REVIEWERS[args.reviewer], prompt, args.model, args.effort, args.timeout, "")
            verdict = None if result["stop"] else _verdict(result["final"])
            outcome, expect = verdict and verdict["verdict"], case["expect"]
        record = task["runs"][-1]
        agent = args.verifier if verifying else args.reviewer
        rows.append({"at": _now(), ("verifier" if verifying else "reviewer"): agent, "model": record["model_used"], "effort": record["effort"],
                     "case": case_dir.name, "expect": expect, ("outcome" if verifying else "verdict"): outcome, "correct": outcome == expect,
                     "seconds": round(time.monotonic() - started), "stop": result["stop"], "usage": result["usage"]})
        print(f"{case_dir.name:<14} expected {expect:<9} got {outcome or result['stop'] or 'unreadable':<11} {'ok' if outcome == expect else 'MISS'}")

    log = _home() / "evals" / ("verifies.jsonl" if verifying else "reviews.jsonl")
    with log.open("a", encoding="utf-8") as file:
        file.writelines(json.dumps(row) + "\n" for row in rows)
    labels = ((("failed", "broken claims caught"), ("verified", "working claims verified")) if verifying
              else (("changes", "planted bugs caught"), ("approve", "clean changes approved")))
    for expect, label in labels:
        scored = [row for row in rows if row["expect"] == expect]
        print(f"{label}: {sum(row['correct'] for row in scored)} of {len(scored)}")
    print(f"Receipts in {root}; every run is appended to {log}.")
    return 0


def _seed(cases: Path, case_dir: Path, root: Path, goal: str, extra: dict[str, str]) -> tuple[Path, Path, str]:
    """Build a throwaway repository with the base commit, plus extra files, and the case's change on top."""
    work = root / case_dir.name
    repo = work / "repo"
    shutil.copytree(cases / "base", repo)
    for name, text in extra.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "init", "-q")
    _commit(repo, "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    shutil.copytree(case_dir / "after", repo, dirs_exist_ok=True)
    _commit(repo, goal)
    return work, repo, base


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Hearth", "-c", "user.email=hearth@localhost", "commit", "-q", "-m", message)
