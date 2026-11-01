"""B3 exact candidate readback consistency, independent of source provenance."""

from dataclasses import replace
import hashlib
import json

import pytest

from obsidian_automation.github_b3_readback_candidate import (
    InvalidCandidate,
    MAX_CONTENT_BYTES,
    RunContext,
    StageReadback,
    VERSION,
    evaluate_b3_readbacks,
)

RUN_ID, BINDING, REVISION = "a" * 32, "b" * 32, "c" * 32
STATUS_PROPOSAL, OVERVIEW_PROPOSAL = "d" * 64, "e" * 64
PREVIOUS = hashlib.sha256(b"old content").hexdigest()
AFTER = hashlib.sha256(b"new content").hexdigest()
NOW = 1750000300.0
STARTED = NOW - 30.0


def _context(**kwargs):
    return replace(RunContext(RUN_ID, BINDING, REVISION, STARTED), **kwargs)


def _stage(
    stage, *, outcome=None, before=PREVIOUS, expected=b"new content",
    observed=b"new content", **kwargs,
):
    item = StageReadback(
        stage=stage,
        run_id=RUN_ID,
        binding_ref=BINDING,
        expected_revision=REVISION,
        proposal_sha256=STATUS_PROPOSAL if stage == "status" else OVERVIEW_PROPOSAL,
        outcome=outcome or ("applied" if stage == "status" else "updated"),
        before_sha256=before,
        expected_bytes=expected,
        observed_bytes=observed,
        completed_at=STARTED + 10,
    )
    return replace(item, **kwargs)


def _evaluate(items=None, *, ctx=None, at=NOW):
    if items is None:
        items = (_stage("status"), _stage("overview"))
    return evaluate_b3_readbacks(ctx or _context(), items, now=at)


def _unknown(output, reason):
    assert output["consistency"] == "unknown"
    assert output["reason"] == reason
    assert output["last_verified_pipeline_success"] is None
    assert output["overall_health"] == "unknown"
    assert output["active_incident"] == "unknown"
    assert output["candidate_outcomes"] == {"status": None, "overview": None}
    assert output["candidate_changed"] is None


def test_both_stages_are_consistent_candidates_but_never_pipeline_success():
    output = _evaluate()
    assert output["schema_version"] == VERSION
    assert output["consistency"] == "candidate_consistent"
    assert output["evidence_scope"] == "private_b3_writer_candidate_only"
    assert output["reason"] is None
    assert output["candidate_outcomes"] == {
        "status": "applied", "overview": "updated",
    }
    assert output["candidate_changed"] is True
    assert output["last_verified_pipeline_success"] is None
    assert output["overall_health"] == "unknown"
    assert output["active_incident"] == "unknown"
    assert _evaluate(tuple(reversed((_stage("status"), _stage("overview"))))) == output


def test_both_noops_are_consistent_candidates_without_vault_claim():
    items = (
        _stage("status", outcome="already_desired", before=AFTER),
        _stage("overview", outcome="already_desired", before=AFTER),
    )
    output = _evaluate(items)
    assert output["consistency"] == "candidate_consistent"
    assert output["candidate_changed"] is False
    assert output["last_verified_pipeline_success"] is None


def test_one_subeffect_missing_is_unknown_not_success():
    _unknown(_evaluate([]), "missing")
    _unknown(_evaluate([_stage("status")]), "incomplete")
    _unknown(_evaluate([_stage("overview")]), "incomplete")


def test_duplicate_or_extra_stage_never_approves():
    _unknown(_evaluate([_stage("status"), _stage("status")]), "invalid")
    _unknown(_evaluate([_stage("overview"), _stage("overview")]), "invalid")
    _unknown(_evaluate([_stage("status"), _stage("overview"), _stage("status")]), "invalid")


def test_unverified_readback_does_not_mark_any_stage_consistent():
    mismatched = _stage("overview", observed=b"remote diverged")
    _unknown(_evaluate([_stage("status"), mismatched]), "invalid")


def test_writer_queued_or_http_ack_is_not_candidate_proof():
    for value in ("queued", "http_200", "pending", "rejected", "failed", "success"):
        _unknown(
            _evaluate([
                _stage("status", outcome=value), _stage("overview"),
            ]), "invalid",
        )


@pytest.mark.parametrize("field,wrong", (
    ("run_id", "f" * 32),
    ("binding_ref", "f" * 32),
    ("expected_revision", "f" * 32),
    ("proposal_sha256", "x" * 64),
    ("proposal_sha256", "../private"),
))
def test_wrong_run_binding_revision_or_proposal_never_joins(field, wrong):
    _unknown(
        _evaluate([
            _stage("status"), _stage("overview", **{field: wrong}),
        ]), "invalid",
    )


@pytest.mark.parametrize("bad", (
    {"outcome": 5},
    {"outcome": "created"},
    {"outcome": "updated"},
    {"before_sha256": None},
    {"before_sha256": True},
    {"before_sha256": "x"},
    {"before_sha256": AFTER},
    {"expected_bytes": "not bytes"},
    {"expected_bytes": b""},
    {"observed_bytes": bytearray(b"new content")},
    {"observed_bytes": b"x" * (MAX_CONTENT_BYTES + 1)},
    {"completed_at": float("nan")},
    {"completed_at": -1},
    {"completed_at": STARTED - 1},
    {"completed_at": NOW + 301},
))
def test_status_malformed_or_unbound_rejected(bad):
    _unknown(_evaluate([_stage("status", **bad), _stage("overview")]), "invalid")


def test_unknown_stage_is_invalid():
    _unknown(_evaluate([
        replace(_stage("status"), stage="other"), _stage("overview"),
    ]), "invalid")


@pytest.mark.parametrize("bad", (
    {"outcome": "applied"},
    {"outcome": "updated", "before_sha256": None},
    {"outcome": "created", "before_sha256": PREVIOUS},
    {"outcome": "created", "before_sha256": AFTER},
    {"outcome": "already_desired", "before_sha256": PREVIOUS},
    {"outcome": "recovered", "before_sha256": AFTER},
))
def test_overview_invalid_outcome_and_change_relationships(bad):
    _unknown(_evaluate([_stage("status"), _stage("overview", **bad)]), "invalid")


def test_overview_created_and_ambiguous_recovered_can_be_consistent():
    created = _evaluate([_stage("status"), _stage("overview", outcome="created", before=None)])
    assert created["consistency"] == "candidate_consistent"
    assert created["last_verified_pipeline_success"] is None
    recovered = _evaluate([
        _stage("status", outcome="recovered"),
        _stage("overview", outcome="recovered", before=None),
    ])
    assert recovered["consistency"] == "candidate_consistent"


def test_inconsistent_status_noop_is_invalid():
    _unknown(_evaluate([
        _stage("status", outcome="already_desired", before=PREVIOUS),
        _stage("overview"),
    ]), "invalid")


def test_readback_too_large_fails_before_any_hashing():
    huge = b"x" * (MAX_CONTENT_BYTES + 1)
    _unknown(_evaluate([
        _stage("status", expected=huge, observed=huge),
        _stage("overview"),
    ]), "invalid")


@pytest.mark.parametrize("bad", (
    None, {}, "string", tuple(), object(),
))
def test_non_records_are_not_success(bad):
    _unknown(_evaluate(bad if bad is not None else []), "invalid" if bad not in (None, tuple()) else "missing")


@pytest.mark.parametrize("bad", (
    {"run_id": "X" * 32},
    {"binding_ref": "invalid"},
    {"expected_revision": "invalid"},
    {"started_at": float("nan")},
    {"started_at": -1},
    {"state": "queued"},
    {"state": "completed"},
    {"state": 1},
))
def test_bad_run_context_fails_closed_explicitly(bad):
    with pytest.raises(InvalidCandidate, match="invalid_context"):
        _evaluate(ctx=_context(**bad))


@pytest.mark.parametrize("bad", (float("nan"), float("inf"), True, -1, "now"))
def test_invalid_now_fails_closed(bad):
    with pytest.raises(InvalidCandidate, match="invalid_context"):
        _evaluate(at=bad)


def test_unconfirmed_attempt_can_be_correlated_but_not_reconciled_by_pure_gate():
    evidence = (_stage("status"), _stage("overview"))
    output = _evaluate(evidence, ctx=_context(state="unconfirmed"))
    assert output["consistency"] == "candidate_consistent"
    assert output["last_verified_pipeline_success"] is None
    assert output["overall_health"] == "unknown"


def test_project_names_content_paths_and_hashes_never_enter_result():
    output = _evaluate([
        _stage("status", expected=b"private text", observed=b"private text"),
        _stage("overview", outcome="created", before=None),
    ])
    # Status with actual changed bytes is internally consistent, never
    # a provenance proof. Do not expose expected, observed or private handles.
    public = json.dumps(output)
    for secret in (RUN_ID, BINDING, REVISION, STATUS_PROPOSAL,
                   OVERVIEW_PROPOSAL, PREVIOUS, "private text", "new content",
                   "10-Project", "/var/lib"):
        assert secret not in public


def test_v2_durable_attempt_joins_without_modifying_or_completing_it(tmp_path):
    from obsidian_automation.github_b3_attempts import B3AttemptJournal

    root = tmp_path / "private-b3"
    root.mkdir(mode=0o700)
    with B3AttemptJournal(root, clock=lambda: STARTED, initialize=True) as ledger:
        decision, entry = ledger.admit(RUN_ID, BINDING, REVISION)
        assert decision == "accepted"
        # The test's private admitted run id is authoritative for this
        # synthetic context; it is not inferred from a browser input.
        started = ledger.start(entry["run_id"])
        assert started["state"] == "running"
        context = RunContext(entry["run_id"], BINDING, REVISION, STARTED)
        evidence = [
            _stage("status", run_id=entry["run_id"]),
            _stage("overview", run_id=entry["run_id"]),
        ]
        assessment = evaluate_b3_readbacks(context, evidence, now=NOW)
        assert assessment["consistency"] == "candidate_consistent"
        assert assessment["last_verified_pipeline_success"] is None
        assert ledger.lookup(entry["run_id"])["state"] == "running"
        assert ledger.snapshot()["last_verified_pipeline_success"] is None



class _ForgedEqual:
    """Untrusted identity field must not override an exact string compare."""

    def __eq__(self, _other):
        return True

    def __ne__(self, _other):
        return False


class _BadCompare:
    def __ne__(self, _other):
        raise AssertionError("untrusted comparison method executed")


@pytest.mark.parametrize("field", ("run_id", "binding_ref", "expected_revision"))
def test_spoofed_object_comparisons_cannot_bypass_identity(field):
    wrong = replace(_stage("status"), **{field: _ForgedEqual()})
    _unknown(_evaluate([wrong, _stage("overview")]), "invalid")


@pytest.mark.parametrize("field", ("run_id", "binding_ref", "expected_revision"))
def test_untrusted_identity_object_methods_are_not_called(field):
    wrong = replace(_stage("overview"), **{field: _BadCompare()})
    _unknown(_evaluate([_stage("status"), wrong]), "invalid")


def test_ambiguous_overview_recovery_cannot_imply_change_without_before_hash():
    output = _evaluate([
        _stage("status", outcome="already_desired", before=AFTER),
        _stage("overview", outcome="recovered", before=None),
    ])
    assert output["consistency"] == "candidate_consistent"
    assert output["candidate_changed"] is None
    assert output["last_verified_pipeline_success"] is None


def test_one_certain_change_is_enough_even_if_other_stage_is_ambiguous():
    output = _evaluate([
        _stage("status", outcome="applied", before=PREVIOUS),
        _stage("overview", outcome="recovered", before=None),
    ])
    assert output["consistency"] == "candidate_consistent"
    assert output["candidate_changed"] is True


def test_proposal_digest_is_not_the_markdown_digest():
    candidate = _stage("status")
    assert candidate.proposal_sha256 != hashlib.sha256(candidate.expected_bytes).hexdigest()
    # Proposal digest belongs to the producer's canonical PROPOSAL, not to
    # expected/readback Markdown bytes; its run binding is a future I/O duty.
    assert _evaluate()["consistency"] == "candidate_consistent"
