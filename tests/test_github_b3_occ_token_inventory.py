"""Synthetic Nextcloud OCC 34 token census; no subprocess/provider calls."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from obsidian_automation.github_b3_occ_token_inventory import (
    B3OccTokenCapture, PROFILE, VERSION, assess_nextcloud_occ_inventory,
    fixed_occ_list_args,
)

NOW = 1_800_000_000
ACCOUNTS = ("legacy-daily", "legacy-writer")
KNOWN = (("legacy-writer", 42),)
TOKENS = {
    "id": 42, "name": "Example old sync process",
    "lastActivity": NOW - 120, "type": 1,
    "scope": {"filesystem": True},
}


def capture(account, tokens=(), *, stdout=None):
    return B3OccTokenCapture(
        account=account, output_profile=PROFILE,
        installed_version="34.0.0",
        command=fixed_occ_list_args(account),
        exit_code=0,
        stdout=json.dumps(
            list(tokens), separators=(",", ":"),
        ).encode() if stdout is None else stdout,
        stderr=b"", observed_at=NOW - 2,
    )


def assess(captures, *, accounts=ACCOUNTS, known=KNOWN,
           version="34.0.0", now=NOW):
    result = assess_nextcloud_occ_inventory(
        captures, expected_accounts=accounts,
        prior_legacy_token_ids=known,
        expected_version=version, now=now,
    )
    assert result["schema_version"] == VERSION
    assert result["cutover_authorized"] is False
    assert set(result) == {
        "schema_version", "state", "reason", "cutover_authorized",
    }
    return result


def fresh():
    return tuple(capture(a) for a in ACCOUNTS)


def test_empty_authenticated_looking_census_is_only_candidate():
    r = assess(fresh())
    assert r == {
        "schema_version": VERSION,
        "state": "candidate",
        "reason": "provider_origin_and_password_auth_unverified",
        "cutover_authorized": False,
    }


def test_known_old_token_blocks_independent_of_name_and_scope():
    rows = (capture("legacy-daily"), capture(
        "legacy-writer", ({**TOKENS, "scope": {"filesystem": False}},),
    ))
    assert assess(rows)["reason"] == "legacy_token_still_present"


def test_unexpected_new_token_blocks_even_when_old_id_absent():
    rows = (capture("legacy-daily"), capture(
        "legacy-writer", ({**TOKENS, "id": 44},),
    ))
    assert assess(rows)["reason"] == "additional_token_authority_unresolved"


@pytest.mark.parametrize("account", ACCOUNTS)
def test_missing_or_duplicate_account_never_mints_completeness(account):
    rows = [c for c in fresh() if c.account != account]
    assert assess(tuple(rows))["state"] == "unknown"
    assert assess((fresh()[0], fresh()[0]))["state"] == "unknown"


@pytest.mark.parametrize("change", (
    {"output_profile": "unknown"},
    {"installed_version": "33.0.0"},
    {"installed_version": "34.0.1"},
    {"command": ("user:auth-tokens:delete", "legacy-writer", "42")},
    {"command": ("user:auth-tokens:list", "legacy-daily", "--output=plain")},
    {"exit_code": 1},
    {"exit_code": True},
    {"stderr": b"could not list"},
    {"observed_at": NOW - 61},
    {"observed_at": NOW + 1},
    {"account": "legacy-Daily"},
))
def test_unverified_capture_is_unknown(change):
    rows = list(fresh())
    rows[0] = replace(rows[0], **change)
    assert assess(tuple(rows))["state"] == "unknown"


@pytest.mark.parametrize("raw", (
    b"", b"not-json", b"{}", b"null", b"true",
    b'{"id":42,"id":42}', b'["a","b"]',
    b'[{ "id": 42, "id": 43 }]',
    b'[{"token":"sensitive"}]',
    b'["x"]',
    b"[" * 1900,
    b"\xff",
    b"a" * 65537,
))
def test_untrusted_or_malformed_stdout_is_unknown(raw):
    rows = list(fresh())
    rows[0] = replace(rows[0], stdout=raw)
    assert assess(tuple(rows))["state"] == "unknown"


@pytest.mark.parametrize("change", (
    {"id": 0}, {"id": True}, {"id": -1}, {"id": "42"},
    {"id": 2**64}, {"name": ""}, {"name": "word\nnew"},
    {"name": "a" * 129}, {"type": 4}, {"type": False},
    {"type": "permanent"}, {"lastActivity": True},
    {"lastActivity": NOW + 1}, {"lastActivity": -1},
    {"scope": []}, {"scope": {"filesystem": "yes"}},
    {"scope": {}}, {"scope": {"arbitrary": True}},
    {"password": "should-not-be-present"},
))
def test_strict_token_row_fail_closed(change):
    rows = list(fresh())
    rows[0] = capture("legacy-daily", ({**TOKENS, **change},))
    assert assess(tuple(rows))["state"] == "unknown"


def test_duplicate_token_id_rejected_without_leaking_metadata():
    rows = list(fresh())
    rows[0] = capture("legacy-daily", (TOKENS, TOKENS))
    assert assess(tuple(rows))["state"] == "unknown"


@pytest.mark.parametrize("known", (
    (), (("legacy-writer", True),),
    (("someone-else", 42),),
    (("legacy-writer", 42), ("legacy-writer", 42)),
    (["legacy-writer", 42],),
))
def test_invalid_prior_token_scope_unknown(known):
    assert assess(fresh(), known=known)["state"] == "unknown"


@pytest.mark.parametrize("accounts", (
    (), ("legacy-writer", "legacy-daily"),
    ("legacy-writer", "legacy-writer"),
    ("legacy-daily", "legacy-writer", "unexpected"),
))
def test_invalid_or_incomplete_principal_scope_unknown(accounts):
    assert assess(fresh(), accounts=accounts)["state"] == "unknown"


@pytest.mark.parametrize("version", ("latest", "35.0.0", "34.0", "", 34, "34.9999.0"))
def test_unknown_server_version_refused(version):
    assert assess(fresh(), version=version)["state"] == "unknown"


def test_argv_builder_does_not_expose_delete_or_freeform_paths():
    assert fixed_occ_list_args("legacy-writer") == (
        "user:auth-tokens:list", "legacy-writer", "--output=json",
    )
    for bad in ("../other", "legacy Writer", "--output=json",
                "legacy;id", "legacy/writer", ""):
        with pytest.raises(ValueError):
            fixed_occ_list_args(bad)


def test_float_clock_accepted_without_promoting_authority():
    result = assess(fresh(), now=NOW + 0.5)
    assert result["state"] == "candidate"
    assert result["cutover_authorized"] is False


def test_no_subprocess_or_provider_authentication_symbols():
    from obsidian_automation import github_b3_occ_token_inventory as module
    for name in ("run", "subprocess", "revoke", "enable_writer",
                 "connect_nextcloud", "write_snapshot"):
        assert not hasattr(module, name)


def test_raw_token_names_and_ids_never_appear_in_diagnostic():
    rows = (capture("legacy-daily"), capture("legacy-writer", (TOKENS,)))
    result = assess(rows)
    assert "Example" not in str(result)
    assert "42" not in str(result)
