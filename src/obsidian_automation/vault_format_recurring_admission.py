"""#472 R2/A0: pure, non-effectful candidate admission for recurring CAS writes.

NOT connected to snapshot_app, Scheduler or WebDAV. It neither grants human
approval nor checks real provider capability. A future independently authorized
Writer MUST re-check source generation, account binding, remote CAS capability,
record durability and the actual selected file bytes at effect time.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable

from .vault_formatter import POLICY_VERSION, SCOPE, DEFAULT_MAX_BYTES
from .vault_format_writer import (
    MAX_PLAN_BYTES, MAX_SELECTED_BYTES, MAX_TARGETS, FormatWriteError,
    _target, _validated_plan,
)

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")


@dataclass(frozen=True)
class RecurringPolicy:
    """Bounded policy descriptor, NOT a self-authenticating approval.

    The policy/proof hashes refer to an independently controlled operator
    decision. No normal app constructor creates this policy or a verifier.
    """
    source_generation: str
    account_binding_sha256: str
    provider_proof_sha256: str
    max_targets: int
    max_selected_bytes: int
    scope: str = SCOPE
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True)
class RecurringAdmission:
    """Immutable proposal to be passed to a *separate* effect-time authority."""
    policy_sha256: str
    plan_sha256: str
    source_generation: str
    account_binding_sha256: str
    provider_proof_sha256: str
    run_id: str
    operation_id: str
    selected_paths: tuple[str, ...]
    selected_before_sha256: tuple[str, ...]
    selected_after_sha256: tuple[str, ...]
    selected_bytes: int
    scope: str
    policy_version: str


def _strict_sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _policy_digest(policy: RecurringPolicy) -> str:
    if (type(policy) is not RecurringPolicy
            or type(policy.scope) is not str
            or type(policy.policy_version) is not str
            or policy.scope != SCOPE or policy.policy_version != POLICY_VERSION
            or not all(_strict_sha(getattr(policy, key)) for key in (
                "source_generation", "account_binding_sha256", "provider_proof_sha256"
            ))
            or type(policy.max_targets) is not int
            or not 1 <= policy.max_targets <= MAX_TARGETS
            or type(policy.max_selected_bytes) is not int
            or not 1 <= policy.max_selected_bytes <= MAX_SELECTED_BYTES):
        raise FormatWriteError("invalid_recurring_policy")
    encoded = json.dumps({
        "schema": 1,
        "scope": policy.scope,
        "policy_version": policy.policy_version,
        "source_generation": policy.source_generation,
        "account_binding_sha256": policy.account_binding_sha256,
        "provider_proof_sha256": policy.provider_proof_sha256,
        "max_targets": policy.max_targets,
        "max_selected_bytes": policy.max_selected_bytes,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_metadata(plan: dict) -> dict:
    """Bound source metadata objects before any JSON string encoding.

    Reject large string/collection members, cycles, unusual objects, and
    excessive nesting while ignoring display-only diffs. The final streamed
    JSON length check still enforces the precise encoded-byte limit.
    """
    if type(plan) is not dict or ("diffs" in plan and type(plan["diffs"]) is not dict):
        raise FormatWriteError("recurring_plan_snapshot_invalid")
    visited: set[int] = set()
    nodes = 0
    weight = 0

    def visit(value: object, depth: int, *, is_root: bool = False) -> object:
        nonlocal nodes, weight
        nodes += 1
        if nodes > MAX_PLAN_BYTES // 8 or depth > 64:
            raise FormatWriteError("recurring_plan_too_large")
        kind = type(value)
        if kind is str:
            # A control character can expand to six UTF-8 JSON bytes (\\u00xx)
            # inside one encoder chunk. Charge the worst case before encoding
            # to avoid allocating an oversized escaped string first.
            weight += len(value) * 6
            result = value  # immutable string, no unbounded copy
        elif kind in (dict, list, tuple):
            if id(value) in visited:
                # Cyclic and aliased containers are rejected conservatively.
                raise FormatWriteError("recurring_plan_snapshot_invalid")
            visited.add(id(value))
            if len(value) > MAX_PLAN_BYTES // 8:
                raise FormatWriteError("recurring_plan_too_large")
            weight += len(value)
            if kind is dict:
                copy: dict[str, object] = {}
                for key, item in value.items():
                    if is_root and key == "diffs":
                        continue
                    if type(key) is not str:
                        raise FormatWriteError("recurring_plan_snapshot_invalid")
                    validated_key = visit(key, depth + 1)
                    copy[validated_key] = visit(item, depth + 1)
                result = copy
            else:
                result = [visit(item, depth + 1) for item in value]
        elif kind is int:
            # JSONEncoder formats the whole decimal integer before yielding
            # a chunk. A hostile process may disable Python's digit limit.
            # Reject huge integers from bit_length before any string conversion.
            if value.bit_length() > 4096:
                raise FormatWriteError("recurring_plan_too_large")
            bits = value.bit_length()
            # Conservative decimal-digit upper bound, without converting
            # the integer; normal small Plan counters stay inexpensive.
            weight += max(2, (bits * 30103 + 99999) // 100000 + 2)
            result = value
        elif kind in (float, bool) or value is None:
            weight += 32
            result = value
        else:
            raise FormatWriteError("recurring_plan_snapshot_invalid")
        if weight > MAX_PLAN_BYTES:
            raise FormatWriteError("recurring_plan_too_large")
        return result

    try:
        # Copy each nested metadata container during the bounded walk. After
        # this point, mutations to caller-owned lists/dicts cannot change the
        # serialized Plan. Large display-only diffs are never traversed.
        candidate = visit(plan, 0, is_root=True)
    except FormatWriteError:
        raise  # Preserve budget vs malformed-input reason without raw data.
    except (ValueError, TypeError, UnicodeError, RecursionError, RuntimeError):
        raise FormatWriteError("recurring_plan_snapshot_invalid") from None
    assert type(candidate) is dict
    return candidate


def prepare_recurring_admission(
    plan: dict, policy: RecurringPolicy, run_id: str, *,
    current_generation: Callable[[], str | None],
    verify_operator_policy: Callable[[str, str, str, str], bool] | None,
) -> RecurringAdmission | None:
    """Validate one complete Plan under externally verified fixed scope/budget.

    The verifier MUST be independently controlled and compare the policy digest
    against a durable, previously Human-approved record. A mere callback
    supplied by an untrusted caller cannot stand in for that record.
    This function is pure apart from the verifier and generation observations:
    it does not read credentials, create a receipt, or issue HTTP requests.
    """
    # Make an inaccessible private copy before callbacks. frozen=True only
    # blocks normal assignment: object.__setattr__ can mutate a caller-owned
    # dataclass during proof verification. Never read policy again afterward.
    if type(policy) is not RecurringPolicy:
        raise FormatWriteError("invalid_recurring_policy")
    pinned = RecurringPolicy(
        source_generation=policy.source_generation,
        account_binding_sha256=policy.account_binding_sha256,
        provider_proof_sha256=policy.provider_proof_sha256,
        max_targets=policy.max_targets,
        max_selected_bytes=policy.max_selected_bytes,
        scope=policy.scope, policy_version=policy.policy_version,
    )
    policy_sha = _policy_digest(pinned)
    if type(run_id) is not str or _ID.fullmatch(run_id) is None:
        raise FormatWriteError("invalid_recurring_run_id")
    if not callable(current_generation) or not callable(verify_operator_policy):
        raise FormatWriteError("recurring_authority_missing")

    # Never clone raw diff bodies. The shared CAS Writer validates only Plan
    # metadata and disregards "diffs". Stream that metadata into a bounded
    # JSON buffer, refusing it BEFORE allocating the decoded snapshot.
    candidate = _bounded_metadata(plan)
    pieces: list[bytes] = []
    size = 0
    try:
        encoder = json.JSONEncoder(
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        )
        for piece in encoder.iterencode(candidate):
            encoded = piece.encode("utf-8")
            size += len(encoded)
            if size > MAX_PLAN_BYTES:
                raise FormatWriteError("recurring_plan_too_large")
            pieces.append(encoded)
        frozen = json.loads(b"".join(pieces))
    except FormatWriteError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise FormatWriteError("recurring_plan_snapshot_invalid") from None
    plan_sha, entries = _validated_plan(frozen)
    if frozen["counts"].get("error", 0) != 0:
        raise FormatWriteError("recurring_plan_scan_error")

    try:
        generation = current_generation()
    except Exception:
        raise FormatWriteError("recurring_source_unavailable") from None
    if type(generation) is not str or generation != pinned.source_generation:
        raise FormatWriteError("recurring_source_generation_changed")
    try:
        verified = verify_operator_policy(
            policy_sha, pinned.source_generation,
            pinned.account_binding_sha256, pinned.provider_proof_sha256,
        )
    except Exception:
        raise FormatWriteError("recurring_policy_unverified") from None
    if verified is not True:
        raise FormatWriteError("recurring_policy_unverified")
    # Detect a verifier that observed/changed the source generation itself.
    # This does NOT replace the mandatory effect-time fence in the CAS Writer.
    try:
        still_current = current_generation()
    except Exception:
        raise FormatWriteError("recurring_source_unavailable") from None
    if type(still_current) is not str or still_current != pinned.source_generation:
        raise FormatWriteError("recurring_source_generation_changed")

    selected = sorted(path for path, item in entries.items()
                      if item["status"] == "would_change")
    if len(selected) != frozen["counts"].get("would_change", 0):
        raise FormatWriteError("recurring_plan_count_mismatch")
    if len(selected) > pinned.max_targets:
        raise FormatWriteError("recurring_target_budget")
    if not selected:
        return None  # No operation ID or remote effect for a zero-change Plan.
    total = 0
    before: list[str] = []
    after: list[str] = []
    for path in selected:
        if _target(path) != path:
            raise FormatWriteError("recurring_target_invalid")
        item = entries[path]
        if (not _strict_sha(item.get("before_sha256"))
                or not _strict_sha(item.get("after_sha256"))
                or item["before_sha256"] == item["after_sha256"]
                or type(item.get("before_size")) is not int
                or type(item.get("after_size")) is not int
                or not 0 <= item["before_size"] <= DEFAULT_MAX_BYTES
                or not 0 <= item["after_size"] <= DEFAULT_MAX_BYTES
                or type(item.get("changed_lines")) is not list
                or not item["changed_lines"]
                or not all(type(n) is int and n > 0
                           for n in item["changed_lines"])):
            raise FormatWriteError("recurring_plan_target_invalid")
        total += max(item["before_size"], item["after_size"])
        if total > pinned.max_selected_bytes:
            raise FormatWriteError("recurring_selected_byte_budget")
        before.append(item["before_sha256"])
        after.append(item["after_sha256"])

    # Never include Plan OR policy SHA in the replay ID: a resumed run_id
    # with different Plan/credential-policy still collides with the previously
    # reserved operation marker in the same trusted receipt root.
    # The CAS Writer must atomically reserve the marker BEFORE any PUT.
    # The marker alone does not prove that an interrupted effect completed.
    operation = hashlib.sha256(
        ("oa472-r2-v1:" + run_id).encode("ascii")
    ).hexdigest()[:32]
    return RecurringAdmission(
        policy_sha256=policy_sha,
        plan_sha256=plan_sha,
        source_generation=pinned.source_generation,
        account_binding_sha256=pinned.account_binding_sha256,
        provider_proof_sha256=pinned.provider_proof_sha256,
        run_id=run_id,
        operation_id=operation,
        selected_paths=tuple(selected),
        selected_before_sha256=tuple(before),
        selected_after_sha256=tuple(after),
        selected_bytes=total,
        scope=pinned.scope,
        policy_version=pinned.policy_version,
    )
