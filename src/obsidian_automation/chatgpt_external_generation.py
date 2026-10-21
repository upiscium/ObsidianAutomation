"""Explicit Fake-Provider external draft with v3 Source-bound GenerationRecord.

This never advances a pre-review stage, queues a Job, or approves a Note.
Only synthetic Fake Provider testing is authorized. Its v3 Generation Record
does not become a selected Job stage output; future Worker integration must
admit a fresh private Source revision and commit output atomically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    store_untrusted_proposal,
)
from .chatgpt_export_review_preview import ExternalReviewPreview
from .chatgpt_external_context_store import (
    ExternalContextStoreError,
    load_external_context_for_review,
)
from .chatgpt_external_generation_contract import (
    ExternalGenerationPrompt,
    ExternalGeneratorContractError,
    render_external_generation_prompt,
)
from .chatgpt_external_job import (
    EXTERNAL_ADMITTED_STATE,
    ExternalJobError,
    inspect_external_job_state,
)
from .generation_artifact import (
    build_generation_record,
    load_generation_record,
    store_generation_record,
)
from .generator_contract import (
    MAX_GENERATOR_OUTPUT_BYTES,
    assemble_knowledge_note_proposal,
    parse_generator_output,
)

FAKE_PROVIDER_NAME = "synthetic-fake-only"
FAKE_MODEL_IDENTIFIER = "explicit-deterministic-synthetic-transport"
FAKE_MODEL_REVISION = "fake-only-no-network"
FAKE_ADAPTER_VERSION = "chatgpt-external-generation-fake-v0"


class ExternalDraftError(ValueError):
    """No source/model output in diagnostics."""


@dataclass(frozen=True, repr=False)
class ExternalFakeDraftReceipt:
    job_id: str
    context_sha256: str
    proposal_sha256: str
    generation_sha256: str
    source_run_sha256: str
    source_revision_sha256: str
    prompt_sha256: str
    state: str = EXTERNAL_ADMITTED_STATE
    real_job_stage_output_selected: bool = False
    awaiting_human_review: bool = False
    generation_authorized: bool = False
    canonical_mutation_authorized: bool = False


def run_fake_external_knowledge_draft(
    ai_root: Path,
    *,
    private_root: Path,
    preview: ExternalReviewPreview,
    job_id: str,
    transport: Callable[[ExternalGenerationPrompt], bytes],
    implementation_revision: str = "0" * 40,
    expected_job_state: str = EXTERNAL_ADMITTED_STATE,
) -> ExternalFakeDraftReceipt:
    """Synthetic Source-bound Fake generation, NOT real worker invocation.

    The callback gets private *unverified summaries*. No actual user Source,
    production transport or LLM must be supplied to this test-only API.
    """
    if not callable(transport):
        raise ExternalDraftError("explicit_fake_transport_required")
    if (
        type(implementation_revision) is not str
        or len(implementation_revision) not in (40, 64)
        or any(c not in "0123456789abcdef" for c in implementation_revision)
    ):
        raise ExternalDraftError("invalid_fake_implementation_revision")
    try:
        status = inspect_external_job_state(
            ai_root,
            private_root=private_root,
            preview=preview,
            job_id=job_id,
            expected_state=expected_job_state,
        )
        ctx_sha = status["context_sha256"]
        context = load_external_context_for_review(ai_root, ctx_sha)
        prompt = render_external_generation_prompt(context)
    except (
        ExternalJobError,
        ExternalContextStoreError,
        ExternalGeneratorContractError,
    ):
        raise ExternalDraftError("external_job_or_context_not_admitted") from None

    try:
        raw = transport(prompt)
    except Exception:  # noqa: BLE001 - sanitize any untrusted callback/provider failure
        raise ExternalDraftError("synthetic_provider_failed") from None
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_GENERATOR_OUTPUT_BYTES:
        raise ExternalDraftError("synthetic_generator_output_invalid")
    try:
        output = parse_generator_output(raw)
    except ArtifactLifecycleError:
        raise ExternalDraftError("synthetic_generator_output_invalid") from None
    if output.source_type != "other":
        raise ExternalDraftError("synthetic_source_type_must_be_other")

    # The source may change while a slow model generates text. Recheck
    # immediately before any proposal artifact is persisted. This is not
    # atomic with future Job activation; no activation exists in this module.
    try:
        inspect_external_job_state(
            ai_root,
            private_root=private_root,
            preview=preview,
            job_id=job_id,
            expected_state=expected_job_state,
        )
    except ExternalJobError:
        raise ExternalDraftError("external_source_changed_during_inference") from None
    try:
        proposal_bytes = assemble_knowledge_note_proposal(
            context_sha256=ctx_sha,
            output=output,
        )
        proposal_sha, _ = store_untrusted_proposal(ai_root, proposal_bytes)
        model_config = {
            "adapter_version": FAKE_ADAPTER_VERSION,
            "transport": "explicit-fake-only",
            "options": {},
        }
        gen = build_generation_record(
            ai_root,
            context_sha256=ctx_sha,
            proposal_sha256=proposal_sha,
            implementation_revision=implementation_revision,
            prompt_template_version=prompt.version,
            prompt_template_sha256=prompt.prompt_sha256,
            model_provider=FAKE_PROVIDER_NAME,
            model_identifier=FAKE_MODEL_IDENTIFIER,
            model_revision=FAKE_MODEL_REVISION,
            model_config=model_config,
            context_kind="chatgpt-external-source",
        )
        generation_sha, _ = store_generation_record(ai_root, gen)
        loaded = load_generation_record(ai_root, generation_sha)
        if loaded != gen or loaded.external_source is None:
            raise ExternalDraftError("external_generation_record_mismatch")
        inspect_external_job_state(
            ai_root,
            private_root=private_root,
            preview=preview,
            job_id=job_id,
            expected_state=expected_job_state,
        )
    except ExternalDraftError:
        raise
    except (
        ArtifactLifecycleError,
        ExternalJobError,
        ExternalContextStoreError,
        OSError,
    ):
        raise ExternalDraftError("external_generation_artifact_rejected") from None
    return ExternalFakeDraftReceipt(
        job_id=job_id,
        context_sha256=ctx_sha,
        proposal_sha256=proposal_sha,
        generation_sha256=generation_sha,
        source_run_sha256=context.source_run_sha256,
        source_revision_sha256=context.source_revision_sha256,
        prompt_sha256=prompt.prompt_sha256,
    )
