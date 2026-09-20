from __future__ import annotations

import argparse
import json
import os
import random
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _require_safe_directory,
    _store_immutable,
    _utc_now,
    sha256_bytes,
)
from .context_bundle import (
    MAX_CONTEXT_BYTES,
    MAX_SOURCE_BYTES,
    build_context_bundle,
    store_context_bundle,
)
from .evaluator_contract import (
    EVALUATOR_PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as evaluator_prompt_sha256,
)
from .generator_contract import (
    PROMPT_TEMPLATE_VERSION,
    prompt_template_sha256 as generator_prompt_sha256,
)
from .openai_compatible import IDENTITY_BINDING, PROVIDER_NAME, identifier_revision
from .openai_evaluator import (
    ADAPTER_VERSION as EVALUATOR_ADAPTER_VERSION,
    EVALUATION_STRATEGY,
)
from .openai_generator import (
    ADAPTER_VERSION as GENERATOR_ADAPTER_VERSION,
    DEFAULT_OPTIONS as GENERATOR_DEFAULT_OPTIONS,
)
from .pre_review_job import (
    PreReviewJobError,
    _connect_ro,
    parse_recipe,
    submit_job,
)
from .production_io import ProductionIOError, mirror_read_lock


RECORD_VERSION = 1
KNOWLEDGE_ROOT = "11-Knowledge"
PROJECT_ROOT = "10-Project"
SELECTION_DIR = "input-selections"
STATE_FILE = "input-planner-state.json"
OBJECTIVE_POLICY = "synthesize-v0"
COVERAGE_POLICY = "coverage-shuffle-v0"
RANDOM_POLICY = "random-set-v0"
DEFAULT_BATCH_SIZE = 6
MAX_BATCH_SIZE = 8
DEFAULT_TARGET_INFLIGHT = 3
HARD_BACKPRESSURE = 8
DEFAULT_COVERAGE_CYCLES = 4
DEFAULT_RANDOM_CYCLES = 1
MAX_CATALOG_FILES = 8192
_ALLOWED_PROJECT_STATUSES = {
    "planning",
    "running",
    "stopped",
    "stable",
    "done",
    "cancelled",
}
_ACTIVE_JOB_STATES = {
    "queued",
    "generating",
    "validating",
    "building_evaluation_context",
    "evaluating",
    "awaiting_human_review",
}
_SYNTHESIS_QUERY = (
    "Synthesize one concise, reusable Obsidian Knowledge Note from the selected "
    "Vault sources. Preserve source distinctions, do not invent facts, and prefer "
    "a durable explanation, procedure, specification, or troubleshooting insight "
    "over project-local status reporting."
)
_WIKILINK_RE = re.compile(r"^\[\[([^\]]]+)\]\]$")


class AIInputPlannerError(ArtifactLifecycleError):
    """Raised when automatic Vault input planning cannot proceed safely."""


@dataclass(frozen=True)
class CatalogEntry:
    path: str
    source_kind: str
    content_sha256: str
    byte_size: int
    project_status: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "source_kind": self.source_kind,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "project_status": self.project_status,
        }


@dataclass(frozen=True)
class InputCatalog:
    entries: tuple[CatalogEntry, ...]
    sha256: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PlannerState:
    catalog_sha256: str | None
    coverage_epoch: int
    coverage_cursor: int
    cycle: int

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "catalog_sha256": self.catalog_sha256,
                "coverage_epoch": self.coverage_epoch,
                "coverage_cursor": self.coverage_cursor,
                "cycle": self.cycle,
            }
        )


@dataclass(frozen=True)
class Selection:
    policy: str
    objective_policy: str
    catalog_sha256: str
    epoch: int
    cycle: int
    seed: str
    entries: tuple[CatalogEntry, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "selection_policy": self.policy,
                "objective_policy": self.objective_policy,
                "catalog_sha256": self.catalog_sha256,
                "epoch": self.epoch,
                "cycle": self.cycle,
                "seed": self.seed,
                "selected": [entry.payload() for entry in self.entries],
            }
        )


def _plain_scalar(raw: str) -> str:
    value = raw.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _frontmatter_scalars(data: bytes, *, path: str) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AIInputPlannerError(f"Vault source is not UTF-8: {path}") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:128]:
        if line.strip() == "---":
            return values
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        if key in values:
            raise AIInputPlannerError(f"duplicate frontmatter key in {path}: {key}")
        values[key] = _plain_scalar(raw)
    return {}


def _safe_tree_files(vault_root: Path, root_name: str) -> list[tuple[str, bytes]]:
    root = vault_root.absolute() / root_name
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise AIInputPlannerError(f"Vault source root is unsafe: {root_name}")

    rows: list[tuple[str, bytes]] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        visible_dirs: list[str] = []
        names = list(dirnames) + list(filenames)
        folded: dict[str, str] = {}
        for name in names:
            previous = folded.get(name.casefold())
            if previous is not None and previous != name:
                raise AIInputPlannerError(
                    f"case-fold collision in {root_name}: {previous!r} / {name!r}"
                )
            folded[name.casefold()] = name

        for name in sorted(dirnames, key=lambda value: (value.casefold(), value)):
            child = directory_path / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise AIInputPlannerError(f"symlink directory is not allowed: {child}")
            if not stat.S_ISDIR(info.st_mode) or name.startswith("."):
                continue
            visible_dirs.append(name)
        dirnames[:] = visible_dirs

        for name in sorted(filenames, key=lambda value: (value.casefold(), value)):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            path = directory_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise AIInputPlannerError(f"Vault source is not a regular file: {path}")
            if info.st_size > MAX_SOURCE_BYTES:
                continue
            data = path.read_bytes()
            if len(data) > MAX_SOURCE_BYTES:
                continue
            relative = path.relative_to(vault_root.absolute()).as_posix()
            rows.append((relative, data))
            if len(rows) > MAX_CATALOG_FILES:
                raise AIInputPlannerError(
                    f"input catalog exceeds {MAX_CATALOG_FILES} Markdown files"
                )
    rows.sort(key=lambda row: (row[0].casefold(), row[0]))
    return rows


def _project_target(value: str) -> str | None:
    match = _WIKILINK_RE.fullmatch(value.strip())
    if match is None:
        return None
    target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
    if not target:
        return None
    if target.endswith(".md"):
        target = target[:-3]
    return target


def _resolve_project_status(
    note_path: str,
    project_ref: str,
    projects_by_path: dict[str, str],
    projects_by_basename: dict[str, tuple[str, ...]],
) -> str | None:
    target = _project_target(project_ref)
    if target is None:
        return None

    exact = target.casefold()
    if exact in projects_by_path:
        return projects_by_path[exact]

    if "/" in target and not target.startswith(PROJECT_ROOT + "/"):
        candidate = (
            PurePosixPath(note_path).parent / PurePosixPath(target)
        ).as_posix()
        normalized = str(PurePosixPath(candidate))
        if normalized.casefold() in projects_by_path:
            return projects_by_path[normalized.casefold()]

    basename = PurePosixPath(target).name.casefold()
    matches = projects_by_basename.get(basename, ())
    if len(matches) == 1:
        return projects_by_path[matches[0]]
    return None


def build_catalog(vault_root: Path) -> InputCatalog:
    knowledge_rows = _safe_tree_files(vault_root, KNOWLEDGE_ROOT)
    project_rows = _safe_tree_files(vault_root, PROJECT_ROOT)
    warnings: list[str] = []
    entries: list[CatalogEntry] = []

    for path, data in knowledge_rows:
        fm = _frontmatter_scalars(data, path=path)
        if fm.get("type") != "knowledge-note" or fm.get("status") != "active":
            continue
        entries.append(
            CatalogEntry(
                path=path,
                source_kind="knowledge",
                content_sha256=sha256_bytes(data),
                byte_size=len(data),
            )
        )

    projects_by_path: dict[str, str] = {}
    basename_candidates: dict[str, list[str]] = {}
    for path, data in project_rows:
        fm = _frontmatter_scalars(data, path=path)
        if fm.get("type") != "project":
            continue
        status = fm.get("status", "")
        if status not in _ALLOWED_PROJECT_STATUSES:
            warnings.append(f"{path}: invalid Project status {status!r}")
            continue
        stem_path = path[:-3]
        key = stem_path.casefold()
        projects_by_path[key] = status
        basename_candidates.setdefault(PurePosixPath(stem_path).name.casefold(), []).append(key)

    projects_by_basename = {
        key: tuple(sorted(values))
        for key, values in basename_candidates.items()
    }

    for path, data in project_rows:
        fm = _frontmatter_scalars(data, path=path)
        if fm.get("type") != "project-note" or fm.get("lifecycle") != "active":
            continue
        project_ref = fm.get("project", "")
        status = _resolve_project_status(
            path,
            project_ref,
            projects_by_path,
            projects_by_basename,
        )
        if status is None:
            warnings.append(f"{path}: Project reference cannot be resolved")
            continue
        if status == "cancelled":
            continue
        entries.append(
            CatalogEntry(
                path=path,
                source_kind="project-note",
                content_sha256=sha256_bytes(data),
                byte_size=len(data),
                project_status=status,
            )
        )

    entries.sort(key=lambda item: (item.path.casefold(), item.path))
    payload = _canonical_json_bytes(
        {
            "record_version": RECORD_VERSION,
            "entries": [entry.payload() for entry in entries],
        }
    )
    return InputCatalog(
        entries=tuple(entries),
        sha256=sha256_bytes(payload),
        warnings=tuple(sorted(warnings)),
    )


def _orchestration_root(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    orchestration = root / "02-Orchestration"
    _require_safe_directory(orchestration, create=True)
    return orchestration


def _load_state(ai_root: Path) -> PlannerState:
    path = _orchestration_root(ai_root) / STATE_FILE
    if not path.exists():
        return PlannerState(None, 1, 0, 0)
    if path.is_symlink() or not path.is_file():
        raise AIInputPlannerError("input planner state path is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AIInputPlannerError("cannot read input planner state") from exc
    required = {
        "record_version",
        "catalog_sha256",
        "coverage_epoch",
        "coverage_cursor",
        "cycle",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise AIInputPlannerError("input planner state properties do not match contract")
    if value["record_version"] != RECORD_VERSION:
        raise AIInputPlannerError("unsupported input planner state version")
    catalog = value["catalog_sha256"]
    if catalog is not None and (
        not isinstance(catalog, str)
        or len(catalog) != 64
        or any(ch not in "0123456789abcdef" for ch in catalog)
    ):
        raise AIInputPlannerError("input planner catalog digest is invalid")
    for name in ("coverage_epoch", "coverage_cursor", "cycle"):
        if type(value[name]) is not int or value[name] < 0:
            raise AIInputPlannerError(f"input planner {name} is invalid")
    if value["coverage_epoch"] < 1:
        raise AIInputPlannerError("input planner coverage_epoch must be >= 1")
    return PlannerState(
        catalog_sha256=catalog,
        coverage_epoch=value["coverage_epoch"],
        coverage_cursor=value["coverage_cursor"],
        cycle=value["cycle"],
    )


def _store_state(ai_root: Path, state: PlannerState) -> Path:
    directory = _orchestration_root(ai_root)
    destination = directory / STATE_FILE
    if destination.exists() and destination.is_symlink():
        raise AIInputPlannerError("input planner state destination is unsafe")
    data = state.to_json_bytes()
    fd, temporary = tempfile.mkstemp(prefix=".input-planner.", dir=directory)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o660)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise AIInputPlannerError("short write while storing input planner state")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp_path, destination)
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return destination


def _selection_dir(ai_root: Path) -> Path:
    root = _orchestration_root(ai_root)
    directory = root / SELECTION_DIR
    _require_safe_directory(directory, create=True)
    return directory


def _store_selection(ai_root: Path, selection: Selection) -> tuple[str, Path]:
    data = selection.to_json_bytes()
    digest = sha256_bytes(data)
    path = _selection_dir(ai_root) / f"{digest}.selection.json"
    return digest, _store_immutable(path, data)


def _seed(*parts: object) -> str:
    return sha256_bytes(
        _canonical_json_bytes(
            {"record_version": RECORD_VERSION, "seed_parts": [str(part) for part in parts]}
        )
    )


def _ordered_for_coverage(catalog: InputCatalog, epoch: int) -> tuple[CatalogEntry, ...]:
    rows = list(catalog.entries)
    seed = _seed("coverage", catalog.sha256, epoch)
    random.Random(int(seed, 16)).shuffle(rows)
    return tuple(rows)


def _fit_entries(
    candidates: Sequence[CatalogEntry],
    *,
    batch_size: int,
) -> tuple[CatalogEntry, ...]:
    selected: list[CatalogEntry] = []
    total = 0
    for entry in candidates:
        if len(selected) >= batch_size:
            break
        if total + entry.byte_size > MAX_CONTEXT_BYTES:
            if selected:
                break
            continue
        selected.append(entry)
        total += entry.byte_size
    return tuple(selected)


def _random_entries(
    catalog: InputCatalog,
    *,
    cycle: int,
    batch_size: int,
) -> tuple[tuple[CatalogEntry, ...], str]:
    seed = _seed("random", catalog.sha256, cycle)
    rng = random.Random(int(seed, 16))
    all_rows = list(catalog.entries)
    rng.shuffle(all_rows)

    selected: list[CatalogEntry] = []
    by_kind = {
        "knowledge": [row for row in all_rows if row.source_kind == "knowledge"],
        "project-note": [row for row in all_rows if row.source_kind == "project-note"],
    }
    if batch_size >= 2 and all(by_kind.values()):
        for kind in ("knowledge", "project-note"):
            chosen = rng.choice(by_kind[kind])
            if chosen not in selected:
                selected.append(chosen)

    remainder = [row for row in all_rows if row not in selected]
    rng.shuffle(remainder)
    selected.extend(remainder)
    return _fit_entries(selected, batch_size=batch_size), seed


def choose_selection(
    catalog: InputCatalog,
    state: PlannerState,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    coverage_cycles: int = DEFAULT_COVERAGE_CYCLES,
    random_cycles: int = DEFAULT_RANDOM_CYCLES,
) -> tuple[Selection | None, PlannerState]:
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise AIInputPlannerError(f"batch_size must be 1..{MAX_BATCH_SIZE}")
    if type(coverage_cycles) is not int or coverage_cycles < 1:
        raise AIInputPlannerError("coverage_cycles must be >= 1")
    if type(random_cycles) is not int or random_cycles < 0:
        raise AIInputPlannerError("random_cycles must be >= 0")
    if not catalog.entries:
        return None, PlannerState(catalog.sha256, 1, 0, state.cycle + 1)

    if state.catalog_sha256 != catalog.sha256:
        state = PlannerState(catalog.sha256, 1, 0, state.cycle)

    period = coverage_cycles + random_cycles
    random_mode = random_cycles > 0 and (state.cycle % period) >= coverage_cycles

    if random_mode:
        selected, seed = _random_entries(
            catalog,
            cycle=state.cycle,
            batch_size=batch_size,
        )
        selection = Selection(
            policy=RANDOM_POLICY,
            objective_policy=OBJECTIVE_POLICY,
            catalog_sha256=catalog.sha256,
            epoch=state.coverage_epoch,
            cycle=state.cycle,
            seed=seed,
            entries=selected,
        )
        return selection, PlannerState(
            catalog.sha256,
            state.coverage_epoch,
            state.coverage_cursor,
            state.cycle + 1,
        )

    epoch = state.coverage_epoch
    cursor = state.coverage_cursor
    ordered = _ordered_for_coverage(catalog, epoch)
    if cursor >= len(ordered):
        epoch += 1
        cursor = 0
        ordered = _ordered_for_coverage(catalog, epoch)

    selected = _fit_entries(ordered[cursor:], batch_size=batch_size)
    if not selected:
        raise AIInputPlannerError("no catalog source fits the Context byte budget")
    next_cursor = cursor + len(selected)
    seed = _seed("coverage", catalog.sha256, epoch)
    selection = Selection(
        policy=COVERAGE_POLICY,
        objective_policy=OBJECTIVE_POLICY,
        catalog_sha256=catalog.sha256,
        epoch=epoch,
        cycle=state.cycle,
        seed=seed,
        entries=selected,
    )
    return selection, PlannerState(
        catalog.sha256,
        epoch,
        next_cursor,
        state.cycle + 1,
    )


def _current_states(ai_root: Path) -> dict[str, int]:
    try:
        conn = _connect_ro(ai_root)
    except PreReviewJobError as exc:
        if "database does not exist" in str(exc):
            return {}
        raise
    try:
        rows = conn.execute(
            """
            SELECT g.state, COUNT(*) AS count
            FROM generations g
            WHERE NOT EXISTS (
                SELECT 1
                FROM generations newer
                WHERE newer.job_id = g.job_id
                  AND newer.generation_index > g.generation_index
            )
            GROUP BY g.state
            """
        ).fetchall()
    finally:
        conn.close()
    return {str(row["state"]): int(row["count"]) for row in rows}


def _build_recipe(
    *,
    deployed_revision: str,
    generator_model: str,
    evaluator_model: str,
):
    if not re.fullmatch(r"[0-9a-f]{40,64}", deployed_revision):
        raise AIInputPlannerError("deployed_revision must be a full lowercase Git digest")
    for label, value in (
        ("generator_model", generator_model),
        ("evaluator_model", evaluator_model),
    ):
        if not value or value != value.strip() or len(value) > 256:
            raise AIInputPlannerError(f"{label} is invalid")

    value = {
        "record_version": 1,
        "pipeline": "knowledge-pre-review-v0",
        "generator": {
            "implementation_revision": deployed_revision,
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "prompt_template_sha256": generator_prompt_sha256(),
            "provider": PROVIDER_NAME,
            "model_identifier": generator_model,
            "model_revision": identifier_revision(generator_model),
            "model_config": {
                "adapter_version": GENERATOR_ADAPTER_VERSION,
                "identity_binding": IDENTITY_BINDING,
                "options": dict(GENERATOR_DEFAULT_OPTIONS),
            },
        },
        "validator": {"policy": "knowledge-note-v0"},
        "evaluation_context": {
            "selection_policy": "bm25-topk-recall-v0",
            "top_k": 5,
        },
        "evaluator": {
            "implementation_revision": deployed_revision,
            "prompt_template_version": EVALUATOR_PROMPT_TEMPLATE_VERSION,
            "prompt_template_sha256": evaluator_prompt_sha256(),
            "provider": PROVIDER_NAME,
            "model_identifier": evaluator_model,
            "model_revision": identifier_revision(evaluator_model),
            "model_config": {
                "adapter_version": EVALUATOR_ADAPTER_VERSION,
                "identity_binding": IDENTITY_BINDING,
                "strategy": EVALUATION_STRATEGY,
                "options": {"temperature": 0},
            },
        },
    }
    return parse_recipe(
        (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    )


def plan_once(
    ai_root: Path,
    vault_root: Path,
    *,
    deployed_revision: str,
    generator_model: str,
    evaluator_model: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    target_inflight: int = DEFAULT_TARGET_INFLIGHT,
    coverage_cycles: int = DEFAULT_COVERAGE_CYCLES,
    random_cycles: int = DEFAULT_RANDOM_CYCLES,
) -> dict[str, object]:
    if type(target_inflight) is not int or not 1 <= target_inflight <= HARD_BACKPRESSURE:
        raise AIInputPlannerError(
            f"target_inflight must be 1..{HARD_BACKPRESSURE}"
        )

    states = _current_states(ai_root)
    if states.get("blocked", 0) or states.get("retry_exhausted", 0):
        return {
            "event": "ai-input-planner",
            "status": "paused_pipeline_unhealthy",
            "states": states,
        }
    awaiting = states.get("awaiting_human_review", 0)
    if awaiting >= HARD_BACKPRESSURE:
        return {
            "event": "ai-input-planner",
            "status": "paused_backpressure",
            "awaiting_human_review": awaiting,
        }
    inflight = sum(states.get(name, 0) for name in _ACTIVE_JOB_STATES)
    if inflight >= target_inflight:
        return {
            "event": "ai-input-planner",
            "status": "target_queue_satisfied",
            "inflight": inflight,
            "target_inflight": target_inflight,
        }

    state = _load_state(ai_root)
    try:
        with mirror_read_lock(ai_root):
            catalog = build_catalog(vault_root)
            selection, next_state = choose_selection(
                catalog,
                state,
                batch_size=batch_size,
                coverage_cycles=coverage_cycles,
                random_cycles=random_cycles,
            )
            if selection is None:
                _store_state(ai_root, next_state)
                return {
                    "event": "ai-input-planner",
                    "status": "idle_no_eligible_sources",
                    "catalog_sha256": catalog.sha256,
                    "warnings": len(catalog.warnings),
                }
            source_paths = [entry.path for entry in selection.entries]
            context = build_context_bundle(
                vault_root,
                query=_SYNTHESIS_QUERY,
                source_paths=source_paths,
            )
            by_path = {entry.path: entry for entry in selection.entries}
            for source in context.sources:
                expected = by_path[source.path]
                if source.content_sha256 != expected.content_sha256:
                    raise AIInputPlannerError(
                        "selected source changed inside stabilized mirror view"
                    )
    except ProductionIOError as exc:
        raise AIInputPlannerError(str(exc)) from exc

    context_sha, _ = store_context_bundle(ai_root, context)
    selection_sha, _ = _store_selection(ai_root, selection)
    recipe = _build_recipe(
        deployed_revision=deployed_revision,
        generator_model=generator_model,
        evaluator_model=evaluator_model,
    )
    submitted = submit_job(
        ai_root,
        context_sha256=context_sha,
        recipe=recipe,
    )
    _store_state(ai_root, next_state)
    return {
        "event": "ai-input-planner",
        "status": "submitted" if submitted["created"] else "existing_job",
        "selection_sha256": selection_sha,
        "selection_policy": selection.policy,
        "objective_policy": selection.objective_policy,
        "context_sha256": context_sha,
        "source_count": len(selection.entries),
        "source_kinds": {
            "knowledge": sum(entry.source_kind == "knowledge" for entry in selection.entries),
            "project-note": sum(entry.source_kind == "project-note" for entry in selection.entries),
        },
        "job_id": submitted["job_id"],
        "created": submitted["created"],
        "catalog_sha256": catalog.sha256,
        "catalog_size": len(catalog.entries),
        "warnings": len(catalog.warnings),
        "coverage_epoch": next_state.coverage_epoch,
        "coverage_cursor": next_state.coverage_cursor,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-ai-input-planner")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--deployed-revision", required=True)
    parser.add_argument("--generator-model", required=True)
    parser.add_argument("--evaluator-model", required=True)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--target-inflight", type=int, default=DEFAULT_TARGET_INFLIGHT)
    parser.add_argument("--coverage-cycles", type=int, default=DEFAULT_COVERAGE_CYCLES)
    parser.add_argument("--random-cycles", type=int, default=DEFAULT_RANDOM_CYCLES)
    args = parser.parse_args(argv)

    try:
        result = plan_once(
            args.ai_root,
            args.vault_root,
            deployed_revision=args.deployed_revision,
            generator_model=args.generator_model,
            evaluator_model=args.evaluator_model,
            batch_size=args.batch_size,
            target_inflight=args.target_inflight,
            coverage_cycles=args.coverage_cycles,
            random_cycles=args.random_cycles,
        )
    except (AIInputPlannerError, ArtifactLifecycleError, PreReviewJobError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
