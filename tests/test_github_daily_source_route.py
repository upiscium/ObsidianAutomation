"""No-network v2 collector source-route attribution adversarial fixtures (#328)."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from obsidian_automation.github_daily_source_route import (
    DailySourceRouteError,
    collect_fresh_verified_routes,
    parse_verified_routes,
    _PinnedGitHubClient,
    _NeverRedirect,
)
from obsidian_automation.github_project_watcher import WatcherConfig
from obsidian_automation.github_daily_summary import GitHubDailySummaryError


REPO = "upiscium/Test"
BASE = f"/repos/{REPO}"
DATE = date(2026, 10, 5)
COMMENT_ISSUE = {
    "id": 31,
    "created_at": "2026-10-05T04:00:00Z",
    "issue_url": "https://api.github.com/repos/upiscium/Test/issues/1",
    "html_url": "https://github.com/upiscium/Test/issues/1#issuecomment-31",
    "body": "Implemented the validation for issue one",
    "user": {"login": "human"},
}
COMMENT_PR = {
    "id": 33,
    "created_at": "2026-10-05T04:20:00Z",
    "issue_url": "https://api.github.com/repos/upiscium/Test/issues/2",
    "html_url": "https://github.com/upiscium/Test/issues/2#issuecomment-33",
    "body": "PR two now passes independent review",
    "user": {"login": "human"},
}
PR_SHA = "a" * 40
PR_COMMIT = {
    "sha": PR_SHA,
    "html_url": f"https://github.com/{REPO}/commit/{PR_SHA}",
    "author": {"login": "human"},
    "commit": {
        "message": "fix issue source verification",
        "committer": {"date": "2026-10-05T04:30:00Z"},
    },
}
REVIEW_COMMENT = {
    "id": 51,
    "created_at": "2026-10-05T04:10:00Z",
    "pull_request_url": "https://api.github.com/repos/upiscium/Test/pulls/2",
    "html_url": "https://github.com/upiscium/Test/pull/2#discussion_r51",
    "body": "Change the invalid route classification",
    "user": {"login": "reviewer"},
}


class _FakeClient(_PinnedGitHubClient):
    api_base = "https://api.github.com"
    token = "synthetic-fixture-credential-not-a-real-token"

    def __init__(
        self, *,
        visible_pr: bool = False,
        include_commit: bool = False,
        include_review_comment: bool = False,
    ):
        self.visible_pr = visible_pr
        self.include_commit = include_commit
        self.include_review_comment = include_review_comment
        self.fail_routes: set[str] = set()
        self.responses: dict[str, object] = {
            f"{BASE}/issues/comments/31": deepcopy(COMMENT_ISSUE),
            f"{BASE}/issues/comments/33": deepcopy(COMMENT_PR),
            f"{BASE}/issues/1": {"id": 1001, "number": 1, "html_url": f"https://github.com/{REPO}/issues/1"},
            f"{BASE}/issues/2": {
                "id": 1002, "number": 2, "html_url": f"https://github.com/{REPO}/pull/2",
                "pull_request": {"url": f"https://api.github.com{BASE}/pulls/2"},
            },
            f"{BASE}/pulls/comments/51": deepcopy(REVIEW_COMMENT),
            f"{BASE}/pulls/2/commits?per_page=100&page=1": [deepcopy(PR_COMMIT)],
        }

    def _repo_path(self, repository: str) -> str:
        assert repository == REPO
        return repository

    def _paged(self, path: str) -> list[dict[str, object]]:
        if "/issues?" in path:
            rows = [{
                "number": 1, "title": "Issue one", "body": "Original content",
                "created_at": "2026-10-05T01:00:00Z",
                "updated_at": "2026-10-05T01:00:00Z",
                "html_url": f"https://github.com/{REPO}/issues/1",
                "state": "open", "user": {"login": "human"},
            }]
            if self.visible_pr:
                rows.append({
                    "number": 2, "title": "PR two", "body": "Patch summary",
                    "created_at": "2026-10-05T01:00:00Z",
                    "updated_at": "2026-10-05T01:00:00Z",
                    "html_url": f"https://github.com/{REPO}/pull/2",
                    "state": "open", "user": {"login": "human"},
                    "pull_request": {"url": f"https://api.github.com{BASE}/pulls/2"},
                })
            return rows
        if path.endswith("/issues/1/events") or path.endswith("/issues/2/events"):
            return []
        if "/issues/comments?" in path:
            return [deepcopy(COMMENT_ISSUE), deepcopy(COMMENT_PR)]
        if "/pulls/comments?" in path:
            return [deepcopy(REVIEW_COMMENT)] if self.include_review_comment else []
        if path.endswith("/pulls/2/reviews"):
            return []
        if path.endswith("/pulls/2/commits"):
            return [deepcopy(PR_COMMIT)] if self.include_commit else []
        if "/commits?" in path:
            return []
        raise AssertionError(f"unknown safe paged route: {path}")

    def _request_json(self, path: str):
        if path in self.fail_routes:
            raise DailySourceRouteError("GitHub source temporarily unavailable")
        if path not in self.responses:
            raise AssertionError(f"unapproved API path {path}")
        return deepcopy(self.responses[path])


def _config(tmp_path: Path) -> WatcherConfig:
    vault = tmp_path / "vault"
    project = vault / "10-Project" / "Test" / "Test.md"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        "---\ntype: project\nstatus: running\n"
        "github_repo: upiscium/Test\ngithub_watch: true\n---\n",
        encoding="utf-8",
    )
    return WatcherConfig(vault_root=vault, state_db=tmp_path / "state.sqlite3")


def _collect(tmp_path: Path, **options):
    return collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE, client=_FakeClient(**options),
        explicit_historical_reconfirmation=True,
    )


def test_comment_on_unknown_pr_is_reclassified_by_authoritative_issue_record(
    tmp_path: Path,
) -> None:
    result = _collect(tmp_path)
    assert result.route_count == 2
    evidence_events = result.evidence.events
    kinds = {(event.source_id, event.kind) for event in evidence_events}
    assert ("31", "issue_comment") in kinds
    # A URL under /issues/N does not prove this belongs to an Issue.
    assert ("33", "pull_request_comment") in kinds
    assert ("33", "issue_comment") not in kinds
    records = parse_verified_routes(result.receipt_bytes, result.evidence.canonical_bytes)
    by_id = {record["source_id"]: record for record in records}
    assert by_id["31"]["entity"] == "issue"
    assert by_id["33"]["entity"] == "pull_request"
    assert by_id["33"]["verification_route"] == f"{BASE}/issues/2"
    assert by_id["33"]["remote_id"] == "1002"
    assert result.receipt_sha256
    assert not hasattr(result, "publication_authorized")
    assert all("body" not in record for record in records)


def test_pr_commit_and_review_comment_are_bound_to_source_api_routes(tmp_path: Path) -> None:
    result = _collect(
        tmp_path, visible_pr=True, include_commit=True, include_review_comment=True,
    )
    records = parse_verified_routes(result.receipt_bytes, result.evidence.canonical_bytes)
    assert len(records) == 4
    committed = next(record for record in records if record["kind"] == "pull_request_commit")
    assert committed["number"] == 2
    assert committed["route"] == f"{BASE}/pulls/2/commits"
    assert committed["remote_id"] == PR_SHA
    reviewed = next(record for record in records if record["kind"] == "pull_request_review_comment")
    assert reviewed["route"] == f"{BASE}/pulls/comments/51"
    assert reviewed["verification_route"] == f"{BASE}/pulls/2"


def test_source_route_verification_does_not_modify_stored_v1_evidence(tmp_path: Path) -> None:
    from obsidian_automation.github_daily_activity import collect_daily_evidence
    config = _config(tmp_path)
    before = collect_daily_evidence(config, target_date=DATE, client=_FakeClient())
    after = collect_fresh_verified_routes(config, target_date=DATE, client=_FakeClient(),
        explicit_historical_reconfirmation=True)
    assert any(event.kind == "issue_comment" and event.source_id == "33" for event in before.events)
    assert any(event.kind == "pull_request_comment" and event.source_id == "33" for event in after.evidence.events)
    assert after.evidence.sha256 != before.sha256
    assert all(path.name != "04-AI" for path in tmp_path.iterdir())


@pytest.mark.parametrize("bad", [
    {"id": 999},
    {"body": "changed content"},
    {"created_at": "2026-10-05T05:00:00Z"},
    {"issue_url": "https://evil.example/repos/upiscium/Test/issues/2"},
    {"issue_url": "https://api.github.com/repos/upiscium/Test/issues/999"},
    {"html_url": "https://github.com/upiscium/Test/issues/99#issuecomment-33"},
])
def test_changed_comment_or_untrusted_route_fails_closed(tmp_path: Path, bad) -> None:
    client = _FakeClient()
    client.responses[f"{BASE}/issues/comments/33"].update(bad)
    with pytest.raises(DailySourceRouteError):
        collect_fresh_verified_routes(_config(tmp_path), target_date=DATE, client=client,
            explicit_historical_reconfirmation=True)


@pytest.mark.parametrize("bad", [
    {"number": 999},
    {"id": None},
    {"pull_request": {"url": f"https://evil.example{BASE}/pulls/2"}},
    {"pull_request": {"url": f"https://api.github.com{BASE}/pulls/999"}},
    {"pull_request": "not an object"},
])
def test_wrong_issue_classification_resource_fails_closed(tmp_path: Path, bad) -> None:
    client = _FakeClient()
    client.responses[f"{BASE}/issues/2"].update(bad)
    with pytest.raises(DailySourceRouteError):
        collect_fresh_verified_routes(_config(tmp_path), target_date=DATE, client=client,
            explicit_historical_reconfirmation=True)


def test_missing_pr_membership_fails_closed(tmp_path: Path) -> None:
    client = _FakeClient(visible_pr=True, include_commit=True)
    client.responses[f"{BASE}/pulls/2/commits?per_page=100&page=1"] = []
    with pytest.raises(DailySourceRouteError, match="membership"):
        collect_fresh_verified_routes(_config(tmp_path), target_date=DATE, client=client,
            explicit_historical_reconfirmation=True)


def test_commit_list_duplicate_or_wrong_public_url_fails_closed(tmp_path: Path) -> None:
    for entries in (
        [deepcopy(PR_COMMIT), deepcopy(PR_COMMIT)],
        [dict(PR_COMMIT, html_url=f"https://github.com/{REPO}/commit/{'b'*40}")],
    ):
        client = _FakeClient(visible_pr=True, include_commit=True)
        client.responses[f"{BASE}/pulls/2/commits?per_page=100&page=1"] = entries
        with pytest.raises(DailySourceRouteError):
            collect_fresh_verified_routes(_config(tmp_path), target_date=DATE, client=client,
            explicit_historical_reconfirmation=True)


def _canonical(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))+"\n").encode()


@pytest.mark.parametrize("mutate", [
    lambda x: x["routes"].pop(),
    lambda x: x["routes"].append(deepcopy(x["routes"][0])),
    lambda x: x["routes"][0].update(entity="pull_request"),
    lambda x: x["routes"][0].update(number=222),
    lambda x: x["routes"][0].update(remote_id="no-id"),
    lambda x: x["routes"][0].update(route="/repos/upiscium/Test/issues/2"),
    lambda x: x.update(evidence_sha256="0" * 64),
    lambda x: x.update(record_version=1),
    lambda x: x.update(collection_mode="retrospective_guess"),
])
def test_receipt_binding_rejects_missing_duplicate_tamper_or_old_version(
    tmp_path: Path, mutate,
) -> None:
    result = _collect(tmp_path)
    obj = json.loads(result.receipt_bytes)
    mutate(obj)
    with pytest.raises(DailySourceRouteError):
        parse_verified_routes(_canonical(obj), result.evidence.canonical_bytes)


def test_canonical_receipt_and_wrong_evidence_bytes_fail_closed(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    with pytest.raises(DailySourceRouteError, match="noncanonical"):
        parse_verified_routes(
            json.dumps(json.loads(result.receipt_bytes), indent=2).encode(),
            result.evidence.canonical_bytes,
        )
    value = json.loads(result.evidence.canonical_bytes)
    value["events"][0]["actor"] = "mismatch"
    with pytest.raises((DailySourceRouteError, GitHubDailySummaryError)):
        parse_verified_routes(result.receipt_bytes, _canonical(value))


def test_api_unavailable_fails_closed_without_persisted_output(tmp_path: Path) -> None:
    client = _FakeClient()
    client.fail_routes.add(f"{BASE}/issues/2")
    with pytest.raises(DailySourceRouteError, match="unavailable"):
        collect_fresh_verified_routes(_config(tmp_path), target_date=DATE, client=client,
            explicit_historical_reconfirmation=True)
    assert not any(tmp_path.rglob("*.github-daily-source-route.json"))

def test_percent_encoded_issue_route_is_matched_by_canonical_api_identity(
    tmp_path: Path,
) -> None:
    client = _FakeClient()
    client.responses[f"{BASE}/issues/comments/33"]["issue_url"] = (
        f"https://api.github.com{BASE.replace('/issues', '/%69ssues')}/%69ssues/2"
    )
    # Encoding of "issues" must be equivalent to the canonical route; this
    # API string is compared but NEVER passed back as a request URL.
    value = collect_fresh_verified_routes(_config(tmp_path), target_date=DATE, client=client,
            explicit_historical_reconfirmation=True)
    assert any(e.kind == "pull_request_comment" for e in value.evidence.events)


@pytest.mark.parametrize("field,value", [
    ("created_at", "2026-10-06T00:00:00Z"),
    ("sha", "b" * 40),
    ("html_url", f"https://github.com/{REPO}/commit/{'b'*40}"),
])
def test_commit_source_cannot_change_between_collection_and_route_verification(
    tmp_path: Path, field: str, value: str,
) -> None:
    client = _FakeClient(visible_pr=True, include_commit=True)
    client.responses[f"{BASE}/pulls/2/commits?per_page=100&page=1"][0][field] = value
    if field == "created_at":
        # The actual GitHub committed_at source is inside the commit payload.
        client.responses[f"{BASE}/pulls/2/commits?per_page=100&page=1"][0][
            "commit"
        ]["committer"]["date"] = value
    with pytest.raises(DailySourceRouteError):
        collect_fresh_verified_routes(_config(tmp_path), target_date=DATE, client=client,
            explicit_historical_reconfirmation=True)


def test_comment_api_query_or_foreign_repo_is_rejected(tmp_path: Path) -> None:
    for url in (
        f"https://api.github.com{BASE}/issues/2?next=1",
        "https://api.github.com/repos/other/Test/issues/2",
        "https://api.github.com.evil.example/repos/upiscium/Test/issues/2",
        "http://api.github.com/repos/upiscium/Test/issues/2",
    ):
        client = _FakeClient()
        client.responses[f"{BASE}/issues/comments/33"]["issue_url"] = url
        with pytest.raises(DailySourceRouteError):
            collect_fresh_verified_routes(_config(tmp_path), target_date=DATE, client=client,
            explicit_historical_reconfirmation=True)


def test_receipt_requires_explicit_nonpublication_and_source_mode(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    raw = json.loads(result.receipt_bytes)
    assert raw["publication_authorized"] is False
    raw["publication_authorized"] = True
    with pytest.raises(DailySourceRouteError):
        parse_verified_routes(_canonical(raw), result.evidence.canonical_bytes)


def test_receipt_input_byte_bounds_fail_without_partial_results(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    with pytest.raises(DailySourceRouteError, match="bound"):
        parse_verified_routes(b"x" * (4 * 1024 * 1024 + 1), result.evidence.canonical_bytes)
    with pytest.raises(DailySourceRouteError, match="bound"):
        parse_verified_routes(result.receipt_bytes, b"x" * (32 * 1024 * 1024 + 1))


def test_source_verification_requests_only_fixed_numeric_api_routes(tmp_path: Path) -> None:
    client = _FakeClient(visible_pr=True, include_commit=True)
    calls = []
    original = client._request_json
    def trace(path):
        calls.append(path)
        return original(path)
    client._request_json = trace
    collect_fresh_verified_routes(_config(tmp_path), target_date=DATE, client=client,
            explicit_historical_reconfirmation=True)
    assert calls
    assert all(path.startswith(BASE + "/") for path in calls)
    assert all("http" not in path and ".." not in path for path in calls)
    assert all(path in client.responses for path in calls)

def test_default_historical_collection_refused_and_explicitly_labelled(tmp_path: Path) -> None:
    from obsidian_automation import github_daily_source_route as source
    client = _FakeClient()
    # A normally running collector may process today's or yesterday's JST
    # Evidence only. Old dates must carry their distinct historical label.
    with pytest.raises(DailySourceRouteError, match="historical"):
        source.collect_fresh_verified_routes(
            _config(tmp_path), target_date=DATE, client=client,
        )
    authorized_for_readonly_recheck = _collect(tmp_path)
    raw = json.loads(authorized_for_readonly_recheck.receipt_bytes)
    assert raw["collection_mode"] == "explicit_historical_reconfirmation"
    assert raw["publication_authorized"] is False
    assert isinstance(raw["captured_at_utc"], str) and raw["captured_at_utc"].endswith("Z")


def test_scheduled_recent_day_uses_separate_fresh_mode(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timezone
    from obsidian_automation import github_daily_source_route as source
    monkeypatch.setattr(
        source, "_utc_now",
        lambda: datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc),
    )
    val = source.collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE, client=_FakeClient(),
    )
    raw = json.loads(val.receipt_bytes)
    assert raw["collection_mode"] == "scheduled_recent_api_reverified"
    assert raw["captured_at_utc"] == "2026-10-06T01:00:00Z"
    assert len(source.parse_verified_routes(val.receipt_bytes, val.evidence.canonical_bytes)) == 2


def test_future_date_cannot_be_reconfirmed_even_with_explicit_history(tmp_path: Path) -> None:
    from datetime import date
    with pytest.raises(DailySourceRouteError, match="future"):
        collect_fresh_verified_routes(
            _config(tmp_path), target_date=date(2100, 1, 1), client=_FakeClient(),
            explicit_historical_reconfirmation=True,
        )


def test_source_uses_only_configured_authenticated_github_api(tmp_path: Path) -> None:
    for api, token in (
        ("https://mirror.example", "synthetic"),
        ("http://api.github.com", "synthetic"),
        ("https://api.github.com.evil.example", "synthetic"),
        ("https://api.github.com", ""),
    ):
        client = _FakeClient()
        client.api_base, client.token = api, token
        with pytest.raises(DailySourceRouteError, match="GitHub"):
            collect_fresh_verified_routes(
                _config(tmp_path), target_date=DATE, client=client,
                explicit_historical_reconfirmation=True,
            )


def test_canonical_receipt_rejects_reordered_route_entries(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    raw = json.loads(result.receipt_bytes)
    assert len(raw["routes"]) == 2
    raw["routes"].reverse()
    with pytest.raises(DailySourceRouteError, match="canonical source ID order"):
        parse_verified_routes(_canonical(raw), result.evidence.canonical_bytes)


def _rehashed_event_and_receipt(result, *, source_id: str | None, field: str, value):
    """Attacker supplies entirely self-consistent but semantically invalid SHA."""
    import hashlib
    raw = json.loads(result.evidence.canonical_bytes)
    idx = next(
        index for index, event in enumerate(raw["events"])
        if event["source_id"] == source_id
    )
    before = raw["events"][idx]["evidence_id"]
    raw["events"][idx][field] = value
    without_sha = dict(raw["events"][idx])
    without_sha.pop("evidence_id")
    new_id = hashlib.sha256(_canonical(without_sha)).hexdigest()
    raw["events"][idx]["evidence_id"] = new_id
    evidence = _canonical(raw)
    receipt = json.loads(result.receipt_bytes)
    receipt["evidence_sha256"] = hashlib.sha256(evidence).hexdigest()
    for entry in receipt["routes"]:
        if entry["evidence_id"] == before:
            entry["evidence_id"] = new_id
    receipt["routes"].sort(key=lambda item: item["evidence_id"])
    return _canonical(receipt), evidence


def test_rehashed_outside_jst_window_comment_is_rejected(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    # Outside 2026-10-05 JST: both the event and bundle hashes are recomputed
    # to emulate a self-consistent but untrusted captured object.
    receipt, evidence = _rehashed_event_and_receipt(
        result, source_id="31", field="occurred_at", value="2026-10-04T08:00:00Z",
    )
    with pytest.raises(DailySourceRouteError, match="outside target JST day"):
        parse_verified_routes(receipt, evidence)


def test_rehashed_unknown_kind_outside_witness_set_is_rejected(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    receipt, evidence = _rehashed_event_and_receipt(
        result, source_id="issue:1:created:2026-10-05T01:00:00Z",
        field="kind", value="unrecognized_foreign_kind",
    )
    with pytest.raises(DailySourceRouteError, match="unknown source event kind"):
        parse_verified_routes(receipt, evidence)


def test_rehashed_event_entity_type_inconsistency_is_rejected(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    receipt, evidence = _rehashed_event_and_receipt(
        result, source_id="31", field="entity_type", value="pull_request_comment",
    )
    with pytest.raises(DailySourceRouteError, match="kind/entity_type"):
        parse_verified_routes(receipt, evidence)


def test_out_of_date_receipt_cannot_fake_scheduled_mode(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    raw = json.loads(result.receipt_bytes)
    raw["collection_mode"] = "scheduled_recent_api_reverified"
    with pytest.raises(DailySourceRouteError, match="historical"):
        parse_verified_routes(_canonical(raw), result.evidence.canonical_bytes)

def test_rehashed_wrong_public_kind_url_is_rejected(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    # Even when an attacker recalculates the full content-addressed chain,
    # an Issue creation must not claim a /pull/N URL.
    receipt, evidence = _rehashed_event_and_receipt(
        result,
        source_id="issue:1:created:2026-10-05T01:00:00Z",
        field="url", value=f"https://github.com/{REPO}/pull/1",
    )
    with pytest.raises(DailySourceRouteError, match="URL/target entity"):
        parse_verified_routes(receipt, evidence)

def test_rehashed_issue_comment_may_not_use_a_pull_request_public_route(tmp_path: Path) -> None:
    result = _collect(tmp_path)
    receipt, evidence = _rehashed_event_and_receipt(
        result, source_id="31", field="url",
        value=f"https://github.com/{REPO}/pull/1#issuecomment-31",
    )
    with pytest.raises(DailySourceRouteError, match="URL/target entity"):
        parse_verified_routes(receipt, evidence)


def test_compact_noncanonical_evidence_date_does_not_gain_new_authority(tmp_path: Path) -> None:
    import hashlib
    result = _collect(tmp_path)
    changed = json.loads(result.evidence.canonical_bytes)
    changed["date"] = "20261005"
    evidence = _canonical(changed)
    receipt = json.loads(result.receipt_bytes)
    receipt["date"] = "20261005"
    receipt["evidence_sha256"] = hashlib.sha256(evidence).hexdigest()
    with pytest.raises((DailySourceRouteError, GitHubDailySummaryError)):
        parse_verified_routes(_canonical(receipt), evidence)


def test_redirect_requests_are_never_followed_or_forwarded_to_other_hosts() -> None:
    from obsidian_automation import github_daily_source_route as route
    import urllib.request
    for code in (301, 302, 303, 307, 308):
        handler = _NeverRedirect()
        assert handler.redirect_request(
            None, None, code, "synthetic redirect", {},
            "https://evil.example/capture-authorization",
        ) is None
    api = _PinnedGitHubClient(
        api_base="https://api.github.com",
        token="synthetic-token", timeout_seconds=2,
    )
    with pytest.raises(DailySourceRouteError, match="external"):
        api._urlopen_bytes(urllib.request.Request(
            "https://evil.example/repos/upiscium/Test/issues/1",
        ))
    with pytest.raises(DailySourceRouteError, match="external"):
        api._urlopen_bytes(urllib.request.Request(
            "http://api.github.com/repos/upiscium/Test/issues/1",
        ))


def test_v2_pinned_client_bounds_collection_source_pagination(monkeypatch) -> None:
    api = _PinnedGitHubClient(
        api_base="https://api.github.com",
        token="synthetic-token", timeout_seconds=2,
    )
    pages = []
    def keep_full(path: str):
        pages.append(path)
        return [{"id": 1}] * 100
    monkeypatch.setattr(api, "_request_json", keep_full)
    with pytest.raises(DailySourceRouteError, match="pagination"):
        api._paged("/repos/upiscium/Test/issues")
    assert len(pages) == api._MAX_PAGES
    assert pages[0].endswith("page=1")
    assert pages[-1].endswith(f"page={api._MAX_PAGES}")


def test_v2_verifier_rejects_unpinned_duck_client_even_with_fake_token(
    tmp_path: Path,
) -> None:
    class Rogue:
        api_base = "https://api.github.com"
        token = "synthetic"
        def _request_json(self, path):
            raise AssertionError("must be refused before the network")
    with pytest.raises(DailySourceRouteError, match="transport"):
        collect_fresh_verified_routes(
            _config(tmp_path), target_date=DATE, client=Rogue(),
            explicit_historical_reconfirmation=True,
        )
