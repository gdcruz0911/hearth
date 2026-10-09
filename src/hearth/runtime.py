from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path


class RuntimeProfileError(ValueError):
    """Raised when a private Hearth runtime profile is malformed or unsafe to create."""


def hearth_home() -> Path:
    """Where Hearth keeps its records and settings: HEARTH_HOME, else ~/.hearth.

    HOME stays the person's, so agents keep their own sign-ins while a run's records go elsewhere.
    """
    return Path(os.environ["HEARTH_HOME"]).expanduser() if os.environ.get("HEARTH_HOME") else Path.home() / ".hearth"


def default_source_roots() -> tuple[Path, ...]:
    """The first three personal folders Hearth may scan after explicit approval."""
    home = Path.home()
    return tuple((home / name).resolve() for name in ("Desktop", "Documents", "Downloads"))


@dataclass(frozen=True)
class RuntimeProfile:
    database: Path | None = None
    embedding_model: Path | None = None
    index_directory: Path | None = None
    reranker_model: Path | None = None
    ocr_output_directory: Path | None = None
    retain_ocr_output: bool = False
    relationship_minimum_score: float = 0.72
    source_roots: tuple[Path, ...] = default_source_roots()
    # Only documents under these folders are handed to agents as recall; none set means recall is refused.
    recall_roots: tuple[Path, ...] = ()


_FORMAT = "hearth-runtime-profile-v1"
_PATH_FIELDS = (
    "database",
    "embedding_model",
    "index_directory",
    "reranker_model",
    "ocr_output_directory",
)
_FIELDS = {"format", *_PATH_FIELDS, "retain_ocr_output", "relationship_minimum_score", "source_roots", "recall_roots"}


def load_runtime_profile(path: Path) -> RuntimeProfile:
    profile_path = path.expanduser().resolve()
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeProfileError("The runtime profile file does not exist.") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeProfileError("The runtime profile must be UTF-8 encoded.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeProfileError("The runtime profile is not valid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) - _FIELDS:
        raise RuntimeProfileError("The runtime profile contains unsupported settings.")
    if payload.get("format") != _FORMAT:
        raise RuntimeProfileError("The runtime profile format is unsupported.")

    paths = {field: _profile_path(payload, field, profile_path.parent) for field in _PATH_FIELDS}
    source_roots = _profile_root_paths(payload, profile_path.parent)
    recall_roots = _profile_root_paths(payload, profile_path.parent, "recall_roots", tuple)
    retain_ocr_output = payload.get("retain_ocr_output", False)
    if not isinstance(retain_ocr_output, bool):
        raise RuntimeProfileError("Runtime profile retain_ocr_output must be true or false.")
    relationship_minimum_score = payload.get("relationship_minimum_score", 0.72)
    if isinstance(relationship_minimum_score, bool) or not isinstance(relationship_minimum_score, (int, float)):
        raise RuntimeProfileError("Runtime profile relationship_minimum_score must be a number.")
    if not 0 <= relationship_minimum_score <= 1:
        raise RuntimeProfileError("Runtime profile relationship_minimum_score must be between zero and one.")
    return RuntimeProfile(
        **paths,
        retain_ocr_output=retain_ocr_output,
        relationship_minimum_score=float(relationship_minimum_score),
        source_roots=source_roots,
        recall_roots=recall_roots,
    )


def write_runtime_profile(path: Path, profile: RuntimeProfile) -> Path:
    profile_path = path.expanduser().resolve()
    if profile_path.exists():
        raise RuntimeProfileError("Refusing to overwrite an existing runtime profile.")
    if not 0 <= profile.relationship_minimum_score <= 1:
        raise RuntimeProfileError("Runtime profile relationship_minimum_score must be between zero and one.")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "format": _FORMAT,
        "retain_ocr_output": profile.retain_ocr_output,
        "relationship_minimum_score": profile.relationship_minimum_score,
    }
    for field in _PATH_FIELDS:
        value = getattr(profile, field)
        if value is not None:
            payload[field] = str(value.expanduser().resolve())
    payload["source_roots"] = [str(value.expanduser().resolve()) for value in _unique_paths(profile.source_roots)]
    if profile.recall_roots:
        payload["recall_roots"] = [str(value.expanduser().resolve()) for value in _unique_paths(profile.recall_roots)]
    try:
        profile_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeProfileError("The runtime profile could not be created.") from exc
    return profile_path


def _profile_path(payload: dict[object, object], field: str, base_directory: Path) -> Path | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuntimeProfileError(f"Runtime profile {field} must be a non-empty path string.")
    candidate = Path(value).expanduser()
    return (base_directory / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _profile_root_paths(
    payload: dict[object, object], base_directory: Path, field: str = "source_roots", default=default_source_roots
) -> tuple[Path, ...]:
    value = payload.get(field)
    if value is None:
        return default()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RuntimeProfileError(f"Runtime profile {field} must be a list of non-empty path strings.")
    roots = []
    for item in value:
        candidate = Path(item).expanduser()
        roots.append((base_directory / candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
    return _unique_paths(roots)


def _unique_paths(paths: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    unique: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)
