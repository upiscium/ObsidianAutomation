"""No-effect first Stage bootstrap from verified Git objects (#294)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

BOOTSTRAP = Path(__file__).resolve().parents[1] / "tools" / "bootstrap_github_stage.py"
SHA_FAKE_RECEIPT = "a" * 64


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True, text=True, capture_output=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def fake_checkout(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "app"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Stage Fixture")
    git(root, "config", "user.email", "stage@example.invalid")
    (root / "README.md").write_text("old installed code does not support Stage")
    git(root, "add", ".")
    git(root, "commit", "-qm", "old-production")
    previous = git(root, "rev-parse", "HEAD")

    package = root / "src" / "obsidian_automation"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# pinned fixture\n")
    (package / "github_production_update.py").write_text("# only target fake legacy\n")
    (package / "github_production_staging.py").write_text(
        'def inspect_stage(*, receipt_dir):\n'
        '    return {"status": "none"}\n'
        'def stage_update(**kwargs):\n'
        '    return "' + SHA_FAKE_RECEIPT + '"\n'
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "reviewed-target")
    target = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", target)
    git(root, "reset", "--hard", previous)
    assert git(root, "status", "--porcelain") == ""
    return root, previous, target


def invoke(root: Path, target: str, *args: str, mimic_root: bool = False):
    arguments = [
        "inspect", "--target-sha", target,
        "--app-root", str(root), "--venv-root", str(root / "fake-venv"),
        "--receipt-dir", str(root / "fake-receipts"),
    ]
    if args and args[0] == "stage":
        arguments[0] = "stage"
        args = args[1:]
    arguments.extend(args)
    if mimic_root:
        # Only the fake scratch test subprocess sees the euid substitution.
        # The loaded fake Stage is a return-value stub: no systemctl or pip.
        wrapper = (
            "import os,runpy,sys;"
            "os.geteuid=lambda:0;"
            "sys.argv=['bootstrap_github_stage.py',*sys.argv[1:]];"
            f"runpy.run_path({str(BOOTSTRAP)!r},run_name='__main__')"
        )
        cmd = [sys.executable, "-I", "-B", "-c", wrapper, *arguments]
    else:
        cmd = [sys.executable, "-I", "-B", str(BOOTSTRAP), *arguments]
    return subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=20)


def test_inspect_reports_exact_reviewed_head_without_any_stage(fake_checkout):
    root, previous, target = fake_checkout
    result = invoke(root, target)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "source_attested_no_effects"
    assert report["current_sha"] == previous
    assert report["reviewed_target_sha"] == target
    assert report["stage_control"] == "none"
    assert report["stage_effects_authorized"] is False
    assert git(root, "rev-parse", "HEAD") == previous
    assert not (root / "fake-receipts").exists()


def test_pinned_stage_requires_explicit_operator_effects_flag(fake_checkout):
    root, previous, target = fake_checkout
    result = invoke(root, target, "stage")
    assert result.returncode == 2
    assert json.loads(result.stderr)["status"] == "blocked"
    assert git(root, "rev-parse", "HEAD") == previous


def test_pinned_stage_invokes_exact_target_source_only_with_flag(fake_checkout):
    root, previous, target = fake_checkout
    result = invoke(root, target, "stage", "--approve-stage-effects", mimic_root=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "staged"
    assert report["target_sha"] == target
    assert report["stage_sha256"] == SHA_FAKE_RECEIPT
    assert report["live_writer_smoke"] == "not_authorized"
    # Actual real-world Stage does change code/timers. The fake Stage in this
    # scratch-only test is a return-value stub, and cannot affect production.
    assert git(root, "rev-parse", "HEAD") == previous


@pytest.mark.parametrize("problem", ("dirty", "unmerged", "unrelated_target", "missing_git_object"))
def test_inspect_fails_closed_on_unreviewed_git_state(fake_checkout, problem):
    root, previous, target = fake_checkout
    if problem == "dirty":
        (root / "do-not-erase.md").write_text("operator edits")
    elif problem == "unmerged":
        git(root, "update-ref", "refs/remotes/origin/main", previous)
    elif problem == "unrelated_target":
        # The original previous SHA cannot be used as a new Stage without
        # violating the requirement to review an actual target commit.
        (root / "README.md").write_text("modified")
        git(root, "add", ".")
        git(root, "commit", "-qm", "unrelated")
    else:
        target = "0" * 40
    result = invoke(root, target)
    assert result.returncode == 2
    report = json.loads(result.stderr)
    assert report["status"] == "blocked"
    assert report["stage_effects_may_have_occurred"] is False


def test_stage_failure_does_not_claim_safe_retry(fake_checkout):
    root, previous, target = fake_checkout
    git(root, "checkout", "-q", target)
    source = root / "src" / "obsidian_automation" / "github_production_staging.py"
    source.write_text(
        'def inspect_stage(*, receipt_dir):\n'
        '    return {"status": "none"}\n'
        'def stage_update(**kwargs):\n'
        '    raise RuntimeError("simulated post-timer failure")\n'
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "reviewed-stage-failure-fixture")
    bad_stage_sha = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", bad_stage_sha)
    git(root, "reset", "--hard", previous)
    output = invoke(root, bad_stage_sha, "stage", "--approve-stage-effects", mimic_root=True)
    assert output.returncode == 2
    record = json.loads(output.stderr)
    assert record["status"] == "failed_manual_reconciliation_required"
    assert record["stage_effects_may_have_occurred"] is True
    assert record["automatic_retry_authorized"] is False
    assert "simulated post-timer failure" not in output.stderr


def test_symlinked_target_module_refuses_execution(fake_checkout):
    root, previous, target = fake_checkout
    git(root, "checkout", "-q", target)
    package = root / "src" / "obsidian_automation"
    (package / "github_production_staging.py").unlink()
    (package / "github_production_staging.py").symlink_to("github_production_update.py")
    git(root, "add", ".")
    git(root, "commit", "-qm", "symlinked module")
    bad_sha = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", bad_sha)
    git(root, "reset", "--hard", previous)
    result = invoke(root, bad_sha)
    assert result.returncode == 2
    assert json.loads(result.stderr)["status"] == "blocked"


def test_inspect_cannot_be_misrepresented_as_stage_permission(fake_checkout):
    root, previous, target = fake_checkout
    result = invoke(root, target, "--approve-stage-effects")
    assert result.returncode != 0
    assert "inspect cannot receive Stage-effects approval" in result.stderr
