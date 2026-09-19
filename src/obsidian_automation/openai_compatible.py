from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object


PROVIDER_NAME = "openai-compatible"
IDENTITY_BINDING = "identifier-only"
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 600.0
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OPTIONS_BYTES = 12 * 1024
MAX_OUTPUT_SCHEMA_BYTES = 64 * 1024
DEFAULT_OPTIONS: Mapping[str, object] = {"temperature": 0}
_RESERVED_OPTIONS = {"model", "messages", "stream", "response_format"}
_IMPLEMENTATION_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class OpenAICompatibleProviderError(RuntimeError):
    """Raised when an OpenAI-compatible provider violates the bounded contract."""


@dataclass(frozen=True)
class OpenAICompatibleModelIdentity:
    requested_identifier: str
    identifier: str
    revision: str
    binding_mode: str = IDENTITY_BINDING


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


def validated_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible base URL must be a non-empty trimmed string"
        )
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible base URL scheme must be http or https"
        )
    if parsed.hostname is None:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible base URL must contain a hostname"
        )
    if parsed.username is not None or parsed.password is not None:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible base URL must not embed credentials"
        )
    if parsed.query or parsed.fragment:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible base URL must not contain query or fragment"
        )
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible base URL path must be empty or /v1"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible base URL contains an invalid port"
        ) from exc

    host = parsed.hostname
    if parsed.scheme == "http" and not _is_loopback_host(host):
        raise OpenAICompatibleProviderError(
            "remote OpenAI-compatible endpoints require HTTPS; HTTP is loopback-only"
        )

    authority_host = f"[{host}]" if ":" in host else host
    authority = authority_host if port is None else f"{authority_host}:{port}"
    return f"{parsed.scheme}://{authority}/v1"


def validated_implementation_revision(value: str) -> str:
    if not isinstance(value, str) or _IMPLEMENTATION_REVISION_RE.fullmatch(value) is None:
        raise ArtifactLifecycleError(
            "implementation revision must be a lowercase 40..64 character hexadecimal commit digest"
        )
    return value


def validated_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise OpenAICompatibleProviderError("timeout must be numeric")
    value = float(timeout)
    if not 0.0 < value <= MAX_TIMEOUT_SECONDS:
        raise OpenAICompatibleProviderError(
            f"timeout must be greater than 0 and at most {MAX_TIMEOUT_SECONDS} seconds"
        )
    return value


def validated_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise OpenAICompatibleProviderError(
            f"{label} must be a non-empty trimmed string up to 512 characters"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise OpenAICompatibleProviderError(f"{label} must not contain control characters")
    return value


def identifier_revision(identifier: str) -> str:
    return f"identifier:{validated_identifier(identifier, label='model identifier')}"


def identity_for_requested_model(model: str) -> OpenAICompatibleModelIdentity:
    identifier = validated_identifier(model, label="model identifier")
    return OpenAICompatibleModelIdentity(
        requested_identifier=identifier,
        identifier=identifier,
        revision=identifier_revision(identifier),
    )


def validated_options(options: Mapping[str, object] | None) -> dict[str, object]:
    value = dict(DEFAULT_OPTIONS if options is None else options)
    if _RESERVED_OPTIONS.intersection(value):
        raise ArtifactLifecycleError(
            "OpenAI-compatible options must not override model/messages/stream/response_format"
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ArtifactLifecycleError(
            "OpenAI-compatible options must be strict JSON values"
        ) from exc
    if len(encoded) > MAX_OPTIONS_BYTES:
        raise ArtifactLifecycleError(
            f"OpenAI-compatible options exceed {MAX_OPTIONS_BYTES} canonical bytes"
        )
    return value


def structured_response_format(
    *,
    schema_name: str,
    output_schema: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(schema_name, str) or _SCHEMA_NAME_RE.fullmatch(schema_name) is None:
        raise ArtifactLifecycleError(
            "structured output schema name must match [A-Za-z0-9_-]{1,64}"
        )
    if not isinstance(output_schema, dict):
        raise ArtifactLifecycleError("structured output schema must be a JSON object")
    try:
        encoded = json.dumps(
            output_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ArtifactLifecycleError(
            "structured output schema must contain strict JSON values"
        ) from exc
    if len(encoded) > MAX_OUTPUT_SCHEMA_BYTES:
        raise ArtifactLifecycleError(
            f"structured output schema exceeds {MAX_OUTPUT_SCHEMA_BYTES} canonical bytes"
        )
    schema = json.loads(encoded.decode("utf-8"))
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_name,
            "strict": True,
            "schema": schema,
        },
    }


def _direct_opener():
    # Context can contain private Knowledge. Never inherit process proxy settings
    # and never follow redirects to another recipient.
    return build_opener(ProxyHandler({}), _NoRedirect())


def request_json(
    base_url: str,
    *,
    method: str,
    path: str,
    payload: Mapping[str, object] | None,
    timeout: float,
    api_key: str | None = None,
) -> dict[str, object]:
    root = validated_base_url(base_url)
    timeout_value = validated_timeout(timeout)
    if method != "POST" or path != "/chat/completions":
        raise OpenAICompatibleProviderError(
            "unsupported OpenAI-compatible API operation"
        )

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
            raise OpenAICompatibleProviderError(
                "OpenAI-compatible request payload is not strict JSON"
            ) from exc
        headers["Content-Type"] = "application/json"

    if api_key is not None:
        if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
            raise OpenAICompatibleProviderError("OpenAI API key is invalid")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in api_key):
            raise OpenAICompatibleProviderError("OpenAI API key contains control characters")
        headers["Authorization"] = f"Bearer {api_key}"

    request = Request(root + path, data=data, headers=headers, method=method)
    try:
        response = _direct_opener().open(request, timeout=timeout_value)
    except HTTPError as exc:
        raise OpenAICompatibleProviderError(
            f"OpenAI-compatible HTTP request failed with status {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible HTTP request failed"
        ) from exc

    with response:
        raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    if len(raw) > MAX_HTTP_RESPONSE_BYTES:
        raise OpenAICompatibleProviderError(
            f"OpenAI-compatible response exceeds {MAX_HTTP_RESPONSE_BYTES} bytes"
        )
    try:
        return _decode_json_object(raw, label="OpenAI-compatible response")
    except ArtifactLifecycleError as exc:
        raise OpenAICompatibleProviderError(str(exc)) from exc


def chat_content(
    base_url: str,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    output_schema: Mapping[str, object],
    schema_name: str,
    options: Mapping[str, object],
    timeout: float,
    api_key: str | None = None,
    transport: JSONTransport | None = None,
) -> tuple[OpenAICompatibleModelIdentity, bytes]:
    identity = identity_for_requested_model(model)
    response_format = structured_response_format(
        schema_name=schema_name,
        output_schema=output_schema,
    )
    request = transport or request_json
    response = request(
        base_url,
        method="POST",
        path="/chat/completions",
        payload={
            "model": identity.identifier,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "response_format": response_format,
            **dict(options),
        },
        timeout=timeout,
        api_key=api_key,
    )

    returned_model = validated_identifier(
        response.get("model"),
        label="response model identifier",
    )
    if returned_model != identity.identifier:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible response model does not match requested model"
        )

    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible response must contain exactly one choice"
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible response choice is invalid"
        )
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible response message is invalid"
        )
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible response content is empty or invalid"
        )
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OpenAICompatibleProviderError(
            "OpenAI-compatible response content is not UTF-8 encodable"
        ) from exc
    return identity, data
