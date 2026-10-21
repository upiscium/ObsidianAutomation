"""Explicitly inert external Source admission into the real pre-review Job DB.

This module does NOT enqueue an existing knowledge-pre-review-v0 worker,
does NOT create a Generation Record, and cannot reach Human Review.
It commits a separate 'external_admitted' generation state so the real
Job state machine can track the bounded Source while awaiting a separately
versioned Generator/Validator/Evaluator integration.

Never enable production callers until the same-UID pilot is replaced with
dedicated service identity and a race-free private Source admission protocol.
"""

from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _require_sha256,
    _store_immutable,
    _utc_now,
    sha256_bytes,
)
from .chatgpt_export_review_preview import ExternalReviewPreview
from .chatgpt_external_context import (
    EXTERNAL_CONTEXT_KIND,
    ExternalContextError,
)
from .chatgpt_external_context_store import (
    ExternalContextStoreError,
    revalidate_saved_external_context,
)
from .chatgpt_external_proposer import (
    PROMPT_VERSION as EXTERNAL_PROPOSER_PROMPT_VERSION,
)
from .chatgpt_external_proposer import (
    prompt_template_sha256,
)
from .pre_review_job import (
    PreReviewJobError,
    _connect_rw,
    _generation_id,
    _job_root,
    job_status,
)

EXTERNAL_PIPELINE = "chatgpt-external-pre-review-v0"
EXTERNAL_RECIPE_VERSION = 1
EXTERNAL_RECIPE_SUFFIX = "external-pre-review-recipe"
EXTERNAL_ADMITTED_STATE = "external_admitted"
EXTERNAL_INPUT_POLICY = "exact-private-source-revision-v0"
EXTERNAL_ADMISSION_MODE = "admission-only-no-worker"
EXTERNAL_REVIEW_POLICY = "semantic-and-human-review-required-v0"
MAX_RECIPE_BYTES = 4 * 1024
MAX_STAGED_ADMISSIONS = 128


class ExternalJobError(ValueError):
    """Fixed, value-free failure codes; no private text/host path leakage."""


@dataclass(frozen=True, repr=False)
class ExternalPreReviewRecipe:
    record_version: int = EXTERNAL_RECIPE_VERSION
    pipeline: str = EXTERNAL_PIPELINE
    context_kind: str = EXTERNAL_CONTEXT_KIND
    input_policy: str = EXTERNAL_INPUT_POLICY
    admission_mode: str = EXTERNAL_ADMISSION_MODE
    proposer_prompt_version: str = EXTERNAL_PROPOSER_PROMPT_VERSION
    proposer_prompt_sha256: str = ""
    review_policy: str = EXTERNAL_REVIEW_POLICY

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": self.record_version,
                "pipeline": self.pipeline,
                "context_kind": self.context_kind,
                "input_policy": self.input_policy,
                "admission_mode": self.admission_mode,
                "proposer_prompt_version": self.proposer_prompt_version,
                "proposer_prompt_sha256": self.proposer_prompt_sha256,
                "review_policy": self.review_policy,
            }
        )


def default_external_admission_recipe() -> ExternalPreReviewRecipe:
    return ExternalPreReviewRecipe(
        proposer_prompt_sha256=prompt_template_sha256(),
    )


def parse_external_admission_recipe(data: bytes) -> ExternalPreReviewRecipe:
    if type(data) is not bytes or len(data) > MAX_RECIPE_BYTES:
        raise ExternalJobError("invalid_external_recipe_bytes")
    try:
        item = _decode_json_object(data, label="external admission recipe")
    except ArtifactLifecycleError:
        raise ExternalJobError("invalid_external_recipe_json") from None
    expected = {
        "record_version",
        "pipeline",
        "context_kind",
        "input_policy",
        "admission_mode",
        "proposer_prompt_version",
        "proposer_prompt_sha256",
        "review_policy",
    }
    if set(item) != expected:
        raise ExternalJobError("invalid_external_recipe_properties")
    recipe = default_external_admission_recipe()
    actual = {k: getattr(recipe, k) for k in expected}
    if any(
        type(item[k]) is not type(actual[k]) or item[k] != actual[k] for k in expected
    ):
        raise ExternalJobError("external_recipe_version_or_policy_unsupported")
    if recipe.to_json_bytes() != _canonical_json_bytes(item):
        raise ExternalJobError("external_recipe_noncanonical")
    return recipe


def _external_recipe_path(ai_root: Path, digest: str) -> Path:
    try:
        recipe_dir = _job_root(ai_root, create=False) / "recipes"
        return (
            recipe_dir
            / f"{_require_sha256(digest, label='recipe')}.{EXTERNAL_RECIPE_SUFFIX}.json"
        )
    except (ArtifactLifecycleError, PreReviewJobError, OSError):
        raise ExternalJobError("external_recipe_directory_unavailable") from None


def load_external_admission_recipe(
    ai_root: Path, digest: str
) -> ExternalPreReviewRecipe:
    path = _external_recipe_path(ai_root, digest)
    try:
        flags = (
            os.O_RDONLY
            | os.O_NONBLOCK
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ExternalJobError("external_recipe_file_invalid") from None
            raise
        try:
            before = os.fstat(fd)
            disk = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (before.st_dev, before.st_ino) != (disk.st_dev, disk.st_ino)
                or not 0 < before.st_size <= MAX_RECIPE_BYTES
            ):
                raise ExternalJobError("external_recipe_file_invalid")
            chunks: list[bytes] = []
            total = 0
            while total <= MAX_RECIPE_BYTES:
                part = os.read(fd, min(1024, MAX_RECIPE_BYTES + 1 - total))
                if not part:
                    break
                chunks.append(part)
                total += len(part)
            after = os.fstat(fd)
            if (
                total != before.st_size
                or total > MAX_RECIPE_BYTES
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise ExternalJobError("external_recipe_file_invalid")
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if sha256_bytes(data) != digest:
            raise ExternalJobError("external_recipe_digest_mismatch")
        return parse_external_admission_recipe(data)
    except ExternalJobError:
        raise
    except (ArtifactLifecycleError, OSError):
        raise ExternalJobError("external_recipe_unreadable") from None


def _external_job_id(context_sha: str, recipe_sha: str) -> str:
    return sha256_bytes(
        _canonical_json_bytes(
            {
                "record_version": 1,
                "pipeline": EXTERNAL_PIPELINE,
                "context_sha256": context_sha,
                "recipe_sha256": recipe_sha,
            }
        )
    )


def _verified_private_context(
    ai_root: Path,
    private_root: Path,
    preview: ExternalReviewPreview,
    context_sha: str,
) -> None:
    try:
        context = revalidate_saved_external_context(
            ai_root,
            private_root=private_root,
            preview=preview,
            external_context_sha256=context_sha,
        )
        if not context.evidence:
            raise ExternalJobError("external_source_no_candidate")
    except ExternalJobError:
        raise
    except (ExternalContextError, ExternalContextStoreError, OSError):
        raise ExternalJobError("external_source_not_current") from None


def stage_external_admission(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    external_context_sha256: str,
    recipe: ExternalPreReviewRecipe | None = None,
    max_admissions: int = MAX_STAGED_ADMISSIONS,
) -> dict[str, object]:
    """Create a *non-runnable* genuine pre-review Job/generation row.

    A private Source is checked before metadata publication. The admitted
    generation is deliberately NOT 'queued', preventing preexisting workers
    from claiming a recipe they do not understand. The Source may change
    after admission; a future activation MUST repeat the strict freshness
    check before a real Worker can run. No activation method exists here.
    """
    if (
        type(max_admissions) is not int
        or not 1 <= max_admissions <= MAX_STAGED_ADMISSIONS
    ):
        raise ExternalJobError("external_admission_limit_invalid")
    try:
        context_sha = _require_sha256(
            external_context_sha256,
            label="external_context_sha256",
        )
    except ArtifactLifecycleError:
        raise ExternalJobError("external_context_sha_invalid") from None
    recipe = recipe if recipe is not None else default_external_admission_recipe()
    if type(recipe) is not ExternalPreReviewRecipe:
        raise ExternalJobError("invalid_external_recipe")
    try:
        parsed = parse_external_admission_recipe(recipe.to_json_bytes())
    except (ExternalJobError, ArtifactLifecycleError, ValueError, TypeError):
        raise ExternalJobError("invalid_external_recipe") from None
    if parsed != recipe:
        raise ExternalJobError("external_recipe_roundtrip_mismatch")
    _verified_private_context(ai_root, private_root, preview, context_sha)

    data = recipe.to_json_bytes()
    recipe_sha = hashlib.sha256(data).hexdigest()
    try:
        # The pre-review Job root might not exist yet. Only create the
        # existing Orchestration metadata hierarchy, never 00-Untrusted or
        # any Vault/Review artifact. This is an admission reservation.
        root = _job_root(ai_root, create=True)
        recipe_path = root / "recipes" / f"{recipe_sha}.{EXTERNAL_RECIPE_SUFFIX}.json"
        _store_immutable(recipe_path, data)
    except (ArtifactLifecycleError, PreReviewJobError, OSError):
        raise ExternalJobError("external_recipe_store_failed") from None

    job_id = _external_job_id(context_sha, recipe_sha)
    generation_id = _generation_id(job_id, 1)
    now = _utc_now()
    try:
        conn = _connect_rw(ai_root)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT context_sha256,recipe_sha256 FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            created = row is None
            if created:
                total = conn.execute(
                    "SELECT COUNT(*) FROM generations WHERE state=?",
                    (EXTERNAL_ADMITTED_STATE,),
                ).fetchone()[0]
                if total >= max_admissions:
                    raise ExternalJobError("external_admission_backpressure")
                conn.execute(
                    "INSERT INTO jobs(job_id,context_sha256,recipe_sha256,created_at)"
                    " VALUES(?,?,?,?)",
                    (job_id, context_sha, recipe_sha, now),
                )
                conn.execute(
                    "INSERT INTO generations("
                    "generation_id,job_id,generation_index,state,created_at,updated_at)"
                    " VALUES(?,?,1,?,?,?)",
                    (generation_id, job_id, EXTERNAL_ADMITTED_STATE, now, now),
                )
            else:
                if (
                    row["context_sha256"] != context_sha
                    or row["recipe_sha256"] != recipe_sha
                ):
                    raise ExternalJobError("external_job_id_collision")
                rows = conn.execute(
                    "SELECT generation_id,generation_index,state FROM generations "
                    "WHERE job_id=?",
                    (job_id,),
                ).fetchall()
                if (
                    len(rows) != 1
                    or rows[0]["generation_id"] != generation_id
                    or rows[0]["generation_index"] != 1
                    or rows[0]["state"] != EXTERNAL_ADMITTED_STATE
                ):
                    raise ExternalJobError("external_job_state_invalid")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except ExternalJobError:
        raise
    except (PreReviewJobError, sqlite3.Error, OSError):
        raise ExternalJobError("external_admission_database_failure") from None

    # Only metadata. Revalidation at activation is still mandatory.
    return {
        "record_version": 1,
        "pipeline": EXTERNAL_PIPELINE,
        "job_id": job_id,
        "generation_id": generation_id,
        "context_sha256": context_sha,
        "recipe_sha256": recipe_sha,
        "state": EXTERNAL_ADMITTED_STATE,
        "created": created,
        "job_queued": False,
        "awaiting_human_review": False,
        "production_enabled": False,
    }


def inspect_external_admission(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
) -> dict[str, object]:
    """Confirm the real Job row/Recipe plus *current* Source before any use."""
    try:
        _require_sha256(job_id, label="job_id")
        job = job_status(ai_root, job_id)
        load_external_admission_recipe(ai_root, job["recipe_sha256"])
        context_sha = job["context_sha256"]
        if _external_job_id(context_sha, job["recipe_sha256"]) != job_id:
            raise ExternalJobError("external_job_binding_mismatch")
        _verified_private_context(ai_root, private_root, preview, context_sha)
        current = job["current_generation"]
        if current["state"] != EXTERNAL_ADMITTED_STATE:
            raise ExternalJobError("external_job_state_invalid")
        return {
            "job_id": job_id,
            "context_sha256": context_sha,
            "recipe_sha256": job["recipe_sha256"],
            "state": EXTERNAL_ADMITTED_STATE,
            "source_current": True,
            "job_queued": False,
            "awaiting_human_review": False,
        }
    except ExternalJobError:
        raise
    except (ArtifactLifecycleError, PreReviewJobError, OSError, KeyError, TypeError):
        raise ExternalJobError("external_admission_unreadable") from None
