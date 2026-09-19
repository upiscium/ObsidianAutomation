from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from pathlib import Path
from typing import Sequence


_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
REQUIRED_UNITS = {
    "obsidian-pre-review-generator.service": (
        "User=obsidian-ai-generator",
        "obsidian-pre-review-generator-worker",
        "EnvironmentFile=/etc/obsidian-ai/pre-review-generator.env",
        "EnvironmentFile=/etc/obsidian-ai/pre-review-revision.env",
        "--openai-base-url",
    ),
    "obsidian-pre-review-validator.service": (
        "User=obsidian-ai-validator",
        "Requires=obsidian-pre-review-generator.service",
        "PrivateNetwork=true",
    ),
    "obsidian-pre-review-reader.service": (
        "User=obsidian-ai-reader",
        "Requires=obsidian-pre-review-validator.service",
        "PrivateNetwork=true",
    ),
    "obsidian-pre-review-evaluator.service": (
        "User=obsidian-ai-evaluator",
        "Requires=obsidian-pre-review-reader.service",
        "obsidian-pre-review-evaluator-worker",
        "EnvironmentFile=/etc/obsidian-ai/pre-review-evaluator.env",
        "EnvironmentFile=/etc/obsidian-ai/pre-review-revision.env",
        "--openai-base-url",
    ),
    "obsidian-pre-review-status.service": (
        "User=obsidian-ai-status",
        "Requires=obsidian-pre-review-evaluator.service",
        "PrivateNetwork=true",
        "obsidian-pre-review-status-project",
    ),
    "obsidian-pre-review.timer": (
        "Unit=obsidian-pre-review-status.service",
        "WantedBy=timers.target",
    ),
}


class PreReviewProductionSmokeError(RuntimeError):
    """Raised when a safe pre-review deployment assertion fails."""


def _regular_file(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        info = absolute.lstat()
    except FileNotFoundError as exc:
        raise PreReviewProductionSmokeError(f"{label} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PreReviewProductionSmokeError(f"{label} must be a regular non-symlink file")
    return absolute


def read_revision(path: Path) -> str:
    source = _regular_file(path, label="pre-review revision env")
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreReviewProductionSmokeError("cannot read pre-review revision env") from exc
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].startswith("OBSIDIAN_AUTOMATION_REVISION="):
        raise PreReviewProductionSmokeError("revision env must contain exactly one revision assignment")
    revision = lines[0].partition("=")[2]
    if _SHA_RE.fullmatch(revision) is None:
        raise PreReviewProductionSmokeError("revision env contains an invalid Git digest")
    return revision


def validate_units(systemd_dir: Path) -> tuple[str, ...]:
    root = systemd_dir.absolute()
    try:
        info = root.lstat()
    except FileNotFoundError as exc:
        raise PreReviewProductionSmokeError("systemd unit directory is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PreReviewProductionSmokeError("systemd unit directory is unsafe")

    checked: list[str] = []
    joined = ""
    for name, markers in sorted(REQUIRED_UNITS.items()):
        path = _regular_file(root / name, label=f"systemd unit {name}")
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                raise PreReviewProductionSmokeError(
                    f"systemd unit {name} is missing required marker: {marker}"
                )
        if "User=root" in text:
            raise PreReviewProductionSmokeError(f"systemd unit {name} must not run as root")
        joined += text + "\n"
        checked.append(name)

    forbidden = (
        "--ollama-base-url",
        "OLLAMA_BASE_URL",
        "obsidian-production-webdav-worker",
        "obsidian-production-knowledge-webdav-worker",
        "obsidian-production-executor",
        "/27-Transport",
        "/25-Execution",
        "/30-Receipts",
    )
    for marker in forbidden:
        if marker in joined:
            raise PreReviewProductionSmokeError(
                f"pre-review unit chain contains forbidden post-review authority marker: {marker}"
            )
    return tuple(checked)


def run_safe_smoke(
    *,
    expected_revision: str,
    revision_env: Path,
    systemd_dir: Path,
) -> dict[str, object]:
    if _SHA_RE.fullmatch(expected_revision) is None:
        raise PreReviewProductionSmokeError("expected revision must be a full lowercase Git digest")
    observed = read_revision(revision_env)
    if observed != expected_revision:
        raise PreReviewProductionSmokeError("installed revision env does not match expected revision")
    units = validate_units(systemd_dir)
    return {
        "event": "pre-review-production-smoke",
        "profile": "safe",
        "status": "passed",
        "revision": observed,
        "unit_count": len(units),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-pre-review-production-smoke")
    parser.add_argument("--profile", choices=["safe"], required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument(
        "--revision-env",
        type=Path,
        default=Path("/etc/obsidian-ai/pre-review-revision.env"),
    )
    parser.add_argument(
        "--systemd-dir",
        type=Path,
        default=Path("/etc/systemd/system"),
    )
    args = parser.parse_args(argv)
    try:
        result = run_safe_smoke(
            expected_revision=args.expected_revision,
            revision_env=args.revision_env,
            systemd_dir=args.systemd_dir,
        )
    except (PreReviewProductionSmokeError, OSError) as exc:
        print(
            json.dumps(
                {
                    "event": "pre-review-production-smoke",
                    "profile": args.profile,
                    "status": "failed",
                    "message": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
