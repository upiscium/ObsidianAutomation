"""Isolated B3 contract rehearsal. Nothing here attaches to a runtime or router.

The injected SyntheticLedger is an in-memory *port fixture*, not a durable store.
Keep the same port across adapter restarts. A lost/unknown port must stay unknown;
creating a fresh fixture is only valid for a new synthetic test universe. Process
durability and production Writer acceptance are deliberately not implemented.

All observations are visibly mock and advertise no executable actions. Explicit
manual_simulation permits sync_project to simulate admission, never to call an
existing watcher, queue, Writer, provider, filesystem, Vault or promotion port.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
import re
from threading import RLock
from typing import Literal, TypedDict
from uuid import uuid4


VERSION = "webui-workflow-control/v1"
HEX = re.compile(r"[0-9a-f]{32}\Z")


def _hex(value: object) -> bool:
    return type(value) is str and HEX.fullmatch(value) is not None


def _time(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


class InvalidRequest(ValueError):
    """Fixed parsing error; malformed correlation is not echoed in an Admission."""

    def __init__(self, code: str = "invalid_request"):
        self.code = code
        super().__init__(code)


def _object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRequest()
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise InvalidRequest()


def _envelope(value: object, action: str) -> dict:
    if type(value) is not dict or set(value) != {
        "schemaVersion", "requestId", "expectedRevision", "action", "input"
    }:
        raise InvalidRequest()
    if value["schemaVersion"] != VERSION:
        raise InvalidRequest("unsupported_version")
    if not _hex(value["requestId"]) or not _hex(value["expectedRevision"]):
        raise InvalidRequest()
    if value["action"] != action:
        raise InvalidRequest("unsupported_action")
    if type(value["input"]) is not dict:
        raise InvalidRequest()
    return value


@dataclass(frozen=True)
class ProjectSyncRequest:
    requestId: str
    expectedRevision: str
    bindingRef: str

    def __post_init__(self) -> None:
        if not all(_hex(v) for v in (self.requestId, self.expectedRevision, self.bindingRef)):
            raise InvalidRequest()

    @classmethod
    def from_mapping(cls, value: object) -> ProjectSyncRequest:
        data = _envelope(value, "sync_project")
        if set(data["input"]) != {"bindingRef"}:
            raise InvalidRequest()
        return cls(data["requestId"], data["expectedRevision"], data["input"]["bindingRef"])

    @classmethod
    def from_json(cls, raw: str) -> ProjectSyncRequest:
        try:
            value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
        except (ValueError, TypeError, RecursionError) as exc:
            if isinstance(exc, InvalidRequest):
                raise
            raise InvalidRequest() from None
        return cls.from_mapping(value)


@dataclass(frozen=True)
class Binding:
    """Server-issued immutable synthetic scope; never constructed from input paths."""

    bindingRef: str
    generation: str
    project: str = "synthetic-project"
    destination: str = "synthetic-destination"

    def __post_init__(self) -> None:
        if not _hex(self.bindingRef) or not _hex(self.generation):
            raise ValueError("invalid synthetic binding")
        if not self.project or not self.destination:
            raise ValueError("invalid synthetic scope")


@dataclass(frozen=True)
class Effect:
    kind: Literal["none", "project_vault"] = "project_vault"
    state: Literal["none", "verified", "unknown"] = "unknown"
    changed: bool | None = None


@dataclass(frozen=True)
class Error:
    code: str
    retry: Literal["never", "lookup", "after_refresh"]


@dataclass(frozen=True)
class Receipt:
    requestId: str
    runId: str
    createdAt: float
    state: Literal["queued", "running", "completed"] = "queued"
    outcome: Literal["success", "failure", "unknown"] | None = None
    startedAt: float | None = None
    completedAt: float | None = None
    effect: Effect = Effect()
    error: Error | None = None
    schemaVersion: str = VERSION
    workflowId: str = "b3"
    action: str = "sync_project"


@dataclass(frozen=True)
class Admission:
    requestId: str
    decision: Literal["accepted", "replayed", "rejected", "indeterminate"]
    receipt: Receipt | None = None
    error: Error | None = None
    schemaVersion: str = VERSION
    workflowId: str = "b3"

    def to_dict(self) -> dict:
        # Defensive copy: clients cannot mutate server ledger through returned DTOs.
        return asdict(self)


def _error(code: str) -> Error:
    retry = "lookup" if code in {"admission_unknown", "request_not_found"} else (
        "never" if code in {"invalid_request", "unsupported_version", "unsupported_action"}
        else "after_refresh"
    )
    return Error(code, retry)


def _deny(request_id: str, code: str) -> Admission:
    return Admission(request_id, "rejected", error=_error(code))


def _unknown(request_id: str, receipt: Receipt | None = None) -> Admission:
    return Admission(request_id, "indeterminate", receipt, _error("admission_unknown"))


@dataclass(frozen=True)
class WriterEvidence:
    """Private fake exact-readback evidence, correlated to one required subeffect."""

    requestId: str
    runId: str
    binding: Binding
    subeffect: str
    outcome: str
    expected: bytes
    observed: bytes


@dataclass(frozen=True)
class SyntheticDocument:
    binding: Binding
    owner_text: str
    managed_text: str
    etag: int


class SyntheticInterruption(BaseException):
    """Test-only worker loss after persisted admission; not a retry signal."""


class SyntheticWriter:
    """Two in-memory CAS destinations; not a wrapper for the real Writer.

    Modes exercise partial/readback/binding failures without accepting callbacks
    or runtime dependencies. A conflict preserves the simulated owner's edit.
    """

    MODES = {"verified", "queue_only", "partial", "cas_conflict", "bad_readback",
             "stale_evidence", "unknown_status", "interrupt"}

    def __init__(self, binding: Binding, *, mode: str = "verified"):
        if mode not in self.MODES:
            raise ValueError("invalid fixture mode")
        self.mode = mode
        self._documents = {
            name: SyntheticDocument(binding, "owner text", "old", 1)
            for name in ("status", "overview")
        }
        self.calls = 0
        self.cas_conflict = False

    @property
    def documents(self) -> dict[str, SyntheticDocument]:
        return dict(self._documents)

    def _apply(self, request: ProjectSyncRequest, run_id: str,
               binding: Binding) -> tuple[WriterEvidence, ...]:
        self.calls += 1
        if self.mode == "interrupt":
            raise SyntheticInterruption()
        if self.mode == "queue_only":
            return ()
        evidence = []
        for name, before in tuple(self._documents.items()):
            if before.binding != binding:
                return ()
            desired = replace(before, managed_text="desired", etag=before.etag + 1)
            if self.mode == "cas_conflict":
                # Owner edits after our read: stale strong-ETag precondition fails.
                current = replace(before, owner_text="owner edited text", etag=before.etag + 1)
                self._documents[name] = current
                self.cas_conflict = current.etag != before.etag
                return ()  # no overwrite or silent conflict retry
            changed = before.managed_text != "desired"
            if changed:
                self._documents[name] = desired
            current = self._documents[name]
            expected = (before.owner_text + "\n" + "desired").encode()
            observed = (current.owner_text + "\n" + current.managed_text).encode()
            outcome = "updated" if changed else "already_desired"
            if self.mode == "unknown_status":
                outcome = "private untrusted backend text"
            evidence.append(WriterEvidence(
                request.requestId if self.mode != "stale_evidence" else "0" * 32,
                run_id, binding, name, outcome, expected,
                b"diverged" if self.mode == "bad_readback" else observed,
            ))
            if self.mode == "partial":
                break
        return tuple(evidence)


@dataclass(frozen=True)
class _Entry:
    binding: Binding
    accepted_revision: str
    receipt: Receipt
    evidence: tuple[WriterEvidence, ...] = ()


class SyntheticLedger:
    """Injected synthetic admission owner. Lock spans binding/revision/records.

    Default is UNKNOWN, not an empty authoritative store. fresh_fixture() is an
    explicit fixture bootstrap, never a recovery mechanism or a GET side effect.
    No TTL, eviction, auto-reset or replay of interrupted work is implemented.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._known = False
        self._binding: Binding | None = None
        self._writer: SyntheticWriter | None = None
        self._records: dict[str, _Entry] = {}
        self._revision = uuid4().hex
        self._watcher = "not_observed"
        self._mirror = "not_observed"

    @classmethod
    def fresh_fixture(cls, binding: Binding | None,
                      writer: SyntheticWriter | None = None) -> SyntheticLedger:
        if writer is not None and type(writer) is not SyntheticWriter:
            raise ValueError("runtime ports forbidden in mock")
        ledger = cls()
        ledger._binding, ledger._writer, ledger._known = binding, writer, True
        return ledger

    def _usable(self) -> bool:
        # A corrupt port is not an empty port or permission to retry an old ID.
        return (self._known and _hex(self._revision) and type(self._records) is dict
                and (self._binding is None or type(self._binding) is Binding)
                and (self._writer is None or type(self._writer) is SyntheticWriter)
                and type(self._watcher) is str
                and self._watcher in {"not_observed", "collected", "unavailable"}
                and type(self._mirror) is str
                and self._mirror in {"not_observed", "current", "stale", "unavailable"}
                and all(self._valid_entry(key, entry) for key, entry in self._records.items()))

    @staticmethod
    def _valid_entry(key: str, entry: _Entry) -> bool:
        if type(entry) is not _Entry or type(entry.receipt) is not Receipt \
                or type(entry.binding) is not Binding or not _hex(entry.accepted_revision):
            return False
        r = entry.receipt
        if not (_hex(key) and key == r.requestId and _hex(r.runId)
                and r.schemaVersion == VERSION and r.workflowId == "b3"
                and r.action == "sync_project" and _time(r.createdAt)
                and type(r.state) is str
                and type(r.effect) is Effect and r.effect.kind == "project_vault"
                and (r.startedAt is None or (_time(r.startedAt) and r.startedAt >= r.createdAt))):
            return False
        if r.state in {"queued", "running"}:
            return (r.outcome is None and r.completedAt is None and r.error is None
                    and r.effect == Effect() and (r.state != "queued" or r.startedAt is None))
        if r.state != "completed" or not _time(r.completedAt) \
                or r.completedAt < (r.startedAt if r.startedAt is not None else r.createdAt):
            return False
        if r.outcome == "success":
            return (r.error is None and r.effect.state == "verified"
                    and type(r.effect.changed) is bool
                    and B3FakeAdapter._verified(entry.evidence, r, entry.binding)
                    and r.effect.changed == any(e.outcome != "already_desired" for e in entry.evidence))
        # This fixture never manufactures a known-no-effect failure. Unresolved
        # collection/queue/Writer results stay unknown and retain their intent.
        return (r.outcome == "unknown" and r.effect == Effect()
                and r.error == Error("effect_unconfirmed", "lookup"))

    def lose_history(self) -> None:
        """Explicit fault injection: uncertainty is sticky, not auto-bootstrap."""
        with self._lock:
            self._known = False
            self._revision = uuid4().hex

    def set_observations(self, *, watcher: str, mirror: str) -> None:
        """Independent synthetic observations; neither is Writer evidence."""
        if watcher not in {"not_observed", "collected", "unavailable"} or mirror not in {
            "not_observed", "current", "stale", "unavailable"
        }:
            raise ValueError("invalid observation")
        with self._lock:
            self._watcher, self._mirror = watcher, mirror

    def rotate_binding(self, binding: Binding, writer: SyntheticWriter | None) -> None:
        if writer is not None and type(writer) is not SyntheticWriter:
            raise ValueError("runtime ports forbidden in mock")
        with self._lock:
            if not self._usable() or any(
                e.receipt.state != "completed" or e.receipt.effect.state == "unknown"
                for e in self._records.values()
            ):
                raise ValueError("reconciliation_required")
            if self._binding is not None and binding.bindingRef == self._binding.bindingRef:
                raise ValueError("immutable bindingRef must not be reused")
            self._binding, self._writer = binding, writer
            self._revision = uuid4().hex

    def mark_interrupted(self, request_id: str, *, at: float) -> None:
        """Explicit fixture recovery mutation. Never called by constructor/GET."""
        if not _time(at):
            raise ValueError("invalid time")
        with self._lock:
            if not self._usable():
                raise ValueError("admission_unknown")
            entry = self._records[request_id]
            if entry.receipt.state != "completed":
                completed_at = max(at, entry.receipt.createdAt)
                if entry.receipt.startedAt is not None:
                    completed_at = max(completed_at, entry.receipt.startedAt)
                receipt = replace(entry.receipt, state="completed", outcome="unknown",
                                  completedAt=completed_at,
                                  error=Error("effect_unconfirmed", "lookup"))
                self._records[request_id] = replace(entry, receipt=receipt)
                self._revision = uuid4().hex


class Workflow(TypedDict):
    schemaVersion: str
    id: str
    label: str
    adapterMode: str
    revision: str
    observedAt: float
    integration: str
    configuration: dict
    schedule: dict
    execution: dict
    lastAttempt: dict | None
    lastVerifiedOutcome: dict | None
    lastAttemptAt: float | None
    availableActions: list[str]
    actionReasons: dict[str, str]


class B3FakeAdapter:
    """Mock WorkflowReadPort + opt-in manual ProjectSyncPort rehearsal.

    manual_simulation is server/test-owned, not an HTTP parameter or authority
    token. This class cannot advertise or implement connected production actions.
    """

    def __init__(self, ledger: SyntheticLedger | None, *, manual_simulation: bool = False,
                 read_only: bool = False, observed_at: float = 1793145600.0):
        if not _time(observed_at):
            raise ValueError("invalid time")
        self._ledger = ledger
        self._manual = manual_simulation
        self._read_only = read_only
        self._at = observed_at
        self._absent_revision = uuid4().hex

    def lookup(self, request_id: str) -> Admission:
        if not _hex(request_id):
            raise InvalidRequest()
        ledger = self._ledger
        if ledger is None:
            return _unknown(request_id)
        with ledger._lock:
            if not ledger._usable():
                return _unknown(request_id)
            entry = ledger._records.get(request_id)
            return (Admission(request_id, "replayed", entry.receipt) if entry
                    else _deny(request_id, "request_not_found"))

    def sync_project(self, request: ProjectSyncRequest) -> Admission:
        if type(request) is not ProjectSyncRequest:
            raise InvalidRequest()
        # Revalidate even typed inputs before ledger access.
        request.__post_init__()
        if self._read_only:
            return _deny(request.requestId, "read_only")
        if not self._manual:
            return _deny(request.requestId, "mock_only")
        ledger = self._ledger
        if ledger is None:
            return _unknown(request.requestId)
        with ledger._lock:
            if not ledger._usable():
                return _unknown(request.requestId)
            old = ledger._records.get(request.requestId)
            if old is not None:
                if old.binding.bindingRef != request.bindingRef:
                    return _deny(request.requestId, "request_conflict")
                return Admission(request.requestId, "replayed", old.receipt)
            if request.expectedRevision != ledger._revision:
                return _deny(request.requestId, "stale_revision")
            if ledger._binding is None:
                return _deny(request.requestId, "configuration_missing")
            if request.bindingRef != ledger._binding.bindingRef:
                return _deny(request.requestId, "input_binding_invalid")
            if ledger._writer is None:
                return _deny(request.requestId, "not_attached")
            for entry in ledger._records.values():
                if entry.receipt.state != "completed":
                    return _deny(request.requestId, "busy")
                if entry.receipt.effect.state == "unknown":
                    return _deny(request.requestId, "reconciliation_required")
            receipt = Receipt(request.requestId, uuid4().hex, self._at)
            binding, writer = ledger._binding, ledger._writer
            # Admission is recorded before any synthetic Writer effect.
            entry = _Entry(binding, request.expectedRevision, receipt)
            ledger._records[request.requestId] = entry
            ledger._revision = uuid4().hex
            receipt = replace(receipt, state="running", startedAt=self._at)
            ledger._records[request.requestId] = replace(entry, receipt=receipt)
        try:
            evidence = writer._apply(request, receipt.runId, binding)
        except Exception:
            # Unknown, never infer no-effect from an exception or echo raw text.
            evidence = ()
        with ledger._lock:
            if not ledger._usable():
                return _unknown(request.requestId, receipt)
            entry = ledger._records[request.requestId]
            if entry.receipt.state == "completed":
                return Admission(request.requestId, "accepted", entry.receipt)
            valid = self._verified(evidence, receipt, binding)
            receipt = replace(
                receipt, state="completed", completedAt=self._at,
                outcome="success" if valid else "unknown",
                effect=Effect(state="verified", changed=any(
                    item.outcome != "already_desired" for item in evidence
                )) if valid else Effect(),
                error=None if valid else Error("effect_unconfirmed", "lookup"),
            )
            ledger._records[request.requestId] = replace(entry, receipt=receipt, evidence=evidence)
            ledger._revision = uuid4().hex
            return Admission(request.requestId, "accepted", receipt)

    @staticmethod
    def _verified(evidence: tuple[WriterEvidence, ...], receipt: Receipt, binding: Binding) -> bool:
        return (type(evidence) is tuple and len(evidence) == 2
                and all(type(e) is WriterEvidence and type(e.subeffect) is str
                        and type(e.outcome) is str for e in evidence)
                and {e.subeffect for e in evidence} == {"status", "overview"}
                and all(e.requestId == receipt.requestId and e.runId == receipt.runId
                        and e.binding == binding and type(e.expected) is bytes
                        and type(e.observed) is bytes and e.expected == e.observed
                        and e.outcome in {"already_desired", "created", "updated", "recovered"}
                        for e in evidence))

    def set_schedule(self, value: object) -> dict:
        data = _envelope(value, "set_schedule")
        inputs = data["input"]
        if set(inputs) != {"enabled", "intervalSeconds"} or type(inputs["enabled"]) is not bool \
                or type(inputs["intervalSeconds"]) is not int:
            raise InvalidRequest()
        # No policy bounds/engine chosen, no settings/run/effect persisted.
        return {"schemaVersion": VERSION, "workflowId": "b3", "requestId": data["requestId"],
                "decision": "rejected", "schedule": None, "revision": None,
                "error": asdict(_error("scheduler_decision_required"))}

    def observe(self) -> Workflow:
        ledger = self._ledger
        if ledger is None:
            return self._workflow(None)
        with ledger._lock:
            return self._workflow(ledger)

    def _workflow(self, ledger: SyntheticLedger | None) -> Workflow:
        known = ledger is not None and ledger._usable()
        binding = ledger._binding if known else None
        records = list(ledger._records.values()) if known else []
        newest = records[-1].receipt if records else None
        active = next((e.receipt for e in records if e.receipt.state != "completed"), None)
        unresolved = any(e.receipt.effect.state == "unknown" for e in records)
        verified = next((e.receipt for e in reversed(records)
                         if e.binding == binding and e.receipt.state == "completed"
                         and e.receipt.effect.state == "verified"
                         and e.receipt.outcome in {"success", "failure"}), None)
        reason = "read_only" if self._read_only else "mock_only"
        if not known:
            reason = "not_attached" if ledger is None else "observation_unavailable"
        schedule_reason = "scheduler_decision_required" if known else reason
        return {
            "schemaVersion": VERSION, "id": "b3", "label": "Project data into Vault (mock)",
            "adapterMode": "mock", "revision": ledger._revision if ledger else self._absent_revision,
            "observedAt": self._at,
            "integration": "pending" if known else ("unavailable" if ledger else "not_attached"),
            "configuration": {"state": "configured" if binding else ("missing" if known else "not_observed"),
                              "reason": None if binding else ("configuration_missing" if known else reason)},
            "schedule": {"state": "unknown", "intervalSeconds": None, "nextDueAt": None,
                         "reason": schedule_reason},
            "execution": {"state": active.state if active else (
                "idle" if known and not unresolved else "unknown"),
                "requestId": active.requestId if active else None,
                "startedAt": active.startedAt if active else None},
            "lastAttempt": {key: asdict(newest)[key] for key in (
                "requestId", "state", "outcome", "completedAt", "effect")} if newest else None,
            "lastVerifiedOutcome": {"status": verified.outcome, "completedAt": verified.completedAt,
                                    "effect": asdict(verified.effect)} if verified else None,
            "lastAttemptAt": newest.createdAt if newest else None,
            "availableActions": [], "actionReasons": {"sync_project": reason,
                                                        "set_schedule": schedule_reason},
        }

    def detail(self) -> dict:
        """Separate sanitized observation, not additional Workflow DTO fields."""
        ledger = self._ledger
        if ledger is None:
            return {"watcher": "not_observed", "mirror": "not_observed",
                    "writer": "not_attached", "cas": "not_observed"}
        with ledger._lock:
            if not ledger._usable():
                return {"watcher": "unavailable", "mirror": "unavailable",
                        "writer": "unavailable", "cas": "not_observed"}
            writer = ledger._writer
            return {"watcher": ledger._watcher, "mirror": ledger._mirror,
                    "writer": "synthetic_attached" if writer else "not_attached",
                    "cas": "conflict" if writer and writer.cas_conflict else "not_observed"}
