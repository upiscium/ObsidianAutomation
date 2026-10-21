"""v3 Source-bound Generation Record tests; synthetic and NOT runnable Job."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_chatgpt_external_context_store import fixture, publish

from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError,
    store_untrusted_proposal,
)
from obsidian_automation.chatgpt_external_generation_contract import (
    PROMPT_VERSION,
    prompt_template_sha256,
    render_external_generation_prompt,
)
from obsidian_automation.generation_artifact import (
    ExternalGenerationProvenance,
    build_generation_record,
    generation_input_context,
    load_generation_record,
    parse_generation_record,
    store_generation_record,
)

REVISION = "e" * 40
MODEL = "synthetic-external-model"
MODEL_CONFIG = {
    "adapter_version": "openai-chat-completions-json-schema-v1",
    "identity_binding": "identifier-only",
    "options": {"temperature": 0},
}


def seeded(tmp_path):
    ai_root, private_root, preview, context = fixture(tmp_path)
    sha, _ = publish(ai_root, private_root, preview, context)
    proposal_sha, _ = store_untrusted_proposal(
        ai_root,
        b'{"contract_version":1,"operation":"create_note",'
        b'"mutation_id":"synthetic-proposal","target":{"path":"11-Knowledge/Synthetic.md"},'
        b'"content":"---\\ntype: knowledge-note\\nstatus: active\\n'
        b"category:\\nmaturity: draft\\nsource_type: other\\n---\\n\\n"
        b'Synthetic draft; not confirmed.\\n"}',
    )
    return ai_root, context, sha, proposal_sha


def record(ai_root, context_sha, proposal_sha, **kwargs):
    return build_generation_record(
        ai_root,
        context_sha256=context_sha,
        proposal_sha256=proposal_sha,
        implementation_revision=REVISION,
        prompt_template_version=kwargs.pop("prompt_template_version", PROMPT_VERSION),
        prompt_template_sha256=kwargs.pop(
            "prompt_template_sha256",
            prompt_template_sha256(),
        ),
        model_provider="openai-compatible",
        model_identifier=MODEL,
        model_revision=f"identifier:{MODEL}",
        model_config=MODEL_CONFIG,
        context_kind=kwargs.pop("context_kind", "chatgpt-external-source"),
        generated_at="2026-10-21T01:00:00Z",
        **kwargs,
    )


def test_version3_record_binds_exact_external_context_and_model_prompt(tmp_path):
    ai_root, context, context_sha, proposal_sha = seeded(tmp_path)
    rec = record(ai_root, context_sha, proposal_sha)
    assert rec.record_version == 3
    assert rec.context_kind == "chatgpt-external-source"
    assert rec.semantic_objective is None
    ext = rec.external_source
    assert type(ext) is ExternalGenerationProvenance
    assert ext.source_scope == context.source_scope
    assert ext.source_hmac == context.source_hmac
    assert ext.source_archive_sha256 == context.source_archive_sha256
    assert ext.source_content_sha256 == context.source_content_sha256
    assert ext.source_revision_sha256 == context.source_revision_sha256
    assert ext.source_run_sha256 == context.source_run_sha256
    assert ext.source_preview_sha256 == context.source_preview_sha256
    assert rec.generator.prompt_template_version == PROMPT_VERSION
    assert rec.generator.prompt_template_sha256 == prompt_template_sha256()
    assert parse_generation_record(rec.to_json_bytes()) == rec
    record_sha, path = store_generation_record(ai_root, rec)
    assert path.exists()
    assert load_generation_record(ai_root, record_sha) == rec
    # v3 must NOT masquerade as a v1 ContextBundle in existing evaluator.
    with pytest.raises(
        ArtifactLifecycleError,
        match="generation record context provenance is invalid",
    ):
        generation_input_context(ai_root, rec)


def test_generation_prompt_is_bounded_and_assistant_claim_is_untrusted(tmp_path):
    _, context, _, _ = seeded(tmp_path)
    prompt = render_external_generation_prompt(context)
    assert prompt.version == PROMPT_VERSION
    assert prompt.prompt_sha256 == prompt_template_sha256()
    assert prompt.output_schema
    assert context.source_hmac not in prompt.user
    assert context.source_preview_sha256 not in prompt.user
    data = json.loads(prompt.user)
    assert data["source_unverified"] is True
    assert data["source_kind"] == "chatgpt-external-source"
    assert data["candidate_evidence"][0]["attribution"] == "assistant-claim-unverified"
    assert "source_type MUST be 'other'" in prompt.system
    assert "not primary established facts" in prompt.system.lower() or (
        "not primary" in prompt.system.lower()
    )


def test_altered_external_source_provenance_cannot_be_stored(tmp_path):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    original = record(ai_root, ctx_sha, proposal_sha)
    ext = original.external_source
    assert ext
    forged = replace(
        original,
        external_source=replace(ext, source_hmac="f" * 64),
    )
    # Shape is canonical but metadata must be exactly CAS-bound.
    assert parse_generation_record(forged.to_json_bytes()) == forged
    with pytest.raises(
        ArtifactLifecycleError,
        match="source/context provenance mismatch",
    ):
        store_generation_record(ai_root, forged)
    assert original.external_source != forged.external_source


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_archive_sha256", "0" * 64),
        ("source_content_sha256", "0" * 64),
        ("source_revision_sha256", "0" * 64),
        ("source_run_sha256", "0" * 64),
        ("source_preview_sha256", "0" * 64),
    ],
)
def test_all_external_digests_independently_bound(tmp_path, field, value):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    original = record(ai_root, ctx_sha, proposal_sha)
    forged = replace(
        original,
        external_source=replace(original.external_source, **{field: value}),
    )
    with pytest.raises(
        ArtifactLifecycleError,
        match="source/context provenance mismatch",
    ):
        store_generation_record(ai_root, forged)


def test_external_generator_prompt_must_match_fixed_version_and_sha(tmp_path):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    with pytest.raises(ArtifactLifecycleError, match="prompt version/hash mismatch"):
        record(
            ai_root,
            ctx_sha,
            proposal_sha,
            prompt_template_sha256="0" * 64,
        )
    with pytest.raises(ArtifactLifecycleError, match="prompt version/hash mismatch"):
        record(
            ai_root,
            ctx_sha,
            proposal_sha,
            prompt_template_version="legacy-knowledge-generator-v2",
        )


def test_v3_parser_refuses_unknown_extra_fields_and_legacy_impersonation(tmp_path):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    original = record(ai_root, ctx_sha, proposal_sha)
    data = json.loads(original.to_json_bytes())
    data["target_path"] = "11-Knowledge/Forged.md"
    with pytest.raises(ArtifactLifecycleError, match="v3 contract"):
        parse_generation_record(json.dumps(data).encode())
    data.pop("target_path")
    data["context_kind"] = "context-bundle"
    with pytest.raises(ArtifactLifecycleError, match="v3 context_kind"):
        parse_generation_record(json.dumps(data).encode())


def test_external_only_provenance_rejected_on_legacy_record_version(tmp_path):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    original = record(ai_root, ctx_sha, proposal_sha)
    with pytest.raises(ArtifactLifecycleError, match="cannot carry external"):
        replace(original, record_version=1).to_json_bytes()
    with pytest.raises(ArtifactLifecycleError, match="cannot carry external"):
        replace(
            original,
            record_version=2,
            semantic_objective=original.external_source,
        ).to_json_bytes()


def test_source_context_cas_corruption_fails_closed(tmp_path):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    original = record(ai_root, ctx_sha, proposal_sha)
    generation_sha, _ = store_generation_record(ai_root, original)
    context_path = ai_root / "05-Context" / f"{ctx_sha}.chatgpt-external-context.json"
    context_path.write_bytes(b"corrupt")
    with pytest.raises(
        ArtifactLifecycleError, match="external Source Context unavailable"
    ):
        load_generation_record(ai_root, generation_sha)


def test_v1_v2_shapes_remain_isolated_from_v3(tmp_path):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    source = record(ai_root, ctx_sha, proposal_sha)
    raw = json.loads(source.to_json_bytes())
    raw["record_version"] = 1
    raw.pop("context_kind")
    raw.pop("external_source")
    legacy = parse_generation_record(json.dumps(raw).encode())
    assert legacy.record_version == 1
    assert legacy.external_source is None
    assert legacy.context_kind == "context-bundle"
    # A syntactically valid legacy record does not permit this new external
    # context kind during build/store. The legacy context file is absent.
    with pytest.raises(ArtifactLifecycleError):
        store_generation_record(ai_root, legacy)


@pytest.mark.parametrize(
    "bad_scope", ["../private", "UPPERCASE", "bad/slash", "space separated"]
)
def test_v3_source_scope_must_match_private_source_scope_contract(tmp_path, bad_scope):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    source = record(ai_root, ctx_sha, proposal_sha)
    forged = replace(
        source,
        external_source=replace(source.external_source, source_scope=bad_scope),
    )
    with pytest.raises(ArtifactLifecycleError, match="external Source scope invalid"):
        parse_generation_record(forged.to_json_bytes())


def test_generation_external_record_rejects_duplicate_props_and_boolean_version(
    tmp_path,
):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    rec = record(ai_root, ctx_sha, proposal_sha)
    value = json.loads(rec.to_json_bytes())
    value["record_version"] = True
    with pytest.raises(
        ArtifactLifecycleError, match="unsupported generation record_version"
    ):
        parse_generation_record(json.dumps(value).encode())
    with pytest.raises(ArtifactLifecycleError, match="duplicate JSON property"):
        parse_generation_record(b'{"record_version":3,"record_version":3}')


@pytest.mark.parametrize("wrong_type", [None, {}, [], "fake", True])
def test_v3_record_rejects_non_provenance_types_with_fixed_diagnostic(
    tmp_path, wrong_type
):
    ai_root, _, ctx_sha, proposal_sha = seeded(tmp_path)
    rec = record(ai_root, ctx_sha, proposal_sha)
    with pytest.raises(
        ArtifactLifecycleError,
        match="v3 requires only external Source provenance",
    ):
        replace(rec, external_source=wrong_type).to_json_bytes()
