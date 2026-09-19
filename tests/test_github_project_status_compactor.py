from __future__ import annotations

import io
import json
from pathlib import Path

from obsidian_automation.github_project_status_compactor import compact_status_requests
from obsidian_automation.github_project_status_mutation import parse_watcher_proposal


def _proposal() -> object:
    return parse_watcher_proposal(
        (
            json.dumps(
                {
                    "change": True,
                    "current_status": "running",
                    "event": "project-status-observation",
                    "latest_commit_at": "2026-09-18T00:00:00Z",
                    "latest_commit_sha": "0" * 40,
                    "observed_at": "2026-09-19T00:00:00Z",
                    "open_issues": [],
                    "open_prs": [],
                    "pending": True,
                    "project": "10-Project/Terreate/Terreate.md",
                    "proposed_status": "planning",
                    "reason": "compactor test",
                    "repository": "upiscium/Terreate",
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    )


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    request_dir = tmp_path / "25-Execution"
    result_dir = tmp_path / "27-Transport"
    request_dir.mkdir()
    result_dir.mkdir()
    return request_dir, result_dir


def _queue(request_dir: Path) -> tuple[Path, object]:
    proposal = _proposal()
    path = request_dir / f"{proposal.sha256}.github-status.json"
    path.write_bytes(proposal.canonical_bytes)
    return path, proposal


def _terminal_payload(proposal_sha256: str, *, outcome: str) -> bytes:
    return (
        json.dumps(
            {
                "record_version": 1,
                "stage": "github_project_status_transport",
                "proposal_sha256": proposal_sha256,
                "outcome": outcome,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def test_compactor_removes_request_with_matching_transport_result(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    request, proposal = _queue(request_dir)
    result = result_dir / f"{proposal.sha256}.github-status.transport-result.json"
    result.write_bytes(_terminal_payload(proposal.sha256, outcome="applied"))
    stdout = io.StringIO()

    rc = compact_status_requests(
        request_dir=request_dir,
        result_dir=result_dir,
        stdout=stdout,
    )

    assert rc == 0
    assert not request.exists()
    assert result.exists()
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "completed"
    assert payload["removed"] == 1
    assert payload["kept_pending"] == 0
    assert payload["failures"] == 0


def test_compactor_removes_request_with_matching_rejection(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    request, proposal = _queue(request_dir)
    rejection = result_dir / f"{proposal.sha256}.github-status.rejection.json"
    rejection.write_bytes(_terminal_payload(proposal.sha256, outcome="rejected_conflict"))

    rc = compact_status_requests(request_dir=request_dir, result_dir=result_dir)

    assert rc == 0
    assert not request.exists()
    assert rejection.exists()


def test_compactor_keeps_pending_request_without_terminal_artifact(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    request, _ = _queue(request_dir)
    stdout = io.StringIO()

    rc = compact_status_requests(
        request_dir=request_dir,
        result_dir=result_dir,
        stdout=stdout,
    )

    assert rc == 0
    assert request.exists()
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "completed"
    assert payload["removed"] == 0
    assert payload["kept_pending"] == 1


def test_compactor_fails_closed_on_mismatched_terminal_artifact(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    request, proposal = _queue(request_dir)
    result = result_dir / f"{proposal.sha256}.github-status.transport-result.json"
    result.write_bytes(_terminal_payload("f" * 64, outcome="applied"))
    stderr = io.StringIO()

    rc = compact_status_requests(
        request_dir=request_dir,
        result_dir=result_dir,
        stderr=stderr,
    )

    assert rc == 1
    assert request.exists()
    assert "proposal SHA does not match" in stderr.getvalue()


def test_compactor_fails_closed_on_malformed_terminal_artifact(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    request, proposal = _queue(request_dir)
    result = result_dir / f"{proposal.sha256}.github-status.transport-result.json"
    result.write_text("{broken", encoding="utf-8")

    rc = compact_status_requests(request_dir=request_dir, result_dir=result_dir)

    assert rc == 1
    assert request.exists()


def test_compactor_fails_closed_when_both_terminal_artifacts_exist(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    request, proposal = _queue(request_dir)
    (result_dir / f"{proposal.sha256}.github-status.transport-result.json").write_bytes(
        _terminal_payload(proposal.sha256, outcome="applied")
    )
    (result_dir / f"{proposal.sha256}.github-status.rejection.json").write_bytes(
        _terminal_payload(proposal.sha256, outcome="rejected_conflict")
    )

    rc = compact_status_requests(request_dir=request_dir, result_dir=result_dir)

    assert rc == 1
    assert request.exists()


def test_compactor_fails_closed_on_request_filename_content_mismatch(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    _, proposal = _queue(request_dir)
    original = request_dir / f"{proposal.sha256}.github-status.json"
    mismatched = request_dir / f"{'e' * 64}.github-status.json"
    original.rename(mismatched)
    (result_dir / f"{'e' * 64}.github-status.transport-result.json").write_bytes(
        _terminal_payload("e" * 64, outcome="applied")
    )

    rc = compact_status_requests(request_dir=request_dir, result_dir=result_dir)

    assert rc == 1
    assert mismatched.exists()


def test_compactor_rejects_symlink_request_and_terminal_artifact(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    request, proposal = _queue(request_dir)

    real_request = tmp_path / "real-request.json"
    real_request.write_bytes(request.read_bytes())
    request.unlink()
    request.symlink_to(real_request)

    rc = compact_status_requests(request_dir=request_dir, result_dir=result_dir)
    assert rc == 1
    assert request.is_symlink()

    request.unlink()
    request.write_bytes(proposal.canonical_bytes)
    real_result = tmp_path / "real-result.json"
    real_result.write_bytes(_terminal_payload(proposal.sha256, outcome="applied"))
    result = result_dir / f"{proposal.sha256}.github-status.transport-result.json"
    result.symlink_to(real_result)

    rc = compact_status_requests(request_dir=request_dir, result_dir=result_dir)
    assert rc == 1
    assert request.exists()


def test_compactor_ignores_overview_requests(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    overview = request_dir / f"{'a' * 64}.github-overview.json"
    overview.write_text("{}\n", encoding="utf-8")
    stdout = io.StringIO()

    rc = compact_status_requests(
        request_dir=request_dir,
        result_dir=result_dir,
        stdout=stdout,
    )

    assert rc == 0
    assert overview.exists()
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "idle"


def test_compactor_is_idempotent_after_terminal_request_removal(tmp_path: Path) -> None:
    request_dir, result_dir = _dirs(tmp_path)
    request, proposal = _queue(request_dir)
    (result_dir / f"{proposal.sha256}.github-status.transport-result.json").write_bytes(
        _terminal_payload(proposal.sha256, outcome="already_desired")
    )

    first = compact_status_requests(request_dir=request_dir, result_dir=result_dir)
    stdout = io.StringIO()
    second = compact_status_requests(
        request_dir=request_dir,
        result_dir=result_dir,
        stdout=stdout,
    )

    assert first == second == 0
    assert not request.exists()
    assert json.loads(stdout.getvalue())["status"] == "idle"
