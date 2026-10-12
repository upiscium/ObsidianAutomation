"""Read-only eligibility proof for #309 deterministic Validation rejection cleanup.

No WebDAV calls, DELETE, service control, regeneration, or Vault access. This
module deliberately requires four *successfully published* original projection
requests and an authoritative current-generation Validator rejection. These
checks authorize only a future narrowly reviewed Sync-side conditional cleanup
implementation; a proof alone is never permission to perform DELETE.
"""
from __future__ import annotations

import os
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    parse_validation_record,
    sha256_bytes,
)
from .human_projection import (
    PROJECTION_ROOT,
    REQUEST_STAGE,
    RESULT_STAGE,
    ROLE_NAMES,
    STAGE_FOLDERS,
    ProjectionRequest,
    _code_fence,
    _inline_code,
    _projection_markdown,
    parse_request,
    parse_result,
)
from .pre_review_job import _connect_ro
from .planner_selection_binding import load_binding
from .rejection_source_binding import verify_original_producers, proposal_fields
from .production_io import canonical_io_lock


VERSION = 1
STAGES = ("input", "context", "generation", "validation")
ROLES = {"input": "reader", "context": "reader",
         "generation": "generator", "validation": "validator"}
SUFFIX = ".rejected-validation-cleanup-attestation.json"
MAX_SCAN = 4096


class RejectionCleanupError(ArtifactLifecycleError):
    """The proposed deletion cannot be proven to be safe."""


class RejectionCleanupPending(RejectionCleanupError):
    """Publication has not yet produced a complete immutable four-stage proof."""


@dataclass(frozen=True)
class AttestedTarget:
    stage: str
    path: str
    request_sha256: str
    result_sha256: str
    content_sha256: str

    def payload(self) -> dict[str, str]:
        return {
            "stage": self.stage, "path": self.path,
            "request_sha256": self.request_sha256,
            "result_sha256": self.result_sha256,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class RejectionCleanupAttestation:
    case_id: str
    job_id: str
    proposal_sha256: str
    validation_sha256: str
    targets: tuple[AttestedTarget, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes({
            "record_version": VERSION,
            "kind": "deterministic_validation_reject_preflight",
            "case_id": self.case_id,
            "job_id": self.job_id,
            "proposal_sha256": self.proposal_sha256,
            "validation_sha256": self.validation_sha256,
            "targets": [target.payload() for target in self.targets],
        })


MAX_REQUEST_BYTES = 768 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


def _bounded_regular_file(path: Path, *, cap: int = MAX_ARTIFACT_BYTES) -> bytes:
    """Read the same nofollow, nonblocking, fstat-verified regular descriptor.

    A producer can replace a pathname after lstat, including with a FIFO;
    O_NONBLOCK is essential under a shared canonical I/O lock. Never trust
    a path precheck to validate a later file descriptor.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RejectionCleanupError("immutable artifact is not safely readable") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size < 0
            or info.st_size > cap
        ):
            raise RejectionCleanupError("unsafe artifact type, links or size")
        content = bytearray()
        while len(content) <= cap:
            chunk = os.read(fd, min(65536, cap + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > cap:
            raise RejectionCleanupError("immutable artifact read size exceeds bound")
        return bytes(content)
    except OSError as exc:
        raise RejectionCleanupError("immutable artifact read failed") from exc
    finally:
        os.close(fd)


def _target(case_id: str, stage: str) -> str:
    if stage not in STAGES:
        raise RejectionCleanupError("cleanup stage is not in the four-path allowlist")
    case = _require_sha256(case_id, label="rejection cleanup case_id")
    return f"{PROJECTION_ROOT}/{STAGE_FOLDERS[stage]}/{case}.md"


def parse_attestation(data: bytes) -> RejectionCleanupAttestation:
    value = _decode_json_object(data, label="rejected Validation cleanup attestation")
    fields = {"record_version", "kind", "case_id", "job_id",
              "proposal_sha256", "validation_sha256", "targets"}
    if (
        set(value) != fields
        or type(value["record_version"]) is not int
        or value["record_version"] != VERSION
        or value["kind"] != "deterministic_validation_reject_preflight"
    ):
        raise RejectionCleanupError("cleanup attestation contract mismatch")
    case = _require_sha256(value["case_id"], label="case_id")
    targets = value["targets"]
    if not isinstance(targets, list) or len(targets) != len(STAGES):
        raise RejectionCleanupError("cleanup must attest exactly four stage targets")
    validated: list[AttestedTarget] = []
    required_target = {
        "stage", "path", "request_sha256", "result_sha256", "content_sha256"
    }
    for expected_stage, item in zip(STAGES, targets):
        if not isinstance(item, dict) or set(item) != required_target:
            raise RejectionCleanupError("cleanup target schema mismatch")
        if item["stage"] != expected_stage or item["path"] != _target(case, expected_stage):
            raise RejectionCleanupError("cleanup target stage, order or path mismatch")
        validated.append(AttestedTarget(
            stage=expected_stage,
            path=item["path"],
            request_sha256=_require_sha256(item["request_sha256"], label="request_sha256"),
            result_sha256=_require_sha256(item["result_sha256"], label="result_sha256"),
            content_sha256=_require_sha256(item["content_sha256"], label="content_sha256"),
        ))
    proof = RejectionCleanupAttestation(
        case_id=case,
        job_id=_require_sha256(value["job_id"], label="job_id"),
        proposal_sha256=_require_sha256(
            value["proposal_sha256"], label="proposal_sha256"),
        validation_sha256=_require_sha256(
            value["validation_sha256"], label="validation_sha256"),
        targets=tuple(validated),
    )
    if proof.to_json_bytes() != data:
        raise RejectionCleanupError("cleanup attestation is not canonical")
    return proof


def _scheduler_authority(ai_root: Path, case_id: str) -> tuple[str, str, str, str, str]:
    """Reject even a genuine published Validation note unless the latest
    authoritative job generation has terminal Validator-reject evidence.
    """
    with closing(_connect_ro(ai_root)) as conn:
        generation = conn.execute(
            "SELECT g.generation_id, g.job_id, g.generation_index, g.state,"
            " j.context_sha256, j.recipe_sha256 FROM generations g JOIN jobs j ON j.job_id=g.job_id"
            " WHERE g.generation_id = ?",
            (case_id,),
        ).fetchone()
        if generation is None or generation["state"] != "deterministic_reject":
            raise RejectionCleanupError("generation is not durably rejected")
        newer = conn.execute(
            "SELECT MAX(generation_index) AS latest_index "
            "FROM generations WHERE job_id = ?",
            (generation["job_id"],),
        ).fetchone()
        if newer is None or generation["generation_index"] != newer["latest_index"]:
            raise RejectionCleanupError("rejected generation is no longer current")
        recent = conn.execute(
            "SELECT stage, status, reason_code FROM attempts "
            "WHERE generation_id = ? ORDER BY rowid DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if (
            recent is None
            or recent["stage"] != "validation"
            or recent["status"] != "deterministic_reject"
            or recent["reason_code"] != "validation_rejected"
        ):
            raise RejectionCleanupError(
                "generation has no final deterministic Validator rejection attempt"
            )
        source = conn.execute(
            "SELECT output_json FROM stage_outputs "
            "WHERE generation_id = ? AND stage = 'generation'",
            (case_id,),
        ).fetchone()
        if source is None or not isinstance(source["output_json"], str):
            raise RejectionCleanupError("rejected generation has no selected proposal")
        output = _decode_json_object(
            source["output_json"].encode("utf-8"), label="selected generation output"
        )
        proposal = _require_sha256(output.get("proposal_sha256"), label="proposal_sha256")
        generation_sha = _require_sha256(
            output.get("generation_sha256"), label="generation_sha256"
        )
        return (
            _require_sha256(generation["job_id"], label="job_id"),
            _require_sha256(generation["context_sha256"], label="context_sha256"),
            _require_sha256(generation["recipe_sha256"], label="recipe_sha256"),
            proposal,
            generation_sha,
        )


def _published_originals(
    ai_root: Path, case_id: str, *, max_scan: int,
) -> dict[str, tuple[str, ProjectionRequest, str]]:
    if type(max_scan) is not int or not 1 <= max_scan <= 16384:
        raise RejectionCleanupError("max_scan must be an integer 1..16384")
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    request_root = root / REQUEST_STAGE
    _require_safe_directory(request_root, create=False)
    result_root = root / RESULT_STAGE
    _require_safe_directory(result_root, create=False)

    by_stage: dict[str, tuple[str, ProjectionRequest, str | None]] = {}
    scanned = 0
    for role in ROLE_NAMES:
        directory = request_root / role
        _require_safe_directory(directory, create=False)
        with os.scandir(directory) as it:
            candidates = []
            for entry in it:
                # Count *all* entries, including hidden/nonmatching, so a
                # writer cannot monopolize canonical-I/O with junk filenames.
                scanned += 1
                if scanned > max_scan:
                    raise RejectionCleanupPending("publication scan bound exceeded")
                if entry.name.endswith(".projection.json"):
                    candidates.append(directory / entry.name)
        for path in sorted(candidates):
            data = _bounded_regular_file(path, cap=MAX_REQUEST_BYTES)
            digest = _require_sha256(
                path.name.removesuffix(".projection.json"),
                label="projection request digest",
            )
            if sha256_bytes(data) != digest:
                raise RejectionCleanupError("projection request hash mismatch")
            request = parse_request(data)
            if request.case_id != case_id:
                continue
            if request.stage not in STAGES:
                raise RejectionCleanupError(
                    "unexpected later-stage projection exists for rejected generation"
                )
            if ROLES[request.stage] != role or request.stage in by_stage:
                raise RejectionCleanupError("projection role or uniqueness is invalid")
            result_path = result_root / f"{digest}.projection-result.json"
            if not os.path.lexists(result_path):
                # Defer Pending until the *whole* bounded scan has checked for
                # contradictory later-stage/duplicate evidence.
                by_stage[request.stage] = (digest, request, None)
                continue
            result_bytes = _bounded_regular_file(result_path, cap=MAX_RESULT_BYTES)
            result = parse_result(result_bytes)
            if (
                result.request_sha256 != digest
                or result.target_path != request.target_path
                or result.content_sha256 != request.content_sha256
                or result.result not in {"created", "already_matching"}
            ):
                raise RejectionCleanupError(
                    "original projection does not have a matching successful publication"
                )
            by_stage[request.stage] = (digest, request, sha256_bytes(result_bytes))
    if set(by_stage) != set(STAGES) or any(result is None for _, _, result in by_stage.values()):
        raise RejectionCleanupPending("complete four-stage publication is not yet proven")
    return {stage: (digest, request, result) for stage, (digest, request, result)
            in by_stage.items() if result is not None}


def _verify_rejected_record(
    ai_root: Path, case_id: str, proposal_sha256: str, request: ProjectionRequest,
) -> str:
    root = ai_root.absolute()
    validation_dir = root / "10-Validation"
    _require_safe_directory(validation_dir, create=False)
    data = _bounded_regular_file(
        validation_dir / f"{proposal_sha256}.validation.json", cap=MAX_RESULT_BYTES
    )
    validation_digest = sha256_bytes(data)
    validation = parse_validation_record(data)
    if (
        validation.result != "rejected"
        or validation.mutation_sha256 is not None
        or validation.proposal_sha256 != proposal_sha256
        or request.stage != "validation"
        or request.case_id != case_id
        or request.source_kind != "validation_record"
        or request.source_sha256 != validation_digest
        or request.created_at != validation.validated_at
    ):
        raise RejectionCleanupError("projection is not bound to rejected Validation")
    target, _candidate = proposal_fields(root, proposal_sha256, read=_bounded_regular_file)
    lines = [
        f"Result: **{validation.result}**",
        f"Target: {_inline_code(target)}",
    ]
    if validation.reason is not None:
        lines.extend(["", "## Reason", "", _code_fence(validation.reason, "text")])
    expected = _projection_markdown(
        case_id=case_id,
        stage="validation",
        source_kind="validation_record",
        source_sha256=validation_digest,
        created_at=validation.validated_at,
        title="Deterministic Validation",
        body="\n".join(lines),
        extra_frontmatter={
            "proposal_sha256": proposal_sha256,
            "mutation_sha256": validation.mutation_sha256,
            "target_path": target,
            "validation_result": validation.result,
        },
    )
    if request.content != expected:
        raise RejectionCleanupError(
            "original Validation projection differs from canonical rejected record"
        )
    return validation_digest


def prepare_rejection_cleanup(
    ai_root: Path, case_id: str, *, max_scan: int = MAX_SCAN,
) -> RejectionCleanupAttestation:
    """Build a bounded deletion *preflight* from private immutable evidence.

    This produces no mutation whatsoever. It does NOT establish that the
    remote bytes/strong ETag are unchanged, nor suppress queued republishing;
    a future Sync-side effect boundary must still verify both.
    """
    try:
        case = _require_sha256(case_id, label="case_id")
        root = ai_root.absolute()
        job_id, context_sha, recipe_sha, proposal, generation_sha = _scheduler_authority(
            root, case
        )
        originals = _published_originals(root, case, max_scan=max_scan)
        assignment = load_binding(root, case, read=_bounded_regular_file)
        if assignment is None:
            # Old jobs lack an immutable per-generation Selection identity.
            # Even a valid alternative Selection with matching Context sources
            # is not proof that it was the one used during submission.
            raise RejectionCleanupPending("original Planner Selection assignment missing")
        if (
            assignment.job_id != job_id
            or assignment.context_sha256 != context_sha
            or assignment.recipe_sha256 != recipe_sha
            or assignment.selection_sha256 != originals["input"][1].source_sha256
        ):
            raise RejectionCleanupError("original Planner submitted Selection identity mismatch")
        verify_original_producers(
            root, case, context_sha, proposal, generation_sha, originals,
            read=_bounded_regular_file,
        )
        validation = _verify_rejected_record(
            root, case, proposal, originals["validation"][1]
        )
        # A regeneration can advance the job while the private projection
        # tree is scanned. Re-read the authoritative generation before
        # producing the immutable proof; the later Sync executor must still
        # re-verify at its own effect boundary.
        confirmed = _scheduler_authority(root, case)
        if confirmed != (job_id, context_sha, recipe_sha, proposal, generation_sha):
            raise RejectionCleanupError("generation changed during preflight")
        targets = tuple(
            AttestedTarget(
                stage=stage,
                path=_target(case, stage),
                request_sha256=originals[stage][0],
                result_sha256=originals[stage][2],
                content_sha256=originals[stage][1].content_sha256,
            )
            for stage in STAGES
        )
        return parse_attestation(RejectionCleanupAttestation(
            case_id=case, job_id=job_id, proposal_sha256=proposal,
            validation_sha256=validation, targets=targets,
        ).to_json_bytes())
    except RejectionCleanupError:
        raise
    except (ArtifactLifecycleError, OSError, ValueError, TypeError) as exc:
        # A malformed immutable source is not a safe candidate for cleanup;
        # do not forward source text or private artifact paths to callers.
        raise RejectionCleanupError("rejection cleanup evidence is invalid") from exc


def verify_rejection_cleanup(
    ai_root: Path, attestation: RejectionCleanupAttestation,
    *, max_scan: int = MAX_SCAN,
) -> None:
    """A later Sync-only executor must re-run the entire proof, not trust the
    provided document. It must separately preflight remote bytes/ETag before
    any DELETE; this function grants no WebDAV or filesystem delete authority.
    """
    if not isinstance(attestation, RejectionCleanupAttestation):
        raise RejectionCleanupError("cleanup attestation type is invalid")
    parsed = parse_attestation(attestation.to_json_bytes())
    if prepare_rejection_cleanup(ai_root, parsed.case_id, max_scan=max_scan) != parsed:
        raise RejectionCleanupError("cleanup attestation is stale or conflicting")


def store_rejection_cleanup_attestation(
    ai_root: Path, case_id: str, *, max_scan: int = MAX_SCAN,
) -> tuple[RejectionCleanupAttestation, Path]:
    """Opt-in private immutable recording, never a remote cleanup trigger.

    This deliberately does not wire into workers, timers, or Sync. It uses the
    existing canonical-I/O lock and validator-owned private directory, and is
    safe to retry when all original evidence is unchanged.
    """
    with canonical_io_lock(ai_root):
        proof = prepare_rejection_cleanup(ai_root, case_id, max_scan=max_scan)
        validator_dir = ai_root.absolute() / REQUEST_STAGE / "validator"
        _require_safe_directory(validator_dir, create=False)
        path = validator_dir / f"{proof.case_id}{SUFFIX}"
        return proof, _store_immutable(path, proof.to_json_bytes())

def load_rejection_cleanup_attestation(
    ai_root: Path, case_id: str,
) -> RejectionCleanupAttestation | None:
    """Read a private proof; consumers MUST also call verify_rejection_cleanup.

    A request file is a bounded audit artifact, not an instruction to delete.
    """
    case = _require_sha256(case_id, label="case_id")
    directory = ai_root.absolute() / REQUEST_STAGE / "validator"
    _require_safe_directory(directory, create=False)
    path = directory / f"{case}{SUFFIX}"
    if not os.path.lexists(path):
        return None
    value = parse_attestation(_bounded_regular_file(path, cap=MAX_RESULT_BYTES))
    if value.case_id != case:
        raise RejectionCleanupError("attestation filename and case are mismatched")
    return value
