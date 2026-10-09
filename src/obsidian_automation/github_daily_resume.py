"""Verified immutable per-context reuse for GitHub Daily model inference.

The key is determined *before* provider inference, never reconstructed from a
later model response. A missing pointer is a miss; an invalid pointer or one
of its bound CAS objects is an error, not a permission to try another output.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    sha256_bytes,
)

VERSION = 1
RESUME_DIR = "resume"
SUMMARY_DIR = "github-daily-summary"
MAX_POINTER_BYTES = 16 * 1024
# Normalized source identities can expand a <=256-KiB provider response.
# Keep a bounded, conservative maximum for the content-addressed form.
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_PROVENANCE_BYTES = 16 * 1024


def _request_bytes(request: Mapping[str, object]) -> bytes:
    required = {
        "stage",
        "input_context_sha256",
        "prompt_template_sha256",
        "implementation_revision",
        "model_provider",
        "model_identifier",
        "model_revision",
        "model_config",
    }
    if set(request) != required:
        raise ArtifactLifecycleError("resume key properties do not match contract")
    if request["stage"] not in {"partial", "ground"}:
        raise ArtifactLifecycleError("resume stage is invalid")
    for name in (
        "input_context_sha256",
        "prompt_template_sha256",
        "model_revision",
    ):
        _require_sha256(request[name], label=f"resume {name}")
    revision = request["implementation_revision"]
    if not isinstance(revision, str) or len(revision) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ArtifactLifecycleError("resume implementation revision is invalid")
    for name in ("model_provider", "model_identifier"):
        value = request[name]
        if not isinstance(value, str) or not value or len(value) > 512 or value != value.strip():
            raise ArtifactLifecycleError(f"resume {name} is invalid")
    if not isinstance(request["model_config"], dict):
        raise ArtifactLifecycleError("resume model_config must be a dictionary")
    return _canonical_json_bytes({"record_version": VERSION, "request": dict(request)})


def _root(state_root: Path, *, create: bool) -> Path | None:
    state_root = state_root.absolute()
    _require_safe_directory(state_root, create=False)
    summary = state_root / SUMMARY_DIR
    resume = summary / RESUME_DIR
    for directory in (summary, resume):
        if not create and not directory.exists():
            # Broken symlinks cannot bypass the safety check.
            if directory.is_symlink():
                raise ArtifactLifecycleError("resume directory is a symlink")
            return None
        _require_safe_directory(directory, create=create)
    return resume


def _read_bounded(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ArtifactLifecycleError("resume artifact cannot be safely opened") from exc
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                or st.st_size < 0 or st.st_size > max_bytes):
            raise ArtifactLifecycleError("resume artifact is not a bounded regular file")
        data = bytearray()
        while len(data) <= max_bytes:
            part = os.read(fd, min(65536, max_bytes + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        if len(data) != st.st_size or len(data) > max_bytes:
            raise ArtifactLifecycleError("resume artifact changed or exceeded size limit")
        return bytes(data)
    finally:
        os.close(fd)


def _cas_read(
    state_root: Path,
    *,
    child: str,
    name: str,
    expected_sha: str,
    max_bytes: int,
) -> bytes:
    _require_sha256(expected_sha, label="resume bound artifact")
    base = state_root.absolute()
    _require_safe_directory(base, create=False)
    summary = base / SUMMARY_DIR
    directory = summary / child
    _require_safe_directory(summary, create=False)
    _require_safe_directory(directory, create=False)
    payload = _read_bounded(directory / name, max_bytes=max_bytes)
    if sha256_bytes(payload) != expected_sha:
        raise ArtifactLifecycleError("resume bound artifact content SHA mismatch")
    return payload


def load_resume(
    state_root: Path, *,
    request: Mapping[str, object],
) -> tuple[str, bytes, str, bytes] | None:
    """Return (output SHA, output bytes, provenance SHA, provenance bytes)."""
    request_bytes = _request_bytes(request)
    key_sha = sha256_bytes(request_bytes)
    parent = _root(state_root, create=False)
    if parent is None:
        return None
    pointer = parent / f"{key_sha}.github-daily-resume.json"
    try:
        pointer.lstat()
    except FileNotFoundError:
        return None
    raw = _read_bounded(pointer, max_bytes=MAX_POINTER_BYTES)
    value = _decode_json_object(raw, label="Daily resume pointer")
    if set(value) != {"record_version", "request", "output_sha256", "provenance_sha256"}:
        raise ArtifactLifecycleError("Daily resume pointer has invalid fields")
    if (
        value["record_version"] != VERSION
        or _canonical_json_bytes({"record_version": VERSION, "request": value["request"]}) != request_bytes
        or _canonical_json_bytes(value) != raw
    ):
        raise ArtifactLifecycleError("Daily resume pointer binding mismatch")
    output_sha = _require_sha256(value["output_sha256"], label="resume output SHA")
    provenance_sha = _require_sha256(value["provenance_sha256"], label="resume provenance SHA")
    stage = request["stage"]
    suffix = "ground" if stage == "ground" else "partial"
    output = _cas_read(
        state_root,
        child="output",
        name=f"{output_sha}.github-daily-{suffix}-output.json",
        expected_sha=output_sha,
        max_bytes=MAX_OUTPUT_BYTES,
    )
    provenance = _cas_read(
        state_root,
        child="provenance",
        name=f"{provenance_sha}.github-daily-inference.json",
        expected_sha=provenance_sha,
        max_bytes=MAX_PROVENANCE_BYTES,
    )
    return output_sha, output, provenance_sha, provenance


def store_resume(
    state_root: Path, *,
    request: Mapping[str, object],
    output_sha256: str,
    provenance_sha256: str,
) -> None:
    request_bytes = _request_bytes(request)
    key_sha = sha256_bytes(request_bytes)
    output_sha = _require_sha256(output_sha256, label="resume output SHA")
    provenance_sha = _require_sha256(provenance_sha256, label="resume provenance SHA")
    # A caller cannot publish an index for a missing, corrupt or symlinked
    # artifact. Both immutable objects must precede pointer publication.
    output_suffix = "ground" if request["stage"] == "ground" else "partial"
    _cas_read(
        state_root, child="output",
        name=f"{output_sha}.github-daily-{output_suffix}-output.json",
        expected_sha=output_sha, max_bytes=MAX_OUTPUT_BYTES,
    )
    _cas_read(
        state_root, child="provenance",
        name=f"{provenance_sha}.github-daily-inference.json",
        expected_sha=provenance_sha, max_bytes=MAX_PROVENANCE_BYTES,
    )
    parent = _root(state_root, create=True)
    assert parent is not None
    payload = _canonical_json_bytes({
        "record_version": VERSION,
        "request": dict(request),
        "output_sha256": output_sha,
        "provenance_sha256": provenance_sha,
    })
    if len(payload) > MAX_POINTER_BYTES:
        raise ArtifactLifecycleError("Daily resume pointer exceeds byte budget")
    _store_immutable(parent / f"{key_sha}.github-daily-resume.json", payload)
