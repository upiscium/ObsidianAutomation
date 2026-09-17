from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from obsidian_automation.core_promotion_transport import HTTPResponse
from obsidian_automation.github_project_overview import (
    MANAGED_END,
    MANAGED_START,
    OverviewItem,
    ProjectOverviewConflict,
    apply_project_overview,
    make_overview_proposal,
    parse_overview_proposal,
    render_status_note,
)
from obsidian_automation.github_project_overview_queue import (
    enqueue_overview,
    run_and_enqueue,
)
from obsidian_automation.github_project_watcher import WatcherConfig


def _proposal(
    *,
    issues: list[OverviewItem] | None = None,
    prs: list[OverviewItem] | None = None,
):
    return make_overview_proposal(
        project_path="10-Project/Terreate/Terreate.md",
        repository="upiscium/Terreate",
        issues=issues or [OverviewItem(203, "First issue"), OverviewItem(210, "Second issue")],
        pull_requests=prs or [OverviewItem(42, "Open PR", draft=True)],
    )


def test_proposal_is_canonical_and_bound_to_project() -> None:
    proposal = make_overview_proposal(
        project_path="10-Project/Terreate/Terreate.md",
        repository="upiscium/Terreate",
        issues=[OverviewItem(210, "B"), OverviewItem(203, "A")],
        pull_requests=[OverviewItem(42, "PR")],
    )
    reparsed = parse_overview_proposal(proposal.canonical_bytes)

    assert [item.number for item in reparsed.issues] == [203, 210]
    assert reparsed.status_path == "10-Project/Terreate/Status.md"
    assert reparsed.project_key == proposal.project_key
    assert reparsed.sha256 == proposal.sha256


def test_render_preserves_checkbox_and_freeform_notes_while_refreshing_items() -> None:
    first = _proposal()
    original = render_status_note(first, None).decode()
    edited = original.replace(
        "- [ ] [#203 First issue]",
        "- [x] [#203 First issue]",
    ).replace("## Notes\n\n", "## Notes\n\nKeep this note.\n")

    second = _proposal(
        issues=[OverviewItem(203, "Renamed issue"), OverviewItem(999, "New issue")],
        prs=[OverviewItem(42, "Open PR", draft=False)],
    )
    refreshed = render_status_note(second, edited.encode()).decode()

    assert "- [x] [#203 Renamed issue]" in refreshed
    assert "- [ ] [#999 New issue]" in refreshed
    assert "#210" not in refreshed
    assert "*(draft)*" not in refreshed
    assert "Keep this note." in refreshed
    assert refreshed.count(MANAGED_START) == 1
    assert refreshed.count(MANAGED_END) == 1


def test_existing_unmanaged_status_note_fails_closed() -> None:
    with pytest.raises(ProjectOverviewConflict, match="managed overview block"):
        render_status_note(_proposal(), b"# Existing human Status\n")


class _WebDAV:
    def __init__(self) -> None:
        self.project = (
            b"---\n"
            b"type: project\n"
            b"status: planning\n"
            b"github_repo: upiscium/Terreate\n"
            b"github_watch: true\n"
            b"---\n"
        )
        self.status: bytes | None = None
        self.status_etag = '"s1"'
        self.methods: list[tuple[str, str]] = []

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
        self.methods.append((method, target_url))
        is_status = target_url.endswith("/10-Project/Terreate/Status.md")
        is_project = target_url.endswith("/10-Project/Terreate/Terreate.md")
        if method == "GET" and is_project:
            return HTTPResponse(status=200, body=self.project, etag='"p1"')
        if method == "GET" and is_status:
            if self.status is None:
                return HTTPResponse(status=404, body=b"", etag=None)
            return HTTPResponse(status=200, body=self.status, etag=self.status_etag)
        if method == "PUT" and is_status:
            request_headers = headers or {}
            if self.status is None:
                if request_headers.get("If-None-Match") != "*":
                    return HTTPResponse(status=412, body=b"", etag=None)
            elif request_headers.get("If-Match") != self.status_etag:
                return HTTPResponse(status=412, body=b"", etag=self.status_etag)
            assert body is not None
            self.status = body
            self.status_etag = '"s2"'
            return HTTPResponse(status=204, body=b"", etag=self.status_etag)
        raise AssertionError((method, target_url))


def test_apply_creates_then_updates_status_note_with_cas() -> None:
    remote = _WebDAV()
    proposal = _proposal()

    created = apply_project_overview(
        proposal,
        base_url="https://nextcloud.example/remote.php/dav/files/writer",
        username="writer",
        password="secret",
        transport=remote,
    )
    assert created.outcome == "created"
    assert remote.status is not None

    remote.status = remote.status.replace(
        b"- [ ] [#203 First issue]",
        b"- [x] [#203 First issue]",
    ).replace(b"## Notes\n\n", b"## Notes\n\nHuman note.\n")
    remote.status_etag = '"s3"'

    changed = _proposal(issues=[OverviewItem(203, "Updated title")], prs=[])
    updated = apply_project_overview(
        changed,
        base_url="https://nextcloud.example/remote.php/dav/files/writer",
        username="writer",
        password="secret",
        transport=remote,
    )
    assert updated.outcome == "updated"
    assert remote.status is not None
    assert b"- [x] [#203 Updated title]" in remote.status
    assert b"Human note." in remote.status

    unchanged = apply_project_overview(
        changed,
        base_url="https://nextcloud.example/remote.php/dav/files/writer",
        username="writer",
        password="secret",
        transport=remote,
    )
    assert unchanged.outcome == "already_desired"


def test_overview_queue_uses_stable_project_path_request(tmp_path: Path) -> None:
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    first = _proposal()
    second = _proposal(issues=[OverviewItem(203, "Changed")], prs=[])

    path1, result1 = enqueue_overview(request_dir, first)
    path2, result2 = enqueue_overview(request_dir, second)

    assert path1 == path2
    assert path1.name == f"{first.project_key}.github-overview.json"
    assert result1 == "queued"
    assert result2 == "updated"
    assert path1.read_bytes() == second.canonical_bytes


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @staticmethod
    def _repo_path(repository: str) -> str:
        return repository

    def _paged(self, path: str) -> list[dict[str, object]]:
        self.calls.append(path)
        if "/issues?" in path:
            return [
                {"number": 1, "title": "Issue one"},
                {"number": 2, "title": "PR shadow", "pull_request": {}},
            ]
        if "/pulls?" in path:
            return [{"number": 2, "title": "PR two", "draft": False}]
        raise AssertionError(path)


def test_overview_enqueue_fetches_shared_repository_once_and_prunes_stale(tmp_path: Path) -> None:
    project_dir = tmp_path / "vault" / "10-Project" / "A"
    project_dir.mkdir(parents=True)
    for name in ("A", "B"):
        directory = tmp_path / "vault" / "10-Project" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.md").write_text(
            "---\n"
            "type: project\n"
            "status: planning\n"
            "github_repo: upiscium/Test\n"
            "github_watch: true\n"
            "---\n",
            encoding="utf-8",
        )
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    (request_dir / ("f" * 64 + ".github-overview.json")).write_text("stale", encoding="utf-8")
    config = WatcherConfig(vault_root=tmp_path / "vault", state_db=tmp_path / "state.sqlite3")
    client = _FakeClient()
    stdout = io.StringIO()

    rc = run_and_enqueue(config, request_dir=request_dir, client=client, stdout=stdout)

    assert rc == 0
    assert len(client.calls) == 2
    assert len(list(request_dir.glob("*.github-overview.json"))) == 2
    rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert sum(row["event"] == "project-overview-enqueued" for row in rows) == 2
    assert any(row["event"] == "project-overview-pruned" for row in rows)
