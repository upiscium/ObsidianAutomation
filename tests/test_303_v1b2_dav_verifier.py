"""#303 V1/B2 verifier against an in-process mock DAV transport (no network)."""
from __future__ import annotations

from dataclasses import replace
import socket
import subprocess
from urllib.parse import quote, unquote

import pytest

from obsidian_automation import vault_root_dav_verifier as v
from obsidian_automation.vault_root_preview import RootSelection

INSTANCE = "ocsynthetic01"
PREFIX = "/remote.php/dav/files/synthetic_user/"
ORIGIN = "https://nextcloud.invalid"
ACCOUNT = v.TrustedDavAccount(ORIGIN, PREFIX, INSTANCE)
VERIFIED = v.ProviderProof(*(["VERIFIED"] * 6))


def oc_id(fileid: int) -> str:
    return str(fileid).zfill(8) + INSTANCE


class FakeNextcloud:
    """Path → (fileid, is_dir, mount_type). Paths are decoded, '' is the root."""

    def __init__(self):
        self.nodes = {
            "": (1, True, ""),
            "Vault": (10, True, ""),
            "Vault/Notes": (11, True, ""),
            "Vault/readme.md": (12, False, ""),
            "Other": (20, True, ""),
            "Shared": (30, True, "shared"),
            "a.md": (40, False, ""),
        }
        self.etag = "root-etag-1"
        self.calls: list[tuple[str, str]] = []
        self.on_call = None
        self.row_hook = None
        self.response_hook = None

    def children(self, parent: str) -> list[str]:
        prefix = parent + "/" if parent else ""
        return [p for p in self.nodes if p and p.startswith(prefix)
                and "/" not in p[len(prefix):]]

    def row(self, path: str) -> str:
        fileid, is_dir, mount = self.nodes[path]
        href = PREFIX + "/".join(quote(p, safe="") for p in path.split("/")) if path else PREFIX
        if path and is_dir:
            href += "/"
        rtype = "<d:collection/>" if is_dir else ""
        etag = self.etag if path == "" else f"etag-{fileid}"
        props = (f"<d:resourcetype>{rtype}</d:resourcetype><d:getetag>&quot;{etag}&quot;</d:getetag>"
                 f"<oc:fileid>{fileid}</oc:fileid><oc:id>{oc_id(fileid)}</oc:id>"
                 f"<nc:mount-type>{mount}</nc:mount-type>")
        text = (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
                f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
        return self.row_hook(path, text) if self.row_hook else text

    def propfind(self, origin, href, depth, body, *, timeout, max_bytes):
        assert origin == ORIGIN and body == v.PROPFIND_BODY
        assert 0 < timeout <= v.PER_CALL_TIMEOUT_S and max_bytes == v.MAX_PAGE_BYTES
        self.calls.append((href, depth))
        if self.on_call:
            self.on_call(len(self.calls))
        parent = unquote(href[len(PREFIX):]).rstrip("/")
        paths = [parent] + (self.children(parent) if depth == "1" else [])
        xml = ('<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" '
               'xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">'
               + "".join(self.row(p) for p in paths) + "</d:multistatus>")
        response = v.DavResponse(207, "application/xml; charset=utf-8", "", False,
                                 xml.encode("utf-8"))
        return self.response_hook(response) if self.response_hook else response


def selection(requested="Vault/Notes", **kw):
    base = dict(requested=requested, active="Other", active_resource_id=oc_id(20),
                expected_revision="rev1", current_revision="rev1", namespace=INSTANCE,
                container_resource_id=oc_id(1), jobs_in_flight=False)
    base.update(kw)
    return RootSelection(**base)


def run(nc, sel=None, *, proof=VERIFIED, revision=lambda: "rev1", clock=None):
    kwargs = {"source_revision": revision, "proof": proof}
    if clock:
        kwargs["clock"] = clock
    return v.verify_root_candidate(sel or selection(), ACCOUNT, nc, **kwargs)


def test_valid_candidate_walks_each_level_then_rechecks_root():
    nc = FakeNextcloud()
    result = run(nc)
    assert (result.status, result.reason) == ("candidate_valid",
                                              "requires_revalidation_and_activation")
    assert result.writes_vault is False and result.activation_allowed is False
    assert nc.calls == [(PREFIX, "1"), (PREFIX + "Vault/", "1"), (PREFIX, "0")]


@pytest.mark.parametrize("proof", [
    v.CURRENT_PROVIDER_PROOF,
    replace(VERIFIED, C="UNKNOWN"),
    replace(VERIFIED, X="OBSERVED_WITH_LIMITS"),
    None,
])
def test_unproven_provider_makes_zero_transport_calls(proof):  # P3-S8
    nc = FakeNextcloud()
    assert run(nc, proof=proof).reason == "provider_unproven"
    assert nc.calls == []


def test_production_register_matches_owner_acceptance():
    # 2026-11-13: owner accepted I/X/L/R (scope V1-S1). A and C are not VERIFIED,
    # so neither the full nor the restricted contract admits provider I/O.
    p = v.CURRENT_PROVIDER_PROOF
    assert (p.I, p.X, p.L, p.R) == ("VERIFIED",) * 4
    assert p.A != "VERIFIED" and p.C != "VERIFIED"
    assert p.all_verified() is False
    assert p.admits(restricted_coherence=True) is False
    nc = FakeNextcloud()
    result = v.verify_root_candidate(selection(), ACCOUNT, nc, source_revision=lambda: "rev1")
    assert result.reason == "provider_unproven" and nc.calls == []


def test_revision_rotated_during_walk_is_stale():  # P3-S1
    nc = FakeNextcloud()
    values = iter(["rev1", "rev2"])
    assert run(nc, revision=lambda: next(values)).reason == "stale_source"


def test_revision_mismatch_before_walk_has_no_io():
    nc = FakeNextcloud()
    assert run(nc, revision=lambda: "rev0").reason == "stale_source"
    assert nc.calls == []


def test_root_etag_change_during_walk_blocks():  # P3-S2
    nc = FakeNextcloud()

    def mutate(n):
        if n == 2:
            nc.etag = "root-etag-2"
    nc.on_call = mutate
    assert run(nc).reason == "snapshot_unverified"


def test_oc_id_instance_mismatch_blocks():  # P3-S3
    nc = FakeNextcloud()
    nc.row_hook = lambda path, t: t.replace(oc_id(11), str(11).zfill(8) + "ocother") \
        if path == "Vault/Notes" else t
    assert run(nc).reason == "identity_unverified"


def test_oc_id_fileid_mismatch_blocks():  # P3-S4
    nc = FakeNextcloud()
    nc.row_hook = lambda path, t: t.replace(f"<oc:fileid>10<", "<oc:fileid>99<") \
        if path == "Vault" else t
    assert run(nc).reason == "identity_unverified"


def test_mounted_target_is_unsafe():  # P3-S5
    assert run(FakeNextcloud(), selection("Shared")).reason == "unsafe_target"


def test_descendant_of_a_mount_is_blocked_at_the_mount_root():
    # P3-D2 (2026-11-12, NC 35.0.2): descendants of a received share also
    # report nc:mount-type "shared"; the walk already stops at the mount root.
    nc = FakeNextcloud()
    nc.nodes["Shared/Inner"] = (31, True, "shared")
    assert run(nc, selection("Shared/Inner")).reason == "unsafe_target"
    assert [href for href, _ in nc.calls] == [PREFIX]


def test_leaf_reporting_a_mount_type_is_unsafe():
    nc = FakeNextcloud()
    nc.nodes["Vault/Inner"] = (31, True, "shared")
    assert run(nc, selection("Vault/Inner")).reason == "unsafe_target"
    nc.nodes["Vault/Inner"] = (31, True, "")
    assert run(nc, selection("Vault/Inner")).status == "candidate_valid"


def test_mounted_unselected_sibling_is_allowed():
    assert run(FakeNextcloud(), selection("Vault")).status == "candidate_valid"


@pytest.mark.parametrize("prop,reason", [
    ("nc:mount-type", "alias_unverified"),
    ("oc:id", "identity_unverified"),
    ("oc:fileid", "identity_unverified"),
])
def test_required_property_in_404_propstat_blocks(prop, reason):  # P3-S6
    nc = FakeNextcloud()

    def hook(path, text):
        if path != "Vault":
            return text
        start = text.index("<" + prop + ">")
        end = text.index("</" + prop + ">") + len(prop) + 3
        text = text[:start] + text[end:]
        return text.replace("</d:propstat>", "</d:propstat><d:propstat><d:prop><"
                            + prop + "/></d:prop><d:status>HTTP/1.1 404 Not Found"
                            "</d:status></d:propstat>", 1)
    nc.row_hook = hook
    assert run(nc).reason == reason


@pytest.mark.parametrize("change,reason", [
    (lambda r: replace(r, status=301), "provider_redirect"),
    (lambda r: replace(r, redirected=True), "provider_redirect"),
    (lambda r: replace(r, status=401), "provider_unavailable"),
    (lambda r: replace(r, status=403), "provider_unavailable"),
    (lambda r: replace(r, status=404), "provider_unavailable"),
    (lambda r: replace(r, status=409), "provider_unavailable"),
    (lambda r: replace(r, status=429), "provider_unavailable"),
    (lambda r: replace(r, status=503), "provider_unavailable"),
    (lambda r: replace(r, content_encoding="gzip"), "invalid_dav_response"),
    (lambda r: replace(r, content_type="text/html"), "invalid_dav_response"),
    (lambda r: replace(r, body=b"x" * (v.MAX_PAGE_BYTES + 1)), "limit_exceeded"),
])
def test_transport_failures_are_typed_and_not_retried(change, reason):  # P3-S7
    nc = FakeNextcloud()
    nc.response_hook = change
    assert run(nc).reason == reason
    assert len(nc.calls) == 1


@pytest.mark.parametrize("kind,reason", [
    ("timeout", "provider_timeout"), ("oversize", "limit_exceeded"),
    ("connect", "provider_unavailable"),
])
def test_transport_errors_map_to_fixed_reasons(kind, reason):
    nc = FakeNextcloud()

    def fail(_):
        raise v.DavTransportError(kind)
    nc.on_call = fail
    assert run(nc).reason == reason
    assert len(nc.calls) == 1


def test_total_budget_exhaustion_stops_walk():
    ticks = iter([0.0, 0.0, 100.0, 100.0])
    nc = FakeNextcloud()
    assert run(nc, clock=lambda: next(ticks)).reason == "provider_timeout"
    assert len(nc.calls) == 1


def test_expected_resource_id_pins_target():
    nc = FakeNextcloud()
    assert v.verify_root_candidate(
        selection(), ACCOUNT, nc, source_revision=lambda: "rev1", proof=VERIFIED,
        expected_resource_id=oc_id(11)).status == "candidate_valid"
    nc.nodes["Vault/Notes"] = (99, True, "")  # deleted and recreated, same name
    assert v.verify_root_candidate(
        selection(), ACCOUNT, nc, source_revision=lambda: "rev1", proof=VERIFIED,
        expected_resource_id=oc_id(11)).reason == "identity_unverified"


def test_closing_root_check_requires_same_resource():
    nc = FakeNextcloud()

    def swap(n):
        if n == 3:
            nc.nodes[""] = (2, True, "")
    nc.on_call = swap
    assert run(nc).reason == "identity_unverified"


def test_out_of_scope_href_blocks():
    nc = FakeNextcloud()
    nc.row_hook = lambda path, t: t.replace(PREFIX + "Other/", "/remote.php/dav/files/x/Other/") \
        if path == "Other" else t
    assert run(nc).reason == "out_of_scope"


def test_self_row_identity_must_match_container():
    nc = FakeNextcloud()
    assert run(nc, selection(container_resource_id=oc_id(2))).reason == "identity_unverified"


def test_casefold_collision_is_delegated_to_v1a():
    nc = FakeNextcloud()
    nc.nodes["vault"] = (50, True, "")
    assert run(nc).reason == "casefold_collision"


def test_duplicate_identity_across_levels_is_alias():
    nc = FakeNextcloud()
    nc.nodes["Vault/readme.md"] = (20, False, "")
    assert run(nc).reason == "identity_alias"


def test_file_target_and_missing_target():
    assert run(FakeNextcloud(), selection("a.md")).reason == "unsafe_target"
    assert run(FakeNextcloud(), selection("Missing")).reason == "candidate_missing"


def test_namespace_and_account_binding():
    nc = FakeNextcloud()
    assert run(nc, selection(namespace="ocother")).reason == "out_of_scope"
    bad = v.TrustedDavAccount("http://nextcloud.invalid", PREFIX, INSTANCE)
    assert v.verify_root_candidate(selection(), bad, nc, source_revision=lambda: "rev1",
                                   proof=VERIFIED).reason == "account_unverified"
    assert nc.calls == []


def test_no_op_and_jobs_in_flight_need_no_io():
    nc = FakeNextcloud()
    assert run(nc, selection("Other")).status == "no_change"
    assert run(nc, selection(jobs_in_flight=True)).reason == "jobs_in_flight"
    assert nc.calls == []


def test_result_never_contains_provider_values():
    result = run(FakeNextcloud())
    text = repr(result)
    for secret in ("Vault", INSTANCE, "etag", PREFIX, ORIGIN):
        assert secret not in text


def test_verifier_has_no_network_or_process_access(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("I/O attempted")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert run(FakeNextcloud()).status == "candidate_valid"


RESTRICTED = replace(VERIFIED, C="UNKNOWN")


def test_restricted_mode_needs_owner_flag_and_other_gates():
    nc = FakeNextcloud()
    assert run(nc, proof=RESTRICTED).reason == "provider_unproven"
    for gate in ("A", "I", "X", "L", "R"):
        result = v.verify_root_candidate(
            selection(), ACCOUNT, nc, source_revision=lambda: "rev1",
            proof=replace(RESTRICTED, **{gate: "UNKNOWN"}), restricted_coherence=True)
        assert result.reason == "provider_unproven"
    assert nc.calls == []
    assert v.CURRENT_PROVIDER_PROOF.admits(restricted_coherence=True) is False


def test_restricted_mode_uses_v1a_without_coherence_claim(monkeypatch):
    seen = []
    monkeypatch.setattr(v, "preview_from_verified_dav_pages",
                        lambda *a: seen.append(a) or pytest.fail("adapter used"))
    preview, chain = v.verify_root_chain(
        selection(), ACCOUNT, FakeNextcloud(), source_revision=lambda: "rev1",
        proof=RESTRICTED, restricted_coherence=True)
    assert preview.status == "candidate_valid" and chain == (oc_id(10), oc_id(11))
    assert seen == []


def test_restricted_mode_still_blocks_on_observed_change():
    nc = FakeNextcloud()

    def mutate(n):
        if n == 2:
            nc.etag = "root-etag-2"
    nc.on_call = mutate
    assert v.verify_root_candidate(
        selection(), ACCOUNT, nc, source_revision=lambda: "rev1", proof=RESTRICTED,
        restricted_coherence=True).reason == "snapshot_unverified"


def test_chain_is_withheld_unless_candidate_valid():
    preview, chain = v.verify_root_chain(
        selection("Shared"), ACCOUNT, FakeNextcloud(), source_revision=lambda: "rev1",
        proof=VERIFIED)
    assert preview.reason == "unsafe_target" and chain is None
