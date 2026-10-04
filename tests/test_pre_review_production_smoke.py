from __future__ import annotations

from pathlib import Path
import shlex
import subprocess

import pytest

from obsidian_automation.pre_review_production_smoke import (
    PreReviewProductionSmokeError,
    REQUIRED_UNITS,
    run_safe_smoke,
)


REVISION = "a" * 40
REFRESH_PREDECESSORS = {
    "obsidian-semantic-index-refresh-prepare.service": "obsidian-ai-vault-pull.service",
    "obsidian-semantic-index-refresh-embed.service": "obsidian-semantic-index-refresh-prepare.service",
    "obsidian-semantic-index-refresh-finalize.service": "obsidian-semantic-index-refresh-embed.service",
}


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    for name in REQUIRED_UNITS:
        source = Path("examples/ai") / name
        (systemd / name).write_bytes(source.read_bytes())

    config = tmp_path / "etc" / "obsidian-ai"
    config.mkdir(parents=True)
    revision = config / "pre-review-revision.env"
    revision.write_text(
        f"OBSIDIAN_AUTOMATION_REVISION={REVISION}\n",
        encoding="utf-8",
    )
    return systemd, revision


def test_safe_smoke_accepts_exact_revision_and_identity_chain(tmp_path: Path) -> None:
    systemd, revision = _fixture(tmp_path)

    result = run_safe_smoke(
        expected_revision=REVISION,
        revision_env=revision,
        systemd_dir=systemd,
    )

    assert result["status"] == "passed"
    assert result["revision"] == REVISION
    assert result["unit_count"] == len(REQUIRED_UNITS)


def test_safe_smoke_rejects_revision_drift(tmp_path: Path) -> None:
    systemd, revision = _fixture(tmp_path)

    with pytest.raises(
        PreReviewProductionSmokeError,
        match="does not match",
    ):
        run_safe_smoke(
            expected_revision="b" * 40,
            revision_env=revision,
            systemd_dir=systemd,
        )


def test_safe_smoke_rejects_root_worker(tmp_path: Path) -> None:
    systemd, revision = _fixture(tmp_path)
    path = systemd / "obsidian-pre-review-generator.service"
    path.write_text(
        path.read_text(encoding="utf-8") + "\nUser=root\n",
        encoding="utf-8",
    )

    with pytest.raises(
        PreReviewProductionSmokeError,
        match="must not run as root",
    ):
        run_safe_smoke(
            expected_revision=REVISION,
            revision_env=revision,
            systemd_dir=systemd,
        )


def test_safe_smoke_rejects_post_review_role_confusion(tmp_path: Path) -> None:
    systemd, revision = _fixture(tmp_path)
    path = systemd / "obsidian-ai-post-review-transport.service"
    text = path.read_text(encoding="utf-8").replace(
        "User=obsidian-ai-sync",
        "User=obsidian-ai-reviewer",
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(
        PreReviewProductionSmokeError,
        match="missing required marker: User=obsidian-ai-sync",
    ):
        run_safe_smoke(
            expected_revision=REVISION,
            revision_env=revision,
            systemd_dir=systemd,
        )


def test_safe_smoke_rejects_symlinked_unit(tmp_path: Path) -> None:
    systemd, revision = _fixture(tmp_path)
    path = systemd / "obsidian-pre-review-reader.service"
    target = tmp_path / "outside.service"
    target.write_text("[Unit]\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(
        PreReviewProductionSmokeError,
        match="regular non-symlink",
    ):
        run_safe_smoke(
            expected_revision=REVISION,
            revision_env=revision,
            systemd_dir=systemd,
        )


@pytest.mark.parametrize("unit", REFRESH_PREDECESSORS)
@pytest.mark.parametrize(
    ("overrides", "accepted"),
    [
        pytest.param({}, True, id="completed-successfully"),
        pytest.param(
            {"MONITOR_SERVICE_RESULT": "timeout"},
            False,
            id="failed-service-result-with-zero-exit",
        ),
        pytest.param(
            {"MONITOR_EXIT_CODE": "killed", "MONITOR_EXIT_STATUS": "TERM"},
            False,
            id="stopped-with-success-result",
        ),
        pytest.param(
            {
                "MONITOR_SERVICE_RESULT": "signal",
                "MONITOR_EXIT_CODE": "killed",
                "MONITOR_EXIT_STATUS": "KILL",
            },
            False,
            id="killed",
        ),
        pytest.param(
            {"MONITOR_SERVICE_RESULT": "exec-condition", "MONITOR_EXIT_STATUS": "1"},
            False,
            id="exec-condition-skipped",
        ),
        pytest.param({"MONITOR_EXIT_STATUS": "1"}, False, id="nonzero-exit"),
        pytest.param(None, False, id="absent-monitor-environment"),
        pytest.param(
            {"MONITOR_UNIT": "unrelated-predecessor.service"},
            False,
            id="wrong-predecessor",
        ),
    ],
)
def test_refresh_guard_checks_actual_predecessor_completion(
    unit: str, overrides: dict[str, str] | None, accepted: bool,
) -> None:
    lines = (Path("examples/ai") / unit).read_text(encoding="utf-8").splitlines()
    guards = [line.partition("=")[2] for line in lines if line.startswith("ExecStartPre=")]
    assert len(guards) == 1
    # systemd consumes $$ before handing the quoted script to /bin/sh.
    command = shlex.split(guards[0].replace("$$", "$"))
    monitor = {
        "MONITOR_UNIT": REFRESH_PREDECESSORS[unit],
        "MONITOR_SERVICE_RESULT": "success",
        "MONITOR_EXIT_CODE": "exited",
        "MONITOR_EXIT_STATUS": "0",
    }
    if overrides is None:
        monitor = {}
    else:
        monitor.update(overrides)
    completed = subprocess.run(
        command, env=monitor, capture_output=True, text=True, timeout=5, check=False,
    )
    assert (completed.returncode == 0) is accepted


@pytest.mark.parametrize("unit", REFRESH_PREDECESSORS)
@pytest.mark.parametrize(
    "guard_prefix",
    [
        "ExecStartPre=",
        "ConditionPathExists=/etc/obsidian-ai/semantic-index-refresh.env",
        "ConditionPathExists=!/run/obsidian-automation/semantic-refresh-inhibited.json",
    ],
    ids=["predecessor-success", "refresh-opt-in", "deployment-inhibit"],
)
def test_safe_smoke_rejects_missing_refresh_guard(
    tmp_path: Path, unit: str, guard_prefix: str,
) -> None:
    systemd, revision = _fixture(tmp_path)
    path = systemd / unit
    lines = path.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith(guard_prefix) for line in lines)
    path.write_text(
        "\n".join(line for line in lines if not line.startswith(guard_prefix)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PreReviewProductionSmokeError, match="missing required marker"):
        run_safe_smoke(
            expected_revision=REVISION,
            revision_env=revision,
            systemd_dir=systemd,
        )


@pytest.mark.parametrize("dependency_kind", ["Requires", "Wants", "Requisite"])
@pytest.mark.parametrize("refresh_unit", REFRESH_PREDECESSORS)
def test_safe_smoke_rejects_planner_refresh_dependency(
    tmp_path: Path, dependency_kind: str, refresh_unit: str,
) -> None:
    systemd, revision = _fixture(tmp_path)
    path = systemd / "obsidian-ai-input-planner.service"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[Unit]\n",
            f"[Unit]\n{dependency_kind}=unrelated.service {refresh_unit}\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        PreReviewProductionSmokeError,
        match="Input Planner must not start Semantic Index refresh",
    ):
        run_safe_smoke(
            expected_revision=REVISION,
            revision_env=revision,
            systemd_dir=systemd,
        )


def test_legacy_planner_does_not_require_semantic_refresh() -> None:
    text = Path("examples/ai/obsidian-ai-input-planner.service").read_text(encoding="utf-8")
    assert "Environment=AI_INPUT_MODE=legacy" in text
    dependencies = {
        dependency
        for line in text.replace("\\\n", " ").splitlines()
        if line.partition("=")[0] in {"Requires", "Requisite", "Wants", "BindsTo"}
        for dependency in line.partition("=")[2].split()
    }
    assert dependencies.isdisjoint(REFRESH_PREDECESSORS)
