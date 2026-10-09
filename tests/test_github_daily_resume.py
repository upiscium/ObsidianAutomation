"""Adversarial context-bound partial and grounding resume regression tests."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.github_daily_activity import _date_window, _make_event, _bounded_text
from obsidian_automation.github_daily_summary import (
    PARTIAL_STAGE, GROUND_STAGE, BoundInfer, EvidenceBundle, GitHubDailySummaryError,
    PreboundInferenceIdentity, InferenceResponse, SummaryContext,
    _normalized_claim, _run_claim_stage, _run_ground_stage, store_context,
    _prompt_spec,
)

REV = "a" * 40
CFG = {
    "adapter_version": "ollama-chat-structured-v0",
    "think": False,
    "options": {"temperature": 0},
    "daily_summary_adapter_version": "ollama-chat-structured-v0",
}


def _fixture(count: int = 7):
    start, _ = _date_window(date(2026, 10, 5))
    excerpt = _bounded_text("implemented change", limit=4096, label="message")
    assert excerpt is not None
    events = [
        _make_event(
            kind="default_branch_commit", repository="upiscium/Test",
            occurred_at=start + timedelta(seconds=i),
            url=f"https://github.com/upiscium/Test/commit/{i + 1:040x}",
            actor="test", entity_type="commit", number=None, source_id=f"event-{i}",
            sha=f"{i+1:040x}", message=excerpt,
        ).to_json()
        for i in range(count)
    ]
    bundle = EvidenceBundle(
        sha256="c" * 64, date="2026-10-05", timezone="Asia/Tokyo",
        window_start="2026-10-04T15:00:00Z",
        window_end="2026-10-05T15:00:00Z", projects=(),
        repositories=("upiscium/Test",), events=tuple(events),
    )
    contexts = tuple(
        SummaryContext(
            stage=PARTIAL_STAGE, evidence_bundle_sha256=bundle.sha256,
            batch_index=i, batch_count=count, source_output_sha256s=(),
            events=(event,),
        )
        for i, event in enumerate(events)
    )
    return bundle, contexts


class MockModel:
    def __init__(self, *, fail_partial_at: int | None = None, empty: bool = False,
                 model_revision: str = "b" * 64, config: dict | None = None):
        self.calls: list[tuple[str, int]] = []
        self.fail_partial_at = fail_partial_at
        self.empty = empty
        self.revision = model_revision
        self.config = config or CFG

    def __call__(self, prompt, ctx):
        self.calls.append((ctx.stage, ctx.batch_index))
        if ctx.stage == PARTIAL_STAGE:
            if ctx.batch_index == self.fail_partial_at:
                raise TimeoutError("injected timeout")
            claims = [] if self.empty else [{
                "kind": "implementation", "summary": f"implemented {ctx.events[0]['source_id']}",
                "source_refs": [0],
            }]
            content = json.dumps({"claims": claims}).encode()
        elif ctx.stage == GROUND_STAGE:
            content = b'{"verdict":"supported","reason":"directly cited"}'
        else:
            raise AssertionError(ctx.stage)
        return InferenceResponse(
            content=content, model_provider="ollama",
            model_identifier="gemma4:12b", model_revision=self.revision,
            model_config=self.config,
        )


def _bound(model, *, revision: str = "b"*64, config: dict | None = None):
    return BoundInfer(
        identity=PreboundInferenceIdentity(
            model_provider="ollama", model_identifier="gemma4:12b",
            model_revision=revision, model_config=config or CFG,
        ),
        invoke=model,
    )


def _partials(state: Path, model, bundle, contexts, *, revision=REV):
    return _run_claim_stage(
        state, bundle, contexts, infer=model, implementation_revision=revision,
    )


def _pointer_files(state: Path):
    return sorted((state / "github-daily-summary" / "resume").glob("*.github-daily-resume.json"))


def test_timeout_at_partial_seven_reuses_first_six_without_model_calls(tmp_path):
    bundle, contexts = _fixture(7)
    first = MockModel(fail_partial_at=6)
    with pytest.raises(GitHubDailySummaryError, match="partial batch 7/7: provider=TimeoutError"):
        _partials(tmp_path, _bound(first), bundle, contexts)
    assert first.calls == [(PARTIAL_STAGE, i) for i in range(7)]
    assert len(_pointer_files(tmp_path)) == 6

    second = MockModel()
    stored, context_shas, provenance_shas = _partials(
        tmp_path, _bound(second), bundle, contexts)
    assert second.calls == [(PARTIAL_STAGE, 6)]
    assert len(stored) == len(context_shas) == len(provenance_shas) == 7
    assert len(_pointer_files(tmp_path)) == 7

    third = MockModel()
    again, ctx_again, prov_again = _partials(tmp_path, _bound(third), bundle, contexts)
    assert third.calls == []
    assert again == stored
    assert ctx_again == context_shas
    assert prov_again == provenance_shas


@pytest.mark.parametrize("change", ["revision", "options", "implementation", "context"])
def test_identity_or_context_drift_cannot_hit_resume(tmp_path, change):
    bundle, contexts = _fixture(1)
    _partials(tmp_path, _bound(MockModel()), bundle, contexts)
    model = MockModel()
    infer = _bound(model)
    revision = REV
    if change == "revision":
        model = MockModel(model_revision="d"*64)
        infer = _bound(model, revision="d"*64)
    elif change == "options":
        opts = dict(CFG)
        opts["options"] = {"temperature": 0, "num_ctx": 8192}
        model = MockModel(config=opts)
        infer = _bound(model, config=opts)
    elif change == "implementation":
        revision = "f" * 40
    else:
        context = contexts[0]
        contexts = (
            SummaryContext(
                stage=context.stage, evidence_bundle_sha256=context.evidence_bundle_sha256,
                batch_index=context.batch_index, batch_count=2,
                source_output_sha256s=(), events=context.events,
            ),
        )
    _partials(tmp_path, infer, bundle, contexts, revision=revision)
    assert model.calls == [(PARTIAL_STAGE, 0)]


def test_empty_partial_output_is_cacheable(tmp_path):
    bundle, contexts = _fixture(1)
    _partials(tmp_path, _bound(MockModel(empty=True)), bundle, contexts)
    fresh = MockModel(empty=True)
    stored, _, _ = _partials(tmp_path, _bound(fresh), bundle, contexts)
    assert stored[0].output.claims == ()
    assert fresh.calls == []


def test_unbound_legacy_callbacks_never_resume(tmp_path):
    bundle, contexts = _fixture(1)
    fake = MockModel()
    _partials(tmp_path, fake, bundle, contexts)
    _partials(tmp_path, fake, bundle, contexts)
    assert fake.calls == [(PARTIAL_STAGE, 0)] * 2
    assert not (tmp_path / "github-daily-summary" / "resume").exists()


def test_unindexed_partial_artifacts_are_not_adopted(tmp_path):
    bundle, contexts = _fixture(1)
    store_context(tmp_path, contexts[0])
    model = MockModel()
    _partials(tmp_path, _bound(model), bundle, contexts)
    assert model.calls == [(PARTIAL_STAGE, 0)]


@pytest.mark.parametrize("corruption", ["invalid_pointer", "wrong_output_sha", "missing_provenance", "symlink_pointer", "wrong_claim_id"])
def test_invalid_resumed_artifact_is_fatal_and_never_inferred(tmp_path, corruption):
    bundle, contexts = _fixture(1)
    _partials(tmp_path, _bound(MockModel()), bundle, contexts)
    pointer = _pointer_files(tmp_path)[0]
    ref = json.loads(pointer.read_bytes())
    if corruption == "invalid_pointer":
        pointer.write_bytes(b'{"unknown":true}')
    elif corruption == "wrong_output_sha":
        ref["output_sha256"] = "f" * 64
        pointer.write_text(json.dumps(ref))
    elif corruption == "missing_provenance":
        sha = ref["provenance_sha256"]
        artifact = tmp_path / "github-daily-summary" / "provenance" / f"{sha}.github-daily-inference.json"
        artifact.unlink()
    elif corruption == "symlink_pointer":
        old = pointer.read_bytes()
        other = tmp_path / "pointer-shadow"
        other.write_bytes(old)
        pointer.unlink()
        pointer.symlink_to(other)
    else:
        sha = ref["output_sha256"]
        artifact = tmp_path / "github-daily-summary" / "output" / f"{sha}.github-daily-partial-output.json"
        obj = json.loads(artifact.read_bytes())
        obj["claims"][0]["claim_id"] = "0" * 64
        artifact.write_text(json.dumps(obj))
    second = MockModel()
    with pytest.raises((ArtifactLifecycleError, GitHubDailySummaryError)):
        _partials(tmp_path, _bound(second), bundle, contexts)
    assert second.calls == []


def test_grounding_resume_is_singleton_and_tied_to_exact_claim(tmp_path):
    bundle, contexts = _fixture(2)
    original = contexts[0].events[0]
    source_id = str(original["evidence_id"])
    claim = _normalized_claim(
        kind="implementation", summary="implemented event-0",
        repository="upiscium/Test", evidence_ids=[source_id],
        allowed_evidence_ids={source_id},
        events_by_id=bundle.events_by_id,
    )
    grounded = SummaryContext(
        stage=GROUND_STAGE, evidence_bundle_sha256=bundle.sha256,
        batch_index=0, batch_count=1,
        source_output_sha256s=("e" * 64,), events=(original,), claims=(claim,),
    )
    first = MockModel()
    a = _run_ground_stage(tmp_path, (grounded,), infer=_bound(first), implementation_revision=REV)
    assert first.calls == [(GROUND_STAGE, 0)]
    second = MockModel()
    b = _run_ground_stage(tmp_path, (grounded,), infer=_bound(second), implementation_revision=REV)
    assert second.calls == []
    assert a == b
    changed_claim = _normalized_claim(
        kind="implementation", summary="different valid claim",
        repository="upiscium/Test", evidence_ids=[source_id],
        allowed_evidence_ids={source_id}, events_by_id=bundle.events_by_id,
    )
    changed = SummaryContext(
        stage=GROUND_STAGE, evidence_bundle_sha256=bundle.sha256,
        batch_index=0, batch_count=1,
        source_output_sha256s=("f"*64,), events=(original,), claims=(changed_claim,),
    )
    third = MockModel()
    _run_ground_stage(tmp_path, (changed,), infer=_bound(third), implementation_revision=REV)
    assert third.calls == [(GROUND_STAGE, 0)]


def test_mismatched_fresh_response_is_rejected_before_resume_publication(tmp_path):
    bundle, contexts = _fixture(1)
    model = MockModel(model_revision="0" * 64)
    with pytest.raises(GitHubDailySummaryError, match="prebound"):
        _partials(tmp_path, _bound(model), bundle, contexts)
    assert not _pointer_files(tmp_path)


def test_syntactically_valid_malicious_cas_is_still_rejected(tmp_path):
    """A hash-consistent but semantically invalid record must not be adopted."""
    from obsidian_automation.artifact_lifecycle import _canonical_json_bytes, sha256_bytes

    bundle, contexts = _fixture(1)
    _partials(tmp_path, _bound(MockModel()), bundle, contexts)
    pointer = _pointer_files(tmp_path)[0]
    meta = json.loads(pointer.read_bytes())
    output_dir = tmp_path / "github-daily-summary" / "output"
    original_path = output_dir / (
        f"{meta['output_sha256']}.github-daily-partial-output.json"
    )
    original = json.loads(original_path.read_bytes())
    original["claims"][0]["claim_id"] = "0" * 64
    forged_bytes = _canonical_json_bytes(original)
    forged_sha = sha256_bytes(forged_bytes)
    (output_dir / f"{forged_sha}.github-daily-partial-output.json").write_bytes(forged_bytes)
    meta["output_sha256"] = forged_sha
    pointer.write_bytes(_canonical_json_bytes(meta))
    model = MockModel()
    with pytest.raises(GitHubDailySummaryError, match="untrusted resume"):
        _partials(tmp_path, _bound(model), bundle, contexts)
    assert model.calls == []


def test_hash_consistent_wrong_model_provenance_is_rejected(tmp_path):
    from obsidian_automation.artifact_lifecycle import _canonical_json_bytes, sha256_bytes

    bundle, contexts = _fixture(1)
    _partials(tmp_path, _bound(MockModel()), bundle, contexts)
    pointer = _pointer_files(tmp_path)[0]
    meta = json.loads(pointer.read_bytes())
    provenance_dir = tmp_path / "github-daily-summary" / "provenance"
    original_path = provenance_dir / (
        f"{meta['provenance_sha256']}.github-daily-inference.json"
    )
    record = json.loads(original_path.read_bytes())
    record["model"]["revision"] = "d" * 64
    forged_bytes = _canonical_json_bytes(record)
    forged_sha = sha256_bytes(forged_bytes)
    (provenance_dir / f"{forged_sha}.github-daily-inference.json").write_bytes(forged_bytes)
    meta["provenance_sha256"] = forged_sha
    pointer.write_bytes(_canonical_json_bytes(meta))
    model = MockModel()
    with pytest.raises(GitHubDailySummaryError, match="untrusted resume"):
        _partials(tmp_path, _bound(model), bundle, contexts)
    assert model.calls == []


def test_nested_prebound_config_mutation_rejected_before_model_call(tmp_path):
    bundle, contexts = _fixture(1)
    fake = MockModel()
    inference = _bound(fake)
    inference.identity.model_config["options"]["temperature"] = 1
    with pytest.raises(GitHubDailySummaryError, match="configuration mutated"):
        _partials(tmp_path, inference, bundle, contexts)
    assert fake.calls == []


def test_full_pipeline_reuses_both_partial_and_grounding(tmp_path):
    from obsidian_automation.github_daily_activity import (
        ProjectBinding, make_daily_evidence_bundle,
    )
    from obsidian_automation.github_daily_summary import run_pipeline

    start, _ = _date_window(date(2026, 10, 5))
    excerpt = _bounded_text("implementation verified", limit=4096, label="message")
    events = [
        _make_event(
            kind="default_branch_commit", repository="upiscium/Test",
            occurred_at=start + timedelta(seconds=i),
            url=f"https://github.com/upiscium/Test/commit/{i+1:040x}",
            actor="test", entity_type="commit", number=None,
            source_id=f"event-{i}", sha=f"{i+1:040x}", message=excerpt,
        ) for i in range(3)
    ]
    raw = make_daily_evidence_bundle(
        target_date=date(2026, 10, 5),
        projects=[ProjectBinding(
            project_path="10-Project/Test/Test.md", repository="upiscium/Test",
        )],
        events=events,
    )
    path = tmp_path / f"{raw.sha256}.github-daily-evidence.json"
    path.write_bytes(raw.canonical_bytes)
    state = tmp_path / "state"
    state.mkdir()
    first = MockModel()
    a = run_pipeline(
        evidence_path=path, state_root=state,
        infer=_bound(first), implementation_revision=REV,
    )
    assert (PARTIAL_STAGE, 0) in first.calls
    assert any(stage == GROUND_STAGE for stage, _ in first.calls)
    second = MockModel()
    b = run_pipeline(
        evidence_path=path, state_root=state,
        infer=_bound(second), implementation_revision=REV,
    )
    assert second.calls == []
    assert a.grounded_summary_sha256 == b.grounded_summary_sha256
    assert a.provenance_sha256s == b.provenance_sha256s
    assert a.claim_count == b.claim_count

def test_intermediate_length_implementation_revision_keeps_existing_contract(tmp_path):
    bundle, contexts = _fixture(1)
    revision = "e" * 48
    model = MockModel()
    _partials(tmp_path, _bound(model), bundle, contexts, revision=revision)
    assert model.calls == [(PARTIAL_STAGE, 0)]
    repeated = MockModel()
    _partials(tmp_path, _bound(repeated), bundle, contexts, revision=revision)
    assert repeated.calls == []
