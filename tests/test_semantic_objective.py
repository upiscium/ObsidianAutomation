from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.human_projection import (
    emit_semantic_objective_generation_projection,
    parse_request,
)
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
    load_semantic_index_manifest,
    prepare_semantic_embedding_plan,
    store_embedding_result,
    store_embedding_result_set,
)
from obsidian_automation.semantic_objective import (
    DEEP_KNOWLEDGE,
    IDEA_DISCOVERY,
    PROJECT_ADOPTION,
    IdeaCandidate,
    NoCandidate,
    ObjectiveContextSource,
    ProjectAdoptionCandidate,
    SemanticObjectiveContext,
    SemanticObjectiveError,
    assess_deep_knowledge_evidence,
    build_objective_context,
    build_objective_generation,
    load_objective_candidate,
    load_objective_context,
    load_objective_generation,
    objective_output_schema,
    parse_objective_output,
    prompt_template_sha256,
    render_objective_prompt,
    store_objective_candidate,
    store_objective_context,
    store_objective_generation,
)
from obsidian_automation.semantic_objective_generation import (
    generate_semantic_objective_with_ollama,
    generate_semantic_objective_with_openai_compatible,
)
from obsidian_automation.semantic_retrieval import RetrievalFilter
from obsidian_automation.semantic_selection import (
    AnchorBinding,
    NoveltyObservation,
    SelectedChunk,
    SemanticSelectionRecord,
    store_semantic_selection,
)


MODEL = "fixture-model:latest"
MODEL_DIGEST = "a" * 64
REVISION = "b" * 40
VECTOR_DIMENSION = 3


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
            "# Note\nCable inventory repeatedly loses location provenance.",
        ),
        encoding="utf-8",
    )

    (vault / "05-Idea" / "Inventory.md").write_text(
        _note(
            "type: idea\n"
            "title: Inventory identity\n"
            "created: 2026-09-29\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: active\n",
            "# Inventory Idea\nUse one stable identifier across labels and stock records.",
        ),
        encoding="utf-8",
    )

    for name, summary, detail in (
        (
            "Inventory",
            "Physical asset inventory with QR-labelled cables.",
            "Map a stable asset identifier to storage and stock state.",
        ),
        (
            "Automation",
            "Automation project for durable event processing.",
            "Track provenance and idempotency across automated state changes.",
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
                f"# Design\n{detail}",
            ),
            encoding="utf-8",
        )

    (vault / "11-Knowledge" / "Provenance.md").write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Provenance\nStable identifiers preserve traceability across state transitions.",
        ),
        encoding="utf-8",
    )
    return vault


VECTORS = {
    "00-DailyNote/2026/09/2026-09-29.md": (0.0, 1.0, 0.0),
    "05-Idea/Inventory.md": (0.0, 1.0, 0.1),
    "10-Project/Inventory/Inventory.md": (0.0, 0.95, 0.05),
    "10-Project/Inventory/Notes.md": (0.0, 0.9, 0.1),
    "10-Project/Automation/Automation.md": (0.2, 0.7, 0.1),
    "10-Project/Automation/Notes.md": (0.25, 0.7, 0.05),
    "11-Knowledge/Provenance.md": (0.5, 0.5, 0.0),
}


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    for path in (
        "00-Untrusted",
        "02-Orchestration/semantic-selections",
        "04-Index",
        "05-Context",
        "16-Human-Projection/generator",
        "24-Locks/read-view",
    ):
        (state / path).mkdir(parents=True)
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
            vector=VECTORS[request.source_path],
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
        vector_dimension=VECTOR_DIMENSION,
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
    return vault, state, corpus, index_sha, index


def _selection(
    state: Path,
    corpus,
    index_sha: str,
    *,
    policy: str,
    paths: list[str],
    anchor_paths: set[str],
) -> str:
    by_path = {source.path: source for source in corpus.sources}
    selected: list[SelectedChunk] = []
    anchors: list[AnchorBinding] = []
    for rank, path in enumerate(paths, 1):
        source = by_path[path]
        chunk = source.chunks[0]
        role = "anchor" if path in anchor_paths else "support"
        selected.append(
            SelectedChunk(
                rank=rank,
                role=role,
                chunk_id=chunk.chunk_id,
                source_path=path,
                source_kind=source.source_kind,
                source_sha256=source.content_sha256,
                content_sha256=chunk.content_sha256,
                score=1.0 - rank * 0.01,
                lexical_score=0.5,
                lexical_normalized=0.5,
                cosine_score=0.8,
                semantic_normalized=0.9,
                source_kind_weight=1.0,
            )
        )
        if path in anchor_paths:
            anchors.append(
                AnchorBinding(
                    role="primary" if not anchors else "bridge-secondary",
                    chunk_id=chunk.chunk_id,
                    source_path=path,
                    source_kind=source.source_kind,
                    source_sha256=source.content_sha256,
                    content_sha256=chunk.content_sha256,
                )
            )
    record = SemanticSelectionRecord(
        selection_policy=policy,
        semantic_index_sha256=index_sha,
        corpus_manifest_sha256=load_semantic_index_manifest(
            state,
            index_sha,
        ).corpus_manifest_sha256,
        metadata_filters=RetrievalFilter().payload(),
        retrieval_mode="hybrid",
        lexical_weight=0.6,
        source_kind_weights={
            "daily": 1.0,
            "idea": 1.0,
            "project": 1.0,
            "project-note": 1.0,
            "knowledge": 1.0,
        },
        anchors=tuple(anchors),
        selected=tuple(selected),
        novelty=NoveltyObservation(
            decision="selected",
            skip_reason=None,
            cluster_coherence=0.8,
            recent_context_max_similarity=None,
            knowledge_max_similarity=0.5,
            recent_contexts=(),
            recent_context_unmatched_count=0,
            thresholds={
                "recent_context_skip": 0.94,
                "knowledge_coverage_skip": 0.98,
                "cluster_coherence_min": 0.35,
            },
        ),
        policy_observations={},
    )
    digest, _ = store_semantic_selection(state, record)
    return digest


def _deep_context(tmp_path: Path):
    vault, state, corpus, index_sha, _ = _semantic_index(tmp_path)
    selection_sha = _selection(
        state,
        corpus,
        index_sha,
        policy="semantic-project-distill-v0",
        paths=[
            "10-Project/Inventory/Notes.md",
            "11-Knowledge/Provenance.md",
            "00-DailyNote/2026/09/2026-09-29.md",
        ],
        anchor_paths={"10-Project/Inventory/Notes.md"},
    )
    context = build_objective_context(
        state,
        vault,
        selection_sha256=selection_sha,
        objective_policy=DEEP_KNOWLEDGE,
        created_at="2026-09-29T03:00:00Z",
    )
    context_sha, _ = store_objective_context(state, context)
    return vault, state, index_sha, selection_sha, context_sha, context


def _idea_context(tmp_path: Path):
    vault, state, corpus, index_sha, _ = _semantic_index(tmp_path)
    selection_sha = _selection(
        state,
        corpus,
        index_sha,
        policy="semantic-gap-v0",
        paths=[
            "00-DailyNote/2026/09/2026-09-29.md",
            "10-Project/Inventory/Notes.md",
            "11-Knowledge/Provenance.md",
        ],
        anchor_paths={"00-DailyNote/2026/09/2026-09-29.md"},
    )
    context = build_objective_context(
        state,
        vault,
        selection_sha256=selection_sha,
        objective_policy=IDEA_DISCOVERY,
        created_at="2026-09-29T03:00:00Z",
    )
    context_sha, _ = store_objective_context(state, context)
    return state, selection_sha, context_sha, context


def _project_context(tmp_path: Path):
    vault, state, corpus, index_sha, _ = _semantic_index(tmp_path)
    selection_sha = _selection(
        state,
        corpus,
        index_sha,
        policy="semantic-idea-development-v0",
        paths=[
            "05-Idea/Inventory.md",
            "10-Project/Inventory/Inventory.md",
            "10-Project/Automation/Automation.md",
            "11-Knowledge/Provenance.md",
        ],
        anchor_paths={"05-Idea/Inventory.md"},
    )
    context = build_objective_context(
        state,
        vault,
        selection_sha256=selection_sha,
        objective_policy=PROJECT_ADOPTION,
        created_at="2026-09-29T03:00:00Z",
    )
    context_sha, _ = store_objective_context(state, context)
    return state, selection_sha, context_sha, context


def _deep_wire() -> bytes:
    return json.dumps(
        {
            "objective_policy": DEEP_KNOWLEDGE,
            "candidate_kind": "knowledge_candidate",
            "candidate": {
                "title": "資産識別子と来歴管理",
                "category": "summary",
                "source_type": "self",
                "body": (
                    "# 中心概念\n\n"
                    "安定した識別子を使うと，ラベル・在庫・保管場所の状態を同じ対象へ結び付けられる。\n\n"
                    "# 仕組み\n\n"
                    "識別子を変えずに状態だけを更新することで，変更履歴と現在状態を分離して追跡できる。\n\n"
                    "# 制約とトレードオフ\n\n"
                    "識別子再利用を避け，物理ラベルと記録の対応を維持する必要がある。\n"
                ),
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _idea_wire(*, extra: dict[str, object] | None = None) -> bytes:
    candidate = {
        "title": "ケーブル識別子から保管履歴を追跡する仕組み",
        "summary": "QRラベルの識別子を在庫だけでなく保管場所履歴にも使う。",
        "rationale": "Dailyの紛失観察とProjectの安定識別子設計を組み合わせられる。",
        "supporting_evidence": [
            "Cable inventory repeatedly loses location provenance.",
            "Map a stable asset identifier to storage and stock state.",
        ],
        "uncertainties": ["既存ラベルの再採番方針は未確定。"],
    }
    if extra:
        candidate.update(extra)
    return json.dumps(
        {
            "objective_policy": IDEA_DISCOVERY,
            "candidate_kind": "idea_candidate",
            "candidate": candidate,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _project_wire(project_path: str) -> bytes:
    return json.dumps(
        {
            "objective_policy": PROJECT_ADOPTION,
            "candidate_kind": "project_adoption_proposal",
            "candidate": {
                "idea_path": "05-Idea/Inventory.md",
                "proposals": [
                    {
                        "project_path": project_path,
                        "fit_rationale": "QR識別子と物理資産管理を直接扱う。",
                        "supporting_evidence": [
                            "Physical asset inventory with QR-labelled cables."
                        ],
                        "risks_conflicts": ["既存の識別子体系との移行が必要。"],
                        "missing_information": ["既存ラベルの互換要件。"],
                    }
                ],
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


def test_objective_context_binds_selection_and_exact_selected_chunks(
    tmp_path: Path,
) -> None:
    _vault_root, state, index_sha, selection_sha, context_sha, context = (
        _deep_context(tmp_path)
    )
    loaded = load_objective_context(state, context_sha)
    assert loaded == context
    assert context.selection_sha256 == selection_sha
    assert context.semantic_index_sha256 == index_sha
    assert context.objective_policy == DEEP_KNOWLEDGE
    assert context.candidate_kind == "knowledge_candidate"
    assert [item.path for item in context.sources] == [
        "10-Project/Inventory/Notes.md",
        "11-Knowledge/Provenance.md",
        "00-DailyNote/2026/09/2026-09-29.md",
    ]
    assert all("---" not in item.content for item in context.sources)
    assert all(item.content_sha256 for item in context.sources)


def test_generation_objectives_are_independent_from_selection_but_bounded(
    tmp_path: Path,
) -> None:
    vault, state, corpus, index_sha, _ = _semantic_index(tmp_path)
    idea_development = _selection(
        state,
        corpus,
        index_sha,
        policy="semantic-idea-development-v0",
        paths=[
            "05-Idea/Inventory.md",
            "10-Project/Inventory/Inventory.md",
            "11-Knowledge/Provenance.md",
        ],
        anchor_paths={"05-Idea/Inventory.md"},
    )
    # deep-knowledge may reuse an idea-development selection.
    deep = build_objective_context(
        state,
        vault,
        selection_sha256=idea_development,
        objective_policy=DEEP_KNOWLEDGE,
    )
    assert deep.objective_policy == DEEP_KNOWLEDGE

    with pytest.raises(SemanticObjectiveError, match="not compatible"):
        build_objective_context(
            state,
            vault,
            selection_sha256=idea_development,
            objective_policy=IDEA_DISCOVERY,
        )


def test_objective_prompts_and_output_contracts_are_explicit(tmp_path: Path) -> None:
    _vault_root, _state, _index_sha, _selection_sha, _context_sha, deep = (
        _deep_context(tmp_path)
    )
    prompt = render_objective_prompt(deep)
    assert prompt.objective_policy == DEEP_KNOWLEDGE
    assert prompt.candidate_kind == "knowledge_candidate"
    assert prompt.template_version == "deep-knowledge-generator-v2"
    assert prompt.template_sha256 == prompt_template_sha256(DEEP_KNOWLEDGE)
    assert "central idea" in prompt.system
    payload = json.loads(prompt.user)
    assert payload["selection_policy"] == "semantic-project-distill-v0"
    assert payload["sources"][0]["content"]
    assert prompt.output_schema["properties"]["objective_policy"]["const"] == DEEP_KNOWLEDGE

    output = parse_objective_output(_deep_wire(), context=deep)
    assert output.title == "資産識別子と来歴管理"
    assert "仕組み" in output.body



def test_deep_knowledge_evidence_gate_rejects_structural_only_context() -> None:
    structural = "## Notes\n"
    context = SemanticObjectiveContext(
        objective_policy=DEEP_KNOWLEDGE,
        candidate_kind="knowledge_candidate",
        selection_sha256="1" * 64,
        selection_policy="semantic-project-distill-v1",
        semantic_index_sha256="2" * 64,
        corpus_manifest_sha256="3" * 64,
        created_at="2026-10-01T00:00:00Z",
        sources=(
            ObjectiveContextSource(
                rank=1,
                role="anchor",
                path="10-Project/A/Status.md",
                source_kind="project-note",
                source_sha256="4" * 64,
                chunk_id="5" * 64,
                content_sha256="6" * 64,
                content=structural,
            ),
            ObjectiveContextSource(
                rank=2,
                role="support",
                path="11-Knowledge/Empty.md",
                source_kind="knowledge",
                source_sha256="7" * 64,
                chunk_id="8" * 64,
                content_sha256="9" * 64,
                content="# Empty\n-\n",
            ),
        ),
    )

    evidence = assess_deep_knowledge_evidence(context)
    assert evidence.sufficient is False
    assert evidence.substantive_source_count == 0
    assert evidence.substantive_bytes == 0
    assert evidence.reason == "insufficient_substantive_sources"


def test_deep_knowledge_supports_structured_no_candidate(tmp_path: Path) -> None:
    _vault_root, _state, _index_sha, _selection_sha, _context_sha, deep = (
        _deep_context(tmp_path)
    )
    payload = json.dumps(
        {
            "objective_policy": DEEP_KNOWLEDGE,
            "candidate_kind": "knowledge_candidate",
            "candidate": {
                "status": "no_candidate",
                "reason": "insufficient_evidence",
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")

    output = parse_objective_output(payload, context=deep)
    assert isinstance(output, NoCandidate)
    assert output.reason == "insufficient_evidence"
    schema = objective_output_schema(deep)
    candidate_schema = schema["properties"]["candidate"]
    assert "anyOf" in candidate_schema


def test_idea_candidate_cannot_choose_canonical_workspace_or_project(
    tmp_path: Path,
) -> None:
    _state_root, _selection_sha, _context_sha, context = _idea_context(tmp_path)
    output = parse_objective_output(_idea_wire(), context=context)
    assert isinstance(output, IdeaCandidate)
    assert output.title

    with pytest.raises(SemanticObjectiveError, match="properties"):
        parse_objective_output(
            _idea_wire(extra={"workspace": "[[03-Workspace/Lab/Lab|Lab]]"}),
            context=context,
        )
    schema = objective_output_schema(context)
    candidate_properties = schema["properties"]["candidate"]["properties"]
    assert "workspace" not in candidate_properties
    assert "project" not in candidate_properties
    assert "status" not in candidate_properties


def test_project_adoption_can_reference_only_selected_project_entries(
    tmp_path: Path,
) -> None:
    _state_root, _selection_sha, _context_sha, context = _project_context(tmp_path)
    schema = objective_output_schema(context)
    project_enum = (
        schema["properties"]["candidate"]["properties"]["proposals"]["items"]
        ["properties"]["project_path"]["enum"]
    )
    assert project_enum == [
        "10-Project/Automation/Automation.md",
        "10-Project/Inventory/Inventory.md",
    ]

    output = parse_objective_output(
        _project_wire("10-Project/Inventory/Inventory.md"),
        context=context,
    )
    assert isinstance(output, ProjectAdoptionCandidate)
    assert output.idea_path == "05-Idea/Inventory.md"

    with pytest.raises(SemanticObjectiveError, match="unselected Project"):
        parse_objective_output(
            _project_wire("10-Project/Unknown/Unknown.md"),
            context=context,
        )


def test_candidate_and_generation_provenance_bind_selection_objective_and_index(
    tmp_path: Path,
) -> None:
    _vault_root, state, index_sha, selection_sha, context_sha, context = (
        _deep_context(tmp_path)
    )
    output = parse_objective_output(_deep_wire(), context=context)
    candidate_sha, candidate_path, candidate = store_objective_candidate(
        state,
        context_sha256=context_sha,
        output=output,
    )
    assert candidate_path.name == f"{candidate_sha}.objective-candidate.json"
    assert load_objective_candidate(state, candidate_sha) == candidate
    assert candidate.selection_sha256 == selection_sha
    assert candidate.semantic_index_sha256 == index_sha

    generation = build_objective_generation(
        state,
        objective_context_sha256=context_sha,
        candidate_sha256=candidate_sha,
        implementation_revision=REVISION,
        prompt_template_version="deep-knowledge-generator-v2",
        prompt_template_sha256_value=prompt_template_sha256(DEEP_KNOWLEDGE),
        model_provider="ollama",
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
        model_config={"adapter_version": "fixture", "options": {"temperature": 0}},
        generated_at="2026-09-29T03:01:00Z",
    )
    generation_sha, generation_path = store_objective_generation(state, generation)
    assert generation_path.name == f"{generation_sha}.objective-generation.json"
    loaded = load_objective_generation(state, generation_sha)
    assert loaded == generation
    assert loaded.objective_policy == DEEP_KNOWLEDGE
    assert loaded.selection_sha256 == selection_sha
    assert loaded.semantic_index_sha256 == index_sha
    assert loaded.candidate_sha256 == candidate_sha


def test_human_projection_explicitly_labels_objective_and_noncanonical_action(
    tmp_path: Path,
) -> None:
    _vault_root, state, _index_sha, _selection_sha, context_sha, context = (
        _deep_context(tmp_path)
    )
    output = parse_objective_output(_deep_wire(), context=context)
    candidate_sha, _candidate_path, _ = store_objective_candidate(
        state,
        context_sha256=context_sha,
        output=output,
    )
    generation = build_objective_generation(
        state,
        objective_context_sha256=context_sha,
        candidate_sha256=candidate_sha,
        implementation_revision=REVISION,
        prompt_template_version="deep-knowledge-generator-v2",
        prompt_template_sha256_value=prompt_template_sha256(DEEP_KNOWLEDGE),
        model_provider="ollama",
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
        model_config={"adapter_version": "fixture"},
        generated_at="2026-09-29T03:01:00Z",
    )
    generation_sha, _ = store_objective_generation(state, generation)

    request_sha, request_path = emit_semantic_objective_generation_projection(
        state,
        case_id="c" * 64,
        objective_generation_sha256=generation_sha,
        candidate_sha256=candidate_sha,
    )
    request = parse_request(request_path.read_bytes())
    assert request_sha
    assert request.stage == "generation"
    assert request.source_kind == "semantic_objective_generation"
    assert "objective_policy: \"deep-knowledge-v1\"" in request.content
    assert "candidate_kind: \"knowledge_candidate\"" in request.content
    assert "Canonical action: **not created by this projection**" in request.content
    assert "11-Knowledge/" not in request.content.split("Canonical action:", 1)[0]


def test_openai_compatible_objective_generator_persists_exact_provenance(
    tmp_path: Path,
) -> None:
    _state0, _selection0, context_sha, context = _idea_context(tmp_path)
    state = _state0
    observed: dict[str, object] = {}

    def transport(base_url: str, **kwargs):
        observed.update(kwargs)
        return {
            "model": MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": _idea_wire().decode("utf-8"),
                    }
                }
            ],
        }

    result = generate_semantic_objective_with_openai_compatible(
        state,
        objective_context_sha256=context_sha,
        base_url="http://127.0.0.1:8000/v1",
        model=MODEL,
        implementation_revision=REVISION,
        transport=transport,
    )
    assert result.objective_policy == IDEA_DISCOVERY
    assert result.candidate_kind == "idea_candidate"
    candidate = load_objective_candidate(state, result.candidate_sha256)
    assert isinstance(candidate.output, IdeaCandidate)
    generation = load_objective_generation(state, result.generation_sha256)
    assert generation.selection_sha256 == context.selection_sha256
    assert generation.semantic_index_sha256 == context.semantic_index_sha256
    assert generation.model.provider == "openai-compatible"
    assert generation.model_config["objective_adapter_version"] == (
        "openai-semantic-objective-json-schema-v0"
    )
    response_format = observed["payload"]["response_format"]
    assert response_format["json_schema"]["schema"]["properties"]["objective_policy"]["const"] == IDEA_DISCOVERY


def test_ollama_objective_generator_enforces_selected_project_enum(
    tmp_path: Path,
) -> None:
    state, _selection_sha, context_sha, context = _project_context(tmp_path)
    calls: list[str] = []

    def transport(base_url: str, *, method: str, path: str, payload, timeout: float):
        calls.append(path)
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": MODEL,
                        "model": MODEL,
                        "digest": MODEL_DIGEST,
                    }
                ]
            }
        assert path == "/api/chat"
        enum = (
            payload["format"]["properties"]["candidate"]["properties"]["proposals"]
            ["items"]["properties"]["project_path"]["enum"]
        )
        assert "10-Project/Inventory/Inventory.md" in enum
        return {
            "done": True,
            "model": MODEL,
            "message": {
                "role": "assistant",
                "content": _project_wire(
                    "10-Project/Inventory/Inventory.md"
                ).decode("utf-8"),
            },
        }

    result = generate_semantic_objective_with_ollama(
        state,
        objective_context_sha256=context_sha,
        base_url="http://127.0.0.1:11434",
        model=MODEL,
        implementation_revision=REVISION,
        transport=transport,
    )
    assert calls == ["/api/tags", "/api/chat"]
    assert result.objective_policy == PROJECT_ADOPTION
    candidate = load_objective_candidate(state, result.candidate_sha256)
    assert isinstance(candidate.output, ProjectAdoptionCandidate)
    assert candidate.output.proposals[0].project_path == (
        "10-Project/Inventory/Inventory.md"
    )
    generation = load_objective_generation(state, result.generation_sha256)
    assert generation.model.provider == "ollama"
    assert generation.model.revision == MODEL_DIGEST
    assert generation.model_config["objective_adapter_version"] == (
        "ollama-semantic-objective-json-schema-v0"
    )
