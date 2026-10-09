from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping

from .artifact_lifecycle import ArtifactLifecycleError, _decode_json_object
from .github_daily_summary import (
    GROUND_STAGE,
    MAX_OUTPUT_BYTES,
    PARTIAL_STAGE,
    REDUCE_STAGE,
    BoundInfer,
    GitHubDailySummaryError,
    InferenceResponse,
    PreboundInferenceIdentity,
    PromptSpec,
    SummaryContext,
    run_pipeline,
)
from .openai_compatible import (
    DEFAULT_TIMEOUT_SECONDS as OPENAI_DEFAULT_TIMEOUT_SECONDS,
    JSONTransport as OpenAIJSONTransport,
    OpenAICompatibleProviderError,
    PROVIDER_NAME as OPENAI_PROVIDER_NAME,
    chat_content,
    identifier_revision,
    validated_base_url,
    validated_implementation_revision,
    validated_options,
    validated_timeout,
)
from .openai_generator import provider_model_config as openai_model_config
from .ollama_generator import (
    DEFAULT_TIMEOUT_SECONDS as OLLAMA_DEFAULT_TIMEOUT_SECONDS,
    JSONTransport as OllamaJSONTransport,
    OllamaProviderError,
    PROVIDER_NAME as OLLAMA_PROVIDER_NAME,
    _provider_model_config as ollama_model_config,
    _request_json as ollama_request_json,
    _validated_base_url as validated_ollama_base_url,
    _validated_implementation_revision as validated_ollama_revision,
    _validated_options as validated_ollama_options,
    _validated_timeout as validated_ollama_timeout,
    resolve_ollama_model,
)


_SCHEMA_NAMES = {
    PARTIAL_STAGE: "github_daily_partial",
    REDUCE_STAGE: "github_daily_reducer",
    GROUND_STAGE: "github_daily_grounding",
}


def _context_user_prompt(context: SummaryContext) -> str:
    """Label model-visible sources without changing immutable contexts."""
    payload = json.loads(context.to_json_bytes())
    if context.stage == PARTIAL_STAGE:
        if not payload["events"]:
            raise GitHubDailySummaryError(
                "Partial model context requires at least one event"
            )
        for index, event in enumerate(payload["events"]):
            event["source_ref"] = index
        # Explicitly restate the valid range in addition to the bounded
        # structured-output enum. Do not change the immutable source context.
        payload["valid_source_ref_range"] = {
            "minimum": 0,
            "maximum": len(payload["events"]) - 1,
        }
    elif context.stage == REDUCE_STAGE:
        for index, claim in enumerate(payload["claims"]):
            claim["source_ref"] = index
    elif context.stage == GROUND_STAGE:
        if len(payload["claims"]) != 1:
            raise GitHubDailySummaryError(
                "Grounding model requires exactly one input claim"
            )
        claim = payload["claims"][0]
        # Model-facing semantics only. Immutable SummaryContext and its SHA
        # still hold exact claim/evidence identifiers for normalized output.
        payload = {
            "claim": {
                "kind": claim["kind"],
                "repository": claim["repository"],
                "summary": claim["summary"],
            },
            "cited_events": [
                {
                    key: value
                    for key, value in event.items()
                    if key != "evidence_id"
                }
                for event in payload["events"]
            ],
        }
    else:
        raise GitHubDailySummaryError("unknown model context stage")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _ollama_content(
    base_url: str,
    *,
    model_identifier: str,
    prompt: PromptSpec,
    context: SummaryContext,
    options: Mapping[str, object],
    timeout: float,
    transport: OllamaJSONTransport | None,
) -> bytes:
    request = transport or ollama_request_json
    response = request(
        base_url,
        method="POST",
        path="/api/chat",
        payload={
            "model": model_identifier,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": _context_user_prompt(context)},
            ],
            "stream": False,
            "think": False,
            "format": dict(prompt.output_schema),
            "options": dict(options),
        },
        timeout=timeout,
    )
    if response.get("done") is not True:
        raise OllamaProviderError("Ollama Daily summary response is not complete")
    if response.get("model") != model_identifier:
        raise OllamaProviderError(
            "Ollama Daily summary response model does not match resolved model"
        )
    message = response.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise OllamaProviderError(
            "Ollama Daily summary response message is invalid"
        )
    content = message.get("content")
    if not isinstance(content, str) or not content:
        raise OllamaProviderError(
            "Ollama Daily summary response content is empty"
        )
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OllamaProviderError(
            "Ollama Daily summary response is not UTF-8 encodable"
        ) from exc
    if len(data) > MAX_OUTPUT_BYTES:
        raise OllamaProviderError(
            "Ollama Daily summary output exceeds byte limit"
        )
    return data


def ollama_infer(
    *,
    base_url: str,
    model: str,
    options: Mapping[str, object] | None = None,
    timeout: float = OLLAMA_DEFAULT_TIMEOUT_SECONDS,
    transport: OllamaJSONTransport | None = None,
):
    root = validated_ollama_base_url(base_url)
    timeout_value = validated_ollama_timeout(timeout)
    inference_options = validated_ollama_options(options)
    identity = resolve_ollama_model(
        root,
        model,
        timeout=timeout_value,
        transport=transport,
    )
    config = dict(ollama_model_config(inference_options))
    config["daily_summary_adapter_version"] = "ollama-chat-structured-v0"

    def infer(prompt: PromptSpec, context: SummaryContext) -> InferenceResponse:
        data = _ollama_content(
            root,
            model_identifier=identity.identifier,
            prompt=prompt,
            context=context,
            options=inference_options,
            timeout=timeout_value,
            transport=transport,
        )
        return InferenceResponse(
            content=data,
            model_provider=OLLAMA_PROVIDER_NAME,
            model_identifier=identity.identifier,
            model_revision=identity.digest,
            model_config=config,
        )

    return BoundInfer(
        identity=PreboundInferenceIdentity(
            model_provider=OLLAMA_PROVIDER_NAME,
            model_identifier=identity.identifier,
            model_revision=identity.digest,
            model_config=config,
        ),
        invoke=infer,
    )


def openai_compatible_infer(
    *,
    base_url: str,
    model: str,
    options: Mapping[str, object] | None = None,
    timeout: float = OPENAI_DEFAULT_TIMEOUT_SECONDS,
    api_key: str | None = None,
    transport: OpenAIJSONTransport | None = None,
):
    root = validated_base_url(base_url)
    timeout_value = validated_timeout(timeout)
    inference_options = validated_options(options)
    config = dict(openai_model_config(inference_options))
    config["daily_summary_adapter_version"] = "openai-chat-structured-v0"

    def infer(prompt: PromptSpec, context: SummaryContext) -> InferenceResponse:
        identity, data = chat_content(
            root,
            model=model,
            system_prompt=prompt.system,
            user_prompt=_context_user_prompt(context),
            output_schema=prompt.output_schema,
            schema_name=_SCHEMA_NAMES[prompt.stage],
            options=inference_options,
            timeout=timeout_value,
            api_key=api_key,
            transport=transport,
        )
        if len(data) > MAX_OUTPUT_BYTES:
            raise OpenAICompatibleProviderError(
                "OpenAI-compatible Daily summary output exceeds byte limit"
            )
        return InferenceResponse(
            content=data,
            model_provider=OPENAI_PROVIDER_NAME,
            model_identifier=identity.identifier,
            model_revision=identifier_revision(identity.identifier),
            model_config=config,
        )

    return infer


def _load_options(path: Path | None) -> Mapping[str, object] | None:
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GitHubDailySummaryError(
            f"cannot read provider options: {path}"
        ) from exc
    return _decode_json_object(data, label="Daily summary provider options")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-daily-summary-run",
        description=(
            "Generate, reduce, and ground one GitHub Daily Project summary."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=("ollama", "openai-compatible"),
        required=True,
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument("--options-file", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        options = _load_options(args.options_file)
        if args.provider == "ollama":
            revision = validated_ollama_revision(
                args.implementation_revision
            )
            infer = ollama_infer(
                base_url=args.base_url,
                model=args.model,
                options=options,
                timeout=args.timeout,
            )
        else:
            revision = validated_implementation_revision(
                args.implementation_revision
            )
            infer = openai_compatible_infer(
                base_url=args.base_url,
                model=args.model,
                options=options,
                timeout=args.timeout,
                api_key=os.environ.get("OPENAI_API_KEY"),
            )
        result = run_pipeline(
            evidence_path=args.evidence,
            state_root=args.state_root,
            infer=infer,
            implementation_revision=revision,
        )
    except (
        ArtifactLifecycleError,
        GitHubDailySummaryError,
        OllamaProviderError,
        OpenAICompatibleProviderError,
        OSError,
    ) as exc:
        print(f"github-daily-summary: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "event": "github-daily-summary-grounded",
                "evidence_bundle_sha256": result.evidence_bundle_sha256,
                "grounded_summary_sha256": (
                    result.grounded_summary_sha256
                ),
                "grounded_summary_path": str(
                    result.grounded_summary_path
                ),
                "partial_batches": len(
                    result.partial_context_sha256s
                ),
                "reduce_batches": len(
                    result.reduce_context_sha256s
                ),
                "ground_batches": len(
                    result.ground_context_sha256s
                ),
                "claims": result.claim_count,
                "rejected": result.rejected_count,
                "inference_records": len(
                    result.provenance_sha256s
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
