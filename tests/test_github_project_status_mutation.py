from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.core_promotion_transport import HTTPResponse
from obsidian_automation.github_project_status_mutation import (
    ProjectStatusMutationConflict,
    ProjectStatusMutationError,
    apply_project_status,
    parse_watcher_proposal,
    persist_receipt,
    prepare_project_update,
)


def _event(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "change": True,
        "current_status": "running",
        "event": "project-status-observation",
        "latest_commit_at": "2026-08-13T04:11:38Z",
        "latest_commit_sha": "0d022ed5140b497b5d3be886408822ecbd981dd3",
        "observed_at": "2026-09-17T03:39:13Z",
        "open_issues": [203, 210],
        "open_prs": [],
        "pending": True,
        "project": "10-Project/Terreate/Terreate.md",
        "proposed_status": "planning",
        "reason": "no commit within 7 days",
        "repository": "upiscium/Terreate",
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False).encode()


def _project(
    *,
    status: str = "running",
    repository: str = "upiscium/Terreate",
    watched: str = "true",
    eol: str = "\n",
) -> bytes:
    return (
        f"---{eol}"
        f"type: project{eol}"
        f"workspace:{eol}"
        f"status: {status}{eol}"
        f"priority:{eol}"
        f"github_repo: {repository}{eol}"
        f"github_watch: {watched}{eol}"
        f"---{eol}"
        f"# Project Summary{eol}"
        f"keep me unchanged{eol}"
    ).encode()


class _WebDAV:
    def __init__(self, content: bytes, *, etag: str | None = '"v1"') -> None:
        self.content = content
        self.etag = etag
        self.methods: list[str] = []
        self.put_headers: dict[str, str] | None = None
        self.put_body: bytes | None = None
        self.force_put_status: int | None = None

    def __call__(
        self,
        *,
        method: str,
        target_url: str,
        username: str,
        password: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float,
        response_limit: int,
    ) -> HTTPResponse:
        self.methods.append(method)
        if method == "GET":
            return HTTPResponse(status=200, body=self.content, etag=self.etag)
        if method == "PUT":
            self.put_headers = dict(headers or {})
            self.put_body = body
            if self.force_put_status is not None:
                return HTTPResponse(status=self.force_put_status, body=b"", etag=self.etag)
            if self.put_headers.get("If-Match") != self.etag:
                return HTTPResponse(status=412, body=b"", etag=self.etag)
            assert body is not None
            self.content = body
            self.etag = '"v2"'
            return HTTPResponse(status=204, body=b"", etag=self.etag)
        raise AssertionError(method)


def test_parse_watcher_observation_as_status_proposal() -> None:
    proposal = parse_watcher_proposal(_event())

    assert proposal.project_path == "10-Project/Terreate/Terreate.md"
    assert proposal.repository == "upiscium/Terreate"
    assert proposal.expected_status == "running"
    assert proposal.desired_status == "planning"
    assert proposal.latest_commit_sha == "0d022ed5140b497b5d3be886408822ecbd981dd3"
    assert len(proposal.sha256) == 64


def test_proposal_must_be_pending_change_and_cannot_automate_stopped_or_terminal_target() -> None:
    with pytest.raises(ProjectStatusMutationError):
        parse_watcher_proposal(_event(change=False))
    with pytest.raises(ProjectStatusMutationError):
        parse_watcher_proposal(_event(pending=False))
    with pytest.raises(ProjectStatusMutationError):
        parse_watcher_proposal(_event(current_status="stopped"))
    with pytest.raises(ProjectStatusMutationError):
        parse_watcher_proposal(_event(proposed_status="done"))


def test_prepare_update_changes_only_status_line_and_preserves_crlf() -> None:
    proposal = parse_watcher_proposal(_event())
    original = _project(eol="\r\n").replace(b"status: running\r\n", b"status: running  # human comment\r\n")

    disposition, updated = prepare_project_update(proposal, original)

    assert disposition == "apply"
    assert updated == original.replace(
        b"status: running  # human comment\r\n",
        b"status: planning  # human comment\r\n",
    )


def test_prepare_update_rejects_binding_changes_and_stopped() -> None:
    proposal = parse_watcher_proposal(_event())

    with pytest.raises(ProjectStatusMutationConflict):
        prepare_project_update(proposal, _project(repository="upiscium/Other"))
    with pytest.raises(ProjectStatusMutationConflict):
        prepare_project_update(proposal, _project(watched="false"))
    with pytest.raises(ProjectStatusMutationConflict):
        prepare_project_update(proposal, _project(status="stopped"))
    with pytest.raises(ProjectStatusMutationConflict):
        prepare_project_update(proposal, _project(status="done"))


def test_apply_uses_strong_etag_and_verifies_exact_remote_bytes() -> None:
    proposal = parse_watcher_proposal(_event())
    remote = _WebDAV(_project())

    receipt = apply_project_status(
        proposal,
        base_url="https://nextcloud.example/remote.php/dav/files/writer/ObsidianVault",
        username="writer",
        password="secret",
        transport=remote,
    )

    assert receipt.result == "applied"
    assert remote.methods == ["GET", "PUT", "GET"]
    assert remote.put_headers is not None
    assert remote.put_headers["If-Match"] == '"v1"'
    assert remote.put_body == _project().replace(b"status: running\n", b"status: planning\n")
    assert remote.content == remote.put_body


def test_already_desired_is_idempotent_and_does_not_put() -> None:
    proposal = parse_watcher_proposal(_event())
    remote = _WebDAV(_project(status="planning"))

    receipt = apply_project_status(
        proposal,
        base_url="https://nextcloud.example/dav",
        username="writer",
        password="secret",
        transport=remote,
    )

    assert receipt.result == "already_desired"
    assert remote.methods == ["GET"]


def test_stale_expected_status_fails_closed_without_put() -> None:
    proposal = parse_watcher_proposal(_event())
    remote = _WebDAV(_project(status="done"))

    with pytest.raises(ProjectStatusMutationConflict):
        apply_project_status(
            proposal,
            base_url="https://nextcloud.example/dav",
            username="writer",
            password="secret",
            transport=remote,
        )

    assert remote.methods == ["GET"]


def test_missing_strong_etag_fails_closed() -> None:
    proposal = parse_watcher_proposal(_event())
    remote = _WebDAV(_project(), etag='W/"weak"')

    with pytest.raises(ProjectStatusMutationError, match="strong ETag"):
        apply_project_status(
            proposal,
            base_url="https://nextcloud.example/dav",
            username="writer",
            password="secret",
            transport=remote,
        )

    assert remote.methods == ["GET"]


def test_conditional_put_conflict_is_not_reported_as_success() -> None:
    proposal = parse_watcher_proposal(_event())
    remote = _WebDAV(_project())
    remote.force_put_status = 412

    with pytest.raises(ProjectStatusMutationError, match="ambiguous CAS outcome"):
        apply_project_status(
            proposal,
            base_url="https://nextcloud.example/dav",
            username="writer",
            password="secret",
            transport=remote,
        )

    assert remote.methods == ["GET", "PUT", "GET"]


def test_receipt_persistence_is_bound_to_proposal_hash(tmp_path: Path) -> None:
    proposal = parse_watcher_proposal(_event())
    remote = _WebDAV(_project(status="planning"))
    receipt = apply_project_status(
        proposal,
        base_url="https://nextcloud.example/dav",
        username="writer",
        password="secret",
        transport=remote,
    )
    path = tmp_path / "receipt.json"

    first = persist_receipt(path, receipt)
    second = persist_receipt(path, receipt)

    assert first == second == path.read_bytes()
    assert json.loads(first)["proposal_sha256"] == proposal.sha256
