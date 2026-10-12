"""Planner-owned immutable per-generation Selection assignment for cleanup proof.

This file is emitted at job submission, BEFORE Input projection publication,
under the existing Reader-owned request directory. Its existence is not an
authorization to delete any remote file. Older jobs without this binding are
never silently upgraded into cleanup-eligible cases.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .artifact_lifecycle import (
    ArtifactLifecycleError, _canonical_json_bytes, _decode_json_object,
    _require_safe_directory, _require_sha256, _store_immutable,
)
from .human_projection import REQUEST_STAGE, projection_enabled

BINDING_VERSION = 1
SUFFIX = ".submitted-selection-binding.json"


@dataclass(frozen=True)
class SubmittedSelectionBinding:
    case_id: str
    job_id: str
    context_sha256: str
    selection_sha256: str
    recipe_sha256: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes({
            "record_version": BINDING_VERSION,
            "kind": "submitted-selection-binding",
            "case_id": self.case_id,
            "job_id": self.job_id,
            "context_sha256": self.context_sha256,
            "selection_sha256": self.selection_sha256,
            "recipe_sha256": self.recipe_sha256,
        })


def parse_binding(data: bytes) -> SubmittedSelectionBinding:
    v = _decode_json_object(data, label="submitted Selection binding")
    if (
        set(v) != {
            "record_version", "kind", "case_id", "job_id",
            "context_sha256", "selection_sha256", "recipe_sha256",
        }
        or type(v["record_version"]) is not int
        or v["record_version"] != BINDING_VERSION
        or v["kind"] != "submitted-selection-binding"
    ):
        raise ArtifactLifecycleError("submitted Selection binding schema mismatch")
    entry = SubmittedSelectionBinding(
        case_id=_require_sha256(v["case_id"], label="case_id"),
        job_id=_require_sha256(v["job_id"], label="job_id"),
        context_sha256=_require_sha256(v["context_sha256"], label="context_sha256"),
        selection_sha256=_require_sha256(v["selection_sha256"], label="selection_sha256"),
        recipe_sha256=_require_sha256(v["recipe_sha256"], label="recipe_sha256"),
    )
    if data != entry.to_json_bytes():
        raise ArtifactLifecycleError("submitted Selection binding is not canonical")
    return entry


def emit_binding(
    ai_root: Path, *, case_id: str, job_id: str,
    context_sha256: str, selection_sha256: str, recipe_sha256: str,
) -> Path | None:
    """Reader-owned optional emission; no added Sync/Validator/Vault authority."""
    if not projection_enabled(ai_root):
        return None
    normalized = parse_binding(SubmittedSelectionBinding(
        case_id=case_id,
        job_id=job_id,
        context_sha256=context_sha256,
        selection_sha256=selection_sha256,
        recipe_sha256=recipe_sha256,
    ).to_json_bytes())
    directory = ai_root.absolute() / REQUEST_STAGE / "reader"
    _require_safe_directory(directory, create=False)
    return _store_immutable(
        directory / f"{normalized.case_id}{SUFFIX}",
        normalized.to_json_bytes(),
    )


def load_binding(
    ai_root: Path, case_id: str, *,
    read: Callable[..., bytes],
) -> SubmittedSelectionBinding | None:
    case = _require_sha256(case_id, label="case_id")
    directory = ai_root.absolute() / REQUEST_STAGE / "reader"
    _require_safe_directory(directory, create=False)
    path = directory / f"{case}{SUFFIX}"
    if not path.exists():
        return None
    binding = parse_binding(read(path, cap=16 * 1024))
    if binding.case_id != case:
        raise ArtifactLifecycleError("submitted Selection binding filename mismatch")
    return binding
