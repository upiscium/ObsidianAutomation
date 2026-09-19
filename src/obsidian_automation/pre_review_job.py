from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    _utc_now,
    sha256_bytes,
)
from .context_bundle import load_context_bundle
from .generation_artifact import validate_model_config


JOB_STAGE = "02-Jobs"
RECIPE_STAGE = "recipes"
DB_NAME = "pre-review-jobs.sqlite3"
PIPELINE_NAME = "knowledge-pre-review-v0"
RECORD_VERSION = 1
MAX_RECIPE_BYTES = 64 * 1024
MAX_METADATA_CHARS = 512
_IMPLEMENTATION_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")

GENERATION_STATES = frozenset(
    {
        "queued",
        "generating",
        "validating",
        "building_evaluation_context",
        "evaluating",
        "awaiting_human_review",
        "retryable_failure",
        "deterministic_reject",
    }
)

ALLOWED_TRANSITIONS = {
    "queued": frozenset({"generating"}),
    "generating": frozenset({"validating", "retryable_failure", "deterministic_reject"}),
    "validating": frozenset(
        {"building_evaluation_context", "retryable_failure", "deterministic_reject"}
    ),
    "building_evaluation_context": frozenset(
        {"evaluating", "retryable_failure", "deterministic_reject"}
    ),
    "evaluating": frozenset(
        {"awaiting_human_review", "retryable_failure", "deterministic_reject"}
    ),
    "retryable_failure": frozenset({"queued"}),
    "awaiting_human_review": frozenset(),
    "deterministic_reject": frozenset(),
}


@dataclass(frozen=True)
class RecipeComponent:
    implementation_revision: str
    prompt_template_version: str
    prompt_template_sha256: str
    provider: str
    model_identifier: str
    model_revision: str
    model_config: Mapping[str, object]


@dataclass(frozen=True)
class PreReviewRecipe:
    generator: RecipeComponent
    validator_policy: str
    evaluation_context_policy: str
    evaluation_context_top_k: int
    evaluator: RecipeComponent

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "pipeline": PIPELINE_NAME,
                "generator": _component_payload(self.generator),
                "validator": {"policy": self.validator_policy},
                "evaluation_context": {
                    "selection_policy": self.evaluation_context_policy,
                    "top_k": self.evaluation_context_top_k,
                },
                "evaluator": _component_payload(self.evaluator),
            }
        )


class PreReviewJobError(ArtifactLifecycleError):
    """Raised when pre-review orchestration metadata is invalid or unsafe."""


def _metadata(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PreReviewJobError(f"{label} must be a non-empty trimmed string")
    if len(value) > MAX_METADATA_CHARS:
        raise PreReviewJobError(f"{label} is too long")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise PreReviewJobError(f"{label} must not contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PreReviewJobError(f"{label} must be UTF-8 encodable") from exc
    return value


def _implementation_revision(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IMPLEMENTATION_REVISION_RE.fullmatch(value) is None:
        raise PreReviewJobError(
            f"{label} must be a lowercase 40..64 character hexadecimal commit digest"
        )
    return value


def _component_payload(component: RecipeComponent) -> dict[str, object]:
    return {
        "implementation_revision": component.implementation_revision,
        "prompt_template_version": component.prompt_template_version,
        "prompt_template_sha256": component.prompt_template_sha256,
        "provider": component.provider,
        "model_identifier": component.model_identifier,
        "model_revision": component.model_revision,
        "model_config": dict(component.model_config),
    }


def _parse_component(value: object, *, label: str) -> RecipeComponent:
    required = {
        "implementation_revision",
        "prompt_template_version",
        "prompt_template_sha256",
        "provider",
        "model_identifier",
        "model_revision",
        "model_config",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise PreReviewJobError(f"{label} properties do not match contract")
    provider = _metadata(value["provider"], label=f"{label}.provider")
    if provider != "ollama":
        raise PreReviewJobError(f"{label}.provider must be ollama in v0")
    model_config = value["model_config"]
    if not isinstance(model_config, dict):
        raise PreReviewJobError(f"{label}.model_config must be an object")
    return RecipeComponent(
        implementation_revision=_implementation_revision(
            value["implementation_revision"],
            label=f"{label}.implementation_revision",
        ),
        prompt_template_version=_metadata(
            value["prompt_template_version"],
            label=f"{label}.prompt_template_version",
        ),
        prompt_template_sha256=_require_sha256(
            value["prompt_template_sha256"],
            label=f"{label}.prompt_template_sha256",
        ),
        provider=provider,
        model_identifier=_metadata(
            value["model_identifier"],
            label=f"{label}.model_identifier",
        ),
        model_revision=_metadata(
            value["model_revision"],
            label=f"{label}.model_revision",
        ),
        model_config=validate_model_config(model_config),
    )


def parse_recipe(data: bytes) -> PreReviewRecipe:
    if len(data) > MAX_RECIPE_BYTES:
        raise PreReviewJobError(f"recipe exceeds {MAX_RECIPE_BYTES} bytes")
    value = _decode_json_object(data, label="pre-review recipe")
    required = {
        "record_version",
        "pipeline",
        "generator",
        "validator",
        "evaluation_context",
        "evaluator",
    }
    if set(value) != required:
        raise PreReviewJobError("recipe properties do not match contract")
    if value["record_version"] != RECORD_VERSION or type(value["record_version"]) is not int:
        raise PreReviewJobError("recipe record_version must be integer 1")
    if value["pipeline"] != PIPELINE_NAME:
        raise PreReviewJobError(f"recipe pipeline must be {PIPELINE_NAME}")

    validator = value["validator"]
    if not isinstance(validator, dict) or set(validator) != {"policy"}:
        raise PreReviewJobError("validator properties do not match contract")
    validator_policy = _metadata(validator["policy"], label="validator.policy")

    evaluation_context = value["evaluation_context"]
    if not isinstance(evaluation_context, dict) or set(evaluation_context) != {
        "selection_policy",
        "top_k",
    }:
        raise PreReviewJobError("evaluation_context properties do not match contract")
    selection_policy = _metadata(
        evaluation_context["selection_policy"],
        label="evaluation_context.selection_policy",
    )
    top_k = evaluation_context["top_k"]
    if type(top_k) is not int or not 1 <= top_k <= 50:
        raise PreReviewJobError("evaluation_context.top_k must be an integer in [1, 50]")

    recipe = PreReviewRecipe(
        generator=_parse_component(value["generator"], label="generator"),
        validator_policy=validator_policy,
        evaluation_context_policy=selection_policy,
        evaluation_context_top_k=top_k,
        evaluator=_parse_component(value["evaluator"], label="evaluator"),
    )
    canonical = recipe.to_json_bytes()
    if parse_recipe_roundtrip_guard(canonical) != recipe:
        raise PreReviewJobError("recipe canonical round-trip mismatch")
    return recipe


def parse_recipe_roundtrip_guard(data: bytes) -> PreReviewRecipe:
    """Parse canonical recipe bytes without recursively invoking the round-trip check."""
    value = _decode_json_object(data, label="pre-review recipe")
    validator = value["validator"]
    evaluation_context = value["evaluation_context"]
    return PreReviewRecipe(
        generator=_parse_component(value["generator"], label="generator"),
        validator_policy=_metadata(validator["policy"], label="validator.policy"),
        evaluation_context_policy=_metadata(
            evaluation_context["selection_policy"],
            label="evaluation_context.selection_policy",
        ),
        evaluation_context_top_k=int(evaluation_context["top_k"]),
        evaluator=_parse_component(value["evaluator"], label="evaluator"),
    )


def _job_root(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    jobs = root / JOB_STAGE
    _require_safe_directory(jobs, create=True)
    recipes = jobs / RECIPE_STAGE
    _require_safe_directory(recipes, create=True)
    return jobs


def _db_path(ai_root: Path) -> Path:
    path = _job_root(ai_root) / DB_NAME
    try:
        st = path.lstat()
    except FileNotFoundError:
        return path
    if path.is_symlink() or not path.is_file():
        raise PreReviewJobError("pre-review job database path is not a regular file")
    if st.st_nlink != 1:
        raise PreReviewJobError("pre-review job database must not have hard links")
    return path


def _connect(ai_root: Path) -> sqlite3.Connection:
    path = _db_path(ai_root)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = FULL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            context_sha256 TEXT NOT NULL,
            recipe_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS generations (
            generation_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            generation_index INTEGER NOT NULL CHECK(generation_index >= 1),
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(job_id, generation_index)
        );
        CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL REFERENCES generations(generation_id),
            stage TEXT NOT NULL,
            attempt_index INTEGER NOT NULL CHECK(attempt_index >= 1),
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            reason_code TEXT,
            UNIQUE(generation_id, stage, attempt_index)
        );
        """
    )
    row = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO metadata(key, value) VALUES('schema_version', '1')")
        conn.commit()
    elif row["value"] != "1":
        conn.close()
        raise PreReviewJobError("unsupported pre-review job database schema")
    try:
        os.chmod(path, 0o600)
    except OSError:
        conn.close()
        raise
    return conn


def store_recipe(ai_root: Path, recipe: PreReviewRecipe) -> tuple[str, Path]:
    root = _job_root(ai_root)
    data = recipe.to_json_bytes()
    digest = sha256_bytes(data)
    path = root / RECIPE_STAGE / f"{digest}.recipe.json"
    return digest, _store_immutable(path, data)


def load_recipe(ai_root: Path, recipe_sha256: str) -> PreReviewRecipe:
    digest = _require_sha256(recipe_sha256, label="recipe_sha256")
    path = _job_root(ai_root) / RECIPE_STAGE / f"{digest}.recipe.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise PreReviewJobError("recipe artifact hash mismatch")
    return parse_recipe(data)


def _job_id(context_sha256: str, recipe_sha256: str) -> str:
    return sha256_bytes(
        _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "pipeline": PIPELINE_NAME,
                "context_sha256": context_sha256,
                "recipe_sha256": recipe_sha256,
            }
        )
    )


def _generation_id(job_id: str, generation_index: int) -> str:
    return sha256_bytes(
        _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "job_id": job_id,
                "generation_index": generation_index,
            }
        )
    )


def _attempt_id(generation_id: str, stage: str, attempt_index: int) -> str:
    return sha256_bytes(
        _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "generation_id": generation_id,
                "stage": stage,
                "attempt_index": attempt_index,
            }
        )
    )


def submit_job(
    ai_root: Path,
    *,
    context_sha256: str,
    recipe: PreReviewRecipe,
) -> dict[str, object]:
    context_digest = _require_sha256(context_sha256, label="context_sha256")
    load_context_bundle(ai_root, context_digest)
    recipe_digest, recipe_path = store_recipe(ai_root, recipe)
    job_id = _job_id(context_digest, recipe_digest)
    now = _utc_now()

    conn = _connect(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT context_sha256, recipe_sha256 FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        created = row is None
        if created:
            conn.execute(
                "INSERT INTO jobs(job_id, context_sha256, recipe_sha256, created_at) VALUES(?, ?, ?, ?)",
                (job_id, context_digest, recipe_digest, now),
            )
            generation_id = _generation_id(job_id, 1)
            conn.execute(
                "INSERT INTO generations(generation_id, job_id, generation_index, state, created_at, updated_at) "
                "VALUES(?, ?, 1, 'queued', ?, ?)",
                (generation_id, job_id, now, now),
            )
        else:
            if row["context_sha256"] != context_digest or row["recipe_sha256"] != recipe_digest:
                raise PreReviewJobError("job_id collision with different job binding")
            gen = conn.execute(
                "SELECT generation_id FROM generations WHERE job_id = ? "
                "ORDER BY generation_index DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if gen is None:
                raise PreReviewJobError("existing job has no generation")
            generation_id = gen["generation_id"]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "record_version": RECORD_VERSION,
        "job_id": job_id,
        "context_sha256": context_digest,
        "recipe_sha256": recipe_digest,
        "recipe_path": str(recipe_path),
        "generation_id": generation_id,
        "created": created,
    }


def regenerate_job(ai_root: Path, job_id: str) -> dict[str, object]:
    digest = _require_sha256(job_id, label="job_id")
    now = _utc_now()
    conn = _connect(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT job_id FROM jobs WHERE job_id = ?", (digest,)).fetchone()
        if job is None:
            raise PreReviewJobError("job does not exist")
        row = conn.execute(
            "SELECT COALESCE(MAX(generation_index), 0) AS last_index FROM generations WHERE job_id = ?",
            (digest,),
        ).fetchone()
        index = int(row["last_index"]) + 1
        generation_id = _generation_id(digest, index)
        conn.execute(
            "INSERT INTO generations(generation_id, job_id, generation_index, state, created_at, updated_at) "
            "VALUES(?, ?, ?, 'queued', ?, ?)",
            (generation_id, digest, index, now, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "record_version": RECORD_VERSION,
        "job_id": digest,
        "generation_id": generation_id,
        "generation_index": index,
        "state": "queued",
    }


def transition_generation(
    ai_root: Path,
    generation_id: str,
    target_state: str,
) -> dict[str, object]:
    digest = _require_sha256(generation_id, label="generation_id")
    if target_state not in GENERATION_STATES:
        raise PreReviewJobError("target generation state is invalid")
    now = _utc_now()
    conn = _connect(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state, job_id, generation_index FROM generations WHERE generation_id = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise PreReviewJobError("generation does not exist")
        current = row["state"]
        if target_state not in ALLOWED_TRANSITIONS[current]:
            raise PreReviewJobError(
                f"generation transition is not allowed: {current} -> {target_state}"
            )
        conn.execute(
            "UPDATE generations SET state = ?, updated_at = ? WHERE generation_id = ?",
            (target_state, now, digest),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "record_version": RECORD_VERSION,
        "job_id": row["job_id"],
        "generation_id": digest,
        "generation_index": row["generation_index"],
        "previous_state": current,
        "state": target_state,
    }


def allocate_attempt(ai_root: Path, generation_id: str, stage: str) -> dict[str, object]:
    digest = _require_sha256(generation_id, label="generation_id")
    stage_name = _metadata(stage, label="stage")
    if stage_name not in {"generation", "validation", "evaluation_context", "evaluation"}:
        raise PreReviewJobError("attempt stage is invalid")
    now = _utc_now()
    required_state = {
        "generation": "generating",
        "validation": "validating",
        "evaluation_context": "building_evaluation_context",
        "evaluation": "evaluating",
    }[stage_name]
    conn = _connect(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        generation = conn.execute(
            "SELECT generation_id, state FROM generations WHERE generation_id = ?",
            (digest,),
        ).fetchone()
        if generation is None:
            raise PreReviewJobError("generation does not exist")
        if generation["state"] != required_state:
            raise PreReviewJobError(
                f"attempt stage {stage_name} requires generation state {required_state}"
            )
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_index), 0) AS last_index FROM attempts "
            "WHERE generation_id = ? AND stage = ?",
            (digest, stage_name),
        ).fetchone()
        index = int(row["last_index"]) + 1
        attempt_id = _attempt_id(digest, stage_name, index)
        conn.execute(
            "INSERT INTO attempts(attempt_id, generation_id, stage, attempt_index, status, started_at) "
            "VALUES(?, ?, ?, ?, 'running', ?)",
            (attempt_id, digest, stage_name, index, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "record_version": RECORD_VERSION,
        "generation_id": digest,
        "attempt_id": attempt_id,
        "stage": stage_name,
        "attempt_index": index,
        "status": "running",
    }


def job_status(ai_root: Path, job_id: str) -> dict[str, object]:
    digest = _require_sha256(job_id, label="job_id")
    conn = _connect(ai_root)
    try:
        job = conn.execute(
            "SELECT job_id, context_sha256, recipe_sha256, created_at FROM jobs WHERE job_id = ?",
            (digest,),
        ).fetchone()
        if job is None:
            raise PreReviewJobError("job does not exist")
        generations = conn.execute(
            "SELECT generation_id, generation_index, state, created_at, updated_at "
            "FROM generations WHERE job_id = ? ORDER BY generation_index",
            (digest,),
        ).fetchall()
        if not generations:
            raise PreReviewJobError("job has no generation")
        current = generations[-1]
    finally:
        conn.close()
    return {
        "record_version": RECORD_VERSION,
        "authority": "orchestration_metadata_only",
        "job_id": job["job_id"],
        "context_sha256": job["context_sha256"],
        "recipe_sha256": job["recipe_sha256"],
        "created_at": job["created_at"],
        "generation_count": len(generations),
        "current_generation": {
            "generation_id": current["generation_id"],
            "generation_index": current["generation_index"],
            "state": current["state"],
            "created_at": current["created_at"],
            "updated_at": current["updated_at"],
        },
    }


def _load_recipe_file(path: Path) -> PreReviewRecipe:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PreReviewJobError(f"cannot read recipe file: {path}") from exc
    return parse_recipe(data)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-pre-review-job")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit")
    submit.add_argument("--ai-root", type=Path, required=True)
    submit.add_argument("--context-sha256", required=True)
    submit.add_argument("--recipe-file", type=Path, required=True)

    regenerate = sub.add_parser("regenerate")
    regenerate.add_argument("--ai-root", type=Path, required=True)
    regenerate.add_argument("--job-id", required=True)

    status = sub.add_parser("status")
    status.add_argument("--ai-root", type=Path, required=True)
    status.add_argument("--job-id", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            result = submit_job(
                args.ai_root,
                context_sha256=args.context_sha256,
                recipe=_load_recipe_file(args.recipe_file),
            )
        elif args.command == "regenerate":
            result = regenerate_job(args.ai_root, args.job_id)
        else:
            result = job_status(args.ai_root, args.job_id)
    except (ArtifactLifecycleError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
