"""Reader-owned external Source Context CAS, with no Job-admission authority.

The only writer entrypoint re-reads private Source/Journals and validates a
bounded, non-authoritative ExternalSourceContext before publication under a
distinct extension in the preexisting Reader-owned 05-Context directory.
This module never submits Jobs, invokes providers or grants Human approval.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _require_sha256,
    _store_immutable,
    sha256_bytes,
)
from .chatgpt_export_review_preview import ExternalReviewPreview
from .chatgpt_external_context import (
    EXTERNAL_CONTEXT_KIND,
    ExternalContextError,
    ExternalSourceContext,
    parse_external_context,
    revalidate_external_context,
)
from .context_bundle import _context_directory

EXTERNAL_CONTEXT_SUFFIX = "chatgpt-external-context"
MAX_CONTEXT_BYTES = 64 * 1024


class ExternalContextStoreError(ValueError):
    """Fixed diagnostics, no source text, filesystem path or input echo."""


def _context_path(ai_root: Path, digest: str) -> Path:
    try:
        sha = _require_sha256(digest, label="external_context_sha256")
        return _context_directory(ai_root) / f"{sha}.{EXTERNAL_CONTEXT_SUFFIX}.json"
    except (ArtifactLifecycleError, OSError):
        raise ExternalContextStoreError(
            "external_context_storage_unavailable"
        ) from None


def _validate_external_input(context: ExternalSourceContext) -> bytes:
    if type(context) is not ExternalSourceContext:
        raise ExternalContextStoreError("invalid_external_context")
    if context.context_kind != EXTERNAL_CONTEXT_KIND:
        raise ExternalContextStoreError("invalid_external_context_kind")
    if not context.evidence:
        # No-candidate is a legitimate reader outcome but no basis for an
        # eventual LLM/Knowledge Job. Do not publish empty Generation context.
        raise ExternalContextStoreError("external_context_no_candidate")
    try:
        data = context.to_json_bytes()
        parsed = parse_external_context(data)
    except (ExternalContextError, OSError):
        raise ExternalContextStoreError("invalid_external_context") from None
    if parsed != context:
        raise ExternalContextStoreError("external_context_noncanonical")
    if len(data) > MAX_CONTEXT_BYTES:
        raise ExternalContextStoreError("external_context_too_large")
    return data


def store_external_context_for_review(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    context: ExternalSourceContext,
) -> tuple[str, Path]:
    """Publish a strictly revalidated, private Reader-owned Context.

    The private Journal verification is performed before CAS publication.
    This is NOT an atomic Source→Job admission: the Source may change
    between the check and future use. A future dedicated Job adapter MUST
    revalidate source freshness immediately before durable admission.
    """
    data = _validate_external_input(context)
    try:
        revalidate_external_context(private_root, preview, context)
    except (ExternalContextError, OSError):
        raise ExternalContextStoreError("external_source_stale_or_forged") from None
    digest = sha256_bytes(data)
    path = _context_path(ai_root, digest)
    try:
        published = _store_immutable(path, data)
    except (ArtifactLifecycleError, OSError):
        raise ExternalContextStoreError("external_context_publication_failed") from None
    return digest, published


def load_external_context_for_review(
    ai_root: Path,
    external_context_sha256: str,
) -> ExternalSourceContext:
    """Read only the distinct external CAS, never the Vault ContextBundle."""
    path = _context_path(ai_root, external_context_sha256)
    try:
        # Check the opened inode rather than only path.lstat(). Read no more
        # than MAX+1 bytes even if an actor replaces/grows the file while
        # this process is reading it. This is a CAS validation boundary.
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
                raise ExternalContextStoreError(
                    "external_context_file_invalid"
                ) from None
            raise
        try:
            before = os.fstat(fd)
            disk = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (before.st_dev, before.st_ino) != (disk.st_dev, disk.st_ino)
                or before.st_size < 1
                or before.st_size > MAX_CONTEXT_BYTES
            ):
                raise ExternalContextStoreError("external_context_file_invalid")
            chunks = []
            count = 0
            while True:
                part = os.read(fd, min(65536, MAX_CONTEXT_BYTES + 1 - count))
                if not part:
                    break
                chunks.append(part)
                count += len(part)
                if count > MAX_CONTEXT_BYTES:
                    raise ExternalContextStoreError("external_context_file_invalid")
            after = os.fstat(fd)
            if count != before.st_size or (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ExternalContextStoreError("external_context_changed_during_read")
            data = b"".join(chunks)
        finally:
            os.close(fd)
        if sha256_bytes(data) != external_context_sha256:
            raise ExternalContextStoreError("external_context_digest_mismatch")
        parsed = parse_external_context(data)
        if not parsed.evidence:
            raise ExternalContextStoreError("external_context_no_candidate")
        return parsed
    except ExternalContextStoreError:
        raise
    except (ArtifactLifecycleError, ExternalContextError, OSError):
        raise ExternalContextStoreError("external_context_unreadable") from None


def revalidate_saved_external_context(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    external_context_sha256: str,
) -> ExternalSourceContext:
    """Re-open private Source AND immutable external Context; no mutation."""
    context = load_external_context_for_review(ai_root, external_context_sha256)
    try:
        revalidate_external_context(private_root, preview, context)
    except (ExternalContextError, OSError):
        raise ExternalContextStoreError("external_source_stale_or_forged") from None
    return context
