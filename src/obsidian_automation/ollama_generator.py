from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _require_sha256,
)
from .generation_artifact import (
    MAX_MODEL_CONFIG_BYTES,
    build_generation_record,
    store_generation_record,
)
from .generator_contract import (
    MAX_GENERATOR_OUTPUT_BYTES,
    KnowledgeGeneratorOutput,
    load_and_render_generator_prompt,
    parse_generator_output,
    store_generator_proposal,
)


PROVIDER_NAME = "ollama"
ADAPTER_VERSION = "ollama-chat-structured-v0"
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 600.0
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OPTIONS_BYTES = 12 * 1024
DEFAULT_OPTIONS: Mapping[str, object] = {"temperature": 0}
_IMPLEMENTATION_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")


class OllamaProviderError(RuntimeError):
    """Raised when the Ollama provider cannot satisfy the generator contract."""


@dataclass(frozen=True)
class OllamaModelIdentity:
    requested_identifier: str
    identifier: str
    digest: str


@dataclass(frozen=True)
class OllamaGenerationResult:
    context_sha256: str
    proposal_sha256: str
    proposal_path: Path
    generation_sha256: str
    generation_path: Path
    model_identifier: str
    model_revision: str
    prompt_template_version: str
    prompt_template_sha256: str


JSONTransport = Callable[..., dict[str, object]]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OllamaProviderError("Ollama base URL must be a non-empty trimmed string")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise OllamaProviderError("Ollama base URL scheme must be http or https")
    if parsed.hostname is None:
        raise OllamaProviderError("Ollama base URL must contain a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise OllamaProviderError("Ollama base URL must not embed credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise OllamaProviderError("Ollama base URL must not contain path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise OllamaProviderError("Ollama base URL contains an invalid port") from exc

    host = parsed.hostname
    if parsed.scheme == "http" and not _is_loopback_host(host):
        raise OllamaProviderError("remote Ollama endpoints require HTTPS; HTTP is loopback-only")

    authority_host = f"[{host}]" if ":" in host else host
    authority = authority_host if port is None else f"{authority_host}:{port}"
    return f"{parsed.scheme}://{authority}"


def _validated_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise OllamaProviderError("timeout must be numeric")
    value = float(timeout)
    if not 0.0 < value <= MAX_TIMEOUT_SECONDS:
        raise OllamaProviderError(
            f"timeout must be greater than 0 and at most {MAX_TIMEOUT_SECONDS} seconds"
        )
    return value


def _direct_opener():
    # Context may contain private Knowledge. Do not inherit environment proxy
    # settings and do not follow redirects to a different data recipient.
    return build_opener(ProxyHandler({}), _NoRedirect())


def _request_json(
    base_url: str,
    *,
    method: str,
    path: str,
    payload: Mapping[str, object] | None,
    timeout: float,
) -> dict[str, object]:
    root = _validated_base_url(base_url)
    timeout_value = _validated_timeout(timeout)
    if path not in {"/api/tags", "/api/chat"}:
        raise OllamaProviderError("unsupported Ollama API path")

    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        try:
            data = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise OllamaProviderError("Ollama request payload is not strict JSON") from exc
        headers["Content-Type"] = "application/json"

    request = Request(root + path, data=data, headers=headers, method=method)
    try:
        response = _direct_opener().open(request, timeout=timeout_value)
    except HTTPError as exc:
        raise OllamaProviderError(f"Ollama HTTP request failed with status {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OllamaProviderError("Ollama HTTP request failed") from exc

    with response:
        raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    if len(raw) > MAX_HTTP_RESPONSE_BYTES:
        raise OllamaProviderError(
            f"Ollama response exceeds {MAX_HTTP_RESPONSE_BYTES} bytes"
        )
    try:
        return _decode_json_object(raw, label="Ollama response")
    except ArtifactLifecycleError as exc:
        raise OllamaProviderError(str(exc)) from exc


def _validated_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise OllamaProviderError(f"{label} must be a non-empty trimmed string up to 512 characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise OllamaProviderError(f"{label} must not contain control characters")
    return value


def resolve_ollama_model(
    base_url: str,
    model: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: JSONTransport | None = None,
) -> OllamaModelIdentity:
    requested = _validated_identifier(model, label="model identifier")
    request_json = transport or _request_json
    response = request_json(
        base_url,
        method="GET",
        path="/api/tags",
        payload=None,
        timeout=timeout,
    )
    models = response.get("models")
    if not isinstance(models, list) or len(models) > 4096:
        raise OllamaProviderError("Ollama model list is invalid")

    parsed: list[tuple[str, str, str]] = []
    for raw in models:
        if not isinstance(raw, dict):
            raise OllamaProviderError("Ollama model list contains an invalid entry")
        name = raw.get("name")
        identifier = raw.get("model")
        digest = raw.get("digest")
        if not isinstance(name, str) or not isinstance(identifier, str) or not isinstance(digest, str):
            raise OllamaProviderError("Ollama model entry is missing name/model/digest")
        try:
            normalized_digest = _require_sha256(digest, label="Ollama model digest")
        except ArtifactLifecycleError as exc:
            raise OllamaProviderError(str(exc)) from exc
        parsed.append((name, identifier, normalized_digest))

    exact = [entry for entry in parsed if requested in entry[:2]]
    if exact:
        matches = exact
    elif ":" not in requested:
        alias = f"{requested}:latest"
        matches = [entry for entry in parsed if alias in entry[:2]]
    else:
        matches = []

    if not matches:
        raise OllamaProviderError(f"Ollama model is not installed: {requested}")
    unique = {(name, identifier, digest) for name, identifier, digest in matches}
    if len(unique) != 1:
        raise OllamaProviderError(f"Ollama model identifier is ambiguous: {requested}")

    name, identifier, digest = next(iter(unique))
    canonical = identifier or name
    return OllamaModelIdentity(
        requested_identifier=requested,
        identifier=canonical,
        digest=digest,
    )


def _validated_implementation_revision(value: str) -> str:
    if not isinstance(value, str) or _IMPLEMENTATION_REVISION_RE.fullmatch(value) is None:
        raise ArtifactLifecycleError(
            "implementation revision must be a lowercase 40..64 character hexadecimal commit digest"
        )
    return value


def _validated_options(options: Mapping[str, object] | None) -> dict[str, object]:
    value = dict(DEFAULT_OPTIONS if options is None else options)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ArtifactLifecycleError("Ollama options must be strict JSON values") from exc
    if len(encoded) > MAX_OPTIONS_BYTES:
        raise ArtifactLifecycleError(f"Ollama options exceed {MAX_OPTIONS_BYTES} canonical bytes")

    model_config = {
        "adapter_version": ADAPTER_VERSION,
        "think": False,
        "options": value,
    }
    if len(_canonical_json_bytes(model_config)) > MAX_MODEL_CONFIG_BYTES:
        raise ArtifactLifecycleError("Ollama generation model_config exceeds provenance limit")
    return value


def _chat_semantic_output(
    base_url: str,
    *,
    identity: OllamaModelIdentity,
    system_prompt: str,
    user_prompt: str,
    output_schema: Mapping[str, object],
    options: Mapping[str, object],
    timeout: float,
    transport: JSONTransport | None,
) -> KnowledgeGeneratorOutput:
    request_json = transport or _request_json
    response = request_json(
        base_url,
        method="POST",
        path="/api/chat",
        payload={
            "model": identity.identifier,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "think": False,
            "format": dict(output_schema),
            "options": dict(options),
        },
        timeout=timeout,
    )
    if response.get("done") is not True:
        raise OllamaProviderError("Ollama chat response is not complete")
    response_model = response.get("model")
    if not isinstance(response_model, str) or response_model != identity.identifier:
        raise OllamaProviderError("Ollama chat response model does not match resolved model")
    message = response.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise OllamaProviderError("Ollama chat response message is invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise OllamaProviderError("Ollama chat response content is empty or invalid")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OllamaProviderError("Ollama chat content is not UTF-8 encodable") from exc
    if len(data) > MAX_GENERATOR_OUTPUT_BYTES:
        raise OllamaProviderError("Ollama semantic output exceeds generator output limit")
    return parse_generator_output(data)


def generate_knowledge_note_with_ollama(
    ai_root: Path,
    *,
    context_sha256: str,
    base_url: str,
    model: str,
    implementation_revision: str,
    options: Mapping[str, object] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: JSONTransport | None = None,
) -> OllamaGenerationResult:
    context_digest = _require_sha256(context_sha256, label="context_sha256")
    revision = _validated_implementation_revision(implementation_revision)
    timeout_value = _validated_timeout(timeout)
    root = _validated_base_url(base_url)
    inference_options = _validated_options(options)

    prompt = load_and_render_generator_prompt(ai_root, context_digest)
    identity = resolve_ollama_model(
        root,
        model,
        timeout=timeout_value,
        transport=transport,
    )
    output = _chat_semantic_output(
        root,
        identity=identity,
        system_prompt=prompt.system,
        user_prompt=prompt.user,
        output_schema=prompt.output_schema,
        options=inference_options,
        timeout=timeout_value,
        transport=transport,
    )
    proposal_sha, proposal_path = store_generator_proposal(
        ai_root,
        context_sha256=context_digest,
        output=output,
    )

    model_config = {
        "adapter_version": ADAPTER_VERSION,
        "think": False,
        "options": inference_options,
    }
    record = build_generation_record(
        ai_root,
        context_sha256=context_digest,
        proposal_sha256=proposal_sha,
        implementation_revision=revision,
        prompt_template_version=prompt.template_version,
        prompt_template_sha256=prompt.template_sha256,
        model_provider=PROVIDER_NAME,
        model_identifier=identity.identifier,
        model_revision=identity.digest,
        model_config=model_config,
    )
    generation_sha, generation_path = store_generation_record(ai_root, record)

    return OllamaGenerationResult(
        context_sha256=context_digest,
        proposal_sha256=proposal_sha,
        proposal_path=proposal_path,
        generation_sha256=generation_sha,
        generation_path=generation_path,
        model_identifier=identity.identifier,
        model_revision=identity.digest,
        prompt_template_version=prompt.template_version,
        prompt_template_sha256=prompt.template_sha256,
    )


def _load_options_file(path: Path | None) -> Mapping[str, object] | None:
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ArtifactLifecycleError(f"cannot read Ollama options file: {path}") from exc
    if len(data) > MAX_OPTIONS_BYTES:
        raise ArtifactLifecycleError(f"Ollama options file exceeds {MAX_OPTIONS_BYTES} bytes")
    return _decode_json_object(data, label="Ollama options file")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-generate")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--context-sha256", required=True)
    parser.add_argument("--ollama-base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument("--options-file", type=Path)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    try:
        result = generate_knowledge_note_with_ollama(
            args.ai_root,
            context_sha256=args.context_sha256,
            base_url=args.ollama_base_url,
            model=args.model,
            implementation_revision=args.implementation_revision,
            options=_load_options_file(args.options_file),
            timeout=args.timeout,
        )
    except (ArtifactLifecycleError, OllamaProviderError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "context_sha256": result.context_sha256,
                "proposal_sha256": result.proposal_sha256,
                "proposal_path": str(result.proposal_path),
                "generation_sha256": result.generation_sha256,
                "generation_path": str(result.generation_path),
                "model_identifier": result.model_identifier,
                "model_revision": result.model_revision,
                "prompt_template_version": result.prompt_template_version,
                "prompt_template_sha256": result.prompt_template_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
