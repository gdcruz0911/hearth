"""ADR-0039: the person's roles file, which names the provider, model, and effort for each role, in fallback order.

`roles.md` in Hearth's home applies to every project; `roles/<project>.md` replaces only the roles it defines. Each role is
a `## <role>` heading followed by one table with the columns order, provider, model, and effort; prose around the tables is
ignored. Anything Hearth cannot read stops the command, so a role is never guessed.
"""

from __future__ import annotations

from pathlib import Path

ROLES = ("supervisor", "implement", "test", "verify", "review", "retro")
COLUMNS = ["order", "provider", "model", "effort"]


class RolesError(ValueError):
    """Raised for a roles file Hearth cannot read exactly."""


def exists(home: Path) -> bool:
    return (home / "roles.md").exists()


def load(home: Path, project: str) -> dict[str, list[dict]]:
    """Each role's rows, in order, as {provider, model, effort}; a model or effort left empty is None."""
    chosen = _parse(home / "roles.md", project_file=False)
    chosen.update(_parse(home / "roles" / f"{project}.md", project_file=True))
    return chosen


def _parse(path: Path, project_file: bool) -> dict[str, list[dict]]:
    if not path.exists():
        return {}
    chosen: dict[str, list[tuple[int, dict]]] = {}
    role, header = None, None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if text.startswith("## "):
            role, header = text[3:].strip().lower(), None
            if role not in ROLES:
                _refuse(path, number, f"unknown role {role!r}; the roles are {', '.join(ROLES)}")
            if project_file and role == "supervisor":
                _refuse(path, number, "the supervisor is set only in roles.md, since a conversation is not tied to one project")
            if role in chosen:
                _refuse(path, number, f"the role {role} appears twice")
            chosen[role] = []
            continue
        if role is None or not text.startswith("|"):
            continue
        cells = [cell.strip() for cell in text.strip("|").split("|")]
        if header is None:
            header = [cell.lower() for cell in cells]
            if header != COLUMNS:
                _refuse(path, number, f"the {role} table's columns must be {' | '.join(COLUMNS)}")
            continue
        if all(set(cell) <= set("-: ") for cell in cells):
            continue  # The separator row under the header.
        if len(cells) != len(COLUMNS):
            _refuse(path, number, f"a {role} row needs {len(COLUMNS)} cells: {' | '.join(COLUMNS)}")
        order, provider, model, effort = cells
        if not order.isdigit():
            _refuse(path, number, f"the order {order!r} is not a whole number")
        if not provider:
            _refuse(path, number, f"a {role} row names no provider")
        if any(int(order) == taken for taken, _ in chosen[role]):
            _refuse(path, number, f"the order {order} appears twice in {role}")
        chosen[role].append((int(order), {"provider": provider, "model": model or None, "effort": effort or None}))
    for role, rows in chosen.items():
        if not rows:
            _refuse(path, None, f"the role {role} has no rows")
    return {role: [row for _, row in sorted(rows, key=lambda item: item[0])] for role, rows in chosen.items()}


def _refuse(path: Path, number: int | None, problem: str) -> None:
    where = f"{path}:{number}" if number else str(path)
    raise RolesError(f"{where}: {problem}.\nNext: fix {path.name}; see ADR-0039 for its format")
