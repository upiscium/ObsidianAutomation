"""B1 source-only read port and synthetic manual-admission harness.

NOT a connected adapter, route, scheduler, provider bridge, or storage migration.
Only the final, built-in FakeReviewProvider is accepted; no runtime imports, paths,
recipe text, credentials or callable model ports exist here. All observations are
nonactionable. SyntheticAccess is a test gate, NOT ingress/CSRF authorization.

The ledger port deliberately has no initializer: its owner must supply already
provisioned durable state. Its transaction must serialize readers/writers and
return only after durable commit, including for unchanged state. Exceptions may
mean commit uncertainty. Missing/corrupt state must raise, NEVER recreate it.
The SQLite implementation lives only in the synthetic tests, not production.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
import re
from typing import Callable, Literal, Protocol, TypeVar, TypedDict, final
from uuid import uuid4


VERSION = "webui-workflow-control/v1"
HEX = re.compile(r"[0-9a-f]{32}\Z")
T = TypeVar("T")


class GenerateInput(TypedDict):
    contextRef: str
    recipeRef: str


class GenerateRequest(TypedDict):
    schemaVersion: Literal["webui-workflow-control/v1"]
    requestId: str
    expectedRevision: str
    action: Literal["generate"]
    input: GenerateInput


class Error(TypedDict):
    code: str
    retry: str


class Effect(TypedDict):
    kind: str
    state: str
    changed: bool | None


class Receipt(TypedDict):
    schemaVersion: Literal["webui-workflow-control/v1"]
    workflowId: Literal["b1"]
    requestId: str
    runId: str
    action: Literal["generate"]
    state: str
    outcome: str | None
    createdAt: float
    startedAt: float | None
    completedAt: float | None
    effect: Effect
    error: Error | None


class Admission(TypedDict):
    schemaVersion: Literal["webui-workflow-control/v1"]
    workflowId: Literal["b1"]
    requestId: str
    decision: str
    receipt: Receipt | None
    error: Error | None


class Workflow(TypedDict):
    schemaVersion: Literal["webui-workflow-control/v1"]
    id: Literal["b1"]
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


class InvalidRequest(ValueError):
    """Fixed code only; never include untrusted input in an exception."""


def _hex(value: object) -> bool:
    return type(value) is str and HEX.fullmatch(value) is not None


def _time(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRequest("invalid_request")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise InvalidRequest("invalid_request")


def parse_generate(value: object) -> GenerateRequest:
    """Strict exact-key JSON or dict boundary; malformed IDs are never echoed."""
    if type(value) in (str, bytes):
        if len(value) > 4096:
            raise InvalidRequest("invalid_request")
        try:
            value = json.loads(value, object_pairs_hook=_unique, parse_constant=_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise InvalidRequest("invalid_request") from None
    if type(value) is not dict or set(value) != {
        "schemaVersion", "requestId", "expectedRevision", "action", "input"
    }:
        raise InvalidRequest("invalid_request")
    if value["schemaVersion"] != VERSION:
        raise InvalidRequest("unsupported_version")
    if not _hex(value["requestId"]) or not _hex(value["expectedRevision"]):
        raise InvalidRequest("invalid_request")
    if value["action"] != "generate":
        raise InvalidRequest("unsupported_action")
    supplied = value["input"]
    if type(supplied) is not dict or set(supplied) != {"contextRef", "recipeRef"}:
        raise InvalidRequest("invalid_request")
    if not all(_hex(supplied[key]) for key in supplied):
        raise InvalidRequest("invalid_request")
    return {**value, "input": dict(supplied)}


def _error(code: str) -> Error:
    retry = ("lookup" if code in {"request_not_found", "admission_unknown"}
             else "never" if code in {"invalid_request", "unsupported_version", "unsupported_action"}
             else "after_refresh")
    return {"code": code, "retry": retry}


def _effect(state: str = "unknown") -> Effect:
    return {"kind": "review_artifact", "state": state, "changed": None}


def _admission(request_id: str, decision: str, receipt: Receipt | None = None,
               code: str | None = None) -> Admission:
    # Copy on output: a browser/test cannot mutate a ledger via a returned DTO.
    return {"schemaVersion": VERSION, "workflowId": "b1", "requestId": request_id,
            "decision": decision, "receipt": _copy(receipt),
            "error": _error(code) if code else None}


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


@dataclass(frozen=True)
class PreparedBinding:
    """Opaque, immutable SYNTHETIC handles; never resolves a path or real context."""
    context_ref: str
    recipe_ref: str
    generation: str
    provider_present: bool = True


@dataclass(frozen=True)
class Attempt:
    context_ref: str
    recipe_ref: str
    generation: str
    accepted_revision: str
    receipt: Receipt


@dataclass(frozen=True)
class LedgerState:
    revision: str
    binding: PreparedBinding
    attempts: tuple[Attempt, ...] = ()


class DurableLedger(Protocol):
    def read(self) -> LedgerState:
        """Noninitializing, coherent bounded read; missing state must raise."""
        ...

    def transact(self, operation: Callable[[LedgerState], tuple[LedgerState, T]]) -> T:
        """Atomic serialized state+receipt commit. Never retry operation internally."""
        ...


def _validate(state: LedgerState) -> LedgerState:
    """Validate attached typed state, not arbitrary backend dictionaries."""
    if type(state) is not LedgerState or not _hex(state.revision):
        raise ValueError("invalid_ledger")
    b = state.binding
    if (type(b) is not PreparedBinding or not all(_hex(x) for x in (
            b.context_ref, b.recipe_ref, b.generation)) or type(b.provider_present) is not bool
            or type(state.attempts) is not tuple):
        raise ValueError("invalid_ledger")
    seen: set[str] = set()
    runs: set[str] = set()
    active = 0
    for a in state.attempts:
        if type(a) is not Attempt or not all(_hex(x) for x in (
                a.context_ref, a.recipe_ref, a.generation, a.accepted_revision)):
            raise ValueError("invalid_ledger")
        r = a.receipt
        if type(r) is not dict or set(r) != set(Receipt.__annotations__):
            raise ValueError("invalid_ledger")
        if (r["schemaVersion"] != VERSION or r["workflowId"] != "b1"
                or r["action"] != "generate" or not _hex(r["requestId"])
                or not _hex(r["runId"]) or r["requestId"] in seen or r["runId"] in runs
                or not _time(r["createdAt"])):
            raise ValueError("invalid_ledger")
        seen.add(r["requestId"])
        runs.add(r["runId"])
        if r["startedAt"] is not None and (
                not _time(r["startedAt"]) or r["startedAt"] < r["createdAt"]):
            raise ValueError("invalid_ledger")
        if r["state"] in ("queued", "running"):
            active += 1
            if (r["outcome"] is not None or r["completedAt"] is not None
                    or r["error"] is not None or r["effect"] != _effect()
                    or r["state"] == "queued" and r["startedAt"] is not None):
                raise ValueError("invalid_ledger")
        elif r["state"] == "completed":
            if (not _time(r["completedAt"])
                    or r["completedAt"] < (r["startedAt"] or r["createdAt"])):
                raise ValueError("invalid_ledger")
            if r["outcome"] == "success":
                if r["effect"] != _effect("verified") or r["error"] is not None:
                    raise ValueError("invalid_ledger")
            elif r["outcome"] in ("unknown", "failure"):
                codes = ({"effect_unconfirmed"} if r["outcome"] == "unknown" else
                         {"execution_failed", "input_rejected", "verification_failed"})
                if (r["effect"] != _effect() or type(r["error"]) is not dict
                        or set(r["error"]) != {"code", "retry"}
                        or r["error"]["code"] not in codes or r["error"]["retry"] != "after_refresh"):
                    raise ValueError("invalid_ledger")
            else:
                raise ValueError("invalid_ledger")
        else:
            raise ValueError("invalid_ledger")
    if active > 1:
        raise ValueError("invalid_ledger")
    return state


def _find(state: LedgerState, request_id: str) -> Attempt | None:
    return next((a for a in state.attempts if a.receipt["requestId"] == request_id), None)


def _blocked(state: LedgerState) -> str | None:
    if any(a.receipt["state"] != "completed" for a in state.attempts):
        return "busy"
    # Even a failure may have an uncertain artifact effect. No automatic retry.
    if any(a.receipt["effect"]["state"] == "unknown" for a in state.attempts):
        return "reconciliation_required"
    return None


class B1ReadAdapter:
    """No worker/provider dependency. Construction/GET do not initialize or repair."""
    mode = "read_only"

    def __init__(self, ledger: DurableLedger | None, *, clock: Callable[[], float]):
        self._ledger = ledger
        self._clock = clock
        self._unavailable_revision = uuid4().hex

    def _read(self) -> LedgerState:
        if self._ledger is None:
            raise ValueError("not_attached")
        return _validate(self._ledger.read())

    def observe(self) -> Workflow:
        now = self._clock()
        if not _time(now):
            raise ValueError("invalid_clock")
        state = None
        try:
            state = self._read()
        except Exception:
            pass
        reason = "not_attached" if self._ledger is None else "observation_unavailable"
        result: Workflow = {
            "schemaVersion": VERSION, "id": "b1", "label": "AI-derived notes (synthetic)" if
            self.mode == "mock" else "AI-derived notes", "adapterMode": self.mode,
            "revision": state.revision if state else self._unavailable_revision,
            "observedAt": now, "integration": "pending" if state else
            "not_attached" if self._ledger is None else "unavailable",
            "configuration": {"state": "not_observed", "reason": reason},
            "schedule": {"state": "unknown", "intervalSeconds": None, "nextDueAt": None,
                         "reason": "scheduler_decision_required"},
            "execution": {"state": "unknown", "requestId": None, "startedAt": None},
            "lastAttempt": None, "lastVerifiedOutcome": None, "lastAttemptAt": None,
            "availableActions": [], "actionReasons": {
                "generate": reason, "set_schedule": "scheduler_decision_required"}}
        if state is None:
            return result
        result["configuration"] = {"state": "configured" if state.binding.provider_present else "missing",
                                   "reason": None if state.binding.provider_present else "configuration_missing"}
        # Simulation/read state is not live admission authority. Execution below
        # still shows queued/unknown honestly, without implying a live action.
        result["actionReasons"]["generate"] = (
            "configuration_missing" if not state.binding.provider_present else
            "mock_only" if self.mode == "mock" else "read_only")
        active = next((a.receipt for a in state.attempts if a.receipt["state"] != "completed"), None)
        result["execution"] = ({"state": active["state"], "requestId": active["requestId"],
                                "startedAt": active["startedAt"]} if active else
                               {"state": "unknown" if _blocked(state) else "idle",
                                "requestId": None, "startedAt": None})
        if state.attempts:
            r = state.attempts[-1].receipt
            result["lastAttempt"] = {k: _copy(r[k]) for k in (
                "requestId", "state", "outcome", "completedAt", "effect")}
            result["lastAttemptAt"] = r["createdAt"]
        for a in reversed(state.attempts):
            r = a.receipt
            if a.generation == state.binding.generation and r["effect"]["state"] == "verified":
                result["lastVerifiedOutcome"] = {"status": r["outcome"],
                    "completedAt": r["completedAt"], "effect": _copy(r["effect"])}
                break
        return result

    def lookup(self, request_id: str) -> Admission:
        if not _hex(request_id):
            raise InvalidRequest("invalid_request")
        try:
            a = _find(self._read(), request_id)
            return (_admission(request_id, "replayed", a.receipt) if a else
                    _admission(request_id, "rejected", code="request_not_found"))
        except Exception:
            return _admission(request_id, "indeterminate", code="admission_unknown")


@dataclass(frozen=True)
class SyntheticAccess:
    approved: bool = False


@dataclass(frozen=True)
class FakeEvidence:
    run_id: str
    context_ref: str
    recipe_ref: str
    generation: str
    destination: Literal["synthetic_review_only"] = "synthetic_review_only"


@final
class FakeReviewProvider:
    """Deterministic local simulation. A Review artifact is NOT approval/apply."""
    def __init__(self, scenario: str = "verified"):
        if scenario not in {"verified", "awaiting_unverified", "mismatched_evidence",
                            "error", "timeout", "retry_exhausted", "runtime_mismatch", "input_rejected"}:
            raise ValueError("invalid_scenario")
        self.scenario = scenario
        self.invocations = 0

    def generate(self, attempt: Attempt) -> FakeEvidence | None:
        self.invocations += 1
        if self.scenario in {"error", "timeout", "retry_exhausted", "runtime_mismatch", "input_rejected"}:
            raise RuntimeError("synthetic private sentinel: recipe/source never forwarded")
        if self.scenario == "awaiting_unverified":
            return None
        return FakeEvidence(attempt.receipt["runId"], attempt.context_ref, attempt.recipe_ref,
                            attempt.generation if self.scenario == "verified" else "0" * 32)


class B1FakeAdapter(B1ReadAdapter):
    """Explicit fake manual port; never attach to shared runtime routes."""
    mode = "mock"

    def __init__(self, ledger: DurableLedger | None, *, provider: FakeReviewProvider | None,
                 access: SyntheticAccess = SyntheticAccess(), clock: Callable[[], float]):
        if provider is not None and type(provider) is not FakeReviewProvider:
            raise TypeError("fake_provider_required")
        if type(access) is not SyntheticAccess or type(access.approved) is not bool:
            raise TypeError("synthetic_access_required")
        super().__init__(ledger, clock=clock)
        self._provider = provider
        self._access = access

    def observe(self) -> Workflow:
        result = super().observe()
        if self._provider is None and result["configuration"]["state"] == "configured":
            result["configuration"] = {"state": "missing", "reason": "configuration_missing"}
            result["actionReasons"]["generate"] = "configuration_missing"
        return result

    def generate(self, request: GenerateRequest) -> Admission:
        request = parse_generate(request)
        rid = request["requestId"]
        if not self._access.approved:
            return _admission(rid, "rejected", code="authority_rejected")

        def admit(state: LedgerState):
            _validate(state)
            old = _find(state, rid)
            supplied = request["input"]
            if old:
                same = (old.context_ref == supplied["contextRef"] and old.recipe_ref == supplied["recipeRef"])
                return state, (_admission(rid, "replayed", old.receipt) if same else
                               _admission(rid, "rejected", code="request_conflict"))
            code = ("stale_revision" if request["expectedRevision"] != state.revision else
                    _blocked(state) or ("configuration_missing" if not state.binding.provider_present
                    or self._provider is None else None))
            if not code and (supplied["contextRef"], supplied["recipeRef"]) != (
                    state.binding.context_ref, state.binding.recipe_ref):
                code = "input_binding_invalid"
            if code:
                return state, _admission(rid, "rejected", code=code)
            now = self._clock()
            if not _time(now):
                raise ValueError("invalid_clock")
            receipt: Receipt = {"schemaVersion": VERSION, "workflowId": "b1", "requestId": rid,
                "runId": uuid4().hex, "action": "generate", "state": "queued", "outcome": None,
                "createdAt": now, "startedAt": None, "completedAt": None, "effect": _effect(), "error": None}
            a = Attempt(supplied["contextRef"], supplied["recipeRef"], state.binding.generation,
                        state.revision, receipt)
            return replace(state, revision=uuid4().hex, attempts=(*state.attempts, a)), _admission(rid, "accepted", receipt)
        try:
            if self._ledger is None:
                return _admission(rid, "rejected", code="not_attached")
            return self._ledger.transact(admit)
        except Exception:
            return _admission(rid, "indeterminate", code="admission_unknown")

    def run_fake(self, request_id: str) -> Admission:
        """Explicit test driver: persist claim BEFORE invoking fake. Never reclaims.

        A process death after claim leaves running/unknown effect, lookup-only;
        a process death after result commit returns that receipt on lookup.
        No constructor, GET, retry, or restart calls this method automatically.
        """
        if not _hex(request_id):
            raise InvalidRequest("invalid_request")
        if not self._access.approved:
            return _admission(request_id, "rejected", code="authority_rejected")
        if self._provider is None:
            return _admission(request_id, "indeterminate", code="admission_unknown")

        def claim(state: LedgerState):
            _validate(state)
            a = _find(state, request_id)
            if a is None or a.receipt["state"] != "queued":
                return state, None
            # Do not execute a queued binding under a rotated configuration.
            if (a.generation != state.binding.generation or not state.binding.provider_present):
                return state, None
            now = self._clock()
            if not _time(now) or now < a.receipt["createdAt"]:
                raise ValueError("invalid_clock")
            running = replace(a, receipt={**a.receipt, "state": "running", "startedAt": now})
            return replace(state, revision=uuid4().hex, attempts=tuple(
                running if x == a else x for x in state.attempts)), running

        try:
            if self._ledger is None:
                return _admission(request_id, "indeterminate", code="admission_unknown")
            a = self._ledger.transact(claim)
            if a is None:
                return self.lookup(request_id)
            outcome, code = "unknown", "effect_unconfirmed"
            effect = _effect()
            try:
                evidence = self._provider.generate(a)
                expected = FakeEvidence(a.receipt["runId"], a.context_ref, a.recipe_ref, a.generation)
                if type(evidence) is FakeEvidence and evidence == expected:
                    outcome, code, effect = "success", None, _effect("verified")
            except Exception:
                if self._provider.scenario != "timeout":
                    outcome = "failure"
                    code = "input_rejected" if self._provider.scenario == "input_rejected" else "execution_failed"

            def complete(state: LedgerState):
                _validate(state)
                current = _find(state, request_id)
                if current != a:
                    raise ValueError("claim_lost")
                now = self._clock()
                if not _time(now) or now < a.receipt["startedAt"]:
                    raise ValueError("invalid_clock")
                r = {**a.receipt, "state": "completed", "outcome": outcome,
                     "completedAt": now, "effect": effect, "error": _error(code) if code else None}
                done = replace(a, receipt=r)
                return replace(state, revision=uuid4().hex, attempts=tuple(
                    done if x == a else x for x in state.attempts)), _admission(request_id, "replayed", r)
            return self._ledger.transact(complete)
        except Exception:
            return _admission(request_id, "indeterminate", code="admission_unknown")

    def set_schedule(self, request: object) -> dict:
        # No bounds chosen; even syntactically plausible settings cannot admit.
        if (type(request) is not dict or set(request) != {
                "schemaVersion", "requestId", "expectedRevision", "action", "input"}
                or not _hex(request.get("requestId")) or not _hex(request.get("expectedRevision"))):
            raise InvalidRequest("invalid_request")
        if request["schemaVersion"] != VERSION:
            raise InvalidRequest("unsupported_version")
        if request["action"] != "set_schedule":
            raise InvalidRequest("unsupported_action")
        supplied = request["input"]
        if (type(supplied) is not dict or set(supplied) != {"enabled", "intervalSeconds"}
                or type(supplied["enabled"]) is not bool or type(supplied["intervalSeconds"]) is not int):
            raise InvalidRequest("invalid_request")
        return {"schemaVersion": VERSION, "workflowId": "b1", "requestId": request["requestId"],
                "decision": "rejected", "schedule": None, "revision": None,
                "error": _error("scheduler_decision_required")}
