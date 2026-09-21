from __future__ import annotations

import argparse
import json
import os
import re
import stat
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
from .context_bundle import ContextBundle
from .evaluation_artifact import load_evaluation_record
from .generation_artifact import load_generation_record
from .production_io import ProductionIOError, canonical_io_lock
from .webdav_create import (
    WebDAVCreateError,
    WebDAVTargetExists,
    conditional_create,
    ensure_collection,
    observe_remote,
    _read_password,
)


REQUEST_STAGE = "16-Human-Projection"
RESULT_STAGE = "17-Human-Projection-Result"
RECORD_VERSION = 1
MAX_MARKDOWN_BYTES = 512 * 1024
MAX_REQUEST_BYTES = 768 * 1024
MAX_BATCH = 64

ROLE_NAMES = (
    "reader",
    "generator",
    "validator",
    "evaluator",
    "reviewer",
    "executor",
    "sync",
)

STAGE_FOLDERS = {
    "input": "00-Input",
    "context": "10-Context",
    "generation": "20-Generation",
    "validation": "30-Validation",
    "evaluation": "40-Evaluation",
    "review": "50-Review",
    "execution": "60-Execution",
    "transport": "70-Transport",
    "completed": "80-Completed",
    "failed": "90-Failed",
}
STAGE_STATUS = {
    "input": "selected",
    "context": "context_ready",
    "generation": "generated",
    "validation": "validated",
    "evaluation": "evaluated",
    "review": "awaiting_human_review",
    "execution": "executing",
    "transport": "transporting",
    "completed": "completed",
    "failed": "failed",
}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class HumanProjectionError(ArtifactLifecycleError):
    """Raised when a human-facing AI projection is invalid or unsafe."""


class HumanProjectionConflict(HumanProjectionError):
    """Raised when a canonical 03-AI projection path contains different bytes."""


@dataclass(frozen=True)
class ProjectionRequest:
    case_id: str
    stage: str
    source_kind: str
    source_sha256: str
    target_path: str
    content_sha256: str
    content: str
    created_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "case_id": self.case_id,
                "stage": self.stage,
                "source_kind": self.source_kind,
                "source_sha256": self.source_sha256,
                "target_path": self.target_path,
                "content_sha256": self.content_sha256,
                "content": self.content,
                "created_at": self.created_at,
            }
        )


@dataclass(frozen=True)
class ProjectionResult:
    request_sha256: str
    target_path: str
    content_sha256: str
    result: str
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "request_sha256": self.request_sha256,
                "target_path": self.target_path,
                "content_sha256": self.content_sha256,
                "result": self.result,
                "completed_at": self.completed_at,
            }
        )


def projection_enabled(ai_root: Path) -> bool:
    path = ai_root.absolute() / REQUEST_STAGE
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HumanProjectionError("human projection request root is unsafe")
    return True


def _case_id(value: str) -> str:
    return _require_sha256(value, label="ai_case_id")


def _stage(value: str) -> str:
    if value not in STAGE_FOLDERS:
        raise HumanProjectionError(f"unsupported human projection stage: {value}")
    return value


def _source_kind(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        raise HumanProjectionError("projection source_kind is invalid")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise HumanProjectionError("projection source_kind contains control characters")
    return value


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _inline_code(value: str) -> str:
    ticks = max((len(match.group(0)) for match in re.finditer(r"`+", value)), default=0)
    fence = "`" * max(1, ticks + 1)
    padding = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{padding}{value}{padding}{fence}"


def _code_fence(content: str, language: str = "markdown") -> str:
    ticks = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, ticks + 1)
    return f"{fence}{language}\n{content.rstrip()}\n{fence}"


def _projection_markdown(
    *,
    case_id: str,
    stage: str,
    source_kind: str,
    source_sha256: str,
    created_at: str,
    title: str,
    body: str,
    extra_frontmatter: Mapping[str, str | None] | None = None,
) -> str:
    case = _case_id(case_id)
    stage_name = _stage(stage)
    source_digest = _require_sha256(source_sha256, label="projection source_sha256")
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise HumanProjectionError("projection created_at must be a UTC Z timestamp")
    fields: list[tuple[str, str | None]] = [
        ("type", "ai-pipeline-projection"),
        ("ai_case_id", case),
        ("ai_stage", stage_name),
        ("ai_status", STAGE_STATUS[stage_name]),
        ("source_kind", _source_kind(source_kind)),
        ("source_sha256", source_digest),
        ("created_at", created_at),
    ]
    for key, value in (extra_frontmatter or {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise HumanProjectionError("projection frontmatter key is invalid")
        if value is not None and (
            not isinstance(value, str)
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
        ):
            raise HumanProjectionError("projection frontmatter value is invalid")
        fields.append((key, value))

    frontmatter = ["---"]
    for key, value in fields:
        frontmatter.append(f"{key}: " + ("" if value is None else _yaml(value)))
    frontmatter.extend(["---", "", f"# {title}", "", body.rstrip(), ""])
    text = "\n".join(frontmatter)
    if len(text.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise HumanProjectionError(
            f"human projection Markdown exceeds {MAX_MARKDOWN_BYTES} bytes"
        )
    return text


def build_request(
    *,
    case_id: str,
    stage: str,
    source_kind: str,
    source_sha256: str,
    content: str,
    created_at: str | None = None,
) -> ProjectionRequest:
    case = _case_id(case_id)
    stage_name = _stage(stage)
    source = _require_sha256(source_sha256, label="projection source_sha256")
    if not isinstance(content, str) or not content:
        raise HumanProjectionError("projection content must be non-empty")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_MARKDOWN_BYTES:
        raise HumanProjectionError("projection content exceeds maximum size")
    timestamp = created_at or _utc_now()
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise HumanProjectionError("projection created_at must be a UTC Z timestamp")
    target = f"03-AI/{STAGE_FOLDERS[stage_name]}/{case}.md"
    request = ProjectionRequest(
        case_id=case,
        stage=stage_name,
        source_kind=_source_kind(source_kind),
        source_sha256=source,
        target_path=target,
        content_sha256=sha256_bytes(encoded),
        content=content,
        created_at=timestamp,
    )
    return parse_request(request.to_json_bytes())


def parse_request(data: bytes) -> ProjectionRequest:
    if len(data) > MAX_REQUEST_BYTES:
        raise HumanProjectionError("projection request exceeds maximum size")
    value = _decode_json_object(data, label="human projection request")
    required = {
        "record_version",
        "case_id",
        "stage",
        "source_kind",
        "source_sha256",
        "target_path",
        "content_sha256",
        "content",
        "created_at",
    }
    if set(value) != required or value["record_version"] != RECORD_VERSION:
        raise HumanProjectionError("projection request properties do not match contract")
    case = _case_id(value["case_id"])
    stage_name = _stage(value["stage"])
    source_kind = _source_kind(value["source_kind"])
    source_sha = _require_sha256(value["source_sha256"], label="projection source_sha256")
    expected_target = f"03-AI/{STAGE_FOLDERS[stage_name]}/{case}.md"
    if value["target_path"] != expected_target:
        raise HumanProjectionError("projection target_path is not deterministic")
    content = value["content"]
    if not isinstance(content, str) or not content:
        raise HumanProjectionError("projection content is invalid")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_MARKDOWN_BYTES:
        raise HumanProjectionError("projection content exceeds maximum size")
    content_sha = _require_sha256(value["content_sha256"], label="projection content_sha256")
    if sha256_bytes(encoded) != content_sha:
        raise HumanProjectionError("projection content does not match content_sha256")
    created_at = value["created_at"]
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise HumanProjectionError("projection created_at is invalid")
    return ProjectionRequest(
        case_id=case,
        stage=stage_name,
        source_kind=source_kind,
        source_sha256=source_sha,
        target_path=expected_target,
        content_sha256=content_sha,
        content=content,
        created_at=created_at,
    )


def _request_role_dir(ai_root: Path, role: str) -> Path:
    if role not in ROLE_NAMES:
        raise HumanProjectionError(f"unknown projection role: {role}")
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    requests = root / REQUEST_STAGE
    _require_safe_directory(requests, create=False)
    role_dir = requests / role
    _require_safe_directory(role_dir, create=False)
    return role_dir


def store_request(
    ai_root: Path,
    *,
    role: str,
    request: ProjectionRequest,
) -> tuple[str, Path]:
    normalized = parse_request(request.to_json_bytes())
    data = normalized.to_json_bytes()
    digest = sha256_bytes(data)
    path = _request_role_dir(ai_root, role) / f"{digest}.projection.json"
    return digest, _store_immutable(path, data)


def _result_dir(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    directory = root / RESULT_STAGE
    _require_safe_directory(directory, create=False)
    return directory


def parse_result(data: bytes) -> ProjectionResult:
    value = _decode_json_object(data, label="human projection result")
    required = {
        "record_version",
        "request_sha256",
        "target_path",
        "content_sha256",
        "result",
        "completed_at",
    }
    if set(value) != required or value["record_version"] != RECORD_VERSION:
        raise HumanProjectionError("projection result properties do not match contract")
    request_sha = _require_sha256(value["request_sha256"], label="request_sha256")
    content_sha = _require_sha256(value["content_sha256"], label="content_sha256")
    target = value["target_path"]
    if not isinstance(target, str) or not target.startswith("03-AI/") or not target.endswith(".md"):
        raise HumanProjectionError("projection result target_path is invalid")
    result = value["result"]
    if result not in {"created", "already_matching", "conflict"}:
        raise HumanProjectionError("projection result status is invalid")
    completed_at = value["completed_at"]
    if not isinstance(completed_at, str) or not completed_at.endswith("Z"):
        raise HumanProjectionError("projection result completed_at is invalid")
    return ProjectionResult(request_sha, target, content_sha, result, completed_at)


def _store_result(ai_root: Path, result: ProjectionResult) -> Path:
    normalized = parse_result(result.to_json_bytes())
    return _store_immutable(
        _result_dir(ai_root) / f"{normalized.request_sha256}.projection-result.json",
        normalized.to_json_bytes(),
    )


def _load_request_path(path: Path) -> tuple[str, ProjectionRequest]:
    name = path.name
    suffix = ".projection.json"
    if not name.endswith(suffix):
        raise HumanProjectionError("projection request filename is invalid")
    digest = name[: -len(suffix)]
    _require_sha256(digest, label="projection request filename SHA")
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise HumanProjectionError("projection request artifact hash mismatch")
    return digest, parse_request(data)


def _existing_result(ai_root: Path, request_sha256: str) -> ProjectionResult | None:
    path = _result_dir(ai_root) / f"{request_sha256}.projection-result.json"
    if not os.path.lexists(path):
        return None
    result = parse_result(_read_exact_file(path))
    if result.request_sha256 != request_sha256:
        raise HumanProjectionError("projection result is bound to another request")
    return result


def _iter_requests(ai_root: Path) -> list[tuple[str, Path]]:
    root = ai_root.absolute() / REQUEST_STAGE
    _require_safe_directory(root, create=False)
    rows: list[tuple[str, Path]] = []
    for role in ROLE_NAMES:
        directory = root / role
        _require_safe_directory(directory, create=False)
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise HumanProjectionError("projection request queue contains unsafe entry")
            if not path.name.endswith(".projection.json"):
                continue
            rows.append((role, path))
    return rows


def _ensure_target_parent(
    *,
    base_url: str,
    request: ProjectionRequest,
    username: str,
    password: str,
    timeout: float,
    allow_http: bool,
) -> None:
    ensure_collection(
        base_url=base_url,
        target_path="03-AI",
        username=username,
        password=password,
        timeout=timeout,
        allow_http=allow_http,
    )
    ensure_collection(
        base_url=base_url,
        target_path=f"03-AI/{STAGE_FOLDERS[request.stage]}",
        username=username,
        password=password,
        timeout=timeout,
        allow_http=allow_http,
    )


def apply_request(
    request_sha256: str,
    request: ProjectionRequest,
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    allow_http: bool = False,
) -> ProjectionResult:
    request_sha = _require_sha256(request_sha256, label="request_sha256")
    content = request.content.encode("utf-8")
    _ensure_target_parent(
        base_url=base_url,
        request=request,
        username=username,
        password=password,
        timeout=timeout,
        allow_http=allow_http,
    )
    try:
        conditional_create(
            base_url=base_url,
            target_path=request.target_path,
            content=content,
            username=username,
            password=password,
            timeout=timeout,
            allow_http=allow_http,
        )
        result = "created"
    except WebDAVTargetExists:
        observation = observe_remote(
            base_url=base_url,
            target_path=request.target_path,
            expected_content_sha256=request.content_sha256,
            username=username,
            password=password,
            timeout=timeout,
            allow_http=allow_http,
        )
        result = "already_matching" if observation.result == "matching" else "conflict"
    return ProjectionResult(
        request_sha256=request_sha,
        target_path=request.target_path,
        content_sha256=request.content_sha256,
        result=result,
        completed_at=_utc_now(),
    )


def run_projection_sync(
    ai_root: Path,
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 30.0,
    max_requests: int = 16,
    allow_http: bool = False,
) -> dict[str, object]:
    if type(max_requests) is not int or not 1 <= max_requests <= MAX_BATCH:
        raise HumanProjectionError(f"max_requests must be 1..{MAX_BATCH}")
    processed = 0
    matching = 0
    created = 0
    with canonical_io_lock(ai_root):
        for _role, path in _iter_requests(ai_root):
            digest, request = _load_request_path(path)
            existing = _existing_result(ai_root, digest)
            if existing is not None:
                if existing.result == "conflict":
                    raise HumanProjectionConflict(
                        f"projection conflict remains unresolved: {existing.target_path}"
                    )
                continue
            result = apply_request(
                digest,
                request,
                base_url=base_url,
                username=username,
                password=password,
                timeout=timeout,
                allow_http=allow_http,
            )
            _store_result(ai_root, result)
            processed += 1
            if result.result == "conflict":
                raise HumanProjectionConflict(
                    f"projection target contains different bytes: {result.target_path}"
                )
            if result.result == "created":
                created += 1
            else:
                matching += 1
            if processed >= max_requests:
                break
    return {
        "event": "ai-human-projection-sync",
        "status": "completed",
        "processed": processed,
        "created": created,
        "already_matching": matching,
    }


def _proposal_fields(ai_root: Path, proposal_sha256: str) -> tuple[str, str]:
    digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    path = ai_root.absolute() / "00-Untrusted" / f"{digest}.proposal.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise HumanProjectionError("proposal artifact hash mismatch")
    value = _decode_json_object(data, label="projection proposal")
    required = {"contract_version", "operation", "mutation_id", "target", "content"}
    if (
        set(value) != required
        or value["contract_version"] != 1
        or value["operation"] != "create_note"
    ):
        raise HumanProjectionError("projection proposal is not create_note v1")
    target = value["target"]
    content = value["content"]
    if (
        not isinstance(target, dict)
        or set(target) != {"path"}
        or not isinstance(target.get("path"), str)
        or not isinstance(content, str)
        or not content
    ):
        raise HumanProjectionError("projection proposal fields are invalid")
    return target["path"], content


def emit_input_projection(
    ai_root: Path,
    *,
    case_id: str,
    selection_sha256: str,
    selection_policy: str,
    objective_policy: str,
    epoch: int,
    cycle: int,
    selected: Sequence[Mapping[str, object]],
    created_at: str,
) -> tuple[str, Path] | None:
    if not projection_enabled(ai_root):
        return None
    source = _require_sha256(selection_sha256, label="selection_sha256")
    lines = [
        f"Selection policy: {_inline_code(selection_policy)}",
        f"Objective: {_inline_code(objective_policy)}",
        f"Coverage epoch: {epoch}",
        f"Scheduler cycle: {cycle}",
        "",
        "## Sources",
    ]
    for item in selected:
        path = item.get("path")
        kind = item.get("source_kind")
        if not isinstance(path, str) or not isinstance(kind, str):
            raise HumanProjectionError("input selection entry is invalid")
        lines.append(f"- {_inline_code(path)} — {kind}")
    markdown = _projection_markdown(
        case_id=case_id,
        stage="input",
        source_kind="input_selection",
        source_sha256=source,
        created_at=created_at,
        title="AI Input",
        body="\n".join(lines),
    )
    return store_request(
        ai_root,
        role="reader",
        request=build_request(
            case_id=case_id,
            stage="input",
            source_kind="input_selection",
            source_sha256=source,
            content=markdown,
            created_at=created_at,
        ),
    )


def emit_context_projection(
    ai_root: Path,
    *,
    case_id: str,
    context_sha256: str,
    context: ContextBundle,
) -> tuple[str, Path] | None:
    if not projection_enabled(ai_root):
        return None
    source = _require_sha256(context_sha256, label="context_sha256")
    lines = [
        "## Query",
        "",
        _code_fence(context.query, "text"),
        "",
        "## Exact Sources",
    ]
    for item in context.sources:
        lines.append(
            f"- {_inline_code(item.path)} — {_inline_code(item.content_sha256)}"
        )
    markdown = _projection_markdown(
        case_id=case_id,
        stage="context",
        source_kind="context_bundle",
        source_sha256=source,
        created_at=context.created_at,
        title="Generation Context",
        body="\n".join(lines),
    )
    return store_request(
        ai_root,
        role="reader",
        request=build_request(
            case_id=case_id,
            stage="context",
            source_kind="context_bundle",
            source_sha256=source,
            content=markdown,
            created_at=context.created_at,
        ),
    )


def emit_generation_projection(
    ai_root: Path,
    *,
    case_id: str,
    generation_sha256: str,
    proposal_sha256: str,
) -> tuple[str, Path] | None:
    if not projection_enabled(ai_root):
        return None
    generation_digest = _require_sha256(generation_sha256, label="generation_sha256")
    proposal_digest = _require_sha256(proposal_sha256, label="proposal_sha256")
    record = load_generation_record(ai_root, generation_digest)
    if record.proposal_sha256 != proposal_digest:
        raise HumanProjectionError("generation projection proposal binding mismatch")
    target, candidate = _proposal_fields(ai_root, proposal_digest)
    markdown = _projection_markdown(
        case_id=case_id,
        stage="generation",
        source_kind="generation_record",
        source_sha256=generation_digest,
        created_at=record.generated_at,
        title="Generated Knowledge Candidate",
        body="\n".join(
            [
                f"Target: {_inline_code(target)}",
                "",
                "## Candidate",
                "",
                _code_fence(candidate),
            ]
        ),
        extra_frontmatter={
            "proposal_sha256": proposal_digest,
            "target_path": target,
        },
    )
    return store_request(
        ai_root,
        role="generator",
        request=build_request(
            case_id=case_id,
            stage="generation",
            source_kind="generation_record",
            source_sha256=generation_digest,
            content=markdown,
            created_at=record.generated_at,
        ),
    )


def _validation_record(
    ai_root: Path,
    proposal_sha256: str,
) -> tuple[str, object]:
    proposal = _require_sha256(proposal_sha256, label="proposal_sha256")
    path = ai_root.absolute() / "10-Validation" / f"{proposal}.validation.json"
    data = _read_exact_file(path)
    return sha256_bytes(data), parse_validation_record(data)


def emit_validation_projection(
    ai_root: Path,
    *,
    case_id: str,
    proposal_sha256: str,
) -> tuple[str, Path] | None:
    if not projection_enabled(ai_root):
        return None
    proposal = _require_sha256(proposal_sha256, label="proposal_sha256")
    source_sha, record = _validation_record(ai_root, proposal)
    target, _candidate = _proposal_fields(ai_root, proposal)
    lines = [
        f"Result: **{record.result}**",
        f"Target: {_inline_code(target)}",
    ]
    if record.reason is not None:
        lines.extend(["", "## Reason", "", _code_fence(record.reason, "text")])
    markdown = _projection_markdown(
        case_id=case_id,
        stage="validation",
        source_kind="validation_record",
        source_sha256=source_sha,
        created_at=record.validated_at,
        title="Deterministic Validation",
        body="\n".join(lines),
        extra_frontmatter={
            "proposal_sha256": proposal,
            "mutation_sha256": record.mutation_sha256,
            "target_path": target,
            "validation_result": record.result,
        },
    )
    return store_request(
        ai_root,
        role="validator",
        request=build_request(
            case_id=case_id,
            stage="validation",
            source_kind="validation_record",
            source_sha256=source_sha,
            content=markdown,
            created_at=record.validated_at,
        ),
    )


def _assessment_lines(record) -> list[str]:
    lines = [
        f"- Groundedness: **{record.assessment.groundedness}**",
        f"- Redundancy: **{record.assessment.redundancy}**",
        f"- Consistency: **{record.assessment.consistency}**",
        f"- Recommendation: **{record.assessment.recommendation}**",
    ]
    if record.assessment.findings:
        lines.extend(["", "### Findings"])
        for finding in record.assessment.findings:
            lines.append(f"- {_inline_code(finding)}")
    return lines


def emit_evaluation_and_review_projections(
    ai_root: Path,
    *,
    case_id: str,
    evaluation_sha256: str,
) -> tuple[tuple[str, Path], tuple[str, Path]] | None:
    if not projection_enabled(ai_root):
        return None
    evaluation = _require_sha256(evaluation_sha256, label="evaluation_sha256")
    record = load_evaluation_record(ai_root, evaluation)
    target, candidate = _proposal_fields(ai_root, record.proposal_sha256)
    source_sha, validation = _validation_record(ai_root, record.proposal_sha256)
    if (
        validation.result != "accepted"
        or validation.mutation_sha256 != record.mutation_sha256
    ):
        raise HumanProjectionError("review projection requires matching accepted validation")

    evaluation_markdown = _projection_markdown(
        case_id=case_id,
        stage="evaluation",
        source_kind="evaluation_record",
        source_sha256=evaluation,
        created_at=record.evaluated_at,
        title="AI Evaluation",
        body="\n".join(
            [
                f"Target: {_inline_code(target)}",
                "",
                "## Assessment",
                "",
                *_assessment_lines(record),
            ]
        ),
        extra_frontmatter={
            "proposal_sha256": record.proposal_sha256,
            "mutation_sha256": record.mutation_sha256,
            "evaluation_sha256": evaluation,
            "target_path": target,
            "recommendation": record.assessment.recommendation,
        },
    )
    evaluation_stored = store_request(
        ai_root,
        role="evaluator",
        request=build_request(
            case_id=case_id,
            stage="evaluation",
            source_kind="evaluation_record",
            source_sha256=evaluation,
            content=evaluation_markdown,
            created_at=record.evaluated_at,
        ),
    )

    review_markdown = _projection_markdown(
        case_id=case_id,
        stage="review",
        source_kind="evaluation_record",
        source_sha256=evaluation,
        created_at=record.evaluated_at,
        title="Human Review",
        body="\n".join(
            [
                f"Target: {_inline_code(target)}",
                "",
                "## Validation",
                "",
                "- Result: **accepted**",
                f"- Validation artifact: {_inline_code(source_sha)}",
                "",
                "## Evaluation",
                "",
                *_assessment_lines(record),
                "",
                "## Candidate",
                "",
                _code_fence(candidate),
                "",
                "## Decision",
                "",
                "**Review request:** INPUT[inlineSelect(option(approve, '✅ Approve'), option(reject, '❌ Reject'), option(null, '▫️ Pending')):review_request]",
                "",
                "This field is a Human request only. Authoritative Review is created separately after exact binding verification.",
            ]
        ),
        extra_frontmatter={
            "proposal_sha256": record.proposal_sha256,
            "mutation_sha256": record.mutation_sha256,
            "evaluation_sha256": evaluation,
            "target_path": target,
            "recommendation": record.assessment.recommendation,
            "review_request": None,
        },
    )
    review_stored = store_request(
        ai_root,
        role="evaluator",
        request=build_request(
            case_id=case_id,
            stage="review",
            source_kind="evaluation_record",
            source_sha256=evaluation,
            content=review_markdown,
            created_at=record.evaluated_at,
        ),
    )
    return evaluation_stored, review_stored


def sync_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-ai-human-projection-sync")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-requests", type=int, default=16)
    args = parser.parse_args(argv)
    try:
        password = _read_password(args.password_file)
        result = run_projection_sync(
            args.ai_root,
            base_url=args.base_url,
            username=args.username,
            password=password,
            timeout=args.timeout,
            max_requests=args.max_requests,
        )
    except (
        ArtifactLifecycleError,
        HumanProjectionError,
        ProductionIOError,
        WebDAVCreateError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(sync_main())
