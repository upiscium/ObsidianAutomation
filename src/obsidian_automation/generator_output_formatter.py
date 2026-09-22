from __future__ import annotations

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
)
from .generator_contract import MAX_GENERATOR_OUTPUT_BYTES


def format_generator_output(data: bytes) -> bytes:
    """Canonicalize meaning-preserving provider representation differences.

    This formatter deliberately does not repair semantic mistakes. It only
    canonicalizes representation differences whose canonical form is
    unambiguous.
    """
    if len(data) > MAX_GENERATOR_OUTPUT_BYTES:
        raise ArtifactLifecycleError(
            f"generator output exceeds {MAX_GENERATOR_OUTPUT_BYTES} bytes"
        )

    value = _decode_json_object(data, label="generator output")
    body = value.get("body")

    # CRLF and LF carry the same Markdown line-break semantics for this
    # contract. Canonicalize only CRLF. A lone CR is deliberately preserved so
    # the strict parser/validator rejects it instead of silently repairing it.
    if isinstance(body, str):
        value["body"] = body.replace("\r\n", "\n")

    try:
        return _canonical_json_bytes(value)
    except UnicodeEncodeError as exc:
        raise ArtifactLifecycleError("generator output must be UTF-8 encodable") from exc
