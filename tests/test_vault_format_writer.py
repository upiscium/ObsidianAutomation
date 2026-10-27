"""Synthetic-only acceptance for guarded Phase1 Vault formatter writer.

No external network access, credentials, Nextcloud writes, or production timers.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from obsidian_automation.core_promotion_transport import HTTPResponse, PromotionTransportNetworkError
from obsidian_automation.vault_format_writer import (
    FormatWriteError, apply_plan,
)
from obsidian_automation.vault_formatter import create_plan

RAW = b"# Title\nparagraph\n  \nmore\n"
DESIRED = b"# Title\nparagraph\n\nmore\n"
SECOND_RAW = b"Second\n  \nEnd\n"
BASE_URL = "https://dav.invalid/remote.php/dav/files/user/Vault"
SOURCE = "98-System/03-template/99-startup/startup-mobile.md"
SECOND = "98-System/another.md"
CREDENTIAL = "SYNTHETIC-PRIVATE-PASSWORD"


class SyntheticDav:
    def __init__(self, files=None):
        self.files = files or {SOURCE: RAW}
        self.etags = {path: '"v1"' for path in self.files}
        self.calls = []
        self.weak = set()
        self.put_error = None
        self.readback_error = False
        self.forced_status = None
        self.force_on_path = None
        self.mutate_on_put = False
        self.update_before_error = False
        self.corrupt_readback = False
        self._put_count = 0

    def __call__(self, *, method, target_url, username, password, headers, body, timeout,
                 response_limit):
        assert username == "user" and password == CREDENTIAL
        assert target_url.startswith(BASE_URL + "/98-System/")
        assert 0 < timeout <= 60
        assert response_limit > 0
        relative = target_url.split("/Vault/", 1)[-1]
        relative = relative.replace("%20", " ")
        self.calls.append((method, relative, headers, body))
        if method == "GET":
            if self.readback_error and self._put_count:
                raise OSError("synthetic readback network loss")
            if relative not in self.files:
                return HTTPResponse(status=404, body=b"", etag=None)
            if self.corrupt_readback and self._put_count:
                return HTTPResponse(status=200, body=b"corrupt-readback", etag='"v2"')
            return HTTPResponse(
                status=200, body=self.files[relative],
                etag='W/"weak"' if relative in self.weak else self.etags.get(relative)
            )
        assert method == "PUT"
        self._put_count += 1
        assert headers.get("If-Match")
        assert headers.get("Content-Type") == "text/markdown; charset=utf-8"
        assert isinstance(body, bytes)
        if self.mutate_on_put:
            self.files[relative] = b"human concurrent change\n"
            self.etags[relative] = '"v-concurrent"'
        if headers["If-Match"] != self.etags.get(relative):
            return HTTPResponse(status=412, body=b"", etag=self.etags.get(relative))
        if self.forced_status and (self.force_on_path is None or self.force_on_path == relative):
            return HTTPResponse(status=self.forced_status, body=b"", etag=self.etags.get(relative))
        if self.put_error == "before":
            raise PromotionTransportNetworkError("synthetic timeout before effect")
        self.files[relative] = body
        self.etags[relative] = '"v2"'
        if self.put_error == "after":
            raise PromotionTransportNetworkError("synthetic timeout after effect")
        return HTTPResponse(status=204, body=b"", etag='"v2"')

    @property
    def puts(self):
        return [x for x in self.calls if x[0] == "PUT"]


@pytest.fixture
def plan_vault(tmp_path):
    root = tmp_path / "source-vault"
    root.mkdir()
    target = root / SOURCE
    target.parent.mkdir(parents=True)
    target.write_bytes(RAW)
    output = create_plan(root)
    assert output["counts"]["would_change"] == 1
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    return root, output, state


def attempt(plan_vault, dav, **kwargs):
    _, plan, state = plan_vault
    return apply_plan(
        plan,
        selected_paths=[SOURCE],
        enabled=True,
        cas_server_verified=True,
        receipt_dir=state,
        base_url=BASE_URL,
        username="user",
        password=CREDENTIAL,
        transport=dav,
        **kwargs,
    )


def test_disabled_default_has_no_remote_calls_and_no_private_writes(plan_vault):
    source, plan, state = plan_vault
    dav = SyntheticDav()
    result = apply_plan(plan, selected_paths=[SOURCE], transport=dav)
    assert result["status"] == "disabled"
    assert result["vault_modified"] is False
    assert not dav.calls
    assert list(state.iterdir()) == []
    assert (source / SOURCE).read_bytes() == RAW


def test_enabling_requires_server_cas_attestation_and_private_receipt_root(plan_vault):
    _, plan, state = plan_vault
    dav = SyntheticDav()
    with pytest.raises(FormatWriteError, match="server_cas_not_verified"):
        apply_plan(plan, selected_paths=[SOURCE], enabled=True, transport=dav)
    assert not dav.calls
    with pytest.raises(FormatWriteError, match="receipt_root_required"):
        apply_plan(plan, selected_paths=[SOURCE], enabled=True, cas_server_verified=True,
                   username="user", password=CREDENTIAL, base_url=BASE_URL, transport=dav)
    assert not dav.calls
    with pytest.raises(FormatWriteError, match="invalid_private_connection"):
        apply_plan(plan, selected_paths=[SOURCE], enabled=True, cas_server_verified=True,
                   receipt_dir=state, username="user", password=CREDENTIAL,
                   base_url="http://dav.invalid/unsafe", transport=dav)
    assert not dav.calls


def test_conditional_put_readback_and_immutable_receipts(plan_vault):
    root, plan, state = plan_vault
    dav = SyntheticDav()
    result = attempt(plan_vault, dav, operation_id="a" * 32)
    assert result["status"] == "complete"
    assert result["vault_modified"] is True
    assert result["outcomes"][0]["result"] == "applied"
    assert dav.files[SOURCE] == DESIRED
    assert len(dav.puts) == 1
    assert dav.puts[0][2]["If-Match"] == '"v1"'
    assert (root / SOURCE).read_bytes() == RAW
    artifacts = list(state.glob("*.vault-formatter-receipt.json"))
    assert len(artifacts) == 3
    phases = {json.loads(p.read_bytes())["stage"] for p in artifacts}
    assert phases == {"accepted", "attempting", "complete"}
    joined = b"".join(p.read_bytes() for p in artifacts)
    assert BASE_URL.encode() not in joined
    assert CREDENTIAL.encode() not in joined
    assert RAW not in joined
    assert DESIRED not in joined
    assert (state / f"operation-{'a' * 32}.marker").is_file()
    with pytest.raises(FormatWriteError, match="operation_replay_blocked"):
        attempt(plan_vault, dav, operation_id="a" * 32)
    assert len(dav.puts) == 1


def test_already_desired_has_no_put_even_without_etag(plan_vault):
    dav = SyntheticDav({SOURCE: DESIRED})
    dav.etags[SOURCE] = None
    result = attempt(plan_vault, dav)
    assert result["status"] == "complete"
    assert result["outcomes"][0]["result"] == "already_desired"
    assert result["vault_modified"] is False
    assert not dav.puts


@pytest.mark.parametrize("change", [
    lambda p: p.update(plan_sha256="0" * 64),
    lambda p: p["results"][0].update(path="../private.md"),
    lambda p: p["results"][0].update(status="error"),
    lambda p: p["results"][0].update(after_sha256="0" * 64),
])
def test_tampered_plan_or_wrong_scope_never_fetches(plan_vault, change):
    _, original, state = plan_vault
    plan = copy.deepcopy(original)
    change(plan)
    dav = SyntheticDav()
    with pytest.raises(FormatWriteError):
        apply_plan(plan, selected_paths=[SOURCE], enabled=True, cas_server_verified=True,
                   receipt_dir=state, base_url=BASE_URL, username="user",
                   password=CREDENTIAL, transport=dav)
    assert not dav.calls


def test_not_selected_target_never_requested(plan_vault):
    _, plan, state = plan_vault
    dav = SyntheticDav()
    with pytest.raises(FormatWriteError, match="non_changing_or_unknown_target"):
        apply_plan(plan, selected_paths=["98-System/different.md"], enabled=True,
                   cas_server_verified=True, receipt_dir=state,
                   base_url=BASE_URL, username="user", password=CREDENTIAL, transport=dav)
    assert not dav.calls


@pytest.mark.parametrize("remote,etype,reason", [
    (b"human concurrent change\n", '"v2"', "source_changed"),
    (RAW, None, "weak_or_missing_etag"),
    (RAW, 'W/"weak"', "weak_or_missing_etag"),
])
def test_preflight_conflicts_have_no_put(plan_vault, remote, etype, reason):
    dav = SyntheticDav({SOURCE: remote})
    dav.etags[SOURCE] = etype
    if etype and etype.startswith("W/"):
        dav.weak.add(SOURCE)
    result = attempt(plan_vault, dav)
    assert result["status"] == "blocked"
    assert result["outcomes"][0]["result"] == reason
    assert not dav.puts


def test_not_found_does_not_create(plan_vault):
    dav = SyntheticDav({SECOND: SECOND_RAW})
    result = attempt(plan_vault, dav)
    assert result["status"] == "blocked"
    assert result["outcomes"][0]["result"] == "remote_missing_or_rejected"
    assert not dav.puts


def test_preflight_all_selected_paths_before_any_put(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for path, data in ((SOURCE, RAW), (SECOND, SECOND_RAW)):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    plan = create_plan(root)
    state = tmp_path / "receipt"
    state.mkdir(mode=0o700)
    dav = SyntheticDav({SOURCE: RAW})
    result = apply_plan(plan, selected_paths=[SECOND, SOURCE], enabled=True,
                        cas_server_verified=True, receipt_dir=state, base_url=BASE_URL,
                        username="user", password=CREDENTIAL, transport=dav)
    assert result["status"] == "blocked"
    assert len([call for call in dav.calls if call[0] == "GET"]) == 2
    assert not dav.puts
    assert dav.files[SOURCE] == RAW


def test_post_preflight_412_preserves_human_edit(plan_vault):
    dav = SyntheticDav()
    dav.mutate_on_put = True
    result = attempt(plan_vault, dav)
    assert result["status"] == "blocked"
    assert result["outcomes"][0]["result"] == "cas_conflict"
    assert dav.files[SOURCE] == b"human concurrent change\n"
    assert len(dav.puts) == 1


@pytest.mark.parametrize("at,expected", [("before", "indeterminate"), ("after", "recovered")])
def test_ambiguous_put_is_read_back_without_resend(plan_vault, at, expected):
    dav = SyntheticDav()
    dav.put_error = at
    result = attempt(plan_vault, dav)
    assert result["status"] == ("indeterminate" if at == "before" else "complete")
    assert result["outcomes"][0]["result"] == expected
    assert len(dav.puts) == 1
    artifacts = list(plan_vault[2].glob("*.vault-formatter-receipt.json"))
    assert any(json.loads(p.read_bytes())["stage"] == "attempting" for p in artifacts)


def test_successful_put_without_matching_readback_is_not_success(plan_vault):
    dav = SyntheticDav()
    dav.readback_error = True
    result = attempt(plan_vault, dav)
    assert result["status"] == "indeterminate"
    assert result["vault_modified"] is None  # remote write is uncertain
    assert len(dav.puts) == 1
    assert dav.files[SOURCE] == DESIRED


def test_receipt_dir_must_be_private_and_no_symlink(plan_vault, tmp_path):
    _, plan, state = plan_vault
    dav = SyntheticDav()
    os.chmod(state, 0o755)
    with pytest.raises(FormatWriteError, match="unsafe_receipt_permissions"):
        attempt(plan_vault, dav)
    os.chmod(state, 0o700)
    link = tmp_path / "link"
    link.symlink_to(state, target_is_directory=True)
    with pytest.raises(FormatWriteError, match="unsafe_receipt_symlink"):
        apply_plan(plan, selected_paths=[SOURCE], enabled=True,
                   cas_server_verified=True, receipt_dir=link, base_url=BASE_URL,
                   username="user", password=CREDENTIAL, transport=dav)
    assert not dav.calls


def test_after_first_verified_write_a_later_conflict_is_partial(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for path, data in ((SOURCE, RAW), (SECOND, SECOND_RAW)):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    plan = create_plan(root)
    state = tmp_path / "receipts"
    state.mkdir(mode=0o700)
    dav = SyntheticDav({SOURCE: RAW, SECOND: SECOND_RAW})
    dav.forced_status = 412
    dav.force_on_path = SECOND
    result = apply_plan(plan, selected_paths=[SOURCE, SECOND], enabled=True,
                        cas_server_verified=True, receipt_dir=state, base_url=BASE_URL,
                        username="user", password=CREDENTIAL, transport=dav)
    assert result["status"] == "partial"
    assert result["vault_modified"] is True
    assert [x["result"] for x in result["outcomes"]] == ["applied", "cas_conflict"]
    assert dav.files[SOURCE] == DESIRED
    assert dav.files[SECOND] == SECOND_RAW
    assert len(dav.puts) == 2
    # All authoritative source bytes remain unaffected by a remote operation.
    assert (root / SOURCE).read_bytes() == RAW
    assert (root / SECOND).read_bytes() == SECOND_RAW


def test_unexpected_success_code_does_not_automatically_count_as_applied(plan_vault):
    dav = SyntheticDav()
    dav.forced_status = 201
    result = attempt(plan_vault, dav)
    assert result["status"] == "indeterminate"
    assert result["outcomes"][0]["result"] == "indeterminate"
    assert result["vault_modified"] is None


def test_invalid_bool_authorization_and_duplicate_selection_are_denied(plan_vault):
    _, plan, state = plan_vault
    dav = SyntheticDav()
    with pytest.raises(FormatWriteError, match="invalid_authorization_mode"):
        apply_plan(plan, selected_paths=[SOURCE], enabled="true",  # type: ignore
                   cas_server_verified=True, receipt_dir=state, transport=dav)
    with pytest.raises(FormatWriteError, match="invalid_selection"):
        apply_plan(plan, selected_paths=[SOURCE, SOURCE], enabled=True,
                   cas_server_verified=True, receipt_dir=state, transport=dav)
    assert not dav.calls


def test_receipt_directory_replaced_during_get_never_allows_put(plan_vault):
    _, plan, private_root = plan_vault
    moved_root = private_root.with_name(private_root.name + "-old")
    dav = SyntheticDav()
    switched = [False]

    def transport(**kwargs):
        if kwargs["method"] == "GET" and not switched[0]:
            private_root.rename(moved_root)
            private_root.mkdir(mode=0o700)
            switched[0] = True
        return dav(**kwargs)

    with pytest.raises(FormatWriteError, match="receipt_root_changed"):
        apply_plan(
            plan, selected_paths=[SOURCE], enabled=True,
            cas_server_verified=True, receipt_dir=private_root,
            base_url=BASE_URL, username="user", password=CREDENTIAL,
            transport=transport, operation_id="c" * 32,
        )
    assert switched[0]
    assert dav.files[SOURCE] == RAW
    assert not dav.puts
    # The original pinned directory retains the operation marker and
    # accepted receipt; the replacement cannot receive private receipts.
    assert (moved_root / ("operation-" + "c" * 32 + ".marker")).is_file()
    assert len(list(moved_root.glob("*.vault-formatter-receipt.json"))) == 1
    assert list(private_root.iterdir()) == []


def test_untrusted_writable_receipt_parent_is_rejected_without_http(
        plan_vault, tmp_path):
    _, plan, _ = plan_vault
    parent = tmp_path / "mutable-parent"
    parent.mkdir(mode=0o700)
    receipt = parent / "receipts"
    receipt.mkdir(mode=0o700)
    dav = SyntheticDav()
    try:
        parent.chmod(0o777)
        with pytest.raises(FormatWriteError, match="unsafe_receipt_ancestor"):
            apply_plan(
                plan, selected_paths=[SOURCE], enabled=True,
                cas_server_verified=True, receipt_dir=receipt,
                base_url=BASE_URL, username="user", password=CREDENTIAL,
                transport=dav,
            )
    finally:
        parent.chmod(0o700)
    assert not dav.calls
    assert dav.files[SOURCE] == RAW


def test_private_receipts_use_owned_dir_and_secure_file_modes(plan_vault):
    _, _, root = plan_vault
    dav = SyntheticDav()
    result = attempt(plan_vault, dav)
    assert result["status"] == "complete"
    for path in root.iterdir():
        assert path.is_file() and not path.is_symlink()
        assert path.stat().st_mode & 0o077 == 0
    assert not list(root.glob(".formatter-receipt-*.tmp"))
