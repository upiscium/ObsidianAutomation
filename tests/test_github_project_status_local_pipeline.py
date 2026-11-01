from __future__ import annotations

import io
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

import obsidian_automation.github_project_status_worker as worker
from obsidian_automation.github_project_status_mutation import (
    ProjectStatusMutationConflict,
    ProjectStatusMutationRejected,
    ProjectStatusTransportResult,
    parse_watcher_proposal,
)
from obsidian_automation.github_project_status_queue import (
    ProjectStatusQueueError,
    _enqueue_output_line,
    enqueue_proposal,
)


def _event(**overrides: object) -> dict[str, object]:
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
    return value


def _proposal_bytes(**overrides: object) -> bytes:
    return (json.dumps(_event(**overrides), ensure_ascii=False) + "\n").encode()


def test_enqueue_proposal_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    request_dir = tmp_path / "25-Execution"
    request_dir.mkdir()
    proposal = parse_watcher_proposal(_proposal_bytes())

    first, first_result = enqueue_proposal(request_dir, proposal)
    second, second_result = enqueue_proposal(request_dir, proposal)

    assert first == second
    assert first.name == f"{proposal.sha256}.github-status.json"
    assert first.read_bytes() == proposal.canonical_bytes
    assert first_result == "queued"
    assert second_result == "already_queued"


def test_enqueue_refuses_hash_path_with_different_bytes(tmp_path: Path) -> None:
    request_dir = tmp_path / "25-Execution"
    request_dir.mkdir()
    proposal = parse_watcher_proposal(_proposal_bytes())
    target = request_dir / f"{proposal.sha256}.github-status.json"
    target.write_bytes(b"different\n")

    with pytest.raises(ProjectStatusQueueError, match="different bytes"):
        enqueue_proposal(request_dir, proposal)


def test_output_line_enqueues_only_pending_changes(tmp_path: Path) -> None:
    request_dir = tmp_path / "25-Execution"
    request_dir.mkdir()
    stdout = io.StringIO()

    changed = json.dumps(_event(), ensure_ascii=False)
    unchanged = json.dumps(_event(change=False, pending=False, proposed_status="running"), ensure_ascii=False)

    _enqueue_output_line(changed, request_dir=request_dir, stdout=stdout)
    _enqueue_output_line(unchanged, request_dir=request_dir, stdout=stdout)

    queued = list(request_dir.glob("*.github-status.json"))
    assert len(queued) == 1
    assert "project-status-enqueued" in stdout.getvalue()


def _prepare_worker_dirs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    request_dir = tmp_path / "pipeline" / "25-Execution"
    result_dir = tmp_path / "pipeline" / "27-Transport"
    state_root = tmp_path / "pipeline"
    lock_dir = state_root / "24-Locks"
    request_dir.mkdir(parents=True)
    result_dir.mkdir()
    lock_dir.mkdir()
    password_file = tmp_path / "password"
    password_file.write_text("secret", encoding="utf-8")
    return request_dir, result_dir, state_root, password_file


def _queue_request(request_dir: Path) -> tuple[Path, object]:
    proposal = parse_watcher_proposal(_proposal_bytes())
    path = request_dir / f"{proposal.sha256}.github-status.json"
    path.write_bytes(proposal.canonical_bytes)
    return path, proposal


def test_worker_applies_once_and_reuses_transport_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_dir, result_dir, state_root, password_file = _prepare_worker_dirs(tmp_path)
    _, proposal = _queue_request(request_dir)
    calls = 0

    def fake_apply(*args: object, **kwargs: object) -> ProjectStatusTransportResult:
        nonlocal calls
        calls += 1
        return ProjectStatusTransportResult(
            proposal_sha256=proposal.sha256,
            project_path=proposal.project_path,
            repository=proposal.repository,
            expected_status=proposal.expected_status,
            desired_status=proposal.desired_status,
            outcome="applied",
            before_content_sha256="a" * 64,
            after_content_sha256="b" * 64,
            completed_at="2026-09-17T04:00:00Z",
        )

    monkeypatch.setattr(worker, "apply_project_status", fake_apply)

    first_out = io.StringIO()
    first_rc = worker.run_worker(
        request_dir=request_dir,
        result_dir=result_dir,
        state_root=state_root,
        base_url="https://nextcloud.example/remote.php/dav/files/obsidian-github-writer",
        username="obsidian-github-writer",
        password_file=password_file,
        stdout=first_out,
    )
    second_out = io.StringIO()
    second_rc = worker.run_worker(
        request_dir=request_dir,
        result_dir=result_dir,
        state_root=state_root,
        base_url="https://nextcloud.example/remote.php/dav/files/obsidian-github-writer",
        username="obsidian-github-writer",
        password_file=password_file,
        stdout=second_out,
    )

    assert first_rc == second_rc == 0
    assert calls == 1
    result = result_dir / f"{proposal.sha256}.github-status.transport-result.json"
    assert result.is_file()
    assert "completed" in first_out.getvalue()
    assert "already_processed" in second_out.getvalue()


def test_worker_persists_conflict_rejection_and_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_dir, result_dir, state_root, password_file = _prepare_worker_dirs(tmp_path)
    _, proposal = _queue_request(request_dir)
    calls = 0

    def fake_apply(*args: object, **kwargs: object) -> ProjectStatusTransportResult:
        nonlocal calls
        calls += 1
        raise ProjectStatusMutationConflict("stale baseline")

    monkeypatch.setattr(worker, "apply_project_status", fake_apply)

    first_rc = worker.run_worker(
        request_dir=request_dir,
        result_dir=result_dir,
        state_root=state_root,
        base_url="https://nextcloud.example/remote.php/dav/files/obsidian-github-writer",
        username="obsidian-github-writer",
        password_file=password_file,
    )
    second_rc = worker.run_worker(
        request_dir=request_dir,
        result_dir=result_dir,
        state_root=state_root,
        base_url="https://nextcloud.example/remote.php/dav/files/obsidian-github-writer",
        username="obsidian-github-writer",
        password_file=password_file,
    )

    assert first_rc == second_rc == 0
    assert calls == 1
    rejection = result_dir / f"{proposal.sha256}.github-status.rejection.json"
    payload = json.loads(rejection.read_text(encoding="utf-8"))
    assert payload["proposal_sha256"] == proposal.sha256
    assert payload["outcome"] == "rejected_conflict"
    assert payload["reason"] == "canonical_conflict"


def test_worker_persists_deterministic_transport_rejection_and_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_dir, result_dir, state_root, password_file = _prepare_worker_dirs(tmp_path)
    _, proposal = _queue_request(request_dir)
    calls = 0

    def fake_apply(*args: object, **kwargs: object) -> ProjectStatusTransportResult:
        nonlocal calls
        calls += 1
        raise ProjectStatusMutationRejected(
            "authority rejected",
            reason_code="authority_rejection",
            http_status=403,
        )

    monkeypatch.setattr(worker, "apply_project_status", fake_apply)

    first_out = io.StringIO()
    first_rc = worker.run_worker(
        request_dir=request_dir,
        result_dir=result_dir,
        state_root=state_root,
        base_url="https://nextcloud.example/remote.php/dav/files/obsidian-github-writer",
        username="obsidian-github-writer",
        password_file=password_file,
        stdout=first_out,
    )
    second_out = io.StringIO()
    second_rc = worker.run_worker(
        request_dir=request_dir,
        result_dir=result_dir,
        state_root=state_root,
        base_url="https://nextcloud.example/remote.php/dav/files/obsidian-github-writer",
        username="obsidian-github-writer",
        password_file=password_file,
        stdout=second_out,
    )

    assert first_rc == second_rc == 0
    assert calls == 1
    rejection = result_dir / f"{proposal.sha256}.github-status.rejection.json"
    payload = json.loads(rejection.read_text(encoding="utf-8"))
    assert payload["proposal_sha256"] == proposal.sha256
    assert payload["outcome"] == "rejected_transport"
    assert payload["reason"] == "authority_rejection"
    assert payload["http_status"] == 403
    assert "rejected_transport" in first_out.getvalue()
    assert "already_rejected" in second_out.getvalue()


def test_worker_rejects_non_content_addressed_request_name(tmp_path: Path) -> None:
    request_dir, result_dir, state_root, password_file = _prepare_worker_dirs(tmp_path)
    (request_dir / "wrong.github-status.json").write_bytes(
        parse_watcher_proposal(_proposal_bytes()).canonical_bytes
    )
    stderr = io.StringIO()

    rc = worker.run_worker(
        request_dir=request_dir,
        result_dir=result_dir,
        state_root=state_root,
        base_url="https://nextcloud.example/remote.php/dav/files/obsidian-github-writer",
        username="obsidian-github-writer",
        password_file=password_file,
        stderr=stderr,
    )

    assert rc == 1
    assert "filename does not match" in stderr.getvalue()


def test_actual_recent_branch_observation_survives_queue_into_legacy_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watcher -> typed Queue -> old canonical Worker, fake effect only."""
    from obsidian_automation.github_project_watcher import (
        ProjectBinding, RepositorySnapshot, StatusDecision, _event_payload,
    )

    request_dir, result_dir, state_root, password_file = _prepare_worker_dirs(tmp_path)
    snapshot = RepositorySnapshot(
        repository="upiscium/Terreate",
        latest_commit_sha="a" * 40,
        latest_commit_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        open_issues=frozenset(),
        open_prs=frozenset(),
        observed_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        recent_commit_sha="b" * 40,
        recent_commit_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
    )
    project = ProjectBinding(
        "10-Project/Terreate/Terreate.md", "upiscium/Terreate", "planning",
    )
    observation = _event_payload(
        project, snapshot,
        StatusDecision("running", "latest commit is within 7 days", True),
    )
    assert set(observation).issuperset({"recent_commit_sha", "recent_commit_at"})
    _enqueue_output_line(
        json.dumps(observation), request_dir=request_dir, stdout=io.StringIO(),
    )
    files = list(request_dir.glob("*.github-status.json"))
    assert len(files) == 1
    proposed = parse_watcher_proposal(files[0].read_bytes())
    assert proposed.expected_status == "planning"
    assert proposed.desired_status == "running"
    assert proposed.latest_commit_sha == "a" * 40
    assert "recent_commit_sha" not in json.loads(proposed.canonical_bytes)

    effects = []

    def fake_effect(value, **_kwargs):
        effects.append(value.sha256)
        return ProjectStatusTransportResult(
            proposal_sha256=value.sha256,
            project_path=value.project_path,
            repository=value.repository,
            expected_status=value.expected_status,
            desired_status=value.desired_status,
            outcome="applied",
            before_content_sha256="c" * 64,
            after_content_sha256="d" * 64,
            completed_at="2026-11-01T00:00:00Z",
        )

    monkeypatch.setattr(worker, "apply_project_status", fake_effect)
    rc = worker.run_worker(
        request_dir=request_dir,
        result_dir=result_dir,
        state_root=state_root,
        base_url="https://nextcloud.invalid/dav",
        username="synthetic",
        password_file=password_file,
        stdout=io.StringIO(),
    )
    assert rc == 0
    assert effects == [proposed.sha256]
    assert (result_dir / f"{proposed.sha256}.github-status.transport-result.json").exists()
