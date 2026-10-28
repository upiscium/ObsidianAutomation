"""B5 source-only, manual read/mock ports for webui-workflow-control/v1.

There is deliberately NO production upload/import adapter, HTTP registration,
filesystem, intake API import, scheduler, Review, Vault or provider dependency.
ReadPort never initializes state. TestOnlyMockPort accepts only exact BytesIO
objects and immutable, explicitly injected synthetic evidence. Its journal is an
immutable *simulation* of durable atomic bindings, NOT a durable store. Snapshot
handoff tests model restart/readback, not disk/crash durability acceptance.

Human/integrator gates: accepted streaming transport/deadlines, ZIP/member/JSON
limits, spool/private permissions, immutable selection policy, real atomic
request journal and exact-next-archive locking/readback. Until those gates pass,
stage_archive is pending_integration and all observed actions are disabled.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from io import BytesIO
import json
import math
import re
from threading import Lock
from uuid import uuid4


SCHEMA_VERSION = "webui-workflow-control/v1"
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class RequestError(ValueError):
    """Fixed parse error; malformed input is never echoed into an envelope."""


def _handle(value: object) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _time(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequestError("invalid_request")
        result[key] = value
    return result


def _nonfinite(_):
    raise RequestError("invalid_request")


def _object(value: object, keys: set[str]) -> dict:
    if type(value) is str:
        try:
            value = json.loads(value, object_pairs_hook=_unique, parse_constant=_nonfinite)
        except (ValueError, RecursionError):
            raise RequestError("invalid_request") from None
    if type(value) is not dict or set(value) != keys:
        raise RequestError("invalid_request")
    if value["schemaVersion"] != SCHEMA_VERSION:
        raise RequestError("unsupported_version")
    if not _handle(value["requestId"]) or not _handle(value["expectedRevision"]):
        raise RequestError("invalid_request")
    return value


@dataclass(frozen=True)
class StageArchiveRequest:
    request_id: str
    expected_revision: str

    def __post_init__(self):
        if not _handle(self.request_id) or not _handle(self.expected_revision):
            raise RequestError("invalid_request")

    @classmethod
    def parse(cls, value: object) -> StageArchiveRequest:
        data = _object(value, {"schemaVersion", "requestId", "expectedRevision"})
        return cls(data["requestId"], data["expectedRevision"])


@dataclass(frozen=True)
class InventoryImportRequest:
    request_id: str
    expected_revision: str
    archive_ref: str
    selection_ref: str

    def __post_init__(self):
        if not all(_handle(v) for v in (
            self.request_id, self.expected_revision, self.archive_ref, self.selection_ref
        )):
            raise RequestError("invalid_request")

    @classmethod
    def parse(cls, value: object) -> InventoryImportRequest:
        data = _object(value, {"schemaVersion", "requestId", "expectedRevision", "action", "input"})
        if data["action"] != "import_inventory":
            raise RequestError("unsupported_action")
        inp = data["input"]
        if type(inp) is not dict or set(inp) != {"archiveRef", "selectionRef"}:
            raise RequestError("invalid_request")
        return cls(data["requestId"], data["expectedRevision"], inp["archiveRef"], inp["selectionRef"])


def _error(code: str) -> dict:
    retry = "after_refresh"
    if code in {"invalid_request", "unsupported_version", "unsupported_action"}:
        retry = "never"
    elif code in {"admission_unknown", "request_not_found"}:
        retry = "lookup"
    return {"code": code, "retry": retry}


def _admission(request_id: str, *, staging: bool, decision: str,
               result: dict | None = None, code: str | None = None) -> dict:
    value = {"schemaVersion": SCHEMA_VERSION, "requestId": request_id,
             "decision": decision, "error": _error(code) if code else None}
    value["stagedArchive" if staging else "receipt"] = result
    if not staging:
        value["workflowId"] = "b5"
    return value


@dataclass(frozen=True, repr=False)
class TestOnlyInspection:
    """Exact synthetic content fixture, never a real ZIP inspector or selector.

    policy_ref stands for an immutable server-owned scope/parser/member policy.
    Ambiguous selection never issues handles: selection_resolved must be true.
    A review-required schema is not import permission: selection_accepted must
    independently be true. Unknown inventory status simulates malformed evidence.
    """

    content_sha256: str
    inspection: str = "inspected"
    selection_resolved: bool = False
    selection_accepted: bool = False
    inventory_status: str = "inventory_only"
    inventory_changed: bool = True
    quarantined_slots: int = 0
    unsupported_parts: int = 0
    absent_not_deleted: int = 0

    def __post_init__(self):
        if (type(self.content_sha256) is not str or not _SHA.fullmatch(self.content_sha256)
                or type(self.inspection) is not str
                or self.inspection not in {"inspected", "schema_review_required"}
                or type(self.selection_resolved) is not bool
                or type(self.selection_accepted) is not bool
                or (self.selection_accepted and not self.selection_resolved)
                or type(self.inventory_changed) is not bool
                or type(self.inventory_status) is not str
                or any(type(n) is not int or n < 0 for n in (
                    self.quarantined_slots, self.unsupported_parts, self.absent_not_deleted))):
            raise ValueError("invalid_test_fixture")


@dataclass(frozen=True, repr=False)
class TestOnlyInspector:
    fixtures: tuple[TestOnlyInspection, ...]

    def __post_init__(self):
        if (type(self.fixtures) is not tuple
                or any(type(f) is not TestOnlyInspection for f in self.fixtures)
                or len({f.content_sha256 for f in self.fixtures}) != len(self.fixtures)):
            raise ValueError("invalid_test_fixture")

    def inspect(self, digest: str) -> TestOnlyInspection | None:
        return next((f for f in self.fixtures if f.content_sha256 == digest), None)


def _inventory_status(evidence: TestOnlyInspection) -> str:
    """One evidence-to-outcome rule for both admission and snapshot readback."""
    status = evidence.inventory_status
    if status not in {"inventory_only", "inventory_review_required", "normalization_rejected"}:
        return "unknown"
    if status == "inventory_only" and (evidence.quarantined_slots or evidence.unsupported_parts
                                       or evidence.inspection == "schema_review_required"):
        return "inventory_review_required"
    return status


@dataclass(frozen=True)
class TestOnlyStreamLimits:
    """Mandatory test fixture bounds. No defaults or production policy selected.

    Only memory bytes are simulated; this is NOT a timeout/spool/ZIP policy.
    Exact BytesIO prevents arbitrary blocking readers and path/file ingestion.
    """

    max_bytes: int
    chunk_bytes: int

    def __post_init__(self):
        if (type(self.max_bytes) is not int or type(self.chunk_bytes) is not int
                or not 1 <= self.chunk_bytes <= self.max_bytes):
            raise ValueError("invalid_test_limits")


@dataclass(frozen=True, repr=False)
class _Staged:
    request: StageArchiveRequest
    generation: str
    policy_ref: str
    evidence: TestOnlyInspection
    archive_ref: str
    selection_ref: str

    def __post_init__(self):
        if (type(self.request) is not StageArchiveRequest
                or type(self.evidence) is not TestOnlyInspection
                or not self.evidence.selection_resolved
                or not all(_handle(v) for v in (
                    self.generation, self.policy_ref, self.archive_ref, self.selection_ref))):
            raise ValueError("invalid_test_staging_evidence")

    def dto(self) -> dict:
        return {"schemaVersion": SCHEMA_VERSION, "requestId": self.request.request_id,
                "archiveRef": self.archive_ref, "selectionRef": self.selection_ref,
                "inspection": self.evidence.inspection}


@dataclass(frozen=True, repr=False)
class _Imported:
    request: InventoryImportRequest
    generation: str
    run_id: str
    created_at: float
    status: str
    changed: bool | None
    evidence: TestOnlyInspection

    def __post_init__(self):
        if (type(self.request) is not InventoryImportRequest
                or type(self.evidence) is not TestOnlyInspection
                or not _handle(self.generation) or not _handle(self.run_id)
                or not _time(self.created_at)
                or self.status != _inventory_status(self.evidence)
                or (self.changed is not None and type(self.changed) is not bool)
                or (self.status not in {"inventory_only", "inventory_review_required"}
                    and self.changed is not None)):
            raise ValueError("invalid_test_import_evidence")

    def dto(self) -> dict:
        status = self.status
        success = status in {"inventory_only", "inventory_review_required"}
        failure = status == "normalization_rejected"
        effect = {"kind": "private_inventory", "state": "unknown", "changed": None}
        if success:
            effect.update(state="verified", changed=self.changed)
        elif failure:
            effect = {"kind": "none", "state": "none", "changed": None}
        return {"schemaVersion": SCHEMA_VERSION, "workflowId": "b5",
                "requestId": self.request.request_id, "runId": self.run_id,
                "action": "import_inventory", "state": "completed",
                "outcome": "success" if success else "failure" if failure else "unknown",
                "createdAt": self.created_at, "startedAt": self.created_at,
                "completedAt": self.created_at, "effect": effect,
                "error": None if success else {
                    "code": "input_rejected" if failure else "effect_unconfirmed",
                    "retry": "never" if failure else "lookup"}}


@dataclass(frozen=True, repr=False)
class TestOnlyPrivateJournal:
    """Attached immutable fake journal; never auto-load/bootstrap/reset a store.

    uncertain IDs model a binding whose admission cannot be established. An
    untrusted/lost journal (verified=False) allows lookup-only unknown results.
    Staging and execution IDs occupy separate namespaces.
    """

    revision: str
    generation: str
    policy_ref: str
    stages: tuple[_Staged, ...] = ()
    imports: tuple[_Imported, ...] = ()
    uncertain_staging: tuple[str, ...] = ()
    uncertain_imports: tuple[str, ...] = ()
    verified: bool = True

    def __post_init__(self):
        if (not all(_handle(v) for v in (self.revision, self.generation, self.policy_ref))
                or type(self.verified) is not bool):
            raise ValueError("invalid_test_journal")
        for records, record_type, uncertain in (
            (self.stages, _Staged, self.uncertain_staging),
            (self.imports, _Imported, self.uncertain_imports),
        ):
            if (type(records) is not tuple or type(uncertain) is not tuple
                    or any(type(r) is not record_type for r in records)
                    or any(not _handle(v) for v in uncertain)):
                raise ValueError("invalid_test_journal")
            ids = [r.request.request_id for r in records] + list(uncertain)
            if len(ids) != len(set(ids)):
                raise ValueError("invalid_test_journal")
        handles = [s.archive_ref for s in self.stages] + [s.selection_ref for s in self.stages]
        if len(handles) != len(set(handles)):
            raise ValueError("invalid_test_journal")
        completed_bindings = set()
        unresolved = False
        for record in self.imports:
            stage = next((s for s in self.stages if
                          (s.archive_ref, s.selection_ref) ==
                          (record.request.archive_ref, record.request.selection_ref)), None)
            if (stage is None or stage.generation != record.generation
                    or stage.evidence != record.evidence or not stage.evidence.selection_accepted
                    or unresolved):
                raise ValueError("invalid_test_journal")
            binding = (stage.evidence.content_sha256, stage.generation, stage.policy_ref)
            if record.status in {"inventory_only", "inventory_review_required"}:
                expected_change = False if binding in completed_bindings else stage.evidence.inventory_changed
                if record.changed is not expected_change:
                    raise ValueError("invalid_test_journal")
            completed_bindings.add(binding)
            unresolved = record.status == "unknown"


class ReadPort:
    """Pure noninitializing observation/lookup over an attached fake snapshot.

    No journal means pending integration, NOT an initialized empty inventory.
    The fake snapshot is explicitly labelled mock, never connected production.
    """

    def __init__(self, journal: TestOnlyPrivateJournal | None = None, *, observed_at: float):
        if journal is not None and type(journal) is not TestOnlyPrivateJournal:
            raise ValueError("test_journal_required")
        if not _time(observed_at):
            raise ValueError("invalid_observed_at")
        self._journal = journal
        self._observed_at = observed_at

    def observe(self) -> dict:
        j = self._journal
        known = j is not None and j.verified
        last = j.imports[-1].dto() if known and j.imports else None
        unresolved = known and (j.uncertain_imports or j.uncertain_staging
                                or any(r.dto()["outcome"] == "unknown" for r in j.imports))
        verified = next((r.dto() for r in reversed(j.imports)
                         if r.generation == j.generation and r.dto()["outcome"] != "unknown"),
                        None) if known else None
        reason = "pending_integration" if j is None else "mock_only" if known else "observation_unavailable"
        return {"schemaVersion": SCHEMA_VERSION, "id": "b5",
                "label": "ChatGPT Export intake (TEST-ONLY mock)" if j else "ChatGPT Export intake",
                "adapterMode": "mock" if j else "read_only",
                "revision": j.revision if j else "0" * 32,
                "observedAt": self._observed_at,
                "integration": "unavailable" if j and not known else "pending",
                "configuration": {"state": "configured" if known else "not_observed", "reason": None if known else reason},
                "schedule": {"state": "not_applicable", "intervalSeconds": None,
                             "nextDueAt": None, "reason": "manual_only"},
                "execution": {"state": "idle" if known and not unresolved else "unknown",
                              "requestId": None, "startedAt": None},
                "lastAttempt": {k: last[k] for k in ("requestId", "state", "outcome", "completedAt", "effect")} if last else None,
                "lastVerifiedOutcome": {"status": verified["outcome"],
                                        "completedAt": verified["completedAt"],
                                        "effect": verified["effect"]} if verified else None,
                "lastAttemptAt": last["createdAt"] if last else None,
                "availableActions": [],
                "actionReasons": {"stage_archive": "pending_integration",
                                  "import_inventory": reason}}

    def _lookup(self, request_id: str, *, staging: bool) -> dict:
        if not _handle(request_id):
            raise RequestError("invalid_request")
        j = self._journal
        if j is None:
            # No attached history cannot prove that this ID was never admitted.
            return _admission(request_id, staging=staging, decision="indeterminate", code="admission_unknown")
        uncertain = j.uncertain_staging if staging else j.uncertain_imports
        if not j.verified or request_id in uncertain:
            return _admission(request_id, staging=staging, decision="indeterminate", code="admission_unknown")
        records = j.stages if staging else j.imports
        record = next((r for r in records if r.request.request_id == request_id), None)
        return _admission(request_id, staging=staging,
                          decision="replayed" if record else "rejected",
                          result=record.dto() if record else None,
                          code=None if record else "request_not_found")

    def lookup(self, request_id: str) -> dict:
        return self._lookup(request_id, staging=False)

    def lookup_staging(self, request_id: str) -> dict:
        return self._lookup(request_id, staging=True)

    def inventory_detail(self, request_id: str) -> dict | None:
        """Fixed private-inventory distinction; no source names/digests/payloads."""
        result = self.lookup(request_id)
        if result["decision"] != "replayed":
            return None
        record = next(r for r in self._journal.imports if r.request.request_id == request_id)
        e = record.evidence
        return {"status": record.status if record.status in {
                    "inventory_only", "inventory_review_required", "normalization_rejected"} else "unknown",
                "quarantined_slots": e.quarantined_slots,
                "unsupported_parts": e.unsupported_parts,
                "absent_not_deleted": e.absent_not_deleted,
                "eligible_for_generation": False, "generation_jobs_queued": 0,
                "review_artifact_created": False, "vault_changed": False}


class TestOnlyMockPort:
    """Offline manual simulation, with one lock and immutable journal replacement.

    No user stream/file or runtime dependency is accepted. No availableAction
    becomes executable. authority_checked simulates the future owner's guard,
    not credentials, ingress or CSRF implementation. Every ambiguous admission
    is lookup-only: this simulation cannot retry/repair it under another ID.
    """

    def __init__(self, *, journal: TestOnlyPrivateJournal, inspector: TestOnlyInspector,
                 limits: TestOnlyStreamLimits, observed_at: float, authority_checked: bool = False):
        if (type(journal) is not TestOnlyPrivateJournal or type(inspector) is not TestOnlyInspector
                or type(limits) is not TestOnlyStreamLimits or not _time(observed_at)
                or type(authority_checked) is not bool):
            raise ValueError("invalid_test_dependency")
        self._journal, self._inspector, self._limits = journal, inspector, limits
        self._now, self._authority = observed_at, authority_checked
        self._lock = Lock()

    @property
    def journal(self) -> TestOnlyPrivateJournal:
        with self._lock:
            return self._journal

    def _reader(self) -> ReadPort:
        return ReadPort(self._journal, observed_at=self._now)

    def observe(self) -> dict:
        with self._lock:
            return self._reader().observe()

    def lookup(self, request_id: str) -> dict:
        with self._lock:
            return self._reader().lookup(request_id)

    def lookup_staging(self, request_id: str) -> dict:
        with self._lock:
            return self._reader().lookup_staging(request_id)

    def _guard(self, request_id: str, staging: bool) -> dict | None:
        if not self._authority:
            return _admission(request_id, staging=staging, decision="rejected", code="authority_rejected")
        j = self._journal
        if not j.verified or request_id in (j.uncertain_staging if staging else j.uncertain_imports):
            return _admission(request_id, staging=staging, decision="indeterminate", code="admission_unknown")
        return None

    def _new_intent_error(self, revision: str) -> str | None:
        j = self._journal
        if revision != j.revision:
            return "stale_revision"
        if j.uncertain_staging or j.uncertain_imports or any(r.dto()["outcome"] == "unknown" for r in j.imports):
            return "reconciliation_required"
        return None

    def stage_archive(self, request: StageArchiveRequest, stream: BytesIO) -> dict:
        if type(request) is not StageArchiveRequest:
            raise RequestError("invalid_request")
        with self._lock:
            guard = self._guard(request.request_id, True)
            if guard:
                return guard
            j = self._journal
            old = next((r for r in j.stages if r.request.request_id == request.request_id), None)
            code = None if old else self._new_intent_error(request.expected_revision)
            if code:
                return _admission(request.request_id, staging=True, decision="rejected", code=code)
            # Exact in-memory type only: never invoke an arbitrary reader/file.
            if type(stream) is not BytesIO:
                return _admission(request.request_id, staging=True, decision="rejected", code="invalid_request")
            digest, total = sha256(), 0
            try:
                while True:
                    block = BytesIO.read(stream, min(self._limits.chunk_bytes, self._limits.max_bytes - total + 1))
                    if not block:
                        break
                    total += len(block)
                    if total > self._limits.max_bytes:
                        return _admission(request.request_id, staging=True, decision="rejected", code="invalid_request")
                    digest.update(block)
            except ValueError:
                return _admission(request.request_id, staging=True, decision="rejected", code="invalid_request")
            identity = digest.hexdigest()
            if old:
                # Original generation/policy/receipt survives configuration rotation.
                same = identity == old.evidence.content_sha256
                return _admission(request.request_id, staging=True,
                                  decision="replayed" if same else "rejected",
                                  result=old.dto() if same else None,
                                  code=None if same else "request_conflict")
            evidence = self._inspector.inspect(identity)
            if evidence is None or not evidence.selection_resolved:
                return _admission(request.request_id, staging=True, decision="rejected", code="input_binding_invalid")
            record = _Staged(request, j.generation, j.policy_ref, evidence, uuid4().hex, uuid4().hex)
            self._journal = replace(j, revision=uuid4().hex, stages=j.stages + (record,))
            return _admission(request.request_id, staging=True, decision="accepted", result=record.dto())

    def import_inventory(self, request: InventoryImportRequest) -> dict:
        if type(request) is not InventoryImportRequest:
            raise RequestError("invalid_request")
        with self._lock:
            guard = self._guard(request.request_id, False)
            if guard:
                return guard
            j = self._journal
            old = next((r for r in j.imports if r.request.request_id == request.request_id), None)
            if old:
                same = (request.archive_ref, request.selection_ref) == (old.request.archive_ref, old.request.selection_ref)
                return _admission(request.request_id, staging=False,
                                  decision="replayed" if same else "rejected",
                                  result=old.dto() if same else None,
                                  code=None if same else "request_conflict")
            code = self._new_intent_error(request.expected_revision)
            selected = next((r for r in j.stages if (r.archive_ref, r.selection_ref) ==
                             (request.archive_ref, request.selection_ref)), None)
            if not code and (selected is None or not selected.evidence.selection_accepted
                             or (selected.generation, selected.policy_ref) != (j.generation, j.policy_ref)):
                code = "input_binding_invalid"
            # Simulate the locked eligible-next constraint; never silently scan
            # and import another older ready archive as inventory_ready would.
            def binding(stage):
                return (stage.evidence.content_sha256, stage.generation, stage.policy_ref)

            completed = {binding(s) for s in j.stages for r in j.imports
                         if (s.archive_ref, s.selection_ref) == (r.request.archive_ref, r.request.selection_ref)}
            eligible = next((s for s in j.stages if s.generation == j.generation
                             and s.policy_ref == j.policy_ref and binding(s) not in completed), None)
            already = selected is not None and binding(selected) in completed
            if not code and not already and (eligible is None or binding(eligible) != binding(selected)):
                code = "input_binding_invalid"
            if code:
                return _admission(request.request_id, staging=False, decision="rejected", code=code)
            e = selected.evidence
            # Unknown source text is never forwarded or promoted to success.
            status = _inventory_status(e)
            changed = (False if already else e.inventory_changed) if status in {
                "inventory_only", "inventory_review_required"} else None
            record = _Imported(request, j.generation, uuid4().hex, self._now, status,
                               changed, e)
            self._journal = replace(j, revision=uuid4().hex, imports=j.imports + (record,))
            return _admission(request.request_id, staging=False, decision="accepted", result=record.dto())
