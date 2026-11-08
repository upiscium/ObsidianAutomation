"""#303 V1/B: synthetic DAV frames only. No real cloud host or credentials."""
from dataclasses import asdict, replace
from pathlib import Path
import json
import socket
import subprocess
import urllib.request

import pytest

from obsidian_automation.vault_root_preview import RootSelection
from obsidian_automation.vault_root_dav_adapter import (
    MAX_PAGE_BYTES, MAX_TOTAL_BYTES,
    VerifiedDavEntry as Entry, VerifiedDavPage as Page,
    preview_from_verified_dav_pages as preview,
)


def request(**changes):
    d = dict(
        requested="Archive/Vault", active="Vault", active_resource_id="obj-old",
        expected_revision="rev-a", current_revision="rev-a",
        namespace="account-a", container_resource_id="obj-root",
    )
    d.update(changes)
    return RootSelection(**d)


def entry(name, identity, kind="directory", **changes):
    d = dict(name=name, resource_id=identity, kind=kind, is_alias=False,
             canonical_identity_verified=True, alias_classification_verified=True)
    d.update(changes)
    return Entry(**d)


def pages():
    return (
        Page(
            parent="", parent_resource_id="obj-root", namespace="account-a",
            source_revision="rev-a", snapshot_token="snap-a",
            cursor=None, next_cursor=None, http_status=207, redirected=False,
            content_bytes=220, complete=True,
            identity_scope_verified=True, coherent_snapshot_verified=True,
            rows=(entry("Vault", "obj-old"), entry("Archive", "obj-archive")),
        ),
        Page(
            parent="Archive", parent_resource_id="obj-archive",
            namespace="account-a", source_revision="rev-a",
            snapshot_token="snap-a", cursor=None, next_cursor=None,
            http_status=207, redirected=False, content_bytes=180, complete=True,
            identity_scope_verified=True, coherent_snapshot_verified=True,
            rows=(entry("Vault", "obj-new"), entry("Other", "obj-other")),
        ),
    )


def blocked(result, why):
    assert (result.status, result.reason) == ("blocked", why)
    assert result.writes_vault is False
    assert result.activation_allowed is False
    assert result.requires_explicit_activation is True
    assert set(asdict(result)) == {
        "version", "status", "reason", "writes_vault",
        "activation_allowed", "requires_explicit_activation",
    }


def test_verified_fabricated_pages_delegate_only_preview():
    result = preview(request(), pages())
    assert result.status == "candidate_valid"
    assert result.reason == "requires_revalidation_and_activation"
    assert result.activation_allowed is False
    assert result.writes_vault is False


def test_same_root_is_noop_not_provider_proof():
    result = preview(request(requested="Vault"), ())
    assert (result.status, result.reason) == ("no_change", "already_selected")
    assert not result.activation_allowed


def test_stale_source_and_active_jobs_precede_any_page_iteration():
    blocked(preview(request(current_revision="rev-b"), pages()), "stale_source")
    blocked(preview(request(jobs_in_flight=True), pages()), "jobs_in_flight")


@pytest.mark.parametrize("status", [0, 200, 201, 204, 301, 302, 307, 308,
                                    400, 401, 403, 404, 409, 412, 423, 429,
                                    500, 502, 503, 504, True, "207"])
def test_only_exact_207_multistatus_is_accepted(status):
    root, nested = pages()
    blocked(preview(request(), (replace(root, http_status=status), nested)),
            "provider_unavailable")


@pytest.mark.parametrize("value", [True, None, 1, "false"])
def test_redirect_or_ambiguous_flag_denied(value):
    root, nested = pages()
    blocked(preview(request(), (replace(root, redirected=value), nested)),
            "provider_redirect")


@pytest.mark.parametrize("change,reason", [
    ({"identity_scope_verified": False}, "identity_unverified"),
    ({"identity_scope_verified": None}, "identity_unverified"),
    ({"coherent_snapshot_verified": False}, "snapshot_unverified"),
    ({"snapshot_token": ""}, "snapshot_unverified"),
    ({"snapshot_token": "etag-not-an-authoritative-revision!"}, "snapshot_unverified"),
    ({"snapshot_token": "snap-b"}, "snapshot_unverified"),
    ({"source_revision": "rev-b"}, "out_of_scope"),
    ({"parent": "Elsewhere"}, "out_of_scope"),
    ({"parent_resource_id": "obj-other"}, "out_of_scope"),
    ({"namespace": "different-account"}, "out_of_scope"),
    ({"content_bytes": MAX_PAGE_BYTES + 1}, "limit_exceeded"),
    ({"content_bytes": -1}, "limit_exceeded"),
    ({"content_bytes": "32"}, "limit_exceeded"),
    ({"rows": ()}, "limit_exceeded"),
    ({"rows": []}, "invalid_inventory"),
    ({"complete": False}, "incomplete_inventory"),
    ({"complete": "true"}, "incomplete_inventory"),
    ({"next_cursor": "cursor-a"}, "incomplete_inventory"),
])
def test_reject_unverified_stale_cross_scope_bad_status(change, reason):
    root, nested = pages()
    blocked(preview(request(), (root, replace(nested, **change))), reason)


@pytest.mark.parametrize("change,reason", [
    ({"canonical_identity_verified": False}, "identity_unverified"),
    ({"alias_classification_verified": False}, "alias_unverified"),
    ({"name": "x" * 256}, "invalid_inventory"),
    ({"name": ""}, "invalid_inventory"),
    ({"resource_id": "path-hash?not-trusted"}, "invalid_inventory"),
    ({"resource_id": None}, "invalid_inventory"),
    ({"kind": "redirect"}, "invalid_inventory"),
    ({"is_alias": None}, "invalid_inventory"),
])
def test_per_entry_evidence_is_checked(change, reason):
    root, nested = pages()
    bad = replace(nested.rows[0], **change)
    blocked(preview(request(), (root, replace(nested, rows=(bad, *nested.rows[1:])))), reason)


@pytest.mark.parametrize("kind,alias,reason", [
    ("file", False, "unsafe_target"),
    ("symlink", False, "unsafe_target"),
    ("unknown", False, "unsafe_target"),
    ("directory", True, "unsafe_target"),
])
def test_selected_alias_and_symlink_never_become_safe(kind, alias, reason):
    root, nested = pages()
    changed = replace(nested.rows[0], kind=kind, is_alias=alias)
    blocked(preview(request(), (root, replace(nested, rows=(changed, *nested.rows[1:])))),
            reason)


def test_propagates_global_resource_id_and_casefold_conflicts():
    root, nested = pages()
    dup = replace(nested.rows[1], resource_id="obj-root")
    blocked(preview(request(), (root, replace(nested, rows=(nested.rows[0], dup)))),
            "identity_alias")
    alias = entry("ｖａｕｌｔ", "obj-different")
    blocked(preview(request(), (root, replace(nested, rows=(*nested.rows, alias)))),
            "casefold_collision")


def test_paginated_inventory_requires_exact_cursor_and_complete_last_page():
    root, nested = pages()
    p1 = replace(root, rows=(root.rows[0],), complete=False, next_cursor="cursor-a")
    p2 = replace(root, rows=(root.rows[1],), complete=True, cursor="cursor-a")
    good = preview(request(), (p1, p2, nested))
    assert good.status == "candidate_valid"
    blocked(preview(request(), (p1, replace(p2, cursor="cursor-b"), nested)),
            "incomplete_inventory")
    blocked(preview(request(), (p1, replace(p2, next_cursor="cursor-b"), nested)),
            "incomplete_inventory")
    blocked(preview(request(), (p1, replace(p2, snapshot_token="snap-b"), nested)),
            "snapshot_unverified")
    blocked(preview(request(), (root, p2, nested)), "out_of_scope")


def test_missing_or_extra_evidence_fails_closed():
    root, nested = pages()
    blocked(preview(request(), ()), "incomplete_inventory")
    blocked(preview(request(), (root,)), "incomplete_inventory")
    blocked(preview(request(), (root, nested, root)), "incomplete_inventory")
    blocked(preview(request(), [root, nested]), "invalid_request")


def test_paging_limit_and_oversize_total_enforced():
    root, nested = pages()
    chunk = replace(root, rows=(entry("Archive", "obj-archive"),),
                    complete=False, next_cursor="cursor-a")
    p2 = replace(root, rows=(entry("Other", "obj-other-2"),),
                 cursor="cursor-a", complete=False, next_cursor="cursor-b")
    p3 = replace(root, rows=(entry("Other2", "obj-other-3"),),
                 cursor="cursor-b", complete=False, next_cursor="cursor-c")
    p4 = replace(root, rows=(entry("Other3", "obj-other-4"),),
                 cursor="cursor-c", complete=False, next_cursor="cursor-d")
    blocked(preview(request(), (chunk, p2, p3, p4, nested)), "incomplete_inventory")
    bloated = replace(root, content_bytes=MAX_PAGE_BYTES)
    other = replace(nested, content_bytes=MAX_PAGE_BYTES)
    assert preview(request(), (bloated, other)).status == "candidate_valid"
    assert MAX_TOTAL_BYTES >= MAX_PAGE_BYTES * 2


def test_duplicate_cursor_and_duplicate_page_resource_id_rejected():
    root, nested = pages()
    p1 = replace(root, rows=(root.rows[0],), complete=False, next_cursor="cursor-a")
    p2 = replace(root, cursor="cursor-a", rows=(root.rows[1],),
                 complete=False, next_cursor="cursor-a")
    blocked(preview(request(), (p1, p2, nested)), "incomplete_inventory")
    p1 = replace(p1, rows=(root.rows[1],))
    p2 = replace(p2, complete=True, next_cursor=None, rows=(root.rows[1],))
    blocked(preview(request(), (p1, p2, nested)), "casefold_collision")


def test_fixed_redacted_output_even_when_input_has_sensitive_values():
    root, nested = pages()
    result = preview(request(), (root, replace(nested, namespace="secret-account-id")))
    wire = json.dumps(asdict(result), sort_keys=True)
    for forbidden in ("Archive", "Vault", "secret-account-id", "obj-root", "snap-a", "cursor-a"):
        assert forbidden not in wire


def test_no_file_network_or_process_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected provider/FS/process I/O")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    assert preview(request(), pages()).status == "candidate_valid"


class SpoofedScope(str):
    def __ne__(self, _other):
        return False


class RaisingScope(str):
    def __ne__(self, _other):
        raise AssertionError("untrusted-scope-secret-must-never-run")


@pytest.mark.parametrize("field", ["parent_resource_id", "namespace", "source_revision"])
@pytest.mark.parametrize("bad", [SpoofedScope("other-scope"), RaisingScope("secret"), None, 42])
def test_scope_fields_require_exact_str_before_comparison(field, bad):
    root, nested = pages()
    blocked(preview(request(), (root, replace(nested, **{field: bad}))),
            "invalid_inventory")


def test_reported_total_byte_budget_at_exact_limit_and_one_over():
    root, nested = pages()
    a = replace(root, rows=(root.rows[0],), complete=False,
                content_bytes=MAX_PAGE_BYTES, next_cursor="cursor-a")
    b = replace(root, rows=(root.rows[1],), complete=False,
                content_bytes=MAX_PAGE_BYTES, cursor="cursor-a",
                next_cursor="cursor-b")
    c = replace(root, rows=(entry("extra-one", "obj-extra-one"),),
                complete=False, content_bytes=MAX_PAGE_BYTES,
                cursor="cursor-b", next_cursor="cursor-c")
    d = replace(root, rows=(entry("extra-two", "obj-extra-two"),),
                complete=True, content_bytes=MAX_PAGE_BYTES,
                cursor="cursor-c", next_cursor=None)
    exact = (a, b, c, d, replace(nested, content_bytes=0))
    assert MAX_TOTAL_BYTES == MAX_PAGE_BYTES * 4
    assert preview(request(), exact).status == "candidate_valid"
    blocked(preview(request(), (*exact[:-1], replace(exact[-1], content_bytes=1))),
            "limit_exceeded")


def test_sibling_2048_boundary_accumulates_across_pages():
    root, nested = pages()
    extras = tuple(entry(f"Child-{n}", f"obj-child-{n}") for n in range(2046))
    first = replace(root, rows=(root.rows[0], *extras[:1023]),
                    complete=False, next_cursor="cursor-a")
    second = replace(root, rows=(root.rows[1], *extras[1023:]),
                     complete=True, cursor="cursor-a")
    assert len(first.rows) + len(second.rows) == 2048
    assert preview(request(), (first, second, nested)).status == "candidate_valid"
    overflow = replace(second, rows=(*second.rows, entry("overflow", "obj-overflow")))
    blocked(preview(request(), (first, overflow, nested)), "limit_exceeded")


class RaisingRequested(str):
    def __ne__(self, _other):
        raise AssertionError("requested-value-comparison-leaked")


class SpoofCursorMeta(type):
    def __eq__(cls, _other):
        return True


class SpoofCursor(metaclass=SpoofCursorMeta):
    def __ne__(self, _other):
        raise AssertionError("cursor-comparison-leaked")


@pytest.mark.parametrize("field", ["requested", "active"])
def test_request_path_type_checked_before_v1a_comparison(field):
    blocked(preview(request(**{field: RaisingRequested("not-trusted")}), ()),
            "invalid_path")


@pytest.mark.parametrize("field", ["cursor", "next_cursor"])
def test_cursor_type_checked_without_metaclass_equality_hooks(field):
    root, nested = pages()
    blocked(preview(request(), (replace(root, **{field: SpoofCursor()}), nested)),
            "invalid_inventory")


@pytest.mark.parametrize("bad_name", [
    ".", "..", "Nested/Leaf", "/absolute", "contains\x00nul",
])
def test_verified_pages_do_not_accept_non_immediate_sibling_names(bad_name):
    first, second = pages()
    changed = replace(second, rows=second.rows + (
        entry(bad_name, "obj-malformed", kind="file"),
    ))
    blocked(preview(request(), (first, changed)), "invalid_inventory")
