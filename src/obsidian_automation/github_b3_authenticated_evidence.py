"""Unattached B3 dual-issuer MAC envelope verification; no runtime authority.

MACs can demonstrate possession of a configured issuer-specific key, not that
Nextcloud or the old LXC actually produced a truthful, complete statement.
This pure module accepts only *detached* trusted inputs supplied by a future
privileged owner; their provenance is NOT established here. No real keys are
loaded, no provider/host is contacted, and cutover is NEVER authorized.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import hashlib
import hmac
import json
import math
import re

from .github_b3_retirement_receipts import (
    B3NextcloudProviderReceipt,
    B3OldHostFenceReceipt,
    assess_b3_retirement_receipts,
)

VERSION = "github-b3-mac-evidence/v1"
ENVELOPE_VERSION = "github-b3-mac-envelope/v1"
_PROVIDER = "nextcloud-provider-probe"
_HOST = "legacy-host-probe"
_CONTEXT = b"obsidian-b3-retirement-issuer-evidence/v1\x00"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ENVELOPE = 12 * 1024
_MAX_SEQUENCE = (1 << 63) - 1
_MAX_CLOCK = 253402300799
_PROVIDER_FIELDS = frozenset(f.name for f in fields(B3NextcloudProviderReceipt))
_HOST_FIELDS = frozenset(f.name for f in fields(B3OldHostFenceReceipt))
_PROVIDER_LISTS = frozenset((
    "principals", "legacy_token_ids", "provider_revoked_token_ids",
))
_HOST_LISTS = frozenset(("principals",))


@dataclass(frozen=True, slots=True)
class B3DetachedIssuerKeys:
    """Key bytes only from a future privileged owner, NEVER from the WebUI.

    Symmetric HMAC: whoever can verify can also create an equivalent tag.
    Authenticity and separation of *issuers* depend entirely on secure key
    lifecycle and on real provider/host collectors, both NYI. Keep secret
    bytes out of repr/error/public result.
    """

    provider_key_id: str
    host_key_id: str
    provider_key: bytes = field(repr=False)
    host_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class B3DetachedSequenceFloor:
    """Untrusted unless obtained from a future protected durable checkpoint.

    A caller can forge this floor; enforcing it does not establish real
    cross-restart or cross-LXC replay protection by itself.
    """

    provider_sequence: int
    host_sequence: int


def _result(state: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": VERSION,
        "state": state,
        "reason": reason,
        "cutover_authorized": False,
    }


def _id(value: object, pattern: re.Pattern[str] = _ID) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _sequence(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_SEQUENCE


def _trusted_inputs(keys: object, floor: object, now: object) -> bool:
    if (
        type(keys) is not B3DetachedIssuerKeys
        or type(floor) is not B3DetachedSequenceFloor
        or not _id(keys.provider_key_id)
        or not _id(keys.host_key_id)
        or keys.provider_key_id == keys.host_key_id
        or type(keys.provider_key) is not bytes
        or type(keys.host_key) is not bytes
        or len(keys.provider_key) != 32
        or len(keys.host_key) != 32
        or hmac.compare_digest(keys.provider_key, keys.host_key)
        or not _sequence(floor.provider_sequence)
        or not _sequence(floor.host_sequence)
        or type(now) not in (int, float)
    ):
        return False
    try:
        return math.isfinite(now) and 0 <= now <= _MAX_CLOCK
    except (OverflowError, TypeError, ValueError):
        return False


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n"
    ).encode("utf-8")


def _loads_exact(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_ENVELOPE:
        raise ValueError("invalid_envelope_size")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for name, value in pairs:
            if name in out:
                raise ValueError("duplicate_key")
            out[name] = value
        return out

    def reject_constant(_: str) -> object:
        raise ValueError("nonfinite_json")

    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=reject_constant)
    if type(value) is not dict or raw != _canonical(value):
        raise ValueError("noncanonical_json")
    return value


def _payload(raw: object, *, provider: bool) -> object:
    if type(raw) is not dict:
        raise ValueError("invalid_payload")
    expected = _PROVIDER_FIELDS if provider else _HOST_FIELDS
    lists = _PROVIDER_LISTS if provider else _HOST_LISTS
    if set(raw) != expected:
        raise ValueError("invalid_payload_fields")

    converted = raw.copy()
    for key in lists:
        values = converted[key]
        if (
            type(values) is not list
            or len(values) > 128
            or any(type(x) is not str or len(x) > 128 for x in values)
        ):
            raise ValueError("invalid_payload_list")
        converted[key] = tuple(values)

    # The receipt parser itself is strict about dates and fields. This
    # dataclass only reifies the MAC-verified bytes, not trusted authority.
    if provider:
        return B3NextcloudProviderReceipt(**converted)
    return B3OldHostFenceReceipt(**converted)


def _verify_one(
    raw: bytes, *,
    issuer: str, key_id: str, key: bytes, minimum_sequence: int,
) -> object:
    envelope = _loads_exact(raw)
    if set(envelope) != {
        "schema_version", "issuer", "key_id", "sequence",
        "payload", "mac_sha256",
    }:
        raise ValueError("invalid_envelope_fields")
    if (
        type(envelope["schema_version"]) is not str
        or envelope["schema_version"] != ENVELOPE_VERSION
        or type(envelope["issuer"]) is not str
        or envelope["issuer"] != issuer
        or type(envelope["key_id"]) is not str
        or envelope["key_id"] != key_id
        or not _sequence(envelope["sequence"])
        or envelope["sequence"] <= minimum_sequence
        or not _id(envelope["mac_sha256"], _SHA)
    ):
        raise ValueError("invalid_envelope_binding")

    unsigned = {k: v for k, v in envelope.items() if k != "mac_sha256"}
    signed = _CONTEXT + issuer.encode("ascii") + b"\x00" + _canonical(unsigned)
    expected = hmac.new(key, signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, envelope["mac_sha256"]):
        raise ValueError("bad_mac")
    return _payload(envelope["payload"], provider=issuer == _PROVIDER)


def assess_b3_authenticated_evidence(
    provider_envelope: bytes,
    host_envelope: bytes,
    *,
    trusted_issuer_keys: B3DetachedIssuerKeys,
    trusted_sequence_floor: B3DetachedSequenceFloor,
    binding_ref: str,
    revision: str,
    expected_generation: str,
    now: float,
) -> dict[str, object]:
    """Check two distinct MACs and proposed anti-replay floor, *never* cutover.

    This is not a credential/probe loader or a key issuer, and exposes no
    signing helper. A future privileged server must authenticate origins,
    load issuer keys from protected source, and persist checkpoint changes.
    """
    if (
        not _trusted_inputs(trusted_issuer_keys, trusted_sequence_floor, now)
        or not all(_id(v) for v in (
            binding_ref, revision, expected_generation,
        ))
    ):
        return _result("unknown", "trusted_evidence_source_unavailable")
    try:
        provider = _verify_one(
            provider_envelope, issuer=_PROVIDER,
            key_id=trusted_issuer_keys.provider_key_id,
            key=trusted_issuer_keys.provider_key,
            minimum_sequence=trusted_sequence_floor.provider_sequence,
        )
        host = _verify_one(
            host_envelope, issuer=_HOST,
            key_id=trusted_issuer_keys.host_key_id,
            key=trusted_issuer_keys.host_key,
            minimum_sequence=trusted_sequence_floor.host_sequence,
        )
    except (TypeError, ValueError, OverflowError, UnicodeError,
            KeyError, RecursionError):
        return _result("unknown", "evidence_authentication_unavailable")

    try:
        coherence = assess_b3_retirement_receipts(
            provider, host, binding_ref=binding_ref, revision=revision,
            expected_generation=expected_generation, now=now,
        )
    except (TypeError, ValueError, OverflowError):
        return _result("unknown", "evidence_binding_unavailable")
    if coherence["state"] == "blocked":
        return _result("blocked", str(coherence["reason"]))
    if coherence["state"] != "candidate":
        return _result("unknown", "evidence_binding_unavailable")

    # MAC proof is only evidence that two distinct *provided keys* were used;
    # neither a legitimate Nextcloud provider nor old-host observer is
    # authenticated without separately owned and provisioned key channels.
    return _result("candidate", "detached_mac_valid_external_authority_unverified")
