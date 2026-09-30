from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from obsidian_automation.context_bundle import (
    build_context_bundle,
    store_context_bundle,
)
from obsidian_automation.planner_cadence import load_cadence_state
from obsidian_automation.semantic_corpus import (
    build_semantic_corpus,
    store_semantic_corpus_manifest,
)
from obsidian_automation.semantic_index import (
    EmbeddingResult,
    EmbeddingResultSet,
    EmbeddingResultSetEntry,
    finalize_semantic_index,
    load_embedding_request,
    prepare_semantic_embedding_plan,
    store_embedding_result,
    store_embedding_result_set,
)
from obsidian_automation.semantic_selection import (
    POLICIES,
    SemanticSelectionError,
    build_semantic_selection,
    load_semantic_selection,
    observe_semantic_selection,
    parse_semantic_selection,
)


MODEL = "fixture-embedding:latest"
MODEL_DIGEST = "a" * 64
DIMENSION = 6


def _note(frontmatter: str, body: str) -> str:
    return f"---\n{frontmatter}---\n{body}\n"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for root in ("00-DailyNote", "05-Idea", "10-Project", "11-Knowledge"):
        (vault / root).mkdir(parents=True)

    daily = vault / "00-DailyNote" / "2026" / "09"
    daily.mkdir(parents=True)
    (daily / "2026-09-29.md").write_text(
        _note(
            "type: daily-review\n",
            "# Note\nQR labels and cable inventory should share one durable identifier.",
        ),
        encoding="utf-8",
    )
    (daily / "2026-09-28.md").write_text(
        _note(
            "type: daily-review\n",
            "# Note\nTrain cabin noise was difficult to suppress.",
        ),
        encoding="utf-8",
    )

    ideas = vault / "05-Idea"
    (ideas / "Inventory.md").write_text(
        _note(
            "type: idea\n"
            "title: Inventory semantics\n"
            "created: 2026-09-29\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: active\n",
            "# Inventory Idea\nUse one semantic identifier across cable labels and stock records.",
        ),
        encoding="utf-8",
    )
    (ideas / "Bridge.md").write_text(
        _note(
            "type: idea\n"
            "title: Cross-domain bridge\n"
            "created: 2026-09-28\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: active\n",
            "# Bridge Idea\nRelate worker assignment to spatial representation routing.",
        ),
        encoding="utf-8",
    )

    for name, summary, note_body in (
        (
            "Inventory",
            "Inventory project for QR-labelled physical assets.",
            "Map QR identifiers to cable stock and storage records.",
        ),
        (
            "LLM",
            "GPU worker scheduling for local language-model inference.",
            "Assign inference work to bounded GPU worker slots.",
        ),
        (
            "Render",
            "Rendering project for queue and synchronization research.",
            "Command queue ownership and barrier scheduling.",
        ),
    ):
        project = vault / "10-Project" / name
        project.mkdir()
        (project / f"{name}.md").write_text(
            _note(
                "type: project\n"
                "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
                "status: running\n",
                f"# Project Summary\n{summary}",
            ),
            encoding="utf-8",
        )
        (project / "Notes.md").write_text(
            _note(
                "type: project-note\n"
                f"project: '[[10-Project/{name}/{name}|{name}]]'\n"
                "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
                "category: design\n"
                "lifecycle: active\n",
                f"# Design\n{note_body}",
            ),
            encoding="utf-8",
        )

    knowledge = vault / "11-Knowledge"
    knowledge_notes = {
        "LLM.md": (
            "system",
            "# GPU Scheduling\nGPU worker queues need bounded assignment and backpressure.",
        ),
        "ANC.md": (
            "audio",
            "# Active Noise Cancellation\nInverse-phase control suppresses persistent ambient sound.",
        ),
        "Spatial.md": (
            "research",
            "# Spatial Integration\nPrivate viewpoints can be integrated into one latent scene representation.",
        ),
        "Render.md": (
            "graphics",
            "# Queue Synchronization\nExplicit queue ownership controls rendering synchronization.",
        ),
        "InventoryPartial.md": (
            "inventory",
            "# Inventory Provenance\nAsset records benefit from stable identifiers and provenance.",
        ),
    }
    for name, (category, body) in knowledge_notes.items():
        (knowledge / name).write_text(
            _note(
                "type: knowledge-note\n"
                "status: active\n"
                f"category: {category}\n"
                "maturity: stable\n"
                "source_type: self\n",
                body,
            ),
            encoding="utf-8",
        )
    return vault


SOURCE_VECTORS = {
    "00-DailyNote/2026/09/2026-09-29.md": (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    "00-DailyNote/2026/09/2026-09-28.md": (0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
    "05-Idea/Bridge.md": (0.7, 0.0, 0.7, 0.0, 0.0, 0.0),
    "05-Idea/Inventory.md": (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    "10-Project/Inventory/Inventory.md": (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    "10-Project/Inventory/Notes.md": (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    "10-Project/LLM/LLM.md": (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "10-Project/LLM/Notes.md": (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "10-Project/Render/Render.md": (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    "10-Project/Render/Notes.md": (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    "11-Knowledge/LLM.md": (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "11-Knowledge/ANC.md": (0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
    "11-Knowledge/Spatial.md": (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
    "11-Knowledge/Render.md": (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    "11-Knowledge/InventoryPartial.md": (0.0, 0.0, 0.0, 0.0, 0.6, 0.8),
}


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "02-Orchestration" / "recipes").mkdir(parents=True)
    (state / "02-Orchestration" / "semantic-selections").mkdir()
    (state / "04-Index").mkdir()
    (state / "05-Context").mkdir()
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    return state


def _semantic_index(tmp_path: Path):
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    corpus = build_semantic_corpus(vault)
    corpus_sha, _ = store_semantic_corpus_manifest(state, corpus)
    plan_sha, _, plan = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )

    results: list[EmbeddingResultSetEntry] = []
    for entry in plan.requests:
        request = load_embedding_request(state, entry.request_sha256)
        result = EmbeddingResult(
            request_sha256=entry.request_sha256,
            provider=plan.provider,
            adapter_version=plan.adapter_version,
            model_identifier=plan.model_identifier,
            model_revision=plan.model_revision,
            vector=SOURCE_VECTORS[request.source_path],
        )
        result_sha, _ = store_embedding_result(state, result)
        results.append(
            EmbeddingResultSetEntry(
                request_sha256=entry.request_sha256,
                result_sha256=result_sha,
            )
        )

    result_set = EmbeddingResultSet(
        plan_sha256=plan_sha,
        vector_dimension=DIMENSION,
        vector_encoding="json-number-finite-v0",
        results=tuple(results),
    )
    result_set_sha, _ = store_embedding_result_set(state, result_set)
    index_sha, _, index = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )
    return vault, state, index_sha, index


def _seed_recent_context(
    vault: Path,
    state: Path,
    *,
    source_path: str,
) -> str:
    context = build_context_bundle(
        vault,
        query="historical automatic generation",
        source_paths=[source_path],
        created_at="2026-09-29T00:00:00Z",
    )
    context_sha, _ = store_context_bundle(state, context)

    db = state / "02-Orchestration" / "pre-review-jobs.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                context_sha256 TEXT NOT NULL,
                recipe_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO metadata(key, value)
            VALUES('schema_version', '1');
            """
        )
        conn.execute(
            """
            INSERT INTO jobs(job_id, context_sha256, recipe_sha256, created_at)
            VALUES(?, ?, ?, ?)
            """,
            ("1" * 64, context_sha, "2" * 64, "2026-09-29T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    return context_sha


@pytest.mark.parametrize("policy", POLICIES)
def test_all_initial_policies_are_deterministic_and_auditable(
    tmp_path: Path,
    policy: str,
) -> None:
    vault, state, index_sha, index = _semantic_index(tmp_path)

    first = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy=policy,
        recent_context_limit=0,
    )
    second = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy=policy,
        recent_context_limit=0,
    )

    assert first.to_json_bytes() == second.to_json_bytes()
    assert first.selection_policy == policy
    assert first.semantic_index_sha256 == index_sha
    assert first.corpus_manifest_sha256 == index.corpus_manifest_sha256
    assert first.retrieval_mode == "hybrid"
    assert 1 <= len(first.anchors) <= 2
    assert 2 <= len(first.selected) <= 8
    assert {item.source_path for item in first.anchors}.issubset(
        {item.source_path for item in first.selected}
    )
    assert all(item.rank == index + 1 for index, item in enumerate(first.selected))
    assert all(-1.0 <= item.cosine_score <= 1.0 for item in first.selected)
    assert all(item.lexical_score >= 0.0 for item in first.selected)
    assert first.novelty.decision in {"selected", "skipped"}
    assert "recent_context_skip" in first.novelty.thresholds
    assert "cluster_coherence_min" in first.novelty.thresholds


def test_project_distill_versions_pin_retrieval_semantics(
    tmp_path: Path,
) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)

    v0 = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-project-distill-v0",
        recent_context_limit=0,
    )
    v1 = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-project-distill-v1",
        recent_context_limit=0,
    )

    assert v0.lexical_weight == pytest.approx(0.60)
    assert "retrieval_profile" not in v0.policy_observations

    assert v1.lexical_weight == pytest.approx(0.15)
    assert v1.policy_observations["retrieval_profile"] == (
        "semantic-retrieval-v1"
    )
    assert v0.source_kind_weights == v1.source_kind_weights


def test_project_distill_v1_parser_rejects_profile_or_weight_drift(
    tmp_path: Path,
) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    record = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-project-distill-v1",
        recent_context_limit=0,
    )
    payload = json.loads(record.to_json_bytes())

    wrong_weight = json.loads(record.to_json_bytes())
    wrong_weight["retrieval"]["lexical_weight"] = 0.60
    wrong_weight["retrieval"]["vector_weight"] = 0.40
    with pytest.raises(
        SemanticSelectionError,
        match="weight does not match policy version",
    ):
        parse_semantic_selection(
            json.dumps(
                wrong_weight,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    payload["policy_observations"]["retrieval_profile"] = (
        "semantic-retrieval-v0"
    )
    with pytest.raises(
        SemanticSelectionError,
        match="profile does not match policy version",
    ):
        parse_semantic_selection(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )


def test_policy_specific_anchor_contracts(tmp_path: Path) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)

    project = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-project-distill-v0",
        recent_context_limit=0,
    )
    assert project.anchors[0].source_kind in {"project", "project-note"}

    timeline = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-timeline-v0",
        recent_context_limit=0,
    )
    assert timeline.anchors[0].source_path == (
        "00-DailyNote/2026/09/2026-09-29.md"
    )
    assert timeline.policy_observations["anchor_date"] == "2026-09-29"

    bridge = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-bridge-v0",
        recent_context_limit=0,
    )
    assert len(bridge.anchors) == 2
    assert bridge.anchors[0].source_kind != bridge.anchors[1].source_kind
    pair_similarity = bridge.policy_observations["anchor_pair_similarity"]
    assert isinstance(pair_similarity, float)
    assert 0.30 <= pair_similarity <= 0.82

    gap = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-gap-v0",
        recent_context_limit=0,
    )
    assert gap.anchors[0].source_kind != "knowledge"
    assert gap.policy_observations["nonknowledge_support_similarity"] >= 0.35
    assert gap.novelty.knowledge_max_similarity is not None
    assert gap.novelty.knowledge_max_similarity < 0.78

    idea = build_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-idea-development-v0",
        recent_context_limit=0,
    )
    assert idea.anchors[0].source_path == "05-Idea/Inventory.md"
    assert idea.policy_observations["idea_status"] == "active"
    selected_kinds = {item.source_kind for item in idea.selected}
    assert "project" in selected_kinds
    assert "knowledge" in selected_kinds


def test_selection_record_is_content_addressed_and_round_trips(
    tmp_path: Path,
) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    selection_sha, path, record = observe_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-gap-v0",
        recent_context_limit=0,
    )

    assert path.name == f"{selection_sha}.semantic-selection.json"
    assert load_semantic_selection(state, selection_sha) == record
    assert parse_semantic_selection(path.read_bytes()) == record

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["selected"][0]["source_sha256"] = "not-a-sha"
    with pytest.raises(SemanticSelectionError, match="selected source SHA"):
        parse_semantic_selection(
            json.dumps(
                tampered,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )


def test_recent_generated_context_triggers_novelty_skip_and_durable_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    context_sha = _seed_recent_context(
        vault,
        state,
        source_path="10-Project/Inventory/Notes.md",
    )

    monkeypatch.setattr(
        "obsidian_automation.semantic_selection._utc_now",
        lambda: "2026-09-29T01:00:00Z",
    )
    selection_sha, _path, record = observe_semantic_selection(
        state,
        vault,
        semantic_index_sha256=index_sha,
        policy="semantic-gap-v0",
        recent_context_limit=8,
        record_skip=True,
    )

    assert selection_sha
    assert record.novelty.decision == "skipped"
    assert record.novelty.skip_reason == "recent_context_too_similar"
    assert record.novelty.recent_context_max_similarity is not None
    assert record.novelty.recent_context_max_similarity >= 0.94
    assert record.novelty.recent_contexts[0].context_sha256 == context_sha

    cadence = load_cadence_state(state)
    assert cadence.last_novelty_skip_at == "2026-09-29T01:00:00Z"
    assert cadence.last_novelty_skip_reason == (
        "semantic-gap-v0:recent_context_too_similar"
    )
    assert cadence.last_submission_at is None


def test_selection_fails_closed_when_semantic_source_changes(
    tmp_path: Path,
) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    source = vault / "05-Idea" / "Inventory.md"
    source.write_text(
        source.read_text(encoding="utf-8") + "\nChanged after index.\n",
        encoding="utf-8",
    )

    with pytest.raises(SemanticSelectionError, match="stale"):
        build_semantic_selection(
            state,
            vault,
            semantic_index_sha256=index_sha,
            policy="semantic-idea-development-v0",
            recent_context_limit=0,
        )

def test_selection_schema_keeps_generation_objective_outside_contract() -> None:
    schema_path = Path("schemas/semantic-selection-v1.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert "selection_policy" in schema["required"]
    assert "semantic_index_sha256" in schema["required"]
    text = schema_path.read_text(encoding="utf-8")
    assert "objective_policy" not in text
    assert "generation_objective" not in text

