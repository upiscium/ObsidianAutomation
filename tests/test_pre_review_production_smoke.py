from __future__ import annotations

from pathlib import Path

import pytest

from obsidian_automation.pre_review_production_smoke import (
    PreReviewProductionSmokeError,
    REQUIRED_UNITS,
    run_safe_smoke,
)


REVISION = "a" * 40


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
