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
from .evaluation_artifact import (
    DEFAULT_EVALUATION_TOP_K,
    EVALUATION_CONTEXT_POLICY_VERSION,
)
from .evaluator_contract import EVALUATOR_PROMPT_TEMPLATE_VERSION
from .generator_contract import PROMPT_TEMPLATE_VERSION
from .knowledge_note_policy import POLICY_NAME
from .openai_compatible import (
    IDENTITY_BINDING,
    PROVIDER_NAME as OPENAI_PROVIDER_NAME,
    identifier_revision,
)
from .openai_evaluator import (
    ADAPTER_VERSION as OPENAI_EVALUATOR_ADAPTER_VERSION,
    EVALUATION_STRATEGY,
)
from .openai_generator import ADAPTER_VERSION as OPENAI_GENERATOR_ADAPTER_VERSION
from .ollama_evaluator import ADAPTER_VERSION as OLLAMA_EVALUATOR_ADAPTER_VERSION
from .ollama_generator import (
    ADAPTER_VERSION as OLLAMA_GENERATOR_ADAPTER_VERSION,
    PROVIDER_NAME as OLLAMA_PROVIDER_NAME,
)


ORCHESTRATION_STAGE = "02-Orchestration"
RECIPE_STAGE = "recipes"
DB_NAME = "pre-review-jobs.sqlite3"
PIPELINE_NAME = "knowledge-pre-review-v0"
RECORD_VERSION = 1
MAX_RECIPE_BYTES = 64 * 1024
MAX_METADATA_CHARS = 512
_IMPLEMENTATION_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")


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


@dataclass(frozen=True)
class StageWorkItem:
    job_id: str
    generation_id: str
    generation_index: int
    context_sha256: str
    recipe_sha256: str
    attempt_id: str
    attempt_index: int
    stage: str


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


def _parse_component(
    value: object,
    *,
    label: str,
    prompt_version: str,
    evaluator: bool,
) -> RecipeComponent:
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
    if provider not in {OPENAI_PROVIDER_NAME, OLLAMA_PROVIDER_NAME}:
        raise PreReviewJobError(
            f"{label}.provider must be {OPENAI_PROVIDER_NAME} or {OLLAMA_PROVIDER_NAME}"
        )
    if value["prompt_template_version"] != prompt_version:
        raise PreReviewJobError(
            f"{label}.prompt_template_version must be {prompt_version}"
        )

    model_identifier = _metadata(
        value["model_identifier"],
        label=f"{label}.model_identifier",
    )
    model_revision = _metadata(
        value["model_revision"],
        label=f"{label}.model_revision",
    )
    model_config = value["model_config"]

    if provider == OPENAI_PROVIDER_NAME:
        if model_revision != identifier_revision(model_identifier):
            raise PreReviewJobError(
                f"{label}.model_revision must explicitly use identifier-only binding"
            )
        expected_config = {"adapter_version", "identity_binding", "options"}
        if evaluator:
            expected_config.add("strategy")
        if not isinstance(model_config, dict) or set(model_config) != expected_config:
            raise PreReviewJobError(
                f"{label}.model_config properties do not match OpenAI-compatible v0 contract"
            )
        expected_adapter = (
            OPENAI_EVALUATOR_ADAPTER_VERSION
            if evaluator
            else OPENAI_GENERATOR_ADAPTER_VERSION
        )
        if model_config["adapter_version"] != expected_adapter:
            raise PreReviewJobError(
                f"{label}.model_config.adapter_version must be {expected_adapter}"
            )
        if model_config["identity_binding"] != IDENTITY_BINDING:
            raise PreReviewJobError(
                f"{label}.model_config.identity_binding must be {IDENTITY_BINDING}"
            )
    else:
        try:
            model_revision = _require_sha256(
                model_revision,
                label=f"{label}.model_revision",
            )
        except ArtifactLifecycleError as exc:
            raise PreReviewJobError(str(exc)) from exc
        expected_config = {"adapter_version", "think", "options"}
        if evaluator:
            expected_config.add("strategy")
        if not isinstance(model_config, dict) or set(model_config) != expected_config:
            raise PreReviewJobError(
                f"{label}.model_config properties do not match Ollama native v0 contract"
            )
        expected_adapter = (
            OLLAMA_EVALUATOR_ADAPTER_VERSION
            if evaluator
            else OLLAMA_GENERATOR_ADAPTER_VERSION
        )
        if model_config["adapter_version"] != expected_adapter:
            raise PreReviewJobError(
                f"{label}.model_config.adapter_version must be {expected_adapter}"
            )
        expected_think: object = "low" if evaluator else False
        if model_config["think"] != expected_think:
            raise PreReviewJobError(
                f"{label}.model_config.think must be {expected_think!r}"
            )

    if not isinstance(model_config["options"], dict):
        raise PreReviewJobError(f"{label}.model_config.options must be an object")
    if evaluator and model_config["strategy"] != EVALUATION_STRATEGY:
        raise PreReviewJobError(
            f"{label}.model_config.strategy must be {EVALUATION_STRATEGY}"
        )

    return RecipeComponent(
        implementation_revision=_implementation_revision(
            value["implementation_revision"],
            label=f"{label}.implementation_revision",
        ),
        prompt_template_version=prompt_version,
        prompt_template_sha256=_require_sha256(
            value["prompt_template_sha256"],
            label=f"{label}.prompt_template_sha256",
        ),
        provider=provider,
        model_identifier=model_identifier,
        model_revision=model_revision,
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
    if validator_policy != POLICY_NAME:
        raise PreReviewJobError(f"validator.policy must be {POLICY_NAME}")

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
    if selection_policy != EVALUATION_CONTEXT_POLICY_VERSION:
        raise PreReviewJobError(
            "evaluation_context.selection_policy must match runtime policy"
        )
    if type(top_k) is not int or top_k != DEFAULT_EVALUATION_TOP_K:
        raise PreReviewJobError(
            f"evaluation_context.top_k must be {DEFAULT_EVALUATION_TOP_K}"
        )

    recipe = PreReviewRecipe(
        generator=_parse_component(
            value["generator"],
            label="generator",
            prompt_version=PROMPT_TEMPLATE_VERSION,
            evaluator=False,
        ),
        validator_policy=validator_policy,
        evaluation_context_policy=selection_policy,
        evaluation_context_top_k=top_k,
        evaluator=_parse_component(
            value["evaluator"],
            label="evaluator",
            prompt_version=EVALUATOR_PROMPT_TEMPLATE_VERSION,
            evaluator=True,
        ),
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
        generator=_parse_component(
            value["generator"],
            label="generator",
            prompt_version=PROMPT_TEMPLATE_VERSION,
            evaluator=False,
        ),
        validator_policy=_metadata(validator["policy"], label="validator.policy"),
        evaluation_context_policy=_metadata(
            evaluation_context["selection_policy"],
            label="evaluation_context.selection_policy",
        ),
        evaluation_context_top_k=int(evaluation_context["top_k"]),
        evaluator=_parse_component(
            value["evaluator"],
            label="evaluator",
            prompt_version=EVALUATOR_PROMPT_TEMPLATE_VERSION,
            evaluator=True,
        ),
    )


def _job_root(ai_root: Path, *, create: bool) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    jobs = root / ORCHESTRATION_STAGE
    _require_safe_directory(jobs, create=create)
    recipes = jobs / RECIPE_STAGE
    _require_safe_directory(recipes, create=create)
    return jobs


def _db_path(ai_root: Path, *, create_dirs: bool) -> Path:
    path = _job_root(ai_root, create=create_dirs) / DB_NAME
    try:
        st = path.lstat()
    except FileNotFoundError:
        return path
    if path.is_symlink() or not path.is_file():
        raise PreReviewJobError("pre-review job database path is not a regular file")
    if st.st_nlink != 1:
        raise PreReviewJobError("pre-review job database must not have hard links")
    return path


def _connect_rw(ai_root: Path) -> sqlite3.Connection:
    path = _db_path(ai_root, create_dirs=True)
    created = False
    if not os.path.lexists(path):
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(path, flags, 0o660)
        except FileExistsError:
            pass
        except OSError as exc:
            raise PreReviewJobError("cannot safely create pre-review job database") from exc
        else:
            os.close(fd)
            created = True
    path = _db_path(ai_root, create_dirs=True)
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
        CREATE TABLE IF NOT EXISTS stage_outputs (
            generation_id TEXT NOT NULL REFERENCES generations(generation_id),
            stage TEXT NOT NULL,
            attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
            output_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(generation_id, stage)
        );
        CREATE TABLE IF NOT EXISTS supersessions (
            generation_id TEXT PRIMARY KEY REFERENCES generations(generation_id),
            reason_code TEXT NOT NULL,
            superseded_at TEXT NOT NULL
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
    if created:
        try:
            os.chmod(path, 0o660)
        except OSError:
            conn.close()
            raise
    return conn


def _connect_ro(ai_root: Path) -> sqlite3.Connection:
    path = _db_path(ai_root, create_dirs=False)
    if not path.exists():
        raise PreReviewJobError("pre-review job database does not exist")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        raise PreReviewJobError("cannot open pre-review job database read-only") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    row = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    if row is None or row["value"] != "1":
        conn.close()
        raise PreReviewJobError("unsupported pre-review job database schema")
    return conn


def store_recipe(ai_root: Path, recipe: PreReviewRecipe) -> tuple[str, Path]:
    root = _job_root(ai_root, create=True)
    data = recipe.to_json_bytes()
    digest = sha256_bytes(data)
    path = root / RECIPE_STAGE / f"{digest}.recipe.json"
    return digest, _store_immutable(path, data)


def load_recipe(ai_root: Path, recipe_sha256: str) -> PreReviewRecipe:
    digest = _require_sha256(recipe_sha256, label="recipe_sha256")
    path = _job_root(ai_root, create=False) / RECIPE_STAGE / f"{digest}.recipe.json"
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

    conn = _connect_rw(ai_root)
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
    conn = _connect_rw(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT job_id FROM jobs WHERE job_id = ?", (digest,)).fetchone()
        if job is None:
            raise PreReviewJobError("job does not exist")
        latest = conn.execute(
            "SELECT generation_index, state FROM generations WHERE job_id = ? "
            "ORDER BY generation_index DESC LIMIT 1",
            (digest,),
        ).fetchone()
        if latest is None:
            raise PreReviewJobError("job has no generation")
        if latest["state"] not in {
            "awaiting_human_review",
            "deterministic_reject",
            "retryable_failure",
            "retry_exhausted",
            "blocked",
        }:
            raise PreReviewJobError(
                "regeneration requires the current generation to be stopped"
            )
        index = int(latest["generation_index"]) + 1
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


_STAGE_START = {
    "generation": ("queued", "generating"),
    "validation": ("validating", "validating"),
    "evaluation_context": (
        "building_evaluation_context",
        "building_evaluation_context",
    ),
    "evaluation": ("evaluating", "evaluating"),
}

_STAGE_SUCCESS = {
    "generation": "validating",
    "validation": "building_evaluation_context",
    "evaluation_context": "evaluating",
    "evaluation": "awaiting_human_review",
}

_STAGE_PREVIOUS = {
    "generation": None,
    "validation": "generation",
    "evaluation_context": "validation",
    "evaluation": "evaluation_context",
}

_STAGE_OUTPUT_FIELDS = {
    "generation": (
        "proposal_sha256",
        "generation_sha256",
    ),
    "validation": (
        "proposal_sha256",
        "generation_sha256",
        "mutation_sha256",
        "request_sha256",
    ),
    "evaluation_context": (
        "proposal_sha256",
        "generation_sha256",
        "mutation_sha256",
        "request_sha256",
        "index_sha256",
        "evaluation_context_sha256",
    ),
    "evaluation": (
        "proposal_sha256",
        "generation_sha256",
        "mutation_sha256",
        "request_sha256",
        "index_sha256",
        "evaluation_context_sha256",
        "evaluation_sha256",
        "recommendation",
    ),
}


def supersede_unstarted_generation(
    ai_root: Path,
    generation_id: str,
    *,
    reason_code: str,
) -> dict[str, object]:
    digest = _require_sha256(generation_id, label="generation_id")
    reason = _metadata(reason_code, label="reason_code")
    now = _utc_now()

    conn = _connect_rw(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT generation_id, job_id, generation_index, state "
            "FROM generations WHERE generation_id = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise PreReviewJobError("generation does not exist")
        if row["state"] == "superseded":
            existing = conn.execute(
                "SELECT reason_code, superseded_at FROM supersessions "
                "WHERE generation_id = ?",
                (digest,),
            ).fetchone()
            if existing is None or existing["reason_code"] != reason:
                raise PreReviewJobError("superseded generation audit record mismatch")
            conn.commit()
            return {
                "record_version": RECORD_VERSION,
                "generation_id": digest,
                "job_id": row["job_id"],
                "generation_index": row["generation_index"],
                "state": "superseded",
                "reason_code": existing["reason_code"],
                "superseded_at": existing["superseded_at"],
                "reused": True,
            }
        if row["state"] not in {"queued", "retryable_failure"}:
            raise PreReviewJobError(
                "only queued or Generation-stage retryable generation can be superseded"
            )
        newer = conn.execute(
            "SELECT generation_id FROM generations "
            "WHERE job_id = ? AND generation_index > ? LIMIT 1",
            (row["job_id"], row["generation_index"]),
        ).fetchone()
        if newer is not None:
            raise PreReviewJobError("generation is not the current job generation")
        running = conn.execute(
            "SELECT COUNT(*) AS count FROM attempts "
            "WHERE generation_id = ? AND status = 'running'",
            (digest,),
        ).fetchone()
        outputs = conn.execute(
            "SELECT COUNT(*) AS count FROM stage_outputs WHERE generation_id = ?",
            (digest,),
        ).fetchone()
        if int(running["count"]) != 0 or int(outputs["count"]) != 0:
            raise PreReviewJobError(
                "generation has running or selected output evidence and cannot be superseded"
            )

        attempts = conn.execute(
            "SELECT stage, status FROM attempts "
            "WHERE generation_id = ? ORDER BY rowid",
            (digest,),
        ).fetchall()
        if row["state"] == "queued":
            if attempts:
                raise PreReviewJobError(
                    "queued generation has attempt evidence and cannot be superseded"
                )
        else:
            if not attempts:
                raise PreReviewJobError(
                    "retryable generation has no attempt evidence"
                )
            if any(item["stage"] != "generation" for item in attempts):
                raise PreReviewJobError(
                    "only Generation-stage retryable evidence can be superseded"
                )
            if attempts[-1]["status"] not in {"retryable_failure", "interrupted"}:
                raise PreReviewJobError(
                    "retryable generation latest attempt is not replaceable"
                )

        conn.execute(
            "UPDATE generations SET state = 'superseded', updated_at = ? "
            "WHERE generation_id = ?",
            (now, digest),
        )
        conn.execute(
            "INSERT INTO supersessions(generation_id, reason_code, superseded_at) "
            "VALUES(?, ?, ?)",
            (digest, reason, now),
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
        "job_id": row["job_id"],
        "generation_index": row["generation_index"],
        "state": "superseded",
        "reason_code": reason,
        "superseded_at": now,
        "reused": False,
    }


def _validated_stage(stage: str) -> str:
    value = _metadata(stage, label="stage")
    if value not in _STAGE_START:
        raise PreReviewJobError("attempt stage is invalid")
    return value


def _validated_stage_output(stage: str, output: Mapping[str, object]) -> dict[str, object]:
    fields = _STAGE_OUTPUT_FIELDS[stage]
    value = dict(output)
    if set(value) != set(fields):
        raise PreReviewJobError(f"{stage} output properties do not match contract")
    normalized: dict[str, object] = {}
    for field in fields:
        item = value[field]
        if field == "recommendation":
            if item not in {"proceed", "manual_review", "do_not_proceed"}:
                raise PreReviewJobError("evaluation recommendation is invalid")
            normalized[field] = item
        else:
            normalized[field] = _require_sha256(item, label=field)
    return normalized


def _load_stage_output_conn(
    conn: sqlite3.Connection,
    generation_id: str,
    stage: str,
) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT output_json FROM stage_outputs WHERE generation_id = ? AND stage = ?",
        (generation_id, stage),
    ).fetchone()
    if row is None:
        return None
    try:
        data = row["output_json"].encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise PreReviewJobError("stored stage output is not valid UTF-8") from exc
    value = _decode_json_object(data, label="pre-review stage output")
    return _validated_stage_output(stage, value)


def stage_output(
    ai_root: Path,
    generation_id: str,
    stage: str,
) -> dict[str, object] | None:
    digest = _require_sha256(generation_id, label="generation_id")
    stage_name = _validated_stage(stage)
    conn = _connect_ro(ai_root)
    try:
        return _load_stage_output_conn(conn, digest, stage_name)
    finally:
        conn.close()


def _validate_output_chain(
    conn: sqlite3.Connection,
    generation_id: str,
    stage: str,
    output: Mapping[str, object],
) -> dict[str, object]:
    normalized = _validated_stage_output(stage, output)
    previous_stage = _STAGE_PREVIOUS[stage]
    if previous_stage is None:
        return normalized
    previous = _load_stage_output_conn(conn, generation_id, previous_stage)
    if previous is None:
        raise PreReviewJobError(f"{stage} output requires {previous_stage} output")
    for key, value in previous.items():
        if normalized.get(key) != value:
            raise PreReviewJobError(
                f"{stage} output is not bound to selected {previous_stage} output"
            )
    return normalized


def start_attempt(ai_root: Path, generation_id: str, stage: str) -> dict[str, object]:
    digest = _require_sha256(generation_id, label="generation_id")
    stage_name = _validated_stage(stage)
    expected_state, active_state = _STAGE_START[stage_name]
    now = _utc_now()

    conn = _connect_rw(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        generation = conn.execute(
            "SELECT generation_id, state FROM generations WHERE generation_id = ?",
            (digest,),
        ).fetchone()
        if generation is None:
            raise PreReviewJobError("generation does not exist")
        running = conn.execute(
            "SELECT attempt_id FROM attempts WHERE generation_id = ? AND status = 'running' LIMIT 1",
            (digest,),
        ).fetchone()
        if running is not None:
            raise PreReviewJobError("generation already has a running attempt")
        if _load_stage_output_conn(conn, digest, stage_name) is not None:
            raise PreReviewJobError("stage already has a selected successful output")
        if generation["state"] != expected_state:
            raise PreReviewJobError(
                f"attempt stage {stage_name} requires generation state {expected_state}"
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
        if active_state != expected_state:
            conn.execute(
                "UPDATE generations SET state = ?, updated_at = ? WHERE generation_id = ?",
                (active_state, now, digest),
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


def complete_attempt(
    ai_root: Path,
    attempt_id: str,
    *,
    outcome: str,
    reason_code: str | None = None,
    output: Mapping[str, object] | None = None,
) -> dict[str, object]:
    digest = _require_sha256(attempt_id, label="attempt_id")
    if outcome not in {
        "succeeded",
        "retryable_failure",
        "deterministic_reject",
        "blocked",
    }:
        raise PreReviewJobError("attempt outcome is invalid")
    if outcome == "succeeded":
        if reason_code is not None:
            raise PreReviewJobError("successful attempt must not contain reason_code")
        if output is None:
            raise PreReviewJobError("successful attempt requires selected stage output")
        normalized_reason = None
    else:
        if output is not None:
            raise PreReviewJobError("failed attempt must not select stage output")
        normalized_reason = _metadata(reason_code, label="reason_code")
    now = _utc_now()

    conn = _connect_rw(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT a.generation_id, a.stage, a.status, g.state, g.job_id, g.generation_index "
            "FROM attempts a JOIN generations g ON g.generation_id = a.generation_id "
            "WHERE a.attempt_id = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise PreReviewJobError("attempt does not exist")
        if row["status"] != "running":
            raise PreReviewJobError("attempt is already completed")
        stage = row["stage"]
        active_state = _STAGE_START[stage][1]
        if row["state"] != active_state:
            raise PreReviewJobError("generation state does not match running attempt")

        normalized_output: dict[str, object] | None = None
        if outcome == "succeeded":
            assert output is not None
            normalized_output = _validate_output_chain(
                conn,
                row["generation_id"],
                stage,
                output,
            )
            if _load_stage_output_conn(conn, row["generation_id"], stage) is not None:
                raise PreReviewJobError("stage already has a selected successful output")
            target_state = _STAGE_SUCCESS[stage]
        elif outcome == "retryable_failure":
            target_state = "retryable_failure"
        elif outcome == "deterministic_reject":
            target_state = "deterministic_reject"
        else:
            target_state = "blocked"

        conn.execute(
            "UPDATE attempts SET status = ?, completed_at = ?, reason_code = ? "
            "WHERE attempt_id = ?",
            (outcome, now, normalized_reason, digest),
        )
        if normalized_output is not None:
            output_json = _canonical_json_bytes(normalized_output).decode("utf-8")
            conn.execute(
                "INSERT INTO stage_outputs(generation_id, stage, attempt_id, output_json, recorded_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (row["generation_id"], stage, digest, output_json, now),
            )
        conn.execute(
            "UPDATE generations SET state = ?, updated_at = ? WHERE generation_id = ?",
            (target_state, now, row["generation_id"]),
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
        "generation_id": row["generation_id"],
        "generation_index": row["generation_index"],
        "attempt_id": digest,
        "stage": stage,
        "outcome": outcome,
        "state": target_state,
        "reason_code": normalized_reason,
        "output": normalized_output,
    }


def _latest_attempt_conn(
    conn: sqlite3.Connection,
    generation_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT attempt_id, stage, attempt_index, status, completed_at, reason_code "
        "FROM attempts WHERE generation_id = ? ORDER BY rowid DESC LIMIT 1",
        (generation_id,),
    ).fetchone()


def _resume_state_for_stage(stage: str) -> str:
    return _STAGE_START[stage][0]


def retry_generation(ai_root: Path, generation_id: str) -> dict[str, object]:
    digest = _require_sha256(generation_id, label="generation_id")
    now = _utc_now()
    conn = _connect_rw(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state, job_id, generation_index FROM generations WHERE generation_id = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise PreReviewJobError("generation does not exist")
        if row["state"] not in {
            "retryable_failure",
            "retry_exhausted",
            "blocked",
        }:
            raise PreReviewJobError(
                "only retryable, exhausted, or blocked generation can be retried"
            )
        running = conn.execute(
            "SELECT attempt_id FROM attempts WHERE generation_id = ? AND status = 'running' LIMIT 1",
            (digest,),
        ).fetchone()
        if running is not None:
            raise PreReviewJobError("generation still has a running attempt")
        latest = _latest_attempt_conn(conn, digest)
        if latest is None:
            raise PreReviewJobError("generation has no attempt to retry")
        resume_state = _resume_state_for_stage(latest["stage"])
        if _load_stage_output_conn(conn, digest, latest["stage"]) is not None:
            raise PreReviewJobError("cannot retry a stage with selected successful output")
        conn.execute(
            "UPDATE generations SET state = ?, updated_at = ? WHERE generation_id = ?",
            (resume_state, now, digest),
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
        "previous_state": row["state"],
        "state": resume_state,
        "stage": latest["stage"],
    }


def awaiting_human_review_count(ai_root: Path) -> int:
    conn = _connect_ro(ai_root)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM generations g
            WHERE g.state = 'awaiting_human_review'
              AND NOT EXISTS (
                SELECT 1 FROM generations newer
                WHERE newer.job_id = g.job_id
                  AND newer.generation_index > g.generation_index
              )
            """
        ).fetchone()
        return int(row["count"])
    finally:
        conn.close()


def _recover_running_stage_attempts(
    conn: sqlite3.Connection,
    *,
    stage: str,
    now: str,
) -> None:
    rows = conn.execute(
        """
        SELECT a.attempt_id, a.generation_id
        FROM attempts a
        JOIN generations g ON g.generation_id = a.generation_id
        WHERE a.stage = ?
          AND a.status = 'running'
          AND NOT EXISTS (
            SELECT 1 FROM generations newer
            WHERE newer.job_id = g.job_id
              AND newer.generation_index > g.generation_index
          )
        """,
        (stage,),
    ).fetchall()
    for row in rows:
        if _load_stage_output_conn(conn, row["generation_id"], stage) is not None:
            raise PreReviewJobError(
                "running attempt unexpectedly has a selected stage output"
            )
        conn.execute(
            "UPDATE attempts SET status = 'interrupted', completed_at = ?, reason_code = 'worker_restart' "
            "WHERE attempt_id = ?",
            (now, row["attempt_id"]),
        )
        conn.execute(
            "UPDATE generations SET state = 'retryable_failure', updated_at = ? "
            "WHERE generation_id = ?",
            (now, row["generation_id"]),
        )


def claim_next_attempt(
    ai_root: Path,
    stage: str,
    *,
    max_attempts: int = 3,
    max_awaiting_review: int = 8,
    recover_running: bool = False,
) -> StageWorkItem | None:
    stage_name = _validated_stage(stage)
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise PreReviewJobError("max_attempts must be an integer in [1, 10]")
    if type(max_awaiting_review) is not int or not 1 <= max_awaiting_review <= 100:
        raise PreReviewJobError(
            "max_awaiting_review must be an integer in [1, 100]"
        )
    expected_state, active_state = _STAGE_START[stage_name]
    now = _utc_now()

    conn = _connect_rw(ai_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if recover_running:
            _recover_running_stage_attempts(conn, stage=stage_name, now=now)

        if stage_name == "generation":
            count_row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM generations g
                WHERE g.state = 'awaiting_human_review'
                  AND NOT EXISTS (
                    SELECT 1 FROM generations newer
                    WHERE newer.job_id = g.job_id
                      AND newer.generation_index > g.generation_index
                  )
                """
            ).fetchone()
            if int(count_row["count"]) >= max_awaiting_review:
                conn.commit()
                return None

        while True:
            candidate = conn.execute(
                """
                SELECT
                    g.generation_id,
                    g.generation_index,
                    g.state,
                    g.job_id,
                    j.context_sha256,
                    j.recipe_sha256
                FROM generations g
                JOIN jobs j ON j.job_id = g.job_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM generations newer
                    WHERE newer.job_id = g.job_id
                      AND newer.generation_index > g.generation_index
                )
                  AND (
                    g.state = ?
                    OR (
                        g.state = 'retryable_failure'
                        AND (
                            SELECT a.stage
                            FROM attempts a
                            WHERE a.generation_id = g.generation_id
                            ORDER BY a.rowid DESC
                            LIMIT 1
                        ) = ?
                    )
                  )
                ORDER BY g.created_at, g.generation_id
                LIMIT 1
                """,
                (expected_state, stage_name),
            ).fetchone()
            if candidate is None:
                conn.commit()
                return None

            if _load_stage_output_conn(
                conn, candidate["generation_id"], stage_name
            ) is not None:
                raise PreReviewJobError(
                    "claim candidate already has selected successful output"
                )

            attempt_count = conn.execute(
                "SELECT COUNT(*) AS count FROM attempts "
                "WHERE generation_id = ? AND stage = ?",
                (candidate["generation_id"], stage_name),
            ).fetchone()
            next_index = int(attempt_count["count"]) + 1

            if candidate["state"] == "retryable_failure":
                if int(attempt_count["count"]) >= max_attempts:
                    conn.execute(
                        "UPDATE generations SET state = 'retry_exhausted', updated_at = ? "
                        "WHERE generation_id = ?",
                        (now, candidate["generation_id"]),
                    )
                    continue
                conn.execute(
                    "UPDATE generations SET state = ?, updated_at = ? "
                    "WHERE generation_id = ?",
                    (expected_state, now, candidate["generation_id"]),
                )

            running = conn.execute(
                "SELECT attempt_id FROM attempts "
                "WHERE generation_id = ? AND status = 'running' LIMIT 1",
                (candidate["generation_id"],),
            ).fetchone()
            if running is not None:
                raise PreReviewJobError("generation already has a running attempt")

            attempt_id = _attempt_id(
                candidate["generation_id"], stage_name, next_index
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, generation_id, stage, attempt_index, status, started_at) "
                "VALUES(?, ?, ?, ?, 'running', ?)",
                (
                    attempt_id,
                    candidate["generation_id"],
                    stage_name,
                    next_index,
                    now,
                ),
            )
            if active_state != expected_state:
                conn.execute(
                    "UPDATE generations SET state = ?, updated_at = ? "
                    "WHERE generation_id = ?",
                    (active_state, now, candidate["generation_id"]),
                )
            conn.commit()
            return StageWorkItem(
                job_id=candidate["job_id"],
                generation_id=candidate["generation_id"],
                generation_index=int(candidate["generation_index"]),
                context_sha256=candidate["context_sha256"],
                recipe_sha256=candidate["recipe_sha256"],
                attempt_id=attempt_id,
                attempt_index=next_index,
                stage=stage_name,
            )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def job_status(ai_root: Path, job_id: str) -> dict[str, object]:
    digest = _require_sha256(job_id, label="job_id")
    conn = _connect_ro(ai_root)
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

    supersede = sub.add_parser("supersede")
    supersede.add_argument("--ai-root", type=Path, required=True)
    supersede.add_argument("--generation-id", required=True)
    supersede.add_argument("--reason-code", required=True)

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
        elif args.command == "supersede":
            result = supersede_unstarted_generation(
                args.ai_root,
                args.generation_id,
                reason_code=args.reason_code,
            )
        else:
            result = job_status(args.ai_root, args.job_id)
    except (ArtifactLifecycleError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
