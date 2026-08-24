from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    _utc_now,
    parse_validation_record,
    sha256_bytes,
)
from .context_bundle import MAX_CONTEXT_BYTES, MAX_SOURCE_BYTES, build_context_bundle
from .generation_artifact import (
    _metadata_string,
    _validated_model_config,
    load_generation_record,
)
from .knowledge_index import (
    MAX_TOP_K,
    KnowledgeIndex,
    RankedDocument,
    load_knowledge_index,
    rank_documents,
    verify_index_current,
)


EVALUATION_REQUEST_STAGE = "12-Evaluation-Request"
EVALUATION_CONTEXT_STAGE = "14-Evaluation-Context"
EVALUATION_STAGE = "15-Evaluation"
EVALUATION_CONTEXT_POLICY_VERSION = "bm25-topk-recall-v0"
DEFAULT_EVALUATION_TOP_K = 5
MAX_EVALUATION_QUERY_CHARS = 4096
MAX_EVALUATION_RECORD_BYTES = 64 * 1024
MAX_FINDINGS = 16
MAX_FINDING_CHARS = 2048

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_GROUNDEDNESS = {"pass", "concern", "unknown"}
_REDUNDANCY = {"none", "possible", "likely"}
_CONSISTENCY = {"pass", "concern", "unknown"}
_RECOMMENDATION = {"proceed", "manual_review", "do_not_proceed"}


@dataclass(frozen=True)
class EvaluationRequest:
    proposal_sha256: str
    mutation_sha256: str
    target_path: str
    query: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "proposal_sha256": self.proposal_sha256,
                "mutation_sha256": self.mutation_sha256,
                "target_path": self.target_path,
                "query": self.query,
            }
        )


@dataclass(frozen=True)
class EvaluationCandidate:
    path: str
    content_sha256: str
    score: str
    content: str


@dataclass(frozen=True)
class EvaluationContext:
    request_sha256: str
    proposal_sha256: str
    mutation_sha256: str
    query: str
    created_at: str
    candidates: tuple[EvaluationCandidate, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "request_sha256": self.request_sha256,
                "proposal_sha256": self.proposal_sha256,
                "mutation_sha256": self.mutation_sha256,
                "query": self.query,
                "created_at": self.created_at,
                "selection_policy": {
                    "version": EVALUATION_CONTEXT_POLICY_VERSION,
                    "top_k": DEFAULT_EVALUATION_TOP_K,
                },
                "candidates": [
                    {
                        "path": candidate.path,
                        "content_sha256": candidate.content_sha256,
                        "score": candidate.score,
                        "content": candidate.content,
                    }
                    for candidate in self.candidates
                ],
            }
        )


@dataclass(frozen=True)
class EvaluationAssessment:
    groundedness: str
    redundancy: str
    consistency: str
    recommendation: str
    findings: tuple[str, ...]


@dataclass(frozen=True)
class EvaluatorMetadata:
    implementation_revision: str
    prompt_template_version: str
    prompt_template_sha256: str


@dataclass(frozen=True)
class EvaluationModelMetadata:
    provider: str
    identifier: str
    revision: str


@dataclass(frozen=True)
class EvaluationRecord:
    proposal_sha256: str
    mutation_sha256: str
    generation_sha256: str
    evaluation_context_sha256: str
    evaluator: EvaluatorMetadata
    model: EvaluationModelMetadata
    model_config: Mapping[str, object]
    assessment: EvaluationAssessment
    evaluated_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": 1,
                "proposal_sha256": self.proposal_sha256,
                "mutation_sha256": self.mutation_sha256,
                "generation_sha256": self.generation_sha256,
                "evaluation_context_sha256": self.evaluation_context_sha256,
                "evaluator": {
                    "implementation_revision": self.evaluator.implementation_revision,
                    "prompt_template_version": self.evaluator.prompt_template_version,
                    "prompt_template_sha256": self.evaluator.prompt_template_sha256,
                },
                "model": {
                    "provider": self.model.provider,
                    "identifier": self.model.identifier,
                    "revision": self.model.revision,
                },
                "model_config": dict(self.model_config),
                "assessment": {
                    "groundedness": self.assessment.groundedness,
                    "redundancy": self.assessment.redundancy,
                    "consistency": self.assessment.consistency,
                    "recommendation": self.assessment.recommendation,
                    "findings": list(self.assessment.findings),
                },
                "evaluated_at": self.evaluated_at,
            }
        )


def _stage_directory(ai_root: Path, stage: str) -> Path:
    root = ai_root.absolute()
    directory = root / stage
    _require_safe_directory(root, create=False)
    _require_safe_directory(directory, create=False)
    return directory


def _verify_proposal(ai_root: Path, proposal_sha256: str) -> bytes:
    digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    path = _stage_directory(ai_root, "00-Untrusted") / f"{digest}.proposal.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise ArtifactLifecycleError("untrusted proposal artifact hash mismatch")
    return data


def _load_accepted_mutation(ai_root: Path, proposal_sha256: str) -> tuple[str, str, str]:
    proposal_digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    _verify_proposal(ai_root, proposal_digest)
    validation_dir = _stage_directory(ai_root, "10-Validation")
    record_path = validation_dir / f"{proposal_digest}.validation.json"
    record = parse_validation_record(_read_exact_file(record_path))
    if record.proposal_sha256 != proposal_digest:
        raise ArtifactLifecycleError("validation record is bound to another proposal")
    if record.result != "accepted" or record.mutation_sha256 is None:
        raise ArtifactLifecycleError("evaluation request requires accepted validation")

    mutation_digest = _require_sha256(record.mutation_sha256, label="mutation_sha256")
    mutation_path = validation_dir / f"{mutation_digest}.mutation.json"
    mutation_bytes = _read_exact_file(mutation_path)
    if sha256_bytes(mutation_bytes) != mutation_digest:
        raise ArtifactLifecycleError("validated mutation artifact hash mismatch")
    value = _decode_json_object(mutation_bytes, label="validated mutation")
    required = {"contract_version", "operation", "mutation_id", "target", "content"}
    if set(value) != required or value.get("contract_version") != 1 or value.get("operation") != "create_note":
        raise ArtifactLifecycleError("validated mutation does not match create_note contract")
    target = value.get("target")
    content = value.get("content")
    if not isinstance(target, dict) or set(target) != {"path"} or not isinstance(target.get("path"), str):
        raise ArtifactLifecycleError("validated mutation target is invalid")
    if not isinstance(content, str) or not content:
        raise ArtifactLifecycleError("validated mutation content is invalid")
    return mutation_digest, target["path"], content


def _evaluation_query(target_path: str, content: str) -> str:
    title = Path(target_path).stem
    lines = content.splitlines()
    body_lines = lines
    if lines and lines[0] == "---":
        try:
            end = lines.index("---", 1)
        except ValueError:
            end = -1
        if end >= 0:
            body_lines = lines[end + 1 :]
    headings = [
        match.group(1)
        for line in body_lines
        if (match := _HEADING_RE.match(line)) is not None
    ][:16]
    body = " ".join(line.strip() for line in body_lines if line.strip())
    query = " ".join([title, *headings, body[:2048]])
    query = " ".join(query.split()).strip()
    if not query:
        raise ArtifactLifecycleError("evaluation retrieval query is empty")
    return query[:MAX_EVALUATION_QUERY_CHARS]


def build_evaluation_request(ai_root: Path, proposal_sha256: str) -> EvaluationRequest:
    proposal_digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    mutation_digest, target_path, content = _load_accepted_mutation(ai_root, proposal_digest)
    return EvaluationRequest(
        proposal_sha256=proposal_digest,
        mutation_sha256=mutation_digest,
        target_path=target_path,
        query=_evaluation_query(target_path, content),
    )


def parse_evaluation_request(data: bytes) -> EvaluationRequest:
    value = _decode_json_object(data, label="evaluation request")
    required = {"record_version", "proposal_sha256", "mutation_sha256", "target_path", "query"}
    if set(value) != required:
        raise ArtifactLifecycleError("evaluation request properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ArtifactLifecycleError("evaluation request record_version must be integer 1")
    proposal = _require_sha256(value["proposal_sha256"], label="proposal_sha256")
    mutation = _require_sha256(value["mutation_sha256"], label="mutation_sha256")
    target = value["target_path"]
    query = value["query"]
    if not isinstance(target, str) or not target.startswith("11-Knowledge/") or not target.endswith(".md"):
        raise ArtifactLifecycleError("evaluation request target_path is invalid")
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_EVALUATION_QUERY_CHARS:
        raise ArtifactLifecycleError("evaluation request query is invalid")
    return EvaluationRequest(proposal, mutation, target, query)


def store_evaluation_request(ai_root: Path, request: EvaluationRequest) -> tuple[str, Path]:
    expected = build_evaluation_request(ai_root, request.proposal_sha256)
    if request != expected:
        raise ArtifactLifecycleError("evaluation request does not match deterministic accepted mutation projection")
    data = request.to_json_bytes()
    if parse_evaluation_request(data) != request:
        raise ArtifactLifecycleError("evaluation request canonical round-trip mismatch")
    digest = sha256_bytes(data)
    path = _stage_directory(ai_root, EVALUATION_REQUEST_STAGE) / f"{digest}.request.json"
    return digest, _store_immutable(path, data)


def create_evaluation_request(ai_root: Path, proposal_sha256: str) -> tuple[str, Path, EvaluationRequest]:
    request = build_evaluation_request(ai_root, proposal_sha256)
    digest, path = store_evaluation_request(ai_root, request)
    return digest, path, request


def load_evaluation_request(ai_root: Path, request_sha256: str) -> EvaluationRequest:
    digest = _require_sha256(request_sha256, label="request_sha256")
    path = _stage_directory(ai_root, EVALUATION_REQUEST_STAGE) / f"{digest}.request.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise ArtifactLifecycleError("evaluation request artifact hash mismatch")
    return parse_evaluation_request(data)


def _select_recall_candidates(
    index: KnowledgeIndex,
    ranked: Sequence[RankedDocument],
    *,
    top_k: int,
) -> tuple[RankedDocument, ...]:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise ArtifactLifecycleError(f"top_k must be an integer in 1..{MAX_TOP_K}")
    byte_size_by_path = {doc.path: doc.byte_size for doc in index.documents}
    selected: list[RankedDocument] = []
    total = 0
    for item in ranked:
        if len(selected) >= top_k:
            break
        size = byte_size_by_path[item.path]
        if total + size > MAX_CONTEXT_BYTES:
            continue
        selected.append(item)
        total += size
    return tuple(selected)


def build_evaluation_context(
    ai_root: Path,
    vault_root: Path,
    *,
    request_sha256: str,
    index_sha256: str,
    top_k: int = DEFAULT_EVALUATION_TOP_K,
    created_at: str | None = None,
) -> EvaluationContext:
    request_digest = _require_sha256(request_sha256, label="request_sha256")
    request = load_evaluation_request(ai_root, request_digest)
    index = load_knowledge_index(ai_root, index_sha256)
    verify_index_current(vault_root, index)
    ranked = rank_documents(index, request.query)
    selected = _select_recall_candidates(index, ranked, top_k=top_k)
    bundle = build_context_bundle(
        vault_root,
        query=request.query,
        source_paths=[item.path for item in selected],
        created_at=created_at,
    )
    ranked_by_path = {item.path: item for item in selected}
    indexed_by_path = {doc.path: doc for doc in index.documents}
    candidates: list[EvaluationCandidate] = []
    for source in bundle.sources:
        expected = indexed_by_path[source.path].content_sha256
        if source.content_sha256 != expected:
            raise ArtifactLifecycleError("Knowledge source changed during evaluation context construction")
        candidates.append(
            EvaluationCandidate(
                path=source.path,
                content_sha256=source.content_sha256,
                score=format(ranked_by_path[source.path].score, ".12g"),
                content=source.content,
            )
        )
    timestamp = bundle.created_at
    return EvaluationContext(
        request_sha256=request_digest,
        proposal_sha256=request.proposal_sha256,
        mutation_sha256=request.mutation_sha256,
        query=request.query,
        created_at=timestamp,
        candidates=tuple(candidates),
    )


def parse_evaluation_context(data: bytes) -> EvaluationContext:
    value = _decode_json_object(data, label="evaluation context")
    required = {
        "record_version",
        "request_sha256",
        "proposal_sha256",
        "mutation_sha256",
        "query",
        "created_at",
        "selection_policy",
        "candidates",
    }
    if set(value) != required:
        raise ArtifactLifecycleError("evaluation context properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ArtifactLifecycleError("evaluation context record_version must be integer 1")
    request_sha = _require_sha256(value["request_sha256"], label="request_sha256")
    proposal_sha = _require_sha256(value["proposal_sha256"], label="proposal_sha256")
    mutation_sha = _require_sha256(value["mutation_sha256"], label="mutation_sha256")
    query = value["query"]
    created_at = value["created_at"]
    policy = value["selection_policy"]
    raw_candidates = value["candidates"]
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_EVALUATION_QUERY_CHARS:
        raise ArtifactLifecycleError("evaluation context query is invalid")
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise ArtifactLifecycleError("evaluation context created_at is invalid")
    if not isinstance(policy, dict) or set(policy) != {"version", "top_k"}:
        raise ArtifactLifecycleError("evaluation context selection_policy is invalid")
    if policy["version"] != EVALUATION_CONTEXT_POLICY_VERSION:
        raise ArtifactLifecycleError("evaluation context selection policy version is unsupported")
    if type(policy["top_k"]) is not int or policy["top_k"] != DEFAULT_EVALUATION_TOP_K:
        raise ArtifactLifecycleError("evaluation context top_k does not match v0 contract")
    if not isinstance(raw_candidates, list) or len(raw_candidates) > DEFAULT_EVALUATION_TOP_K:
        raise ArtifactLifecycleError("evaluation context candidates are invalid")

    candidates: list[EvaluationCandidate] = []
    seen: set[str] = set()
    total = 0
    for raw in raw_candidates:
        if not isinstance(raw, dict) or set(raw) != {"path", "content_sha256", "score", "content"}:
            raise ArtifactLifecycleError("evaluation candidate properties do not match contract")
        path = raw["path"]
        digest = raw["content_sha256"]
        score = raw["score"]
        content = raw["content"]
        if not isinstance(path, str) or not path.startswith("11-Knowledge/") or not path.endswith(".md"):
            raise ArtifactLifecycleError("evaluation candidate path is invalid")
        folded = path.casefold()
        if folded in seen:
            raise ArtifactLifecycleError("evaluation context contains duplicate candidate paths")
        seen.add(folded)
        digest = _require_sha256(digest, label="candidate content_sha256")
        if not isinstance(score, str) or not score:
            raise ArtifactLifecycleError("evaluation candidate score is invalid")
        if not isinstance(content, str):
            raise ArtifactLifecycleError("evaluation candidate content is invalid")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_SOURCE_BYTES or sha256_bytes(encoded) != digest:
            raise ArtifactLifecycleError("evaluation candidate bytes do not match content_sha256")
        total += len(encoded)
        if total > MAX_CONTEXT_BYTES:
            raise ArtifactLifecycleError("evaluation context exceeds aggregate byte limit")
        candidates.append(EvaluationCandidate(path, digest, score, content))

    return EvaluationContext(
        request_sha256=request_sha,
        proposal_sha256=proposal_sha,
        mutation_sha256=mutation_sha,
        query=query,
        created_at=created_at,
        candidates=tuple(candidates),
    )


def store_evaluation_context(ai_root: Path, context: EvaluationContext) -> tuple[str, Path]:
    request = load_evaluation_request(ai_root, context.request_sha256)
    if request.proposal_sha256 != context.proposal_sha256 or request.mutation_sha256 != context.mutation_sha256 or request.query != context.query:
        raise ArtifactLifecycleError("evaluation context is not bound to its request")
    data = context.to_json_bytes()
    if parse_evaluation_context(data) != context:
        raise ArtifactLifecycleError("evaluation context canonical round-trip mismatch")
    digest = sha256_bytes(data)
    path = _stage_directory(ai_root, EVALUATION_CONTEXT_STAGE) / f"{digest}.context.json"
    return digest, _store_immutable(path, data)


def load_evaluation_context(ai_root: Path, context_sha256: str) -> EvaluationContext:
    digest = _require_sha256(context_sha256, label="evaluation_context_sha256")
    path = _stage_directory(ai_root, EVALUATION_CONTEXT_STAGE) / f"{digest}.context.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise ArtifactLifecycleError("evaluation context artifact hash mismatch")
    return parse_evaluation_context(data)


def _assessment(
    *,
    groundedness: str,
    redundancy: str,
    consistency: str,
    recommendation: str,
    findings: Sequence[str],
) -> EvaluationAssessment:
    if groundedness not in _GROUNDEDNESS:
        raise ArtifactLifecycleError("groundedness assessment is invalid")
    if redundancy not in _REDUNDANCY:
        raise ArtifactLifecycleError("redundancy assessment is invalid")
    if consistency not in _CONSISTENCY:
        raise ArtifactLifecycleError("consistency assessment is invalid")
    if recommendation not in _RECOMMENDATION:
        raise ArtifactLifecycleError("evaluation recommendation is invalid")
    if len(findings) > MAX_FINDINGS:
        raise ArtifactLifecycleError("evaluation contains too many findings")
    normalized: list[str] = []
    for finding in findings:
        if not isinstance(finding, str) or not finding.strip() or len(finding) > MAX_FINDING_CHARS:
            raise ArtifactLifecycleError("evaluation finding is invalid")
        normalized.append(finding)
    return EvaluationAssessment(
        groundedness=groundedness,
        redundancy=redundancy,
        consistency=consistency,
        recommendation=recommendation,
        findings=tuple(normalized),
    )


def build_evaluation_record(
    ai_root: Path,
    *,
    proposal_sha256: str,
    mutation_sha256: str,
    generation_sha256: str,
    evaluation_context_sha256: str,
    implementation_revision: str,
    prompt_template_version: str,
    prompt_template_sha256: str,
    model_provider: str,
    model_identifier: str,
    model_revision: str,
    model_config: Mapping[str, object],
    groundedness: str,
    redundancy: str,
    consistency: str,
    recommendation: str,
    findings: Sequence[str],
    evaluated_at: str | None = None,
) -> EvaluationRecord:
    proposal = _require_sha256(proposal_sha256, label="proposal_sha256")
    mutation = _require_sha256(mutation_sha256, label="mutation_sha256")
    generation = _require_sha256(generation_sha256, label="generation_sha256")
    evaluation_context = _require_sha256(evaluation_context_sha256, label="evaluation_context_sha256")

    accepted_mutation, _, _ = _load_accepted_mutation(ai_root, proposal)
    if accepted_mutation != mutation:
        raise ArtifactLifecycleError("evaluation mutation does not match accepted validation")
    generation_record = load_generation_record(ai_root, generation)
    if generation_record.proposal_sha256 != proposal:
        raise ArtifactLifecycleError("evaluation generation record is bound to another proposal")
    context = load_evaluation_context(ai_root, evaluation_context)
    if context.proposal_sha256 != proposal or context.mutation_sha256 != mutation:
        raise ArtifactLifecycleError("evaluation context is bound to another mutation")

    timestamp = evaluated_at or _utc_now()
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ArtifactLifecycleError("evaluated_at must be a UTC timestamp ending in Z")
    record = EvaluationRecord(
        proposal_sha256=proposal,
        mutation_sha256=mutation,
        generation_sha256=generation,
        evaluation_context_sha256=evaluation_context,
        evaluator=EvaluatorMetadata(
            implementation_revision=_metadata_string(implementation_revision, label="evaluator.implementation_revision"),
            prompt_template_version=_metadata_string(prompt_template_version, label="evaluator.prompt_template_version"),
            prompt_template_sha256=_require_sha256(prompt_template_sha256, label="evaluator.prompt_template_sha256"),
        ),
        model=EvaluationModelMetadata(
            provider=_metadata_string(model_provider, label="model.provider"),
            identifier=_metadata_string(model_identifier, label="model.identifier"),
            revision=_metadata_string(model_revision, label="model.revision"),
        ),
        model_config=_validated_model_config(dict(model_config)),
        assessment=_assessment(
            groundedness=groundedness,
            redundancy=redundancy,
            consistency=consistency,
            recommendation=recommendation,
            findings=findings,
        ),
        evaluated_at=timestamp,
    )
    return parse_evaluation_record(record.to_json_bytes())


def parse_evaluation_record(data: bytes) -> EvaluationRecord:
    if len(data) > MAX_EVALUATION_RECORD_BYTES:
        raise ArtifactLifecycleError("evaluation record exceeds maximum size")
    value = _decode_json_object(data, label="evaluation record")
    required = {
        "record_version",
        "proposal_sha256",
        "mutation_sha256",
        "generation_sha256",
        "evaluation_context_sha256",
        "evaluator",
        "model",
        "model_config",
        "assessment",
        "evaluated_at",
    }
    if set(value) != required:
        raise ArtifactLifecycleError("evaluation record properties do not match contract")
    if type(value["record_version"]) is not int or value["record_version"] != 1:
        raise ArtifactLifecycleError("evaluation record_version must be integer 1")
    proposal = _require_sha256(value["proposal_sha256"], label="proposal_sha256")
    mutation = _require_sha256(value["mutation_sha256"], label="mutation_sha256")
    generation = _require_sha256(value["generation_sha256"], label="generation_sha256")
    evaluation_context = _require_sha256(value["evaluation_context_sha256"], label="evaluation_context_sha256")

    raw_evaluator = value["evaluator"]
    if not isinstance(raw_evaluator, dict) or set(raw_evaluator) != {"implementation_revision", "prompt_template_version", "prompt_template_sha256"}:
        raise ArtifactLifecycleError("evaluation evaluator properties do not match contract")
    evaluator = EvaluatorMetadata(
        implementation_revision=_metadata_string(raw_evaluator["implementation_revision"], label="evaluator.implementation_revision"),
        prompt_template_version=_metadata_string(raw_evaluator["prompt_template_version"], label="evaluator.prompt_template_version"),
        prompt_template_sha256=_require_sha256(raw_evaluator["prompt_template_sha256"], label="evaluator.prompt_template_sha256"),
    )

    raw_model = value["model"]
    if not isinstance(raw_model, dict) or set(raw_model) != {"provider", "identifier", "revision"}:
        raise ArtifactLifecycleError("evaluation model properties do not match contract")
    model = EvaluationModelMetadata(
        provider=_metadata_string(raw_model["provider"], label="model.provider"),
        identifier=_metadata_string(raw_model["identifier"], label="model.identifier"),
        revision=_metadata_string(raw_model["revision"], label="model.revision"),
    )
    model_config = _validated_model_config(value["model_config"])

    raw_assessment = value["assessment"]
    if not isinstance(raw_assessment, dict) or set(raw_assessment) != {"groundedness", "redundancy", "consistency", "recommendation", "findings"}:
        raise ArtifactLifecycleError("evaluation assessment properties do not match contract")
    findings = raw_assessment["findings"]
    if not isinstance(findings, list):
        raise ArtifactLifecycleError("evaluation findings must be a list")
    assessment = _assessment(
        groundedness=raw_assessment["groundedness"],
        redundancy=raw_assessment["redundancy"],
        consistency=raw_assessment["consistency"],
        recommendation=raw_assessment["recommendation"],
        findings=findings,
    )
    evaluated_at = value["evaluated_at"]
    if not isinstance(evaluated_at, str) or not evaluated_at.endswith("Z"):
        raise ArtifactLifecycleError("evaluation evaluated_at is invalid")
    return EvaluationRecord(
        proposal_sha256=proposal,
        mutation_sha256=mutation,
        generation_sha256=generation,
        evaluation_context_sha256=evaluation_context,
        evaluator=evaluator,
        model=model,
        model_config=model_config,
        assessment=assessment,
        evaluated_at=evaluated_at,
    )


def store_evaluation_record(ai_root: Path, record: EvaluationRecord) -> tuple[str, Path]:
    normalized = build_evaluation_record(
        ai_root,
        proposal_sha256=record.proposal_sha256,
        mutation_sha256=record.mutation_sha256,
        generation_sha256=record.generation_sha256,
        evaluation_context_sha256=record.evaluation_context_sha256,
        implementation_revision=record.evaluator.implementation_revision,
        prompt_template_version=record.evaluator.prompt_template_version,
        prompt_template_sha256=record.evaluator.prompt_template_sha256,
        model_provider=record.model.provider,
        model_identifier=record.model.identifier,
        model_revision=record.model.revision,
        model_config=record.model_config,
        groundedness=record.assessment.groundedness,
        redundancy=record.assessment.redundancy,
        consistency=record.assessment.consistency,
        recommendation=record.assessment.recommendation,
        findings=record.assessment.findings,
        evaluated_at=record.evaluated_at,
    )
    data = normalized.to_json_bytes()
    digest = sha256_bytes(data)
    path = _stage_directory(ai_root, EVALUATION_STAGE) / f"{digest}.evaluation.json"
    return digest, _store_immutable(path, data)


def load_evaluation_record(ai_root: Path, evaluation_sha256: str) -> EvaluationRecord:
    digest = _require_sha256(evaluation_sha256, label="evaluation_sha256")
    path = _stage_directory(ai_root, EVALUATION_STAGE) / f"{digest}.evaluation.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise ArtifactLifecycleError("evaluation record artifact hash mismatch")
    return parse_evaluation_record(data)


def request_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-evaluation-request")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--proposal-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        digest, path, request = create_evaluation_request(args.ai_root, args.proposal_sha256)
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"request_sha256": digest, "path": str(path), "proposal_sha256": request.proposal_sha256, "mutation_sha256": request.mutation_sha256, "query": request.query}, ensure_ascii=False, sort_keys=True))
    return 0


def context_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-evaluation-context")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--index-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        context = build_evaluation_context(
            args.ai_root,
            args.vault_root,
            request_sha256=args.request_sha256,
            index_sha256=args.index_sha256,
        )
        digest, path = store_evaluation_context(args.ai_root, context)
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"evaluation_context_sha256": digest, "path": str(path), "proposal_sha256": context.proposal_sha256, "mutation_sha256": context.mutation_sha256, "candidate_count": len(context.candidates), "selection_policy": EVALUATION_CONTEXT_POLICY_VERSION, "candidates": [{"path": candidate.path, "score": candidate.score} for candidate in context.candidates]}, ensure_ascii=False, sort_keys=True))
    return 0
