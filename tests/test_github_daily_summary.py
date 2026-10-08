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
    ClaimOutput,
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
        raise AssertionError("deterministic reducer must not call the model")
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
    # Deterministic reducer emits content-addressed outputs but no fake
    # model inference provenance. Only real Partial + Ground calls are counted.
    assert len(result.provenance_sha256s) == (
        len(result.partial_context_sha256s)
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
                "summary": "implemented changes",
                "source_refs": [2, 0],
            }]
        }).encode(),
        context=context,
        input_context_sha256="a" * 64,
        events_by_id=events_by_id,
    )
    assert output.claims[0].repository == "upiscium/Test"
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
                "summary": "combined",
                "source_refs": [0, 1],
            }]
        }).encode(),
        context=context,
        input_context_sha256="c" * 64,
        events_by_id=bundle.events_by_id,
    )
    assert output.claims[0].repository == "upiscium/Test"
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


def _multi_repo_evidence(tmp_path: Path, count: int = 16) -> Path:
    start, _ = _date_window(date(2026, 10, 5))
    message = _bounded_text("implemented change", limit=4096, label="message")
    assert message is not None
    events = []
    for index in range(count):
        repository = (
            "upiscium/Alpha" if index % 2 == 0 else "upiscium/Beta"
        )
        events.append(
            _make_event(
                kind="default_branch_commit",
                repository=repository,
                occurred_at=start + timedelta(seconds=index),
                url=(
                    f"https://github.com/{repository}/commit/"
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
    projects = [
        ProjectBinding(
            project_path=f"10-Project/{name}/{name}.md",
            repository=f"upiscium/{name}",
        )
        for name in ("Alpha", "Beta")
    ]
    bundle = make_daily_evidence_bundle(
        target_date=date(2026, 10, 5),
        projects=projects,
        events=events,
    )
    path = tmp_path / f"{bundle.sha256}.github-daily-evidence.json"
    path.write_bytes(bundle.canonical_bytes)
    return path


def test_interleaved_repositories_make_isolated_bounded_lossless_partials(
    tmp_path: Path,
) -> None:
    bundle = parse_evidence_bundle(
        _multi_repo_evidence(tmp_path).read_bytes()
    )
    contexts = partition_evidence(bundle, max_bytes=2400)
    assert len(contexts) >= 2
    assert all(len(c.to_json_bytes()) <= 2400 for c in contexts)
    assert all(
        len({event["repository"] for event in c.events}) == 1
        for c in contexts
    )
    observed = [
        event["evidence_id"]
        for context in contexts
        for event in context.events
    ]
    expected = [event["evidence_id"] for event in bundle.events]
    assert len(observed) == len(expected)
    assert set(observed) == set(expected)
    for repository in bundle.repositories:
        assert [
            event["evidence_id"]
            for context in contexts
            for event in context.events
            if event["repository"] == repository
        ] == [
            event["evidence_id"]
            for event in bundle.events
            if event["repository"] == repository
        ]


def test_reducer_contexts_are_single_repository_and_lossless(
    tmp_path: Path,
) -> None:
    bundle = parse_evidence_bundle(
        _multi_repo_evidence(tmp_path).read_bytes()
    )
    claims = tuple(
        SummaryClaim(
            claim_id=f"{i + 1:064x}",
            kind="implementation",
            repository=str(event["repository"]),
            summary=f"implemented event {i}",
            evidence_ids=(str(event["evidence_id"]),),
        )
        for i, event in enumerate(bundle.events)
    )
    outputs = [
        StoredClaimOutput(
            "c" * 64,
            ClaimOutput(
                stage=PARTIAL_STAGE,
                input_context_sha256="a" * 64,
                claims=claims,
            ),
        )
    ]
    contexts = build_reduce_contexts(
        bundle,
        outputs,
        max_bytes=2200,
    )
    assert len(contexts) >= 2
    assert all(len(c.to_json_bytes()) <= 2200 for c in contexts)
    assert all(
        len({claim.repository for claim in c.claims}) == 1
        for c in contexts
    )
    observed = [
        claim.claim_id for context in contexts for claim in context.claims
    ]
    assert set(observed) == {claim.claim_id for claim in claims}
    assert len(observed) == len(claims)


def test_model_source_rejects_cross_repository_citations(
    tmp_path: Path,
) -> None:
    bundle = parse_evidence_bundle(
        _multi_repo_evidence(tmp_path, 2).read_bytes()
    )
    # Malicious/invalid fabricated context: the real partitioner never emits it.
    mixed = SummaryContext(
        stage=PARTIAL_STAGE,
        evidence_bundle_sha256=bundle.sha256,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=(),
        events=bundle.events,
    )
    with pytest.raises(GitHubDailySummaryError, match="span repositories"):
        _parse_model_claim_output(
            json.dumps({
                "claims": [{
                    "kind": "implementation",
                    "summary": "unrelated projects merged",
                    "source_refs": [0, 1],
                }]
            }).encode(),
            context=mixed,
            input_context_sha256="a" * 64,
            events_by_id=bundle.events_by_id,
        )


def test_model_rejects_unrequested_repository_property(
    tmp_path: Path,
) -> None:
    path, _ = _evidence(tmp_path, 2)
    bundle = parse_evidence_bundle(path.read_bytes())
    context = partition_evidence(bundle)[0]
    with pytest.raises(
        GitHubDailySummaryError,
        match="properties do not match contract",
    ):
        _parse_model_claim_output(
            json.dumps({
                "claims": [{
                    "kind": "implementation",
                    "repository": "upiscium/Other",
                    "summary": "invalid repo override",
                    "source_refs": [0],
                }]
            }).encode(),
            context=context,
            input_context_sha256="a" * 64,
            events_by_id=bundle.events_by_id,
        )


def test_full_pipeline_multi_repo_keeps_exact_repository_evidence(
    tmp_path: Path,
) -> None:
    evidence_path = _multi_repo_evidence(tmp_path, 12)
    bundle = parse_evidence_bundle(evidence_path.read_bytes())
    state = tmp_path / "multi-summary-state"
    state.mkdir()
    result = run_pipeline(
        evidence_path=evidence_path,
        state_root=state,
        infer=_fake_infer,
        implementation_revision="3" * 40,
        partial_context_bytes=3200,
        reduce_context_bytes=3200,
        ground_context_bytes=5000,
    )
    assert len(result.partial_context_sha256s) >= 2
    assert result.claim_count == 11
    assert result.rejected_count == 1
    final = json.loads(result.grounded_summary_path.read_bytes())
    for claim in final["claims"]:
        assert {
            bundle.events_by_id[evidence_id]["repository"]
            for evidence_id in claim["evidence_ids"]
        } == {claim["repository"]}


def test_reducer_rejects_source_claim_repository_provenance_mismatch(
    tmp_path: Path,
) -> None:
    bundle = parse_evidence_bundle(
        _multi_repo_evidence(tmp_path, 2).read_bytes()
    )
    alpha = next(
        event for event in bundle.events
        if event["repository"] == "upiscium/Alpha"
    )
    invalid_claim = SummaryClaim(
        claim_id="a" * 64,
        kind="implementation",
        repository="upiscium/Beta",
        summary="inconsistent source claim",
        evidence_ids=(str(alpha["evidence_id"]),),
    )
    context = SummaryContext(
        stage=REDUCE_STAGE,
        evidence_bundle_sha256=bundle.sha256,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=("b" * 64,),
        claims=(invalid_claim,),
    )
    with pytest.raises(
        GitHubDailySummaryError,
        match="reducer source repository does not match cited evidence",
    ):
        _parse_model_claim_output(
            json.dumps({
                "claims": [{
                    "kind": "implementation",
                    "summary": "inconsistent source claim",
                    "source_refs": [0],
                }]
            }).encode(),
            context=context,
            input_context_sha256="c" * 64,
            events_by_id=bundle.events_by_id,
        )


def test_model_claim_schema_does_not_request_repository_name() -> None:
    for stage in (PARTIAL_STAGE, REDUCE_STAGE):
        schema = prompt_spec(stage).output_schema
        claim = schema["properties"]["claims"]["items"]
        assert claim["required"] == ["kind", "summary", "source_refs"]
        assert "repository" not in claim["properties"]
        assert claim["properties"]["source_refs"]["items"]["type"] == "integer"


def _partial_claims_for_events(
    bundle,
    evidence_positions: list[tuple[int, ...]],
) -> list[StoredClaimOutput]:
    claims = tuple(
        SummaryClaim(
            claim_id=f"{index + 1:064x}",
            kind="implementation",
            repository="upiscium/Test",
            summary=f"source claim {index}",
            evidence_ids=tuple(
                str(bundle.events[position]["evidence_id"])
                for position in positions
            ),
        )
        for index, positions in enumerate(evidence_positions)
    )
    return [
        StoredClaimOutput(
            "b" * 64,
            ClaimOutput(
                stage=PARTIAL_STAGE,
                input_context_sha256="a" * 64,
                claims=claims,
            ),
        )
    ]


def test_reducer_batch_evidence_union_is_bounded_and_lossless(
    tmp_path: Path,
) -> None:
    path, _ = _evidence(tmp_path, 20)
    bundle = parse_evidence_bundle(path.read_bytes())
    outputs = _partial_claims_for_events(
        bundle, [(index,) for index in range(20)]
    )
    contexts = build_reduce_contexts(
        bundle,
        outputs,
        max_bytes=64 * 1024,
    )

    assert len(contexts) == 3
    assert [len(context.claims) for context in contexts] == [8, 8, 4]
    assert [context.batch_index for context in contexts] == [0, 1, 2]
    assert all(context.batch_count == 3 for context in contexts)
    assert all(context.source_output_sha256s == ("b" * 64,) for context in contexts)
    assert all(len(context.to_json_bytes()) <= 64 * 1024 for context in contexts)
    assert all(
        len({
            eid for claim in context.claims for eid in claim.evidence_ids
        }) <= 8
        for context in contexts
    )
    assert [
        claim.claim_id for context in contexts for claim in context.claims
    ] == [
        claim.claim_id for claim in outputs[0].output.claims
    ]


def test_reducer_evidence_budget_counts_unique_overlap_not_claims(
    tmp_path: Path,
) -> None:
    path, _ = _evidence(tmp_path, 9)
    bundle = parse_evidence_bundle(path.read_bytes())
    outputs = _partial_claims_for_events(
        bundle,
        [(0, 1), (1, 2), (2, 3), (4, 5), (6, 7), (7, 8)],
    )
    contexts = build_reduce_contexts(
        bundle, outputs, max_bytes=64 * 1024,
    )
    assert [len(context.claims) for context in contexts] == [5, 1]
    assert [
        len({
            eid for claim in context.claims for eid in claim.evidence_ids
        })
        for context in contexts
    ] == [8, 2]


def test_reducer_handles_max_sized_source_and_rejects_oversized_source(
    tmp_path: Path,
) -> None:
    path, _ = _evidence(tmp_path, 9)
    bundle = parse_evidence_bundle(path.read_bytes())
    valid = _partial_claims_for_events(
        bundle, [tuple(range(8)), (8,)]
    )
    contexts = build_reduce_contexts(
        bundle, valid, max_bytes=64 * 1024,
    )
    assert [len(context.claims) for context in contexts] == [1, 1]
    assert [
        len({
            eid for claim in context.claims for eid in claim.evidence_ids
        })
        for context in contexts
    ] == [8, 1]

    oversized = _partial_claims_for_events(
        bundle, [tuple(range(9))]
    )
    with pytest.raises(
        GitHubDailySummaryError,
        match="reducer source claim evidence count is invalid",
    ):
        build_reduce_contexts(
            bundle, oversized, max_bytes=64 * 1024,
        )


def test_deterministic_reducer_preserves_source_evidence_budget(
    tmp_path: Path,
) -> None:
    evidence_path, _ = _evidence(tmp_path, 20)
    bundle = parse_evidence_bundle(evidence_path.read_bytes())
    state = tmp_path / "bounded-reducer-state"
    state.mkdir()

    def source_only_model(prompt, context):
        assert prompt.stage != REDUCE_STAGE
        return _fake_infer(prompt, context)

    result = run_pipeline(
        evidence_path=evidence_path,
        state_root=state,
        infer=source_only_model,
        implementation_revision="4" * 40,
        partial_context_bytes=64 * 1024,
        reduce_context_bytes=64 * 1024,
        ground_context_bytes=256 * 1024,
    )
    assert len(result.reduce_context_sha256s) == 3
    assert result.claim_count == 19
    assert result.rejected_count == 1
    final = json.loads(result.grounded_summary_path.read_bytes())
    assert len(final["claims"]) == 19
    observed = [
        evidence_id
        for claim in final["claims"]
        for evidence_id in claim["evidence_ids"]
    ]
    rejected = [
        claim for claim in final["rejected_claims"]
    ]
    assert len(observed) == 19
    assert len(rejected) == 1
    assert set(observed) <= {
        str(event["evidence_id"]) for event in bundle.events
    }
    assert all(len(claim["evidence_ids"]) <= 8 for claim in final["claims"])
    assert len(result.provenance_sha256s) == (
        len(result.partial_context_sha256s)
        + len(result.ground_context_sha256s)
    )


def test_reducer_limits_source_count_with_reused_evidence(
    tmp_path: Path,
) -> None:
    path, _ = _evidence(tmp_path, 1)
    bundle = parse_evidence_bundle(path.read_bytes())
    outputs = _partial_claims_for_events(
        bundle, [(0,)] * 20,
    )
    contexts = build_reduce_contexts(
        bundle, outputs, max_bytes=64 * 1024,
    )

    assert [len(context.claims) for context in contexts] == [8, 8, 4]
    assert all(
        len({
            eid for claim in context.claims for eid in claim.evidence_ids
        }) == 1
        for context in contexts
    )
    assert [
        claim.claim_id for context in contexts for claim in context.claims
    ] == [
        claim.claim_id for claim in outputs[0].output.claims
    ]


def test_deterministic_reduce_preserves_claim_id_and_evidence(
    tmp_path: Path,
) -> None:
    from obsidian_automation.github_daily_summary import (
        _run_deterministic_reduce_stage,
    )

    evidence_path, _ = _evidence(tmp_path, 3)
    bundle = parse_evidence_bundle(evidence_path.read_bytes())
    events = bundle.events
    source = SummaryClaim(
        claim_id="0" * 64,
        kind="implementation",
        repository="upiscium/Test",
        summary="implementation verified",
        evidence_ids=(str(events[0]["evidence_id"]),),
    )
    # The SHA binding is not optional: an invented claim_id cannot be copied.
    context = SummaryContext(
        stage=REDUCE_STAGE,
        evidence_bundle_sha256=bundle.sha256,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=("b" * 64,),
        claims=(source,),
    )
    state = tmp_path / "deterministic-reduce"
    state.mkdir()
    with pytest.raises(
        GitHubDailySummaryError, match="source claim identity mismatch"
    ):
        _run_deterministic_reduce_stage(state, bundle, (context,))

    from obsidian_automation.github_daily_summary import (
        _normalized_claim,
    )
    canonical = _normalized_claim(
        kind=source.kind,
        repository=source.repository,
        summary=source.summary,
        evidence_ids=list(source.evidence_ids),
        allowed_evidence_ids=set(bundle.events_by_id),
        events_by_id=bundle.events_by_id,
    )
    valid_context = SummaryContext(
        stage=REDUCE_STAGE,
        evidence_bundle_sha256=bundle.sha256,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=("b" * 64,),
        claims=(canonical,),
    )
    stored, context_shas = _run_deterministic_reduce_stage(
        state, bundle, (valid_context,)
    )
    assert len(stored) == len(context_shas) == 1
    assert stored[0].output.claims == (canonical,)
    assert stored[0].output.claims[0].evidence_ids == source.evidence_ids
    assert stored[0].output.input_context_sha256 == context_shas[0]
    assert stored[0].output.stage == REDUCE_STAGE
    output_path = (
        state / "github-daily-summary" / "output"
        / f"{stored[0].sha256}.github-daily-reduce-output.json"
    )
    assert output_path.is_file()
    assert json.loads(output_path.read_bytes())["claims"][0][
        "claim_id"
    ] == canonical.claim_id

    # Re-execution with identical inputs must not manufacture different
    # output identities or inference provenance.
    again, repeat_shas = _run_deterministic_reduce_stage(
        state, bundle, (valid_context,)
    )
    assert again == stored
    assert repeat_shas == context_shas


def test_deterministic_reduce_rejects_wrong_bundle_binding(
    tmp_path: Path,
) -> None:
    from obsidian_automation.github_daily_summary import (
        _run_deterministic_reduce_stage,
    )
    evidence_path, _ = _evidence(tmp_path, 1)
    bundle = parse_evidence_bundle(evidence_path.read_bytes())
    context = SummaryContext(
        stage=REDUCE_STAGE,
        evidence_bundle_sha256="f" * 64,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=(),
        claims=(),
    )
    state = tmp_path / "deterministic-boundary"
    state.mkdir()
    with pytest.raises(
        GitHubDailySummaryError, match="evidence binding mismatch"
    ):
        _run_deterministic_reduce_stage(state, bundle, (context,))


def test_deterministic_reduce_rejects_cross_repository_source_claim(
    tmp_path: Path,
) -> None:
    from obsidian_automation.github_daily_summary import (
        _run_deterministic_reduce_stage,
    )
    evidence_path = _multi_repo_evidence(tmp_path, 2)
    bundle = parse_evidence_bundle(evidence_path.read_bytes())
    alpha = next(
        row for row in bundle.events
        if row["repository"] == "upiscium/Alpha"
    )
    source = SummaryClaim(
        claim_id="a" * 64,
        kind="implementation",
        repository="upiscium/Beta",
        summary="invalid repo",
        evidence_ids=(str(alpha["evidence_id"]),),
    )
    context = SummaryContext(
        stage=REDUCE_STAGE,
        evidence_bundle_sha256=bundle.sha256,
        batch_index=0,
        batch_count=1,
        source_output_sha256s=("b" * 64,),
        claims=(source,),
    )
    state = tmp_path / "deterministic-repository"
    state.mkdir()
    with pytest.raises(
        GitHubDailySummaryError, match="repository does not match cited evidence"
    ):
        _run_deterministic_reduce_stage(state, bundle, (context,))


def test_pipeline_without_reducer_inference_preserves_all_supported_claims(
    tmp_path: Path,
) -> None:
    evidence_path = _multi_repo_evidence(tmp_path, 16)
    bundle = parse_evidence_bundle(evidence_path.read_bytes())
    state = tmp_path / "deterministic-pipeline"
    state.mkdir()

    def no_reducer_model(prompt, context):
        assert prompt.stage != REDUCE_STAGE
        return _fake_infer(prompt, context)

    result = run_pipeline(
        evidence_path=evidence_path,
        state_root=state,
        infer=no_reducer_model,
        implementation_revision="5" * 40,
        partial_context_bytes=3200,
        reduce_context_bytes=3200,
        ground_context_bytes=6000,
    )
    assert result.reduce_context_sha256s
    assert len(result.reduce_output_sha256s) == len(
        result.reduce_context_sha256s
    )
    assert len(result.provenance_sha256s) == (
        len(result.partial_context_sha256s)
        + len(result.ground_context_sha256s)
    )
    final = json.loads(result.grounded_summary_path.read_bytes())
    assert len(final["claims"]) == 15
    assert len(final["rejected_claims"]) == 1
    assert all(
        claim["claim_id"]
        for claim in final["claims"]
    )
    for claim in final["claims"]:
        assert {
            bundle.events_by_id[eid]["repository"]
            for eid in claim["evidence_ids"]
        } == {claim["repository"]}
