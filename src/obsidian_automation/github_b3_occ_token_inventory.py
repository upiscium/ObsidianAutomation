"""Offline Nextcloud OCC token census: no execution, secrets or cutover.

The documented Nextcloud 34 JSON token rows contain id, name, lastActivity,
type and scope. Only a future independently authenticated admin collector
may provide real output. This module does NOT contact a real Nextcloud host,
prove any account census is exhaustive, or permit B3 Writer activation.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re

VERSION = "github-b3-occ-token-inventory/v1"
PROFILE = "nextcloud-34-occ-auth-tokens-json/v1"
_ACCOUNT = re.compile(r"[a-z0-9_.-]{1,64}\Z")
_SERVER = re.compile(r"34\.[0-9]{1,3}\.[0-9]{1,3}\Z")
_TOKEN_FIELDS = frozenset(("id", "name", "lastActivity", "type", "scope"))
_MAX_RAW = 65536
_MAX_ROWS = 128
_MAX_TIME = 253402300799
_FRESH = 60


@dataclass(frozen=True, slots=True)
class B3OccTokenCapture:
    """Untrusted representation of an externally provided read-only result."""

    account: str
    output_profile: str
    installed_version: str
    command: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes
    observed_at: int


def fixed_occ_list_args(account: str) -> tuple[str, ...]:
    """Construct only the allowlisted OCC subcommand; never run it."""
    if type(account) is not str or _ACCOUNT.fullmatch(account) is None:
        raise ValueError("invalid_account")
    return ("user:auth-tokens:list", account, "--output=json")


def _time(value: object, now: object, *, fresh: bool) -> bool:
    try:
        return (
            type(value) is int and type(now) in (int, float)
            and math.isfinite(now) and 0 <= value <= now <= _MAX_TIME
            and (not fresh or now - value <= _FRESH)
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _token_ids(raw: bytes, now: float) -> frozenset[int]:
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_RAW:
        raise ValueError("invalid_output_size")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for k, v in pairs:
            if k in result:
                raise ValueError("duplicate_key")
            result[k] = v
        return result

    def bad_constant(_: str) -> object:
        raise ValueError("nonfinite_json")

    rows = json.loads(raw, object_pairs_hook=unique,
                      parse_constant=bad_constant)
    if type(rows) is not list or len(rows) > _MAX_ROWS:
        raise ValueError("invalid_token_rows")
    found: set[int] = set()
    for row in rows:
        if type(row) is not dict or set(row) != _TOKEN_FIELDS:
            raise ValueError("invalid_token_fields")
        token_id = row["id"]
        if (
            type(token_id) is not int or not 1 <= token_id < (1 << 63)
            or token_id in found
            or type(row["name"]) is not str
            or not 1 <= len(row["name"]) <= 128
            or any(ord(c) < 32 or ord(c) == 127 for c in row["name"])
            or not _time(row["lastActivity"], now, fresh=False)
            or type(row["type"]) is not int or row["type"] not in (0, 1, 2, 3)
            or type(row["scope"]) is not dict
            or not 1 <= len(row["scope"]) <= 2
            or any(
                type(k) is not str
                or k not in ("filesystem", "password-unconfirmable")
                or type(v) is not bool
                for k, v in row["scope"].items()
            )
        ):
            raise ValueError("invalid_token_row")
        found.add(token_id)
    return frozenset(found)


def _valid_now(now: object) -> bool:
    try:
        return (
            type(now) in (int, float)
            and math.isfinite(now) and 0 <= now <= _MAX_TIME
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _result(state: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": VERSION,
        "state": state,
        "reason": reason,
        "cutover_authorized": False,
    }


def assess_nextcloud_occ_inventory(
    captures: tuple[B3OccTokenCapture, ...],
    *,
    expected_accounts: tuple[str, ...],
    prior_legacy_token_ids: tuple[tuple[str, int], ...],
    expected_version: str,
    now: float,
) -> dict[str, object]:
    """Fail closed on incomplete, conflicting or ambiguous token inventory.

    The prior legacy token census and expected accounts are external trusted
    inputs not authenticated here. An empty active-token listing does not
    prove account-password login has been disabled or old-host egress denied.
    """
    try:
        if (
            type(captures) is not tuple
            or type(expected_accounts) is not tuple
            or not 1 <= len(expected_accounts) <= 32
            or any(type(x) is not str or _ACCOUNT.fullmatch(x) is None
                   for x in expected_accounts)
            or tuple(sorted(set(expected_accounts))) != expected_accounts
            or type(expected_version) is not str
            or _SERVER.fullmatch(expected_version) is None
            or type(prior_legacy_token_ids) is not tuple
            or not 1 <= len(prior_legacy_token_ids) <= 128
            or tuple(sorted(set(prior_legacy_token_ids))) != prior_legacy_token_ids
            or any(type(pair) is not tuple or len(pair) != 2
                   or type(pair[0]) is not str
                   or pair[0] not in expected_accounts
                   or type(pair[1]) is not int or not 1 <= pair[1] < (1 << 63)
                   for pair in prior_legacy_token_ids)
            or len(captures) != len(expected_accounts)
            or not _valid_now(now)
        ):
            return _result("unknown", "invalid_census_contract")
        seen: set[str] = set()
        visible: set[tuple[str, int]] = set()
        for capture in captures:
            if (
                type(capture) is not B3OccTokenCapture
                or capture.account not in expected_accounts
                or capture.account in seen
                or capture.output_profile != PROFILE
                or capture.installed_version != expected_version
                or capture.command != fixed_occ_list_args(capture.account)
                or type(capture.exit_code) is not int or capture.exit_code != 0
                or type(capture.stderr) is not bytes or capture.stderr != b""
                or not _time(capture.observed_at, now, fresh=True)
            ):
                return _result("unknown", "unverified_occ_capture")
            seen.add(capture.account)
            visible.update(
                (capture.account, token) for token in
                _token_ids(capture.stdout, now)
            )
        if seen != set(expected_accounts):
            return _result("unknown", "incomplete_occ_census")
        if any(token in visible for token in prior_legacy_token_ids):
            return _result("blocked", "legacy_token_still_present")
        if visible:
            return _result("blocked", "additional_token_authority_unresolved")
        return _result("candidate", "provider_origin_and_password_auth_unverified")
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError,
            OverflowError, UnicodeError):
        return _result("unknown", "unverified_occ_capture")
