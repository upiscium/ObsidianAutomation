"""#303 V1/B: fabricated DAV fixture contract, no real provider or credentials."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

import pytest

from obsidian_automation.vault_root_dav_fixture import (
    SyntheticClassification, SyntheticDavCapture, SyntheticDavSource,
    preview_synthetic_dav_root,
)
from obsidian_automation.vault_root_preview import RootSelection

PREFIX = "/remote.php/dav/files/synthetic/"
BASE_ID = "file-root"
REVISION = "source-r1"
GENERATION = "gen-r1"
NAMESPACE = "account-synthetic"


def source(**fields):
    data = dict(
        account_prefix=PREFIX, namespace=NAMESPACE,
        root_resource_id=BASE_ID, source_revision=REVISION,
        listing_revision=GENERATION,
    )
    data.update(fields)
    return SyntheticDavSource(**data)


def selection(**fields):
    data = dict(
        requested="Archive/Vault", active="Vault",
        active_resource_id="file-old", expected_revision=REVISION,
        current_revision=REVISION, namespace=NAMESPACE,
        container_resource_id=BASE_ID, jobs_in_flight=False,
    )
    data.update(fields)
    return RootSelection(**data)


def child(name, fileid, kind="directory", *, alias=False):
    return name, fileid, kind, alias


ROOT_CHILDREN = (
    child("Vault", "file-old"),
    child("Archive", "file-archive"),
)
NESTED_CHILDREN = (
    child("Vault", "file-new"),
    child("Other", "file-other"),
)


def canonical_href(parent, name, directory=True):
    parent_href = PREFIX + (
        "/".join(quote(part, safe="") for part in parent.split("/")) + "/"
        if parent else ""
    )
    return parent_href + quote(name, safe="") + ("/" if directory else "")


def dav_row(href, fileid, *, directory=True, status="HTTP/1.1 200 OK",
            include_fileid=True, override_resource_type=None):
    types = ("<d:collection/>" if directory else "")
    if override_resource_type is not None:
        types = override_resource_type
    idxml = f"<oc:fileid>{escape(fileid)}</oc:fileid>" if include_fileid else ""
    return (
        "<d:response>"
        f"<d:href>{escape(href)}</d:href>"
        "<d:propstat><d:prop>"
        f"<d:resourcetype>{types}</d:resourcetype>{idxml}"
        "</d:prop>"
        f"<d:status>{escape(status)}</d:status>"
        "</d:propstat></d:response>"
    )


def frame(parent="", parent_id=BASE_ID, *,
          children=ROOT_CHILDREN, self_id=None, self_href=None,
          row_status="HTTP/1.1 200 OK", parent_included=True):
    href = PREFIX + (
        "/".join(quote(part, safe="") for part in parent.split("/")) + "/"
        if parent else ""
    )
    rows = []
    if parent_included:
        rows.append(dav_row(href if self_href is None else self_href,
                            parent_id if self_id is None else self_id))
    classes = []
    for name, fileid, kind, alias in children:
        directory = kind not in ("file",)
        child_href = canonical_href(parent, name, directory)
        rows.append(dav_row(child_href, fileid, directory=directory,
                            status=row_status))
        classes.append(SyntheticClassification(
            child_href, fileid, kind, alias,
        ))
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            + "".join(rows) + "</d:multistatus>").encode("utf-8")
    return SyntheticDavCapture(
        parent=parent, parent_resource_id=parent_id, namespace=NAMESPACE,
        source_revision=REVISION, listing_revision=GENERATION,
        http_status=207, depth="1", content_type="application/xml",
        redirect_count=0, complete=True, has_more=False,
        content_encoding="identity", body=body, classifications=tuple(classes),
    )


def frames(*, first=None, second=None):
    return (
        first if first is not None else frame(),
        second if second is not None else frame("Archive", "file-archive",
                                                children=NESTED_CHILDREN),
    )


def assert_denied(output, reason):
    assert output.status == "blocked"
    assert output.reason == reason
    assert output.writes_vault is False
    assert output.activation_allowed is False
    assert output.requires_explicit_activation is True


def test_synthetic_207_decodes_two_level_parent_chain_but_cannot_activate():
    result = preview_synthetic_dav_root(selection(), source(), frames())
    assert result.status == "candidate_valid"
    assert result.reason == "requires_revalidation_and_activation"
    assert result.writes_vault is False
    assert result.activation_allowed is False
    assert result.requires_explicit_activation is True
    assert set(asdict(result)) == {
        "version", "status", "reason", "writes_vault", "activation_allowed",
        "requires_explicit_activation",
    }


def test_legacy_no_op_and_inflight_guards_precede_fixture_parsing():
    assert preview_synthetic_dav_root(
        selection(requested="Vault", jobs_in_flight=True), None, (),
    ).status == "no_change"
    assert_denied(preview_synthetic_dav_root(
        selection(jobs_in_flight=True), source(), frames(),
    ), "jobs_in_flight")
    assert_denied(preview_synthetic_dav_root(
        selection(current_revision="stale"), source(), frames(),
    ), "stale_source")
    assert_denied(preview_synthetic_dav_root(
        selection(requested="../Vault"), source(), frames(),
    ), "invalid_path")


@pytest.mark.parametrize("changed,reason", [
    (dict(origin="fake_authenticated"), "unverified_origin"),
    (dict(account_prefix="https://example.invalid/remote.php/dav/files/synthetic/"),
     "unverified_origin"),
    (dict(account_prefix="/remote.php/dav/files/../"), "unverified_origin"),
    (dict(namespace="other-account"), "out_of_scope"),
    (dict(root_resource_id="wrong-id"), "out_of_scope"),
    (dict(source_revision="rev-stale"), "stale_source"),
    (dict(listing_revision="!bad-revision"), "unverified_origin"),
])
def test_source_fixture_contract_is_scoped_and_redacted(changed, reason):
    assert_denied(preview_synthetic_dav_root(
        selection(), source(**changed), frames(),
    ), reason)


@pytest.mark.parametrize("change,reason", [
    (dict(origin="captured_live_dav"), "unverified_origin"),
    (dict(parent="Outside"), "out_of_scope"),
    (dict(parent_resource_id="wrong-parent"), "out_of_scope"),
    (dict(namespace="other-account"), "out_of_scope"),
    (dict(source_revision="source-r2"), "stale_source"),
    (dict(listing_revision="gen-r2"), "stale_inventory"),
    (dict(http_status=401), "provider_unavailable"),
    (dict(http_status=403), "provider_unavailable"),
    (dict(http_status=404), "provider_unavailable"),
    (dict(http_status=429), "provider_unavailable"),
    (dict(http_status=500), "provider_unavailable"),
    (dict(http_status=302), "provider_unavailable"),
    (dict(redirect_count=1), "redirect_disallowed"),
    (dict(depth="0"), "invalid_dav_response"),
    (dict(depth="infinity"), "invalid_dav_response"),
    (dict(content_encoding="gzip"), "invalid_dav_response"),
    (dict(content_type="text/plain"), "invalid_dav_response"),
    (dict(complete=False), "incomplete_inventory"),
    (dict(has_more=True), "incomplete_inventory"),
    (dict(parent_alias=True), "identity_unverified"),
])
def test_bad_transport_and_snapshot_envelopes_fail_closed(change, reason):
    first, second = frames()
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(first, **change), second),
    ), reason)


@pytest.mark.parametrize("changed,reason", [
    (dict(body=b"<d:multistatus"), "invalid_dav_response"),
    (dict(body=b"<!DOCTYPE foo><x/>"), "invalid_dav_response"),
    (dict(body=b"<x/>"), "invalid_dav_response"),
    (dict(body=b"x" * 1_048_577), "invalid_dav_response"),
    (dict(body=b""), "invalid_dav_response"),
    (dict(classifications=()), "identity_unverified"),
    (dict(classifications="untrusted"), "identity_unverified"),
])
def test_malformed_or_oversized_dav_and_missing_classifier_rejected(changed, reason):
    first, second = frames()
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(first, **changed), second),
    ), reason)


def test_incomplete_frame_count_rejected():
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (frames()[0],),
    ), "incomplete_inventory")


@pytest.mark.parametrize("bad_href", [
    "https://evil.invalid/remote.php/dav/files/synthetic/",
    "//evil.invalid/remote.php/dav/files/synthetic/",
    "/remote.php/dav/files/other/",
    "/remote.php/dav/files/synthetic/%2e/",
    "/remote.php/dav/files/synthetic/%2F/",
    "/remote.php/dav/files/synthetic/?token=redacted",
    "/remote.php/dav/files/synthetic/#fragment",
])
def test_parent_self_href_must_be_exact_canonical_account_path(bad_href):
    first = frame(self_href=bad_href)
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(first=first),
    ), "out_of_scope" if bad_href.startswith("/remote.php/dav/files/other/") else
       "unsafe_href")


def test_parent_identity_mismatch_and_missing_parent_are_blocked():
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(first=frame(self_id="wrong-id")),
    ), "identity_unverified")
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(first=frame(parent_included=False)),
    ), "incomplete_inventory")


def test_duplicate_ids_and_cross_level_identity_alias_refused():
    first = frame(children=ROOT_CHILDREN + (child("Third", "file-archive"),))
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(first=first),
    ), "identity_alias")
    second = frame("Archive", "file-archive", children=(
        child("Vault", "file-root"),
        child("Other", "file-other"),
    ))
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(second=second),
    ), "identity_alias")


def test_unselected_unicode_casefold_collision_refused():
    first = frame(children=ROOT_CHILDREN + (
        child("Ａrchive", "file-width"),
    ))
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(first=first),
    ), "casefold_collision")


@pytest.mark.parametrize("selected_kind,alias,reason", [
    ("file", False, "unsafe_target"),
    ("symlink", False, "unsafe_target"),
    ("unknown", False, "unsafe_target"),
    ("directory", True, "unsafe_target"),
])
def test_selected_files_symlinks_unknown_and_aliases_never_pass(
    selected_kind, alias, reason,
):
    second = frame("Archive", "file-archive", children=(
        child("Vault", "file-new", selected_kind, alias=alias),
    ))
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(second=second),
    ), reason)


def test_alias_classification_is_id_bound_not_just_a_name_claim():
    first, second = frames()
    claims = tuple(replace(claim, resource_id="wrong-id")
                   if "Archive" in claim.href else claim
                   for claim in first.classifications)
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(first, classifications=claims), second),
    ), "identity_unverified")


def test_missing_fileid_is_not_invented_from_href_or_etag():
    first, second = frames()
    malformed_xml = first.body.replace(
        b"<oc:fileid>file-archive</oc:fileid>", b"<d:getetag>etag-not-id</d:getetag>",
    )
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(first, body=malformed_xml), second),
    ), "invalid_dav_response")


def test_no_field_or_proof_is_reflected_in_preview():
    item = preview_synthetic_dav_root(
        selection(requested="Archive/Missing"), source(), frames(),
    )
    blob = json.dumps(asdict(item), sort_keys=True)
    for forbidden in (
        "Archive", "Missing", "synthetic", "file-root", "file-new",
        "remote.php", "source-r1", "gen-r1", "password", "oc:fileid",
    ):
        assert forbidden not in blob


def test_dav_fixture_never_opens_network_files_processes_or_writes(monkeypatch):
    import socket
    import subprocess
    import urllib.request

    def deny(*args, **kwargs):
        raise AssertionError("Synthetic DAV parser tried external I/O")

    monkeypatch.setattr(Path, "open", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(urllib.request, "urlopen", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    assert preview_synthetic_dav_root(
        selection(), source(), frames(),
    ).status == "candidate_valid"


@pytest.mark.parametrize("bad_name", [
    "bad\x00name", "bad\nname", "bad\x7fname", "A/B",
])
def test_captured_children_with_controls_or_nested_href_are_not_accepted(bad_name):
    first = frame(children=ROOT_CHILDREN + (child(bad_name, "file-unsafe"),))
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(first=first),
    ), "unsafe_href")


def test_extra_duplicate_dav_response_is_not_silently_accepted():
    first, second = frames()
    duplicate = dav_row(canonical_href("", "Archive"), "file-archive")
    malformed = first.body.replace(b"</d:multistatus>",
                                   duplicate.encode("utf-8") + b"</d:multistatus>")
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(first, body=malformed), second),
    ), "invalid_dav_response")


def test_extra_classifier_rows_are_bounded_before_processing():
    first, second = frames()
    gigantic = first.classifications * 1025
    assert len(gigantic) > 2048
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(first, classifications=gigantic), second),
    ), "incomplete_inventory")


def test_non_200_dav_propstat_is_not_equated_with_successful_identity():
    first = frame(row_status="HTTP/1.1 404 Not Found")
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(first=first),
    ), "provider_unavailable")


def test_unknown_alias_evidence_and_kind_mismatch_both_fail_closed():
    first, second = frames()
    unknown = replace(first.classifications[0], is_alias=None)
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(
            first, classifications=(unknown,) + first.classifications[1:]), second),
    ), "identity_unverified")
    mismatched = replace(first.classifications[0], kind="file")
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(
            first, classifications=(mismatched,) + first.classifications[1:]), second),
    ), "identity_unverified")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-be"])
def test_valid_dav_structure_with_internal_dtd_entity_is_rejected(encoding):
    first, second = frames()
    xml = first.body.decode("utf-8")
    xml = xml.replace(
        '?>', '?><!DOCTYPE d:multistatus [<!ENTITY alias "file-archive">]>', 1,
    )
    xml = xml.replace(
        "<oc:fileid>file-archive</oc:fileid>",
        "<oc:fileid>&alias;</oc:fileid>",
        1,
    )
    xml = xml.replace('encoding="UTF-8"', f'encoding="{encoding.upper()}"')
    raw = xml.encode(encoding)
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(first, body=raw), second),
    ), "invalid_dav_response")


def test_unknown_declared_xml_encoding_is_fixed_redacted_denial():
    first, second = frames()
    xml = first.body.replace(
        b'encoding="UTF-8"', b'encoding="x-no-such-codec"',
    )
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(first, body=xml), second),
    ), "invalid_dav_response")


def test_valid_intermediate_folder_space_and_final_leaf_space():
    first = frame(children=(
        child("Vault", "file-old"), child("Archive One", "file-archive"),
    ))
    second = frame("Archive One", "file-archive", children=NESTED_CHILDREN)
    assert preview_synthetic_dav_root(
        selection(requested="Archive One/Vault"), source(), (first, second),
    ).status == "candidate_valid"
    final = frame("Archive", "file-archive", children=(
        child("Vault One", "file-new"), child("Other", "file-other"),
    ))
    assert preview_synthetic_dav_root(
        selection(requested="Archive/Vault One"), source(), frames(second=final),
    ).status == "candidate_valid"


def test_oversized_account_prefix_is_rejected_prior_to_regex():
    assert_denied(preview_synthetic_dav_root(
        selection(), source(account_prefix="/remote.php/dav/files/" + "a"*100_000 + "/"),
        frames(),
    ), "unverified_origin")


def test_oversized_content_type_header_is_rejected():
    first, second = frames()
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), (replace(
            first, content_type="application/xml" + " "*100_000), second),
    ), "invalid_dav_response")


def test_unknown_unselected_kind_is_not_selection_authority():
    first = frame(children=ROOT_CHILDREN + (
        child("Unclassified", "file-unknown", "unknown"),
    ))
    assert preview_synthetic_dav_root(
        selection(), source(), frames(first=first),
    ).status == "candidate_valid"
    second = frame("Archive", "file-archive", children=(
        child("Vault", "file-new", "unknown"),
    ))
    assert_denied(preview_synthetic_dav_root(
        selection(), source(), frames(second=second),
    ), "unsafe_target")


# #303 V1/B0 integrated follow-up: exact-type validation at the synthetic
# evidence boundary. Dataclass annotations do not validate runtime values.
class _HostileString(str):
    def __ne__(self, _other):
        raise AssertionError("untrusted comparison must not be invoked")

    def __eq__(self, _other):
        raise AssertionError("untrusted comparison must not be invoked")


@pytest.mark.parametrize("field", ["requested", "active"])
def test_selection_path_subclass_is_rejected_before_v1a_comparisons(field):
    hostile = selection(**{field: _HostileString("secret-synthetic-path")})
    assert_denied(preview_synthetic_dav_root(hostile, source(), frames()),
                  "invalid_path")


def test_source_origin_subclass_cannot_spoof_fixture_provenance():
    result = preview_synthetic_dav_root(
        selection(), source(origin=_HostileString("synthetic_fixture")), frames()
    )
    assert_denied(result, "unverified_origin")


@pytest.mark.parametrize("field,reason", [
    ("origin", "unverified_origin"),
    ("parent", "invalid_dav_response"),
    ("parent_resource_id", "invalid_dav_response"),
    ("namespace", "invalid_dav_response"),
    ("source_revision", "invalid_dav_response"),
    ("listing_revision", "invalid_dav_response"),
    ("depth", "invalid_dav_response"),
    ("content_encoding", "invalid_dav_response"),
])
def test_capture_scope_and_transport_fields_are_exact_typed(field, reason):
    first, second = frames()
    altered = replace(first, **{field: _HostileString("synthetic_fixture")})
    assert_denied(
        preview_synthetic_dav_root(selection(), source(), (altered, second)),
        reason,
    )


def test_classification_kind_cannot_bypass_enum_with_hostile_equality():
    first, second = frames()
    altered = replace(
        first,
        classifications=(
            replace(first.classifications[0], kind=_HostileString("directory")),
            *first.classifications[1:],
        ),
    )
    assert_denied(
        preview_synthetic_dav_root(selection(), source(), (altered, second)),
        "identity_unverified",
    )


# Independent V1/B0 review P2/P3: reject malformed-but-well-formed XML
# rather than accepting it as authoritative collection/encoding evidence.
@pytest.mark.parametrize("fragment", [
    b"<d:collection><d:redirectref/></d:collection>",
    b"<d:collection>unexpected</d:collection>",
    b"<d:collection/>unexpected",
    b"<d:collection x:claim='alias' xmlns:x='urn:synthetic'/>",
])
@pytest.mark.parametrize("which", ["parent", "selected_child"])
def test_dav_collection_must_have_exact_empty_marker_shape(fragment, which):
    first, second = frames()
    target = first if which == "parent" else second
    marker = b"<d:collection/>"
    if which == "parent":
        rewritten = target.body.replace(marker, fragment, 1)
    else:
        # The first marker in the nested Depth:1 frame is the parent SELF.
        # Mutate the second marker so this case really covers selected Vault.
        first_pos = target.body.find(marker)
        second_pos = target.body.find(marker, first_pos + len(marker))
        assert first_pos >= 0 and second_pos >= 0
        rewritten = (
            target.body[:second_pos] + fragment
            + target.body[second_pos + len(marker):]
        )
    assert rewritten != target.body
    altered = replace(target, body=rewritten)
    captures = (altered, second) if which == "parent" else (first, altered)
    assert_denied(
        preview_synthetic_dav_root(selection(), source(), captures),
        "invalid_dav_response",
    )


@pytest.mark.parametrize("fragment", [
    b"<d:resourcetype>not-empty<d:collection/></d:resourcetype>",
    b"<d:resourcetype><d:collection/>not-empty</d:resourcetype>",
    b"<d:resourcetype x:claim='alias' xmlns:x='urn:synthetic'><d:collection/></d:resourcetype>",
])
def test_resource_type_rejects_non_whitespace_mixed_content(fragment):
    first, second = frames()
    altered = replace(first, body=first.body.replace(
        b"<d:resourcetype><d:collection/></d:resourcetype>",
        fragment, 1,
    ))
    assert_denied(
        preview_synthetic_dav_root(selection(), source(), (altered, second)),
        "invalid_dav_response",
    )


@pytest.mark.parametrize("padding", [1, 220, 256, 384])
def test_non_utf8_xml_declaration_rejected_beyond_initial_256_bytes(padding):
    first, second = frames()
    altered = replace(first, body=first.body.replace(
        b'encoding="UTF-8"', b" " * padding + b'encoding="ISO-8859-1"', 1,
    ))
    assert_denied(
        preview_synthetic_dav_root(selection(), source(), (altered, second)),
        "invalid_dav_response",
    )
