"""Metadata-only citation versus semantic omission adversarial fixtures (#290)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from obsidian_automation.github_daily_coverage_eval import (
    Adjudication, BatchedClaim, CoverageError, CoverageInput,
    OriginalEvent, SupportedSingleClaim, evaluate_coverage,
)

def h(letter: str) -> str:
    return letter * 64


def fixture() -> CoverageInput:
    events = tuple(OriginalEvent(h(str(i)), "upiscium/Research") for i in range(1, 8))
    # All seven original events retained, only five cited: structure != semantics.
    return CoverageInput(
        evidence_bundle_sha256=h("a"),
        partition_sha256=h("b"),
        batch_output_sha256=h("c"),
        original_events=events,
        partitioned_ids=(tuple(x.evidence_id for x in events[:3]),
                         tuple(x.evidence_id for x in events[3:])),
        batched_claims=(
            BatchedClaim(h("d"), "upiscium/Research",
                         tuple(x.evidence_id for x in events[:3])),
            BatchedClaim(h("e"), "upiscium/Research",
                         tuple(x.evidence_id for x in events[3:5])),
        ),
        supported_single_claims=(
            SupportedSingleClaim(h("f"), "upiscium/Research", events[5].evidence_id, h("a")),
            SupportedSingleClaim(h("0"), "upiscium/Research", events[6].evidence_id, h("b")),
        ),
        adjudications=(),
    )


def test_uncited_claims_do_not_automatically_mean_missing_facts():
    report = evaluate_coverage(fixture())
    assert report["original_event_count"] == 7
    assert report["partitioned_event_count"] == 7
    assert report["directly_cited_event_count"] == 5
    assert report["uncited_event_count"] == 2
    assert report["supported_uncited_single_claim_count"] == 2
    assert report["human_semantic_counts"]["unreviewed"] == 2
    assert report["sample_status"] == "insufficient_human_review"
    assert report["publication_authorized"] is False


def test_model_judgment_is_advisory_and_cannot_satisfy_human_gate():
    original = fixture()
    observed = replace(original, adjudications=(
        Adjudication(h("f"), "model", "entailed"),
        Adjudication(h("0"), "model", "missing"),
    ))
    result = evaluate_coverage(observed)
    assert result["model_advisory_missing_count"] == 1
    assert result["human_semantic_counts"]["unreviewed"] == 2
    assert result["sample_status"] == "insufficient_human_review"


def test_human_missing_flagged_but_human_entailed_is_not_loss():
    f = fixture()
    result = evaluate_coverage(replace(f, adjudications=(
        Adjudication(h("f"), "human", "entailed"),
        Adjudication(h("0"), "human", "missing"),
        Adjudication(h("0"), "model", "entailed"),  # Model cannot override human.
    )))
    assert result["human_semantic_counts"] == {
        "entailed": 1, "missing": 1, "uncertain": 0, "unreviewed": 0
    }
    assert result["sample_status"] == "human_confirmed_omission"
    assert not result["publication_authorized"]


def test_no_omission_in_reviewed_sample_is_not_publication_authorization():
    f = fixture()
    result = evaluate_coverage(replace(f, adjudications=(
        Adjudication(h("f"), "human", "entailed"),
        Adjudication(h("0"), "human", "entailed"),
    )))
    assert result["sample_status"] == "no_omission_in_reviewed_sample"
    assert result["publication_authorized"] is False


@pytest.mark.parametrize("change", (
    lambda x: replace(x, partitioned_ids=(x.partitioned_ids[0], x.partitioned_ids[1][:-1])),
    lambda x: replace(x, partitioned_ids=(x.partitioned_ids[0], x.partitioned_ids[0], x.partitioned_ids[1])),
    lambda x: replace(x, partitioned_ids=(x.partitioned_ids[1], x.partitioned_ids[0])),
    lambda x: replace(x, batched_claims=(BatchedClaim(h("d"), "upiscium/Research", (h("9"),)),)),
    lambda x: replace(x, batched_claims=(BatchedClaim(h("d"), "upiscium/Other", (h("1"),)),)),
    lambda x: replace(x, adjudications=(Adjudication(h("9"), "human", "entailed"),)),
    lambda x: replace(x, adjudications=(Adjudication(h("0"), "human", "entailed"),
                                         Adjudication(h("0"), "human", "missing"))),
    lambda x: replace(x, evidence_bundle_sha256="not-a-sha"),
))
def test_bad_or_stale_provenance_fails_closed(change):
    with pytest.raises(CoverageError):
        evaluate_coverage(change(fixture()))


def test_routine_event_without_single_claim_is_not_forced_into_a_claim():
    f = fixture()
    no_supported = replace(f, supported_single_claims=(), adjudications=())
    report = evaluate_coverage(no_supported)
    assert report["human_semantic_counts"]["unreviewed"] == 0
    assert report["supported_single_claim_count"] == 0
    assert report["uncited_event_count"] == 2
    assert report["sample_status"] == "no_supported_single_claim_baseline"
    assert report["publication_authorized"] is False


def test_diagnostic_does_not_emit_raw_claim_text_or_identifiers():
    result = evaluate_coverage(fixture())
    assert "summary" not in repr(result)
    assert "upiscium/Research" not in repr(result)
    assert h("f") not in repr(result)
