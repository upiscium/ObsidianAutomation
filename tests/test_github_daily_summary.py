from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from obsidian_automation.github_daily_activity import (
    ProjectBinding,
    _bounded_text,
    _date_window,
    _make_event,
    make_daily_evidence_bundle,
)
from obsidian_automation.github_daily_summary import (
    GROUND_STAGE,
    PARTIAL_STAGE,
    REDUCE_STAGE,
    GitHubDailySummaryError,
    InferenceResponse,
    StoredClaimOutput,
    SummaryClaim,
    SummaryContext,
    _parse_model_claim_output,
    _parse_model_ground_output,
    build_ground_contexts,
    build_reduce_contexts,
    parse_claim_output,
    parse_evidence_bundle,
    partition_evidence,
    prompt_spec,
    run_pipeline,
)


def _evidence(tmp_path: Path, count: int = 24) -> tuple[Path, object]:
    start, _ = _date_window(date(2026, 10, 5))
    message = _bounded_text("implemented change", limit=4096, label="message")
    assert message is not None
    events = []
    for index in range(count):
        events.append(
            _make_event(
                kind="default_branch_commit",
                repository="upiscium/Test",
                occurred_at=start + timedelta(seconds=index),
                url=(
                    "https://github.com/upiscium/Test/commit/"
                    f"{index + 1:040x}"
                ),
                actor="upiscium",
                entity_type="commit",
                number=None,
                source_id=f"event-{index}",
                sha=f"{index + 1:040x}",
                message=message,
            )
        )
    bundle = make_daily_evidence_bundle(
        target_date=date(2026, 10, 5),
        projects=[
            ProjectBinding(
                project_path="10-Project/Test/Test.md",
                repository="upiscium/Test",
            )
        ],
        events=events,
    )
    path = tmp_path / f"{bundle.sha256}.github-daily-evidence.json"
    path.write_bytes(bundle.canonical_bytes)
    return path, bundle


def _fake_infer(prompt, context):
    if prompt.stage == PARTIAL_STAGE:
        claims = []
        for source_ref, event in enumerate(context.events):
            source = str(event["source_id"])
            claims.append(
                {
                    "kind": "implementation",
                    "repository": str(event["repository"]),
                    "summary": (
                        "reject event-1"
                        if source == "event-1"
                        else f"implemented {source}"
                    ),
                    "source_refs": [source_ref],
                }
            )
        content = json.dumps({"claims": claims}).encode()
    elif prompt.stage == REDUCE_STAGE:
        claims = [
            {
                "kind": claim.kind,
                "repository": claim.repository,
                "summary": claim.summary,
                "source_refs": [source_ref],
            }
            for source_ref, claim in enumerate(context.claims)
        ]
        content = json.dumps({"claims": claims}).encode()
    elif prompt.stage == GROUND_STAGE:
        content = json.dumps(
            {
                "assessments": [
                    {
                        "claim_ref": claim_ref,
                        "verdict": (
                            "unsupported"
                            if claim.summary.startswith("reject ")
                            else "supported"
                        ),
                        "reason": (
                            "claim overstates the cited event"
                            if claim.summary.startswith("reject ")
                            else "claim is directly supported"
                        ),
                    }
                    for claim_ref, claim in enumerate(context.claims)
                ]
            }
        ).encode()
    else:
        raise AssertionError(prompt.stage)
    return InferenceResponse(
        content=content,
        model_provider="test-provider",
        model_identifier="test-model",
        model_revision="test-revision",
        model_config={"temperature": 0},
    )


def test_parse_evidence_reverifies_event_identity(tmp_path: Path) -> None:
    path, bundle = _evidence(tmp_path, 2)
    parsed = parse_evidence_bundle(path.read_bytes())
    assert parsed.sha256 == bundle.sha256
    assert len(parsed.events) == 2

    value = json.loads(path.read_bytes())
    value["events"][0]["message"]["text"] = "tampered"
    with pytest.raises(
        GitHubDailySummaryError,
        match="identity mismatch",
    ):
        parse_evidence_bundle(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )


def test_event_partition_is_lossless_and_bounded(tmp_path: Path) -> None:
    path, _ = _evidence(tmp_path, 80)
    bundle = parse_evidence_bundle(path.read_bytes())

    contexts = partition_evidence(bundle, max_bytes=5000)

    assert len(contexts) > 1
    assert all(len(item.to_json_bytes()) <= 5000 for item in contexts)
    observed = [
        event["evidence_id"]
        for context in contexts
        for event in context.events
    ]
    expected = [event["evidence_id"] for event in bundle.events]
    assert observed == expected


def test_claim_parser_rejects_out_of_context_evidence(tmp_path: Path) -> None:
    path, _ = _evidence(tmp_path, 2)
    bundle = parse_evidence_bundle(path.read_bytes())
    contexts = partition_evidence(bundle, max_bytes=50_000)
    context = contexts[0]
    allowed = {str(item["evidence_id"]) for item in context.events}

    with pytest.raises(
        GitHubDailySummaryError,
        match="outside its context",
    ):
        parse_claim_output(
            json.dumps(
                {
                    "claims": [
                        {
                            "kind": "decision",
                            "repository": "upiscium/Test",
                            "summary": "made a decision",
                            "evidence_ids": ["f" * 64],
                        }
                    ]
                }
            ).encode(),
            stage=PARTIAL_STAGE,
            input_context_sha256="a" * 64,
            allowed_evidence_ids=allowed,
            events_by_id=bundle.events_by_id,
        )


def test_reducer_context_preserves_original_evidence_closure(
    tmp_path: Path,
) -> None:
    path, _ = _evidence(tmp_path, 6)
    bundle = parse_evidence_bundle(path.read_bytes())
    partial_context = partition_evidence(bundle, max_bytes=50_000)[0]
    allowed = {
        str(item["evidence_id"]) for item in partial_context.events
    }
    partial = parse_claim_output(
        json.dumps(
            {
                "claims": [
                    {
                        "kind": "implementation",
                        "repository": "upiscium/Test",
                        "summary": "implemented a bounded change",
                        "evidence_ids": [next(iter(allowed))],
                    }
                ]
            }
        ).encode(),
        stage=PARTIAL_STAGE,
        input_context_sha256="a" * 64,
        allowed_evidence_ids=allowed,
        events_by_id=bundle.events_by_id,
    )
    stored = StoredClaimOutput("b" * 64, partial)

    reduce_context = build_reduce_contexts(
        bundle,
        [stored],
        max_bytes=50_000,
    )[0]
    reduce_allowed = {
        evidence_id
        for claim in reduce_context.claims
        for evidence_id in claim.evidence_ids
    }

    with pytest.raises(
        GitHubDailySummaryError,
        match="outside its context",
    ):
        parse_claim_output(
            json.dumps(
                {
                    "claims": [
                        {
                            "kind": "implementation",
                            "repository": "upiscium/Test",
                            "summary": "invented broader change",
                            "evidence_ids": ["e" * 64],
                        }
                    ]
                }
            ).encode(),
            stage=REDUCE_STAGE,
            input_context_sha256="c" * 64,
            allowed_evidence_ids=reduce_allowed,
            events_by_id=bundle.events_by_id,
        )


def test_ground_context_contains_only_cited_raw_events(tmp_path: Path) -> None:
    path, _ = _evidence(tmp_path, 5)
    bundle = parse_evidence_bundle(path.read_bytes())
    contexts = partition_evidence(bundle, max_bytes=50_000)
    allowed = {str(item["evidence_id"]) for item in contexts[0].events}
    selected = list(allowed)[:2]
    output = parse_claim_output(
        json.dumps(
            {
                "claims": [
                    {
                        "kind": "bugfix",
                        "repository": "upiscium/Test",
                        "summary": "fixed one bug",
                        "evidence_ids": selected,
                    }
                ]
            }
        ).encode(),
        stage=REDUCE_STAGE,
        input_context_sha256="a" * 64,
        allowed_evidence_ids=allowed,
        events_by_id=bundle.events_by_id,
    )
    stored = StoredClaimOutput("b" * 64, output)

    ground = build_ground_contexts(
        bundle,
        [stored],
        max_bytes=100_000,
    )[0]

    assert {
        event["evidence_id"] for event in ground.events
    } == set(selected)
    assert [claim.claim_id for claim in ground.claims] == [
        output.claims[0].claim_id
    ]


def test_full_pipeline_multibatch_preserves_evidence_and_filters_unsupported(
    tmp_path: Path,
) -> None:
    evidence_path, bundle = _evidence(tmp_path, 24)
    state = tmp_path / "state"
    state.mkdir()

    result = run_pipeline(
        evidence_path=evidence_path,
        state_root=state,
        infer=_fake_infer,
        implementation_revision="1" * 40,
        partial_context_bytes=4500,
        reduce_context_bytes=3500,
        ground_context_bytes=5000,
    )

    assert len(result.partial_context_sha256s) > 1
    assert len(result.reduce_context_sha256s) > 1
    assert len(result.ground_context_sha256s) > 1
    assert result.claim_count == 23
    assert result.rejected_count == 1
    assert len(result.provenance_sha256s) == (
        len(result.partial_context_sha256s)
        + len(result.reduce_context_sha256s)
        + len(result.ground_context_sha256s)
    )

    final = json.loads(result.grounded_summary_path.read_bytes())
    original_ids = {
        event.evidence_id for event in bundle.events
    }
    assert len(final["claims"]) == 23
    assert len(final["rejected_claims"]) == 1
    assert all(
        set(claim["evidence_ids"]) <= original_ids
        for claim in final["claims"]
    )

    context_files = list(
        (state / "github-daily-summary" / "context").glob("*.json")
    )
    assert context_files
    for context_file in context_files:
        context = json.loads(context_file.read_bytes())
        if context["stage"] == PARTIAL_STAGE:
            assert context["events"]


def test_empty_evidence_produces_empty_grounded_summary(
    tmp_path: Path,
) -> None:
    bundle = make_daily_evidence_bundle(
        target_date=date(2026, 10, 5),
        projects=[],
        events=[],
    )
    evidence_path = (
        tmp_path / f"{bundle.sha256}.github-daily-evidence.json"
    )
    evidence_path.write_bytes(bundle.canonical_bytes)
    state = tmp_path / "state"
    state.mkdir()

    result = run_pipeline(
        evidence_path=evidence_path,
        state_root=state,
        infer=_fake_infer,
        implementation_revision="2" * 40,
    )

    assert result.claim_count == 0
    assert result.rejected_count == 0
    assert result.partial_context_sha256s == ()
    assert result.reduce_context_sha256s == ()
    assert result.ground_context_sha256s == ()
    assert result.provenance_sha256s == ()


def test_prompt_identity_is_stable_and_stage_specific() -> None:
    partial = prompt_spec(PARTIAL_STAGE)
    reduce = prompt_spec(REDUCE_STAGE)
    ground = prompt_spec(GROUND_STAGE)

    assert partial.template_sha256 == prompt_spec(
        PARTIAL_STAGE
    ).template_sha256
    assert len(
        {
            partial.template_sha256,
            reduce.template_sha256,
            ground.template_sha256,
        }
    ) == 3



def test_model_partial_source_refs_normalize_to_exact_evidence_ids(
    tmp_path: Path,
) -> None:
    path, bundle = _evidence(tmp_path, 3)
    bundle = parse_evidence_bundle(path.read_bytes())
    context = partition_evidence(bundle, max_bytes=50_000)[0]
    events_by_id = bundle.events_by_id
    output = _parse_model_claim_output(
        json.dumps({
            "claims": [{
                "kind": "implementation",
                "repository": "upiscium/Test",
                "summary": "implemented changes",
                "source_refs": [2, 0],
            }]
        }).encode(),
        context=context,
        input_context_sha256="a" * 64,
        events_by_id=events_by_id,
    )
    assert output.claims[0].evidence_ids == (
        str(context.events[2]["evidence_id"]),
        str(context.events[0]["evidence_id"]),
    )


@pytest.mark.parametrize("bad_refs", [
    [-1], [99], [True], [0, 0], ["0"], [], [0] * 9,
])
def test_model_partial_invalid_source_refs_fail_closed(
    tmp_path: Path,
    bad_refs: list[object],
) -> None:
    path, bundle = _evidence(tmp_path, 2)
    bundle = parse_evidence_bundle(path.read_bytes())
    context = partition_evidence(bundle, max_bytes=50_000)[0]
    with pytest.raises(GitHubDailySummaryError, match="source_refs"):
        _parse_model_claim_output(
            json.dumps({
                "claims": [{
                    "kind": "implementation",
                    "repository": "upiscium/Test",
                    "summary": "bad citation",
                    "source_refs": bad_refs,
                }]
            }).encode(),
            context=context,
            input_context_sha256="a" * 64,
            events_by_id=bundle.events_by_id,
        )


def test_model_reducer_inherits_union_of_exact_source_claim_evidence(
    tmp_path: Path,
) -> None:
    path, bundle = _evidence(tmp_path, 3)
    bundle = parse_evidence_bundle(path.read_bytes())
    event_ids = [str(e["evidence_id"]) for e in bundle.events]
    source_a = SummaryClaim(
        claim_id="a" * 64, kind="implementation",
        repository="upiscium/Test", summary="first",
        evidence_ids=(event_ids[0], event_ids[1]),
    )
    source_b = SummaryClaim(
        claim_id="b" * 64, kind="implementation",
        repository="upiscium/Test", summary="second",
        evidence_ids=(event_ids[1], event_ids[2]),
    )
    context = SummaryContext(
        stage=REDUCE_STAGE,
        evidence_bundle_sha256=bundle.sha256,
        batch_index=0, batch_count=1,
        source_output_sha256s=(),
        claims=(source_a, source_b),
    )
    output = _parse_model_claim_output(
        json.dumps({
            "claims": [{
                "kind": "implementation",
                "repository": "upiscium/Test",
                "summary": "combined",
                "source_refs": [0, 1],
            }]
        }).encode(),
        context=context,
        input_context_sha256="c" * 64,
        events_by_id=bundle.events_by_id,
    )
    assert output.claims[0].evidence_ids == tuple(event_ids)


def test_model_ground_short_refs_require_exact_claim_set(
    tmp_path: Path,
) -> None:
    path, bundle = _evidence(tmp_path, 2)
    bundle = parse_evidence_bundle(path.read_bytes())
    event_ids = [str(e["evidence_id"]) for e in bundle.events]
    claims = (
        SummaryClaim(
            claim_id="a" * 64, kind="implementation",
            repository="upiscium/Test", summary="first",
            evidence_ids=(event_ids[0],),
        ),
        SummaryClaim(
            claim_id="b" * 64, kind="implementation",
            repository="upiscium/Test", summary="second",
            evidence_ids=(event_ids[1],),
        ),
    )
    context = SummaryContext(
        stage=GROUND_STAGE,
        evidence_bundle_sha256=bundle.sha256,
        batch_index=0, batch_count=1,
        source_output_sha256s=(),
        claims=claims,
    )
    valid = _parse_model_ground_output(
        json.dumps({
            "assessments": [
                {"claim_ref": 1, "verdict": "unsupported", "reason": "overstated"},
                {"claim_ref": 0, "verdict": "supported", "reason": "grounded"},
            ]
        }).encode(),
        context=context,
        input_context_sha256="c" * 64,
    )
    assert [a.claim_id for a in valid.assessments] == [
        claims[0].claim_id, claims[1].claim_id,
    ]
    for refs in ([0, 0], [1], [0, True], [0, 4]):
        with pytest.raises(GitHubDailySummaryError):
            _parse_model_ground_output(
                json.dumps({
                    "assessments": [
                        {"claim_ref": r, "verdict": "supported", "reason": "test"}
                        for r in refs
                    ]
                }).encode(),
                context=context,
                input_context_sha256="c" * 64,
            )
