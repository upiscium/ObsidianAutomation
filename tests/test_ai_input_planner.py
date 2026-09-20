from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import obsidian_automation.ai_input_planner as planner
from obsidian_automation.ai_input_planner import (
    COVERAGE_POLICY,
    RANDOM_POLICY,
    PlannerState,
    build_catalog,
    choose_selection,
    plan_once,
)
from obsidian_automation.context_bundle import load_context_bundle
from obsidian_automation.human_projection import parse_request
from obsidian_automation.pre_review_job import job_status


REVISION = "a" * 40


def _note(frontmatter: str, body: str) -> str:
    return f"---\n{frontmatter}---\n{body}\n"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "Active.md").write_text(
        _note(
            "type: knowledge-note\nstatus: active\ncategory: summary\n",
            "# Active\nReusable knowledge.",
        ),
        encoding="utf-8",
    )
    (knowledge / "Archived.md").write_text(
        _note(
            "type: knowledge-note\nstatus: archived\ncategory: summary\n",
            "# Archived\nDo not select.",
        ),
        encoding="utf-8",
    )

    running = vault / "10-Project" / "Running"
    running.mkdir(parents=True)
    (running / "Running.md").write_text(
        _note(
            "type: project\nstatus: running\nworkspace: [[03-Workspace/Dev]]\n",
            "# Running",
        ),
        encoding="utf-8",
    )
    (running / "Design.md").write_text(
        _note(
            'type: project-note\nlifecycle: active\nproject: "[[10-Project/Running/Running|Running]]"\n',
            "# Design\nProject-local design insight.",
        ),
        encoding="utf-8",
    )

    cancelled = vault / "10-Project" / "Cancelled"
    cancelled.mkdir(parents=True)
    (cancelled / "Cancelled.md").write_text(
        _note("type: project\nstatus: cancelled\n", "# Cancelled"),
        encoding="utf-8",
    )
    (cancelled / "Old.md").write_text(
        _note(
            'type: project-note\nlifecycle: active\nproject: "[[Cancelled]]"\n',
            "# Old\nMust not be selected.",
        ),
        encoding="utf-8",
    )
    return vault


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "02-Orchestration" / "recipes").mkdir(parents=True)
    (state / "05-Context").mkdir()
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    return state



def _enable_human_projection(state: Path) -> None:
    root = state / "16-Human-Projection"
    root.mkdir()
    for role in ("reader", "generator", "validator", "evaluator", "reviewer", "executor", "sync"):
        (root / role).mkdir()
    (state / "17-Human-Projection-Result").mkdir()

def test_catalog_mixes_active_knowledge_and_project_notes(tmp_path: Path) -> None:
    catalog = build_catalog(_vault(tmp_path))

    assert [(entry.source_kind, entry.path) for entry in catalog.entries] == [
        ("project-note", "10-Project/Running/Design.md"),
        ("knowledge", "11-Knowledge/Active.md"),
    ]
    project = next(entry for entry in catalog.entries if entry.source_kind == "project-note")
    assert project.project_status == "running"
    assert not catalog.warnings


def test_coverage_and_random_policies_are_deterministic_and_mixed(tmp_path: Path) -> None:
    catalog = build_catalog(_vault(tmp_path))
    state = PlannerState(None, 1, 0, 0)

    coverage, next_state = choose_selection(
        catalog,
        state,
        batch_size=2,
        coverage_cycles=1,
        random_cycles=1,
    )
    assert coverage is not None
    assert coverage.policy == COVERAGE_POLICY
    assert {entry.source_kind for entry in coverage.entries} == {
        "knowledge",
        "project-note",
    }

    random_selection, _ = choose_selection(
        catalog,
        next_state,
        batch_size=2,
        coverage_cycles=1,
        random_cycles=1,
    )
    repeated, _ = choose_selection(
        catalog,
        next_state,
        batch_size=2,
        coverage_cycles=1,
        random_cycles=1,
    )
    assert random_selection is not None and repeated is not None
    assert random_selection.policy == RANDOM_POLICY
    assert random_selection.to_json_bytes() == repeated.to_json_bytes()
    assert {entry.source_kind for entry in random_selection.entries} == {
        "knowledge",
        "project-note",
    }


def _jobs(state: Path):
    db = state / "02-Orchestration" / "pre-review-jobs.sqlite3"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT j.job_id, j.context_sha256, j.recipe_sha256,
                   g.generation_id, g.state
            FROM jobs j
            JOIN generations g ON g.job_id = j.job_id
            ORDER BY j.created_at, g.generation_index
            """
        ).fetchall()
    finally:
        conn.close()


def test_plan_once_recovers_submitted_job_after_projection_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    _enable_human_projection(state)
    original_emit = planner.emit_input_projection

    def fail_projection(*_args, **_kwargs):
        raise OSError("synthetic projection failure")

    monkeypatch.setattr(planner, "emit_input_projection", fail_projection)
    with pytest.raises(OSError, match="synthetic projection failure"):
        planner.plan_once(
            state,
            vault,
            deployed_revision=REVISION,
            generator_model="gemma4:12b",
            evaluator_model="gemma4:12b",
            batch_size=2,
            target_inflight=1,
            coverage_cycles=1,
            random_cycles=0,
        )

    before = _jobs(state)
    assert len(before) == 1
    assert before[0]["state"] == "queued"
    assert (state / "02-Orchestration" / "input-planner-pending.json").is_file()
    assert not (state / "02-Orchestration" / "input-planner-state.json").exists()

    monkeypatch.setattr(planner, "emit_input_projection", original_emit)
    recovered = planner.plan_once(
        state,
        vault,
        deployed_revision=REVISION,
        generator_model="gemma4:12b",
        evaluator_model="gemma4:12b",
        batch_size=2,
        target_inflight=1,
        coverage_cycles=1,
        random_cycles=0,
    )

    assert recovered["status"] == "recovered_pending_submission"
    after = _jobs(state)
    assert len(after) == 1
    assert after[0]["job_id"] == before[0]["job_id"]
    assert after[0]["generation_id"] == before[0]["generation_id"]
    assert not (state / "02-Orchestration" / "input-planner-pending.json").exists()

    requests = [
        parse_request(path.read_bytes())
        for path in sorted(
            (state / "16-Human-Projection" / "reader").glob("*.projection.json")
        )
    ]
    assert sorted(item.stage for item in requests) == ["context", "input"]
    assert {item.case_id for item in requests} == {before[0]["generation_id"]}


def test_plan_once_supersedes_stale_queued_job_when_revision_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    _enable_human_projection(state)
    original_emit = planner.emit_input_projection

    def fail_projection(*_args, **_kwargs):
        raise OSError("synthetic projection failure")

    monkeypatch.setattr(planner, "emit_input_projection", fail_projection)
    with pytest.raises(OSError):
        planner.plan_once(
            state,
            vault,
            deployed_revision=REVISION,
            generator_model="gemma4:12b",
            evaluator_model="gemma4:12b",
            batch_size=2,
            target_inflight=1,
            coverage_cycles=1,
            random_cycles=0,
        )

    old = _jobs(state)
    assert len(old) == 1
    old_job = str(old[0]["job_id"])
    old_generation = str(old[0]["generation_id"])
    old_context = str(old[0]["context_sha256"])

    monkeypatch.setattr(planner, "emit_input_projection", original_emit)
    new_revision = "c" * 40
    recovered = planner.plan_once(
        state,
        vault,
        deployed_revision=new_revision,
        generator_model="gemma4:12b",
        evaluator_model="gemma4:12b",
        batch_size=2,
        target_inflight=1,
        coverage_cycles=1,
        random_cycles=0,
    )

    assert recovered["status"] == "recovered_revision_submission"
    assert recovered["superseded_stale_generation"] is True
    rows = _jobs(state)
    assert len(rows) == 2
    by_job = {str(row["job_id"]): row for row in rows}
    assert by_job[old_job]["state"] == "superseded"

    new_job = str(recovered["job_id"])
    assert new_job != old_job
    assert by_job[new_job]["state"] == "queued"
    assert str(by_job[new_job]["context_sha256"]) == old_context
    assert str(recovered["generation_id"]) != old_generation
    assert not (state / "02-Orchestration" / "input-planner-pending.json").exists()


def test_plan_once_creates_mixed_context_and_one_durable_job(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    _enable_human_projection(state)

    result = plan_once(
        state,
        vault,
        deployed_revision=REVISION,
        generator_model="gemma4:12b",
        evaluator_model="gemma4:12b",
        batch_size=2,
        target_inflight=1,
        coverage_cycles=1,
        random_cycles=0,
    )

    assert result["status"] == "submitted"
    assert result["created"] is True
    assert result["source_kinds"] == {"knowledge": 1, "project-note": 1}
    context = load_context_bundle(state, str(result["context_sha256"]))
    assert {source.path for source in context.sources} == {
        "11-Knowledge/Active.md",
        "10-Project/Running/Design.md",
    }
    status = job_status(state, str(result["job_id"]))
    assert status["current_generation"]["state"] == "queued"

    requests = [
        parse_request(path.read_bytes())
        for path in sorted(
            (state / "16-Human-Projection" / "reader").glob("*.projection.json")
        )
    ]
    assert sorted(item.stage for item in requests) == ["context", "input"]
    assert all(
        item.case_id == status["current_generation"]["generation_id"]
        for item in requests
    )
    input_projection = next(item for item in requests if item.stage == "input")
    assert "10-Project/Running/Design.md" in input_projection.content
    assert "11-Knowledge/Active.md" in input_projection.content

    second = plan_once(
        state,
        vault,
        deployed_revision=REVISION,
        generator_model="gemma4:12b",
        evaluator_model="gemma4:12b",
        batch_size=2,
        target_inflight=1,
        coverage_cycles=1,
        random_cycles=0,
    )
    assert second["status"] == "target_queue_satisfied"
    assert second["inflight"] == 1
