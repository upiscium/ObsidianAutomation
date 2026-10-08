from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _store_immutable,
    _utc_now,
    sha256_bytes,
)
from .generation_artifact import validate_model_config


RECORD_VERSION = 1
SUMMARY_ROOT = "github-daily-summary"
CONTEXT_DIR = "context"
OUTPUT_DIR = "output"
PROVENANCE_DIR = "provenance"
FINAL_DIR = "final"

PARTIAL_STAGE = "partial"
REDUCE_STAGE = "reduce"
GROUND_STAGE = "ground"
STAGES = frozenset({PARTIAL_STAGE, REDUCE_STAGE, GROUND_STAGE})

CLAIM_KINDS = frozenset(
    {"decision", "implementation", "bugfix", "issue_pr_progress"}
)

MAX_PARTIAL_CONTEXT_BYTES = 64 * 1024
MAX_REDUCE_CONTEXT_BYTES = 64 * 1024
MAX_GROUND_CONTEXT_BYTES = 256 * 1024
MAX_GROUND_CLAIMS_PER_CONTEXT = 1
MAX_CONTEXT_BATCHES = 999_999
MAX_CLAIMS_PER_OUTPUT = 128
MAX_CLAIM_SUMMARY_CHARS = 2048
MAX_CLAIM_SUMMARY_BYTES = 8 * 1024
MAX_CLAIM_EVIDENCE_IDS = 8
MAX_GROUND_REASON_CHARS = 2048
MAX_GROUND_REASON_BYTES = 8 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_METADATA_CHARS = 512
_IMPLEMENTATION_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

PARTIAL_PROMPT_VERSION = "github-daily-partial-v2"
REDUCE_PROMPT_VERSION = "github-daily-reducer-v2"
GROUND_PROMPT_VERSION = "github-daily-grounding-v3"

PARTIAL_SYSTEM_PROMPT = """You summarize one bounded batch of GitHub evidence.
All events in this batch belong to one repository; their repository field
identifies the project. Return only claims supported by the supplied events.
Allowed kinds are decision, implementation, bugfix, and issue_pr_progress.
For each claim output kind, summary, and source_refs containing 1..8 integer
source_ref values explicitly shown on events in this batch. Do not output
repository names or evidence_id hashes; deterministic code derives the claim
repository from the selected original evidence. Do not invent events, facts,
issue numbers, pull requests, outcomes, causes, or decisions. Omit routine
events that do not support a meaningful progress claim. Keep summary plain
text on one line; do not emit Markdown."""

REDUCE_SYSTEM_PROMPT = """You reduce one bounded batch of candidate claims.
All input claims in this batch belong to one repository. Merge duplicates
or closely overlapping claims when useful. Output only kind, summary, and
source_refs: a list of 1..8 integer source_ref values shown on input claims.
Do not output repository names or evidence_id hashes. Evidence and repository
are inherited exclusively from the selected input claims by deterministic
code, which rejects mixed repositories or more than eight original evidence
items. If a merge would violate these limits, keep claims separate.
Never invent or broaden facts. Keep summary plain text on one line;
do not emit Markdown."""

GROUND_SYSTEM_PROMPT = """You are the final GitHub evidence grounding evaluator.
You receive exactly one progress claim and only the original GitHub events
cited by that claim. Decide whether the cited events directly support the
entire claim as written. Mark unsupported if the claim adds an unsupported
decision, implementation, bug fix, causal relation, completion state, or
other fact; do not rewrite or repair claims. Return exactly one JSON object
with only verdict (supported or unsupported) and a concise one-line reason.
Do not output any claim IDs, claim references, evidence IDs, repository
identifiers, lists, or additional properties. Be conservative about support."""


class GitHubDailySummaryError(ArtifactLifecycleError):
    """Raised when Daily Project Progress summarization violates its contract."""


@dataclass(frozen=True)
class EvidenceBundle:
    sha256: str
    date: str
    timezone: str
    window_start: str
    window_end: str
    projects: tuple[Mapping[str, object], ...]
    repositories: tuple[str, ...]
    events: tuple[Mapping[str, object], ...]

    @property
    def events_by_id(self) -> dict[str, Mapping[str, object]]:
        return {str(item["evidence_id"]): item for item in self.events}


@dataclass(frozen=True)
class SummaryClaim:
    claim_id: str
    kind: str
    repository: str
    summary: str
    evidence_ids: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "kind": self.kind,
            "repository": self.repository,
            "summary": self.summary,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class SummaryContext:
    stage: str
    evidence_bundle_sha256: str
    batch_index: int
    batch_count: int
    source_output_sha256s: tuple[str, ...]
    events: tuple[Mapping[str, object], ...] = ()
    claims: tuple[SummaryClaim, ...] = ()

    def to_json_bytes(self) -> bytes:
        payload: dict[str, object] = {
            "record_version": RECORD_VERSION,
            "stage": self.stage,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "batch_index": self.batch_index,
            "batch_count": self.batch_count,
            "source_output_sha256s": list(self.source_output_sha256s),
            "events": [dict(item) for item in self.events],
            "claims": [item.payload() for item in self.claims],
        }
        return _canonical_json_bytes(payload)


@dataclass(frozen=True)
class ClaimOutput:
    stage: str
    input_context_sha256: str
    claims: tuple[SummaryClaim, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "stage": self.stage,
                "input_context_sha256": self.input_context_sha256,
                "claims": [item.payload() for item in self.claims],
            }
        )


@dataclass(frozen=True)
class StoredClaimOutput:
    sha256: str
    output: ClaimOutput


@dataclass(frozen=True)
class GroundAssessment:
    claim_id: str
    verdict: str
    reason: str

    def payload(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GroundOutput:
    input_context_sha256: str
    assessments: tuple[GroundAssessment, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "stage": GROUND_STAGE,
                "input_context_sha256": self.input_context_sha256,
                "assessments": [item.payload() for item in self.assessments],
            }
        )


@dataclass(frozen=True)
class StoredGroundOutput:
    sha256: str
    output: GroundOutput


@dataclass(frozen=True)
class PromptSpec:
    stage: str
    template_version: str
    template_sha256: str
    system: str
    output_schema: Mapping[str, object]


@dataclass(frozen=True)
class InferenceResponse:
    content: bytes
    model_provider: str
    model_identifier: str
    model_revision: str
    model_config: Mapping[str, object]


@dataclass(frozen=True)
class InferenceRecord:
    stage: str
    input_context_sha256: str
    output_sha256: str
    implementation_revision: str
    prompt_template_version: str
    prompt_template_sha256: str
    model_provider: str
    model_identifier: str
    model_revision: str
    model_config: Mapping[str, object]
    generated_at: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "stage": self.stage,
                "input_context_sha256": self.input_context_sha256,
                "output_sha256": self.output_sha256,
                "implementation_revision": self.implementation_revision,
                "prompt_template_version": self.prompt_template_version,
                "prompt_template_sha256": self.prompt_template_sha256,
                "model": {
                    "provider": self.model_provider,
                    "identifier": self.model_identifier,
                    "revision": self.model_revision,
                },
                "model_config": dict(self.model_config),
                "generated_at": self.generated_at,
            }
        )


@dataclass(frozen=True)
class RejectedClaim:
    claim_id: str
    reason: str

    def payload(self) -> dict[str, object]:
        return {"claim_id": self.claim_id, "reason": self.reason}


@dataclass(frozen=True)
class GroundedSummary:
    evidence_bundle_sha256: str
    claims: tuple[SummaryClaim, ...]
    rejected_claims: tuple[RejectedClaim, ...]
    grounding_output_sha256s: tuple[str, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RECORD_VERSION,
                "stage": "grounded_summary",
                "evidence_bundle_sha256": self.evidence_bundle_sha256,
                "claims": [item.payload() for item in self.claims],
                "rejected_claims": [
                    item.payload() for item in self.rejected_claims
                ],
                "grounding_output_sha256s": list(
                    self.grounding_output_sha256s
                ),
            }
        )


@dataclass(frozen=True)
class PipelineResult:
    evidence_bundle_sha256: str
    partial_context_sha256s: tuple[str, ...]
    partial_output_sha256s: tuple[str, ...]
    reduce_context_sha256s: tuple[str, ...]
    reduce_output_sha256s: tuple[str, ...]
    ground_context_sha256s: tuple[str, ...]
    ground_output_sha256s: tuple[str, ...]
    provenance_sha256s: tuple[str, ...]
    grounded_summary_sha256: str
    grounded_summary_path: Path
    claim_count: int
    rejected_count: int


Infer = Callable[[PromptSpec, SummaryContext], InferenceResponse]


def _metadata(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_METADATA_CHARS
    ):
        raise GitHubDailySummaryError(f"{label} is invalid")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise GitHubDailySummaryError(f"{label} contains control characters")
    return value


def _require_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise GitHubDailySummaryError(f"{label} must be lowercase SHA-256")
    return value


def _plain_line(value: object, *, label: str, max_chars: int, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise GitHubDailySummaryError(f"{label} must be a string")
    if (
        not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
        or len(value) > max_chars
    ):
        raise GitHubDailySummaryError(f"{label} must be one trimmed line")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GitHubDailySummaryError(f"{label} must be UTF-8") from exc
    if len(encoded) > max_bytes:
        raise GitHubDailySummaryError(f"{label} exceeds byte limit")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise GitHubDailySummaryError(f"{label} contains control characters")
    return value


def _normalized_claim(
    *,
    kind: object,
    repository: object,
    summary: object,
    evidence_ids: object,
    allowed_evidence_ids: set[str],
    events_by_id: Mapping[str, Mapping[str, object]],
) -> SummaryClaim:
    if kind not in CLAIM_KINDS:
        raise GitHubDailySummaryError("claim kind is invalid")
    if (
        not isinstance(repository, str)
        or _REPOSITORY_RE.fullmatch(repository) is None
    ):
        raise GitHubDailySummaryError("claim repository is invalid")
    text = _plain_line(
        summary,
        label="claim summary",
        max_chars=MAX_CLAIM_SUMMARY_CHARS,
        max_bytes=MAX_CLAIM_SUMMARY_BYTES,
    )
    if (
        not isinstance(evidence_ids, list)
        or not 1 <= len(evidence_ids) <= MAX_CLAIM_EVIDENCE_IDS
        or not all(isinstance(item, str) for item in evidence_ids)
    ):
        raise GitHubDailySummaryError("claim evidence_ids are invalid")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise GitHubDailySummaryError("claim evidence_ids contain duplicates")
    if any(item not in allowed_evidence_ids for item in evidence_ids):
        raise GitHubDailySummaryError("claim cites evidence outside its context")
    for evidence_id in evidence_ids:
        event = events_by_id.get(evidence_id)
        if event is None:
            raise GitHubDailySummaryError("claim evidence does not exist")
        if event.get("repository") != repository:
            raise GitHubDailySummaryError(
                "claim repository does not match cited evidence"
            )
    normalized_ids = tuple(evidence_ids)
    identity = _canonical_json_bytes(
        {
            "kind": kind,
            "repository": repository,
            "summary": text,
            "evidence_ids": list(normalized_ids),
        }
    )
    return SummaryClaim(
        claim_id=hashlib.sha256(identity).hexdigest(),
        kind=str(kind),
        repository=repository,
        summary=text,
        evidence_ids=normalized_ids,
    )


def _event_identity(event: object) -> tuple[str, Mapping[str, object]]:
    if not isinstance(event, dict):
        raise GitHubDailySummaryError("evidence event must be an object")
    evidence_id = _require_sha(event.get("evidence_id"), label="evidence_id")
    normalized = dict(event)
    normalized.pop("evidence_id")
    actual = sha256_bytes(_canonical_json_bytes(normalized))
    if actual != evidence_id:
        raise GitHubDailySummaryError("evidence event identity mismatch")
    repository = event.get("repository")
    if (
        not isinstance(repository, str)
        or _REPOSITORY_RE.fullmatch(repository) is None
    ):
        raise GitHubDailySummaryError("evidence event repository is invalid")
    return evidence_id, event


def parse_evidence_bundle(data: bytes) -> EvidenceBundle:
    value = _decode_json_object(data, label="GitHub Daily evidence bundle")
    required = {
        "record_version",
        "date",
        "timezone",
        "window_start",
        "window_end",
        "projects",
        "repositories",
        "events",
    }
    if set(value) != required or value.get("record_version") != RECORD_VERSION:
        raise GitHubDailySummaryError(
            "GitHub Daily evidence bundle properties do not match contract"
        )
    if value.get("timezone") != "Asia/Tokyo":
        raise GitHubDailySummaryError("Daily evidence timezone is not canonical")
    for key in ("date", "window_start", "window_end"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise GitHubDailySummaryError(f"Daily evidence {key} is invalid")
    raw_projects = value["projects"]
    raw_repositories = value["repositories"]
    raw_events = value["events"]
    if not isinstance(raw_projects, list) or not isinstance(raw_repositories, list):
        raise GitHubDailySummaryError("Daily evidence Project metadata is invalid")
    if not isinstance(raw_events, list):
        raise GitHubDailySummaryError("Daily evidence events are invalid")

    projects: list[Mapping[str, object]] = []
    for item in raw_projects:
        if (
            not isinstance(item, dict)
            or set(item) != {"project_path", "repository"}
            or not isinstance(item["project_path"], str)
            or not item["project_path"].startswith("10-Project/")
            or not item["project_path"].endswith(".md")
            or not isinstance(item["repository"], str)
            or _REPOSITORY_RE.fullmatch(item["repository"]) is None
        ):
            raise GitHubDailySummaryError("Daily evidence Project binding is invalid")
        projects.append(dict(item))

    repositories = tuple(raw_repositories)
    if (
        not all(
            isinstance(item, str)
            and _REPOSITORY_RE.fullmatch(item) is not None
            for item in repositories
        )
        or len(set(repositories)) != len(repositories)
        or list(repositories) != sorted(repositories, key=str.casefold)
    ):
        raise GitHubDailySummaryError("Daily evidence repositories are invalid")

    events: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for raw in raw_events:
        evidence_id, event = _event_identity(raw)
        if evidence_id in seen:
            raise GitHubDailySummaryError("Daily evidence contains duplicate event")
        seen.add(evidence_id)
        if event.get("repository") not in repositories:
            raise GitHubDailySummaryError(
                "Daily evidence event repository is not declared"
            )
        events.append(dict(event))

    return EvidenceBundle(
        sha256=sha256_bytes(data),
        date=str(value["date"]),
        timezone=str(value["timezone"]),
        window_start=str(value["window_start"]),
        window_end=str(value["window_end"]),
        projects=tuple(projects),
        repositories=repositories,
        events=tuple(events),
    )


def load_evidence_bundle(path: Path) -> EvidenceBundle:
    data = _read_exact_file(path)
    bundle = parse_evidence_bundle(data)
    name = path.name
    suffix = ".github-daily-evidence.json"
    if not name.endswith(suffix):
        raise GitHubDailySummaryError("evidence artifact filename is invalid")
    expected = name[: -len(suffix)]
    if expected != bundle.sha256:
        raise GitHubDailySummaryError(
            "evidence artifact filename does not match content SHA"
        )
    return bundle


def _stage_directory(state_root: Path, child: str) -> Path:
    root = state_root.absolute()
    _require_safe_directory(root, create=False)
    summary = root / SUMMARY_ROOT
    _require_safe_directory(summary, create=True)
    target = summary / child
    _require_safe_directory(target, create=True)
    return target


def _store(
    state_root: Path,
    child: str,
    suffix: str,
    data: bytes,
) -> tuple[str, Path]:
    digest = sha256_bytes(data)
    path = _stage_directory(state_root, child) / f"{digest}.{suffix}.json"
    return digest, _store_immutable(path, data)


def store_context(
    state_root: Path,
    context: SummaryContext,
) -> tuple[str, Path]:
    return _store(
        state_root,
        CONTEXT_DIR,
        f"github-daily-{context.stage}-context",
        context.to_json_bytes(),
    )


def store_claim_output(
    state_root: Path,
    output: ClaimOutput,
) -> tuple[str, Path]:
    return _store(
        state_root,
        OUTPUT_DIR,
        f"github-daily-{output.stage}-output",
        output.to_json_bytes(),
    )


def store_ground_output(
    state_root: Path,
    output: GroundOutput,
) -> tuple[str, Path]:
    return _store(
        state_root,
        OUTPUT_DIR,
        "github-daily-ground-output",
        output.to_json_bytes(),
    )


def store_inference_record(
    state_root: Path,
    record: InferenceRecord,
) -> tuple[str, Path]:
    return _store(
        state_root,
        PROVENANCE_DIR,
        "github-daily-inference",
        record.to_json_bytes(),
    )


def store_grounded_summary(
    state_root: Path,
    summary: GroundedSummary,
) -> tuple[str, Path]:
    return _store(
        state_root,
        FINAL_DIR,
        "github-daily-grounded-summary",
        summary.to_json_bytes(),
    )


def _partition_payloads(
    items: Sequence[object],
    *,
    max_bytes: int,
    envelope: Callable[[Sequence[object], int, int], bytes],
) -> list[list[object]]:
    if max_bytes < 1024:
        raise GitHubDailySummaryError("context byte limit is too small")
    groups: list[list[object]] = []
    current: list[object] = []
    for item in items:
        candidate = [*current, item]
        if len(envelope(candidate, MAX_CONTEXT_BATCHES, MAX_CONTEXT_BATCHES)) <= max_bytes:
            current = candidate
            continue
        if not current:
            raise GitHubDailySummaryError(
                "one normalized item exceeds the context byte limit"
            )
        groups.append(current)
        current = [item]
        if len(envelope(current, MAX_CONTEXT_BATCHES, MAX_CONTEXT_BATCHES)) > max_bytes:
            raise GitHubDailySummaryError(
                "one normalized item exceeds the context byte limit"
            )
    if current:
        groups.append(current)
    if len(groups) > MAX_CONTEXT_BATCHES:
        raise GitHubDailySummaryError("context batch count exceeds contract")
    return groups


def partition_evidence(
    bundle: EvidenceBundle,
    *,
    max_bytes: int = MAX_PARTIAL_CONTEXT_BYTES,
) -> tuple[SummaryContext, ...]:
    if not bundle.events:
        return ()
    raw = list(bundle.events)

    def envelope(
        items: Sequence[object],
        index: int,
        count: int,
    ) -> bytes:
        context = SummaryContext(
            stage=PARTIAL_STAGE,
            evidence_bundle_sha256=bundle.sha256,
            batch_index=index,
            batch_count=count,
            source_output_sha256s=(),
            events=tuple(item for item in items if isinstance(item, dict)),
        )
        return context.to_json_bytes()

    # One repository per model context: preserve order within each repo.
    grouped: dict[str, list[object]] = {}
    for event in raw:
        grouped.setdefault(str(event["repository"]), []).append(event)
    groups: list[list[object]] = []
    for repository_events in grouped.values():
        groups.extend(
            _partition_payloads(
                repository_events,
                max_bytes=max_bytes,
                envelope=envelope,
            )
        )
    if len(groups) > MAX_CONTEXT_BATCHES:
        raise GitHubDailySummaryError(
            "context batch count exceeds contract"
        )
    contexts = tuple(
        SummaryContext(
            stage=PARTIAL_STAGE,
            evidence_bundle_sha256=bundle.sha256,
            batch_index=index,
            batch_count=len(groups),
            source_output_sha256s=(),
            events=tuple(item for item in group if isinstance(item, dict)),
        )
        for index, group in enumerate(groups)
    )
    if any(len(item.to_json_bytes()) > max_bytes for item in contexts):
        raise GitHubDailySummaryError("final partial context exceeds byte limit")
    flattened = [
        str(event["evidence_id"])
        for context in contexts
        for event in context.events
    ]
    original = [str(event["evidence_id"]) for event in bundle.events]
    if len(flattened) != len(original) or set(flattened) != set(original):
        raise GitHubDailySummaryError("partial partition is not lossless")
    for repository, events in grouped.items():
        original_repo_ids = [str(event["evidence_id"]) for event in events]
        observed_repo_ids = [
            str(event["evidence_id"])
            for context in contexts
            for event in context.events
            if event["repository"] == repository
        ]
        if observed_repo_ids != original_repo_ids:
            raise GitHubDailySummaryError(
                "partial partition changed repository event order"
            )
    if any(
        len({str(event["repository"]) for event in context.events}) != 1
        for context in contexts
    ):
        raise GitHubDailySummaryError(
            "partial context mixes repositories"
        )
    return contexts


def claim_output_schema() -> dict[str, object]:
    """Model-facing references; stored claims retain exact SHA evidence IDs."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["claims"],
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": MAX_CLAIMS_PER_OUTPUT,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "kind", "summary", "source_refs",
                    ],
                    "properties": {
                        "kind": {"enum": sorted(CLAIM_KINDS)},
                        "summary": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_CLAIM_SUMMARY_CHARS,
                        },
                        "source_refs": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_CLAIM_EVIDENCE_IDS,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 0},
                        },
                    },
                },
            }
        },
    }


def grounding_output_schema() -> dict[str, object]:
    """One-claim verdict schema: no model-generated identity fields."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "reason"],
        "properties": {
            "verdict": {"enum": ["supported", "unsupported"]},
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_GROUND_REASON_CHARS,
            },
        },
    }



def _prompt_spec(stage: str) -> PromptSpec:
    if stage == PARTIAL_STAGE:
        version = PARTIAL_PROMPT_VERSION
        system = PARTIAL_SYSTEM_PROMPT
        schema = claim_output_schema()
    elif stage == REDUCE_STAGE:
        version = REDUCE_PROMPT_VERSION
        system = REDUCE_SYSTEM_PROMPT
        schema = claim_output_schema()
    elif stage == GROUND_STAGE:
        version = GROUND_PROMPT_VERSION
        system = GROUND_SYSTEM_PROMPT
        schema = grounding_output_schema()
    else:
        raise GitHubDailySummaryError("unknown inference stage")
    identity = _canonical_json_bytes(
        {
            "template_version": version,
            "system": system,
            "output_schema": schema,
        }
    )
    return PromptSpec(
        stage=stage,
        template_version=version,
        template_sha256=sha256_bytes(identity),
        system=system,
        output_schema=schema,
    )


def prompt_spec(stage: str) -> PromptSpec:
    return _prompt_spec(stage)


def _model_source_indices(
    refs: object,
    *,
    source_count: int,
) -> tuple[int, ...]:
    """Accept only explicit in-batch integer citations, never fuzzy IDs."""
    if (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= MAX_CLAIM_EVIDENCE_IDS
        or any(type(ref) is not int for ref in refs)
        or len(set(refs)) != len(refs)
        or any(ref < 0 or ref >= source_count for ref in refs)
    ):
        raise GitHubDailySummaryError(
            "model source_refs are invalid or outside the context"
        )
    return tuple(refs)


def _parse_model_claim_output(
    data: bytes,
    *,
    context: SummaryContext,
    input_context_sha256: str,
    events_by_id: Mapping[str, Mapping[str, object]],
) -> ClaimOutput:
    """Resolve short refs to original IDs before invoking the exact closure gate."""
    if len(data) > MAX_OUTPUT_BYTES:
        raise GitHubDailySummaryError("claim provider output exceeds byte limit")
    value = _decode_json_object(data, label=f"{context.stage} provider output")
    if (
        context.stage not in {PARTIAL_STAGE, REDUCE_STAGE}
        or set(value) != {"claims"}
        or not isinstance(value["claims"], list)
        or len(value["claims"]) > MAX_CLAIMS_PER_OUTPUT
    ):
        raise GitHubDailySummaryError("model claim output contract mismatch")

    source_count = (
        len(context.events)
        if context.stage == PARTIAL_STAGE
        else len(context.claims)
    )
    allowed = (
        {str(event["evidence_id"]) for event in context.events}
        if context.stage == PARTIAL_STAGE
        else {
            evidence_id
            for claim in context.claims
            for evidence_id in claim.evidence_ids
        }
    )

    normalized: list[dict[str, object]] = []
    for raw in value["claims"]:
        if not isinstance(raw, dict) or set(raw) != {
            "kind", "summary", "source_refs",
        }:
            raise GitHubDailySummaryError(
                "model claim properties do not match contract"
            )
        refs = _model_source_indices(
            raw["source_refs"],
            source_count=source_count,
        )
        if context.stage == PARTIAL_STAGE:
            evidence_ids = [
                str(context.events[index]["evidence_id"])
                for index in refs
            ]
        else:
            evidence_ids = list(dict.fromkeys(
                evidence_id
                for index in refs
                for evidence_id in context.claims[index].evidence_ids
            ))
            if len(evidence_ids) > MAX_CLAIM_EVIDENCE_IDS:
                raise GitHubDailySummaryError(
                    "model-selected sources exceed original evidence limit"
                )
        repositories: list[str] = []
        for evidence_id in evidence_ids:
            event = events_by_id.get(evidence_id)
            if event is None:
                raise GitHubDailySummaryError(
                    "model source evidence does not exist"
                )
            repository = event.get("repository")
            if not isinstance(repository, str):
                raise GitHubDailySummaryError(
                    "model source evidence repository is invalid"
                )
            repositories.append(repository)
        if len(set(repositories)) != 1:
            raise GitHubDailySummaryError(
                "model source_refs span repositories"
            )
        repository = repositories[0]
        if context.stage == REDUCE_STAGE and any(
            context.claims[index].repository != repository
            for index in refs
        ):
            raise GitHubDailySummaryError(
                "reducer source repository does not match cited evidence"
            )
        normalized.append({
            "kind": raw["kind"],
            "repository": repository,
            "summary": raw["summary"],
            "evidence_ids": evidence_ids,
        })

    return parse_claim_output(
        _canonical_json_bytes({"claims": normalized}),
        stage=context.stage,
        input_context_sha256=input_context_sha256,
        allowed_evidence_ids=allowed,
        events_by_id=events_by_id,
    )


def _parse_model_ground_output(
    data: bytes,
    *,
    context: SummaryContext,
    input_context_sha256: str,
) -> GroundOutput:
    """Bind one verdict to the sole source claim, never a model-chosen ID."""
    if context.stage != GROUND_STAGE or len(context.claims) != 1:
        raise GitHubDailySummaryError(
            "model grounding requires exactly one input claim"
        )
    if len(data) > MAX_OUTPUT_BYTES:
        raise GitHubDailySummaryError("grounding output exceeds byte limit")
    value = _decode_json_object(data, label="grounding provider output")
    if set(value) != {"verdict", "reason"}:
        raise GitHubDailySummaryError(
            "model grounding verdict properties do not match contract"
        )

    # The model does not identify a target. Normalized GroundOutput retains
    # exactly the content-addressed claim identity from this input context.
    normalized = _canonical_json_bytes(
        {
            "assessments": [{
                "claim_id": context.claims[0].claim_id,
                "verdict": value["verdict"],
                "reason": value["reason"],
            }],
        }
    )
    return parse_ground_output(
        normalized,
        input_context_sha256=input_context_sha256,
        claims=context.claims,
    )



def parse_claim_output(
    data: bytes,
    *,
    stage: str,
    input_context_sha256: str,
    allowed_evidence_ids: set[str],
    events_by_id: Mapping[str, Mapping[str, object]],
) -> ClaimOutput:
    if stage not in {PARTIAL_STAGE, REDUCE_STAGE}:
        raise GitHubDailySummaryError("claim output stage is invalid")
    if len(data) > MAX_OUTPUT_BYTES:
        raise GitHubDailySummaryError("claim provider output exceeds byte limit")
    value = _decode_json_object(data, label=f"{stage} provider output")
    if set(value) != {"claims"} or not isinstance(value["claims"], list):
        raise GitHubDailySummaryError("claim provider output contract mismatch")
    if len(value["claims"]) > MAX_CLAIMS_PER_OUTPUT:
        raise GitHubDailySummaryError("claim provider output has too many claims")
    claims: list[SummaryClaim] = []
    seen: set[str] = set()
    for raw in value["claims"]:
        if not isinstance(raw, dict) or set(raw) != {
            "kind",
            "repository",
            "summary",
            "evidence_ids",
        }:
            raise GitHubDailySummaryError("claim properties do not match contract")
        claim = _normalized_claim(
            kind=raw["kind"],
            repository=raw["repository"],
            summary=raw["summary"],
            evidence_ids=raw["evidence_ids"],
            allowed_evidence_ids=allowed_evidence_ids,
            events_by_id=events_by_id,
        )
        if claim.claim_id in seen:
            continue
        seen.add(claim.claim_id)
        claims.append(claim)
    return ClaimOutput(
        stage=stage,
        input_context_sha256=_require_sha(
            input_context_sha256,
            label="input context SHA",
        ),
        claims=tuple(claims),
    )


def build_reduce_contexts(
    bundle: EvidenceBundle,
    outputs: Sequence[StoredClaimOutput],
    *,
    max_bytes: int = MAX_REDUCE_CONTEXT_BYTES,
) -> tuple[SummaryContext, ...]:
    indexed: list[tuple[SummaryClaim, str]] = []
    for stored in outputs:
        if stored.output.stage != PARTIAL_STAGE:
            raise GitHubDailySummaryError(
                "reducer accepts only partial outputs"
            )
        for claim in stored.output.claims:
            indexed.append((claim, stored.sha256))
    if not indexed:
        return ()

    def envelope(
        items: Sequence[object],
        index: int,
        count: int,
    ) -> bytes:
        pairs = [
            item for item in items
            if isinstance(item, tuple) and len(item) == 2
        ]
        hashes = tuple(dict.fromkeys(str(item[1]) for item in pairs))
        claims = tuple(item[0] for item in pairs if isinstance(item[0], SummaryClaim))
        return SummaryContext(
            stage=REDUCE_STAGE,
            evidence_bundle_sha256=bundle.sha256,
            batch_index=index,
            batch_count=count,
            source_output_sha256s=hashes,
            claims=claims,
        ).to_json_bytes()

    # The reducer must not have cross-project citation choices.
    grouped: dict[str, list[object]] = {}
    for claim, source_sha in indexed:
        grouped.setdefault(claim.repository, []).append(
            (claim, source_sha)
        )
    groups: list[list[object]] = []
    for repository_claims in grouped.values():
        # Byte-bounding alone is insufficient: eight input claims may each
        # carry up to eight different original evidence IDs. Bound their
        # entire union before invoking the model, then apply the byte cap.
        evidence_bounded: list[list[object]] = []
        current: list[object] = []
        current_evidence: set[str] = set()
        for pair in repository_claims:
            claim = pair[0]
            evidence_ids = set(claim.evidence_ids)
            if (
                not evidence_ids
                or len(evidence_ids) != len(claim.evidence_ids)
                or len(evidence_ids) > MAX_CLAIM_EVIDENCE_IDS
            ):
                raise GitHubDailySummaryError(
                    "reducer source claim evidence count is invalid"
                )
            if current and (
                len(current) >= MAX_CLAIM_EVIDENCE_IDS
                or len(current_evidence | evidence_ids)
                > MAX_CLAIM_EVIDENCE_IDS
            ):
                evidence_bounded.append(current)
                current = []
                current_evidence = set()
            current.append(pair)
            current_evidence.update(evidence_ids)
        if current:
            evidence_bounded.append(current)

        for group in evidence_bounded:
            groups.extend(
                _partition_payloads(
                    group,
                    max_bytes=max_bytes,
                    envelope=envelope,
                )
            )
    if len(groups) > MAX_CONTEXT_BATCHES:
        raise GitHubDailySummaryError(
            "context batch count exceeds contract"
        )
    contexts: list[SummaryContext] = []
    for index, group in enumerate(groups):
        pairs = [
            item for item in group
            if isinstance(item, tuple) and len(item) == 2
        ]
        context = SummaryContext(
            stage=REDUCE_STAGE,
            evidence_bundle_sha256=bundle.sha256,
            batch_index=index,
            batch_count=len(groups),
            source_output_sha256s=tuple(
                dict.fromkeys(str(item[1]) for item in pairs)
            ),
            claims=tuple(
                item[0] for item in pairs if isinstance(item[0], SummaryClaim)
            ),
        )
        if len(context.to_json_bytes()) > max_bytes:
            raise GitHubDailySummaryError("final reducer context exceeds byte limit")
        if len({claim.repository for claim in context.claims}) != 1:
            raise GitHubDailySummaryError(
                "reducer context mixes repositories"
            )
        if len({
            evidence_id
            for claim in context.claims
            for evidence_id in claim.evidence_ids
        }) > MAX_CLAIM_EVIDENCE_IDS:
            raise GitHubDailySummaryError(
                "reducer context exceeds original evidence budget"
            )
        if len(context.claims) > MAX_CLAIM_EVIDENCE_IDS:
            raise GitHubDailySummaryError(
                "reducer context exceeds model source reference budget"
            )
        contexts.append(context)
    return tuple(contexts)


def _dedupe_claims(
    outputs: Sequence[StoredClaimOutput],
    *,
    stage: str,
) -> tuple[SummaryClaim, ...]:
    result: list[SummaryClaim] = []
    seen: set[str] = set()
    for stored in outputs:
        if stored.output.stage != stage:
            raise GitHubDailySummaryError("claim output stage mismatch")
        for claim in stored.output.claims:
            if claim.claim_id in seen:
                continue
            seen.add(claim.claim_id)
            result.append(claim)
    return tuple(result)


def build_ground_contexts(
    bundle: EvidenceBundle,
    outputs: Sequence[StoredClaimOutput],
    *,
    max_bytes: int = MAX_GROUND_CONTEXT_BYTES,
) -> tuple[SummaryContext, ...]:
    """Exactly one claim and its exact cited raw events per Grounding call."""
    claims = _dedupe_claims(outputs, stage=REDUCE_STAGE)
    if not claims:
        return ()
    if len(claims) > MAX_CONTEXT_BATCHES:
        raise GitHubDailySummaryError(
            "ground context batch count exceeds contract"
        )

    events_by_id = bundle.events_by_id
    source_by_claim: dict[str, str] = {}
    for stored in outputs:
        for claim in stored.output.claims:
            source_by_claim.setdefault(claim.claim_id, stored.sha256)

    contexts: list[SummaryContext] = []
    for index, claim in enumerate(claims):
        cited_ids = set(claim.evidence_ids)
        if not cited_ids or len(cited_ids) != len(claim.evidence_ids):
            raise GitHubDailySummaryError(
                "grounding claim evidence citations are invalid"
            )
        if any(
            evidence_id not in events_by_id
            or events_by_id[evidence_id].get("repository")
            != claim.repository
            for evidence_id in cited_ids
        ):
            raise GitHubDailySummaryError(
                "grounding claim evidence/repository closure mismatch"
            )
        events = tuple(
            event for event in bundle.events
            if str(event["evidence_id"]) in cited_ids
        )
        if len(events) != len(cited_ids):
            raise GitHubDailySummaryError(
                "grounding cited event coverage is incomplete"
            )
        context = SummaryContext(
            stage=GROUND_STAGE,
            evidence_bundle_sha256=bundle.sha256,
            batch_index=index,
            batch_count=len(claims),
            source_output_sha256s=(source_by_claim[claim.claim_id],),
            events=events,
            claims=(claim,),
        )
        if len(context.to_json_bytes()) > max_bytes:
            raise GitHubDailySummaryError(
                "one grounding claim exceeds context byte limit"
            )
        contexts.append(context)

    return tuple(contexts)



def parse_ground_output(
    data: bytes,
    *,
    input_context_sha256: str,
    claims: Sequence[SummaryClaim],
) -> GroundOutput:
    if len(data) > MAX_OUTPUT_BYTES:
        raise GitHubDailySummaryError("grounding output exceeds byte limit")
    value = _decode_json_object(data, label="grounding provider output")
    if set(value) != {"assessments"} or not isinstance(
        value["assessments"], list
    ):
        raise GitHubDailySummaryError("grounding output contract mismatch")
    expected = {claim.claim_id for claim in claims}
    observed: set[str] = set()
    assessments: list[GroundAssessment] = []
    for raw in value["assessments"]:
        if not isinstance(raw, dict) or set(raw) != {
            "claim_id",
            "verdict",
            "reason",
        }:
            raise GitHubDailySummaryError(
                "grounding assessment properties do not match contract"
            )
        claim_id = _require_sha(raw["claim_id"], label="grounding claim_id")
        if claim_id not in expected or claim_id in observed:
            raise GitHubDailySummaryError(
                "grounding assessment claim set does not match context"
            )
        verdict = raw["verdict"]
        if verdict not in {"supported", "unsupported"}:
            raise GitHubDailySummaryError("grounding verdict is invalid")
        reason = _plain_line(
            raw["reason"],
            label="grounding reason",
            max_chars=MAX_GROUND_REASON_CHARS,
            max_bytes=MAX_GROUND_REASON_BYTES,
        )
        observed.add(claim_id)
        assessments.append(
            GroundAssessment(
                claim_id=claim_id,
                verdict=str(verdict),
                reason=reason,
            )
        )
    if observed != expected:
        raise GitHubDailySummaryError(
            "grounding output must assess every input claim exactly once"
        )
    by_claim = {item.claim_id: item for item in assessments}
    return GroundOutput(
        input_context_sha256=_require_sha(
            input_context_sha256,
            label="input context SHA",
        ),
        assessments=tuple(by_claim[claim.claim_id] for claim in claims),
    )


def finalize_grounded_summary(
    bundle: EvidenceBundle,
    claims: Sequence[SummaryClaim],
    grounding_outputs: Sequence[StoredGroundOutput],
) -> GroundedSummary:
    assessments: dict[str, GroundAssessment] = {}
    for stored in grounding_outputs:
        for assessment in stored.output.assessments:
            if assessment.claim_id in assessments:
                raise GitHubDailySummaryError(
                    "claim was assessed by multiple grounding outputs"
                )
            assessments[assessment.claim_id] = assessment
    expected = {claim.claim_id for claim in claims}
    if set(assessments) != expected:
        raise GitHubDailySummaryError(
            "grounding outputs do not cover final claim set"
        )
    accepted: list[SummaryClaim] = []
    rejected: list[RejectedClaim] = []
    for claim in claims:
        assessment = assessments[claim.claim_id]
        if assessment.verdict == "supported":
            accepted.append(claim)
        else:
            rejected.append(
                RejectedClaim(
                    claim_id=claim.claim_id,
                    reason=assessment.reason,
                )
            )
    return GroundedSummary(
        evidence_bundle_sha256=bundle.sha256,
        claims=tuple(accepted),
        rejected_claims=tuple(rejected),
        grounding_output_sha256s=tuple(
            stored.sha256 for stored in grounding_outputs
        ),
    )


def _inference_record(
    *,
    stage: str,
    input_context_sha256: str,
    output_sha256: str,
    implementation_revision: str,
    prompt: PromptSpec,
    response: InferenceResponse,
) -> InferenceRecord:
    if stage not in STAGES:
        raise GitHubDailySummaryError("inference record stage is invalid")
    if (
        not isinstance(implementation_revision, str)
        or _IMPLEMENTATION_REVISION_RE.fullmatch(implementation_revision)
        is None
    ):
        raise GitHubDailySummaryError(
            "implementation revision must be lowercase 40..64 hex"
        )
    config = validate_model_config(dict(response.model_config))
    return InferenceRecord(
        stage=stage,
        input_context_sha256=_require_sha(
            input_context_sha256,
            label="inference input SHA",
        ),
        output_sha256=_require_sha(
            output_sha256,
            label="inference output SHA",
        ),
        implementation_revision=implementation_revision,
        prompt_template_version=_metadata(
            prompt.template_version,
            label="prompt template version",
        ),
        prompt_template_sha256=_require_sha(
            prompt.template_sha256,
            label="prompt template SHA",
        ),
        model_provider=_metadata(
            response.model_provider,
            label="model provider",
        ),
        model_identifier=_metadata(
            response.model_identifier,
            label="model identifier",
        ),
        model_revision=_metadata(
            response.model_revision,
            label="model revision",
        ),
        model_config=config,
        generated_at=_utc_now(),
    )


def _run_claim_stage(
    state_root: Path,
    bundle: EvidenceBundle,
    contexts: Sequence[SummaryContext],
    *,
    infer: Infer,
    implementation_revision: str,
) -> tuple[
    tuple[StoredClaimOutput, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    stored: list[StoredClaimOutput] = []
    context_shas: list[str] = []
    provenance_shas: list[str] = []
    events_by_id = bundle.events_by_id
    for context in contexts:
        context_sha, _ = store_context(state_root, context)
        context_shas.append(context_sha)
        prompt = _prompt_spec(context.stage)
        response = infer(prompt, context)
        try:
            output = _parse_model_claim_output(
                response.content,
                context=context,
                input_context_sha256=context_sha,
                events_by_id=events_by_id,
            )
        except GitHubDailySummaryError as exc:
            raise GitHubDailySummaryError(
                f"{context.stage} batch {context.batch_index + 1}/"
                f"{context.batch_count}: {exc}"
            ) from exc
        output_sha, _ = store_claim_output(state_root, output)
        stored.append(StoredClaimOutput(output_sha, output))
        record = _inference_record(
            stage=context.stage,
            input_context_sha256=context_sha,
            output_sha256=output_sha,
            implementation_revision=implementation_revision,
            prompt=prompt,
            response=response,
        )
        provenance_sha, _ = store_inference_record(state_root, record)
        provenance_shas.append(provenance_sha)
    return tuple(stored), tuple(context_shas), tuple(provenance_shas)


def _run_deterministic_reduce_stage(
    state_root: Path,
    bundle: EvidenceBundle,
    contexts: Sequence[SummaryContext],
) -> tuple[
    tuple[StoredClaimOutput, ...],
    tuple[str, ...],
]:
    """Carry forward validated Partial claims without an LLM citation step.

    Reduction is intentionally evidence-preserving rather than semantic
    rewriting. Every copied claim is revalidated against the original raw
    evidence and its canonical claim identity before storage. Later
    _dedupe_claims() removes only exact claim-ID duplicates.
    """
    stored: list[StoredClaimOutput] = []
    context_shas: list[str] = []
    events_by_id = bundle.events_by_id
    allowed_evidence_ids = set(events_by_id)

    for context in contexts:
        if context.stage != REDUCE_STAGE:
            raise GitHubDailySummaryError(
                "deterministic reducer requires reduce context"
            )
        if context.evidence_bundle_sha256 != bundle.sha256:
            raise GitHubDailySummaryError(
                "deterministic reducer context evidence binding mismatch"
            )

        # Recompute every claimed identity and citation closure from the
        # original immutable Evidence before creating a reducer output.
        for claim in context.claims:
            normalized = _normalized_claim(
                kind=claim.kind,
                repository=claim.repository,
                summary=claim.summary,
                evidence_ids=list(claim.evidence_ids),
                allowed_evidence_ids=allowed_evidence_ids,
                events_by_id=events_by_id,
            )
            if normalized != claim:
                raise GitHubDailySummaryError(
                    "deterministic reducer source claim identity mismatch"
                )

        context_sha, _ = store_context(state_root, context)
        context_shas.append(context_sha)
        output = ClaimOutput(
            stage=REDUCE_STAGE,
            input_context_sha256=context_sha,
            claims=context.claims,
        )
        output_sha, _ = store_claim_output(state_root, output)
        stored.append(StoredClaimOutput(output_sha, output))

    return tuple(stored), tuple(context_shas)


def _run_ground_stage(
    state_root: Path,
    contexts: Sequence[SummaryContext],
    *,
    infer: Infer,
    implementation_revision: str,
) -> tuple[
    tuple[StoredGroundOutput, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    stored: list[StoredGroundOutput] = []
    context_shas: list[str] = []
    provenance_shas: list[str] = []
    for context in contexts:
        if context.stage != GROUND_STAGE:
            raise GitHubDailySummaryError("ground stage context is invalid")
        context_sha, _ = store_context(state_root, context)
        context_shas.append(context_sha)
        prompt = _prompt_spec(GROUND_STAGE)
        response = infer(prompt, context)
        try:
            output = _parse_model_ground_output(
                response.content,
                context=context,
                input_context_sha256=context_sha,
            )
        except GitHubDailySummaryError as exc:
            raise GitHubDailySummaryError(
                f"ground batch {context.batch_index + 1}/"
                f"{context.batch_count}: {exc}"
            ) from exc
        output_sha, _ = store_ground_output(state_root, output)
        stored.append(StoredGroundOutput(output_sha, output))
        record = _inference_record(
            stage=GROUND_STAGE,
            input_context_sha256=context_sha,
            output_sha256=output_sha,
            implementation_revision=implementation_revision,
            prompt=prompt,
            response=response,
        )
        provenance_sha, _ = store_inference_record(state_root, record)
        provenance_shas.append(provenance_sha)
    return tuple(stored), tuple(context_shas), tuple(provenance_shas)


def run_pipeline(
    *,
    evidence_path: Path,
    state_root: Path,
    infer: Infer,
    implementation_revision: str,
    partial_context_bytes: int = MAX_PARTIAL_CONTEXT_BYTES,
    reduce_context_bytes: int = MAX_REDUCE_CONTEXT_BYTES,
    ground_context_bytes: int = MAX_GROUND_CONTEXT_BYTES,
) -> PipelineResult:
    bundle = load_evidence_bundle(evidence_path)
    partial_contexts = partition_evidence(
        bundle,
        max_bytes=partial_context_bytes,
    )
    partial_outputs, partial_context_shas, partial_provenance = (
        _run_claim_stage(
            state_root,
            bundle,
            partial_contexts,
            infer=infer,
            implementation_revision=implementation_revision,
        )
    )

    reduce_contexts = build_reduce_contexts(
        bundle,
        partial_outputs,
        max_bytes=reduce_context_bytes,
    )
    reduce_outputs, reduce_context_shas = (
        _run_deterministic_reduce_stage(
            state_root,
            bundle,
            reduce_contexts,
        )
    )
    final_claims = _dedupe_claims(
        reduce_outputs,
        stage=REDUCE_STAGE,
    )

    ground_contexts = build_ground_contexts(
        bundle,
        reduce_outputs,
        max_bytes=ground_context_bytes,
    )
    ground_outputs, ground_context_shas, ground_provenance = (
        _run_ground_stage(
            state_root,
            ground_contexts,
            infer=infer,
            implementation_revision=implementation_revision,
        )
    )

    if final_claims:
        grounded = finalize_grounded_summary(
            bundle,
            final_claims,
            ground_outputs,
        )
    else:
        grounded = GroundedSummary(
            evidence_bundle_sha256=bundle.sha256,
            claims=(),
            rejected_claims=(),
            grounding_output_sha256s=(),
        )
    grounded_sha, grounded_path = store_grounded_summary(
        state_root,
        grounded,
    )
    return PipelineResult(
        evidence_bundle_sha256=bundle.sha256,
        partial_context_sha256s=partial_context_shas,
        partial_output_sha256s=tuple(
            item.sha256 for item in partial_outputs
        ),
        reduce_context_sha256s=reduce_context_shas,
        reduce_output_sha256s=tuple(
            item.sha256 for item in reduce_outputs
        ),
        ground_context_sha256s=ground_context_shas,
        ground_output_sha256s=tuple(
            item.sha256 for item in ground_outputs
        ),
        provenance_sha256s=(
            *partial_provenance,
            *ground_provenance,
        ),
        grounded_summary_sha256=grounded_sha,
        grounded_summary_path=grounded_path,
        claim_count=len(grounded.claims),
        rejected_count=len(grounded.rejected_claims),
    )
