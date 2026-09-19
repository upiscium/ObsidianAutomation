from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .core_promotion_transport import HTTPResponse
from .github_project_status_mutation import (
    ProjectStatusMutationConflict,
    ProjectStatusMutationRejected,
    apply_project_status,
    parse_watcher_proposal,
)


class ProductionSmokeError(RuntimeError):
    """Raised when a production smoke check fails."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]
SmokeCheck = Callable[[], None]

LIVE_UNITS = (
    "obsidian-github-sync-vault-pull.service",
    "obsidian-github-sync.service",
    "obsidian-github-writer.service",
    "obsidian-github-compactor.service",
)


def _default_runner(argv: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _proposal_bytes() -> bytes:
    return json.dumps(
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
            "reason": "production smoke",
            "repository": "upiscium/Terreate",
        },
        sort_keys=True,
    ).encode("utf-8")


_PROJECT = b"""---
type: project
status: running
github_repo: upiscium/Terreate
github_watch: true
---
"""


class _FakeWebDAV:
    def __init__(self, put_status: int) -> None:
        self.put_status = put_status
        self.methods: list[str] = []

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
            return HTTPResponse(status=200, body=_PROJECT, etag='"v1"')
        if method == "PUT":
            return HTTPResponse(status=self.put_status, body=b"", etag='"v1"')
        raise AssertionError(method)


def smoke_http_classification() -> None:
    proposal = parse_watcher_proposal(_proposal_bytes())

    authority = _FakeWebDAV(403)
    try:
        apply_project_status(
            proposal,
            base_url="https://example.invalid/dav",
            username="writer",
            password="dummy",
            transport=authority,
        )
    except ProjectStatusMutationRejected as exc:
        if exc.reason_code != "authority_rejection" or exc.http_status != 403:
            raise ProductionSmokeError("403 classification contract changed") from exc
    else:
        raise ProductionSmokeError("403 was not rejected deterministically")
    if authority.methods != ["GET", "PUT"]:
        raise ProductionSmokeError("403 unexpectedly entered post-GET recovery")

    conflict = _FakeWebDAV(412)
    try:
        apply_project_status(
            proposal,
            base_url="https://example.invalid/dav",
            username="writer",
            password="dummy",
            transport=conflict,
        )
    except ProjectStatusMutationConflict as exc:
        if exc.reason_code != "etag_cas_conflict" or exc.http_status != 412:
            raise ProductionSmokeError("412 classification contract changed") from exc
    else:
        raise ProductionSmokeError("412 was not classified as a CAS conflict")
    if conflict.methods != ["GET", "PUT"]:
        raise ProductionSmokeError("412 unexpectedly entered post-GET recovery")


SAFE_SMOKES: tuple[tuple[str, SmokeCheck], ...] = (
    ("http-classification", smoke_http_classification),
)


def _systemctl_properties(unit: str, *, runner: CommandRunner) -> dict[str, str]:
    result = runner(
        (
            "systemctl",
            "show",
            unit,
            "--property=Result",
            "--property=ExecMainStatus",
        )
    )
    if result.returncode != 0:
        raise ProductionSmokeError(f"cannot inspect systemd unit {unit}")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def run_safe_smokes() -> tuple[str, ...]:
    completed: list[str] = []
    for name, check in SAFE_SMOKES:
        check()
        completed.append(name)
    return tuple(completed)


def run_live_smoke(*, runner: CommandRunner = _default_runner) -> tuple[str, ...]:
    started = runner(("systemctl", "start", "obsidian-github-compactor.service"))
    if started.returncode != 0:
        raise ProductionSmokeError("production compactor cycle failed to start or complete")

    completed: list[str] = []
    for unit in LIVE_UNITS:
        properties = _systemctl_properties(unit, runner=runner)
        if properties.get("Result") != "success":
            raise ProductionSmokeError(f"{unit} Result is not success")
        if properties.get("ExecMainStatus") != "0":
            raise ProductionSmokeError(f"{unit} ExecMainStatus is not zero")
        completed.append(unit)
    return tuple(completed)


def run_profile(
    profile: str,
    *,
    runner: CommandRunner = _default_runner,
) -> tuple[str, ...]:
    if profile == "safe":
        return run_safe_smokes()
    if profile == "live":
        return run_live_smoke(runner=runner)
    raise ProductionSmokeError(f"unsupported smoke profile: {profile}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-production-smoke",
        description="Run registered Obsidian GitHub production smoke checks.",
    )
    parser.add_argument("--profile", choices=("safe", "live"), required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        completed = run_profile(args.profile)
    except ProductionSmokeError as exc:
        print(
            json.dumps(
                {
                    "event": "obsidian-github-production-smoke",
                    "profile": args.profile,
                    "status": "failed",
                    "message": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "event": "obsidian-github-production-smoke",
                "profile": args.profile,
                "status": "passed",
                "checks": list(completed),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
