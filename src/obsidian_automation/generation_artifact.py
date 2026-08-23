from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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


UNTRUSTED_STAGE = "00-Untrusted"
MAX_GENERATION_RECORD_BYTES = 64 * 1024
MAX_MODEL_CONFIG_BYTES = 16 * 1024
MAX_MODEL_CONFIG_DEPTH = 4
MAX_MODEL_CONFIG_ITEMS = 128
MAX_METADATA_CHARS = 512
MAX_CONFIG_STRING_CHARS = 4096


@dataclass(frozen=True)
class GeneratorMetadata:
    implementation_revision: str
    prompt_template_version: str
    prompt_template_sha256: str


@dataclass(frozen=True)
class ModelMetadata:
    provider: str
    identifier: str
    revision: str


@dataclass(frozen=True)
class GenerationRecord:
    context_sha256: str
    proposal_sha256: str
    generator: GeneratorMetadata
    model: ModelMetadata
    model_config: Mapping[str, object]
    generated_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "context_sha256": self.context_sha256,
                "proposal_sha256": self.proposal_sha256,
                "generator": {
                    "implementation_revision": self.generator.implementation_revision,
                    "prompt_template_version": self.generator.prompt_template_version,
                    "prompt_template_sha256": self.generator.prompt_template_sha256,
                },
                "model": {
                    "provider": self.model.provider,
                    "identifier": self.model.identifier,
                    "revision": self.model.revision,
                },
                "model_config": dict(self.model_config),
                "generated_at": self.generated_at,
            }
        )


def _untrusted_directory(ai_root: Path) -> Path:
    root = ai_root.absolute()
    untrusted = root / UNTRUSTED_STAGE
    _require_safe_directory(root, create=False)
    _require_safe_directory(untrusted, create=False)
    return untrusted


def _metadata_string(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ArtifactLifecycleError(f"{label} must be a string")
    if not value or value != value.strip() or len(value) > MAX_METADATA_CHARS:
        raise ArtifactLifecycleError(
            f"{label} must be non-empty, trimmed, and at most {MAX_METADATA_CHARS} characters"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ArtifactLifecycleError(f"{label} must not contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ArtifactLifecycleError(f"{label} must be UTF-8 encodable") from exc
    return value


def _validate_model_config_value(value: object, *, depth: int) -> None:
    if depth > MAX_MODEL_CONFIG_DEPTH:
        raise ArtifactLifecycleError("model_config exceeds maximum nesting depth")
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ArtifactLifecycleError("model_config floats must be finite")
        return
    if isinstance(value, str):
        if len(value) > MAX_CONFIG_STRING_CHARS:
            raise ArtifactLifecycleError("model_config string is too long")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ArtifactLifecycleError("model_config strings must be UTF-8 encodable") from exc
        return
    if isinstance(value, list):
        if len(value) > MAX_MODEL_CONFIG_ITEMS:
            raise ArtifactLifecycleError("model_config list contains too many items")
        for item in value:
            _validate_model_config_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_MODEL_CONFIG_ITEMS:
            raise ArtifactLifecycleError("model_config object contains too many properties")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > MAX_METADATA_CHARS:
                raise ArtifactLifecycleError("model_config property names are invalid")
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
                raise ArtifactLifecycleError(
                    "model_config property names must not contain control characters"
                )
            _validate_model_config_value(item, depth=depth + 1)
        return
    raise ArtifactLifecycleError("model_config contains a non-JSON value")


def _validated_model_config(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ArtifactLifecycleError("model_config must be a JSON object")
    _validate_model_config_value(value, depth=0)
    canonical = _canonical_json_bytes(value)
    if len(canonical) > MAX_MODEL_CONFIG_BYTES:
        raise ArtifactLifecycleError(
            f"model_config exceeds {MAX_MODEL_CONFIG_BYTES} canonical bytes"
        )
    return dict(value)


def validate_model_config(value: Mapping[str, object]) -> dict[str, object]:
    """Validate provider configuration before inference uses it."""

    return _validated_model_config(dict(value))


def parse_generation_record(data: bytes) -> GenerationRecord:
    if len(data) > MAX_GENERATION_RECORD_BYTES:
        raise ArtifactLifecycleError(
            f"generation record exceeds {MAX_GENERATION_RECORD_BYTES} bytes"
        )
    value = _decode_json_object(data, label="generation record")
    required = {
        "record_version",
        "context_sha256",
        "proposal_sha256",
        "generator",
        "model",
        "model_config",
        "generated_at",
    }
    if set(value) != required:
        raise ArtifactLifecycleError("generation record properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ArtifactLifecycleError("generation record_version must be integer 1")

    context_sha256 = _require_sha256(value["context_sha256"], label="context_sha256")
    proposal_sha256 = _require_sha256(value["proposal_sha256"], label="proposal_sha256")

    raw_generator = value["generator"]
    if not isinstance(raw_generator, dict) or set(raw_generator) != {
        "implementation_revision",
        "prompt_template_version",
        "prompt_template_sha256",
    }:
        raise ArtifactLifecycleError("generation generator properties do not match contract")
    generator = GeneratorMetadata(
        implementation_revision=_metadata_string(
            raw_generator["implementation_revision"],
            label="generator.implementation_revision",
        ),
        prompt_template_version=_metadata_string(
            raw_generator["prompt_template_version"],
            label="generator.prompt_template_version",
        ),
        prompt_template_sha256=_require_sha256(
            raw_generator["prompt_template_sha256"],
            label="generator.prompt_template_sha256",
        ),
    )

    raw_model = value["model"]
    if not isinstance(raw_model, dict) or set(raw_model) != {
        "provider",
        "identifier",
        "revision",
    }:
        raise ArtifactLifecycleError("generation model properties do not match contract")
    model = ModelMetadata(
        provider=_metadata_string(raw_model["provider"], label="model.provider"),
        identifier=_metadata_string(raw_model["identifier"], label="model.identifier"),
        revision=_metadata_string(raw_model["revision"], label="model.revision"),
    )

    model_config = _validated_model_config(value["model_config"])
    generated_at = value["generated_at"]
    if not isinstance(generated_at, str) or not generated_at.endswith("Z"):
        raise ArtifactLifecycleError("generated_at must be a UTC timestamp ending in Z")

    return GenerationRecord(
        context_sha256=context_sha256,
        proposal_sha256=proposal_sha256,
        generator=generator,
        model=model,
        model_config=model_config,
        generated_at=generated_at,
    )


def _verify_proposal_binding(ai_root: Path, proposal_sha256: str) -> None:
    digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    path = _untrusted_directory(ai_root) / f"{digest}.proposal.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise ArtifactLifecycleError("untrusted proposal artifact hash mismatch")


def build_generation_record(
    ai_root: Path,
    *,
    context_sha256: str,
    proposal_sha256: str,
    implementation_revision: str,
    prompt_template_version: str,
    prompt_template_sha256: str,
    model_provider: str,
    model_identifier: str,
    model_revision: str,
    model_config: Mapping[str, object],
    generated_at: str | None = None,
) -> GenerationRecord:
    context_digest = _require_sha256(context_sha256, label="context_sha256")
    proposal_digest = _require_sha256(proposal_sha256, label="proposal_sha256")

    load_context_bundle(ai_root, context_digest)
    _verify_proposal_binding(ai_root, proposal_digest)

    record = GenerationRecord(
        context_sha256=context_digest,
        proposal_sha256=proposal_digest,
        generator=GeneratorMetadata(
            implementation_revision=implementation_revision,
            prompt_template_version=prompt_template_version,
            prompt_template_sha256=prompt_template_sha256,
        ),
        model=ModelMetadata(
            provider=model_provider,
            identifier=model_identifier,
            revision=model_revision,
        ),
        model_config=_validated_model_config(dict(model_config)),
        generated_at=generated_at or _utc_now(),
    )
    return parse_generation_record(record.to_json_bytes())


def store_generation_record(ai_root: Path, record: GenerationRecord) -> tuple[str, Path]:
    normalized = GenerationRecord(
        context_sha256=record.context_sha256,
        proposal_sha256=record.proposal_sha256,
        generator=record.generator,
        model=record.model,
        model_config=_validated_model_config(dict(record.model_config)),
        generated_at=record.generated_at,
    )
    data = normalized.to_json_bytes()
    parsed = parse_generation_record(data)
    if parsed != normalized:
        raise ArtifactLifecycleError("generation record canonical round-trip mismatch")

    load_context_bundle(ai_root, parsed.context_sha256)
    _verify_proposal_binding(ai_root, parsed.proposal_sha256)

    digest = sha256_bytes(data)
    path = _untrusted_directory(ai_root) / f"{digest}.generation.json"
    return digest, _store_immutable(path, data)


def load_generation_record(ai_root: Path, generation_sha256: str) -> GenerationRecord:
    digest = _require_sha256(generation_sha256, label="generation_sha256")
    path = _untrusted_directory(ai_root) / f"{digest}.generation.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise ArtifactLifecycleError("generation record artifact hash mismatch")
    return parse_generation_record(data)
