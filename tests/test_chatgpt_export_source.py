"""Synthetic, read-only unit tests for external ChatGPT Source/Context v0."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from obsidian_automation.chatgpt_export_normalize import normalize_export
from obsidian_automation.chatgpt_export_source import (
    SOURCE_CONTRACT_VERSION,
    SOURCE_KIND,
    SourceContractError,
    SourceCursor,
    build_source_context_page,
    validate_source_context_page,
)

KEY = "f" * 64
PRIVATE = "PRIVATE_UNIT_SENTINEL_NEVER_LOG"


def chosen(tmp_path, messages=None, *, title=PRIVATE, alternate=True):
    if messages is None:
        messages = [
            ("user", "short question"),
            ("assistant", "Long response with evidence."),
            ("user", "follow-up"),
        ]
    mapping = {
        "root": {"id": "root", "parent": None, "message": None},
    }
    parent = "root"
    for n, (role, text) in enumerate(messages):
        node = "node" + str(n)
        mapping[node] = {
            "id": node, "parent": parent,
            "message": {
                "id": "private_msg_" + str(n),
                "create_time": float(n),
                "author": {"role": role},
                "content": {"content_type": "text", "parts": [text]},
            },
        }
        parent = node
    if alternate:
        mapping["alternate"] = {
            "id": "alternate", "parent": "root", "message": {
                "id": "not-visible-message", "author": {"role": "assistant"},
                "content": {"content_type": "thoughts", "thoughts": [PRIVATE]},
            },
        }
    data = [{
        "id": "opaque-private-conversation", "title": title,
        "mapping": mapping, "current_node": parent,
    }]
    path = tmp_path / "synthetic-conversations.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    batch = normalize_export(path, scope="private-scope", input_format="json")
    return batch.conversations[0]


def page(conv, **opts):
    return build_source_context_page(
        conv, scope="private-scope", source_hmac=KEY, **opts,
    )


def assert_valid(fragment, conv):
    validate_source_context_page(fragment, conv, scope="private-scope", source_hmac=KEY)


def test_bounded_page_contains_exact_role_attribution_and_private_provenance(tmp_path):
    conv = chosen(tmp_path)
    frag = page(conv)
    assert frag.complete is True
    assert frag.source_kind == SOURCE_KIND == "chatgpt-export"
    assert frag.contract_version == SOURCE_CONTRACT_VERSION
    assert frag.generation_authorized is False
    assert frag.unverified_child_links == conv.unverified_child_links
    assert frag.off_branch_nodes == 1
    assert len(frag.units) >= 1
    pieces = [sl for unit in frag.units for sl in unit.evidence]
    assert [sl.text for sl in pieces] == ["short question", "Long response with evidence.", "follow-up"]
    assert [sl.attribution for sl in pieces] == [
        "user-report-unverified", "assistant-claim-unverified", "user-report-unverified",
    ]
    assert all(not unit.generation_authorized for unit in frag.units)
    assert all(unit.trust_level == "untrusted-evidence" for unit in frag.units)
    assert all(len(sl.text.encode("utf-8")) <= unit.context_bytes for unit in frag.units for sl in unit.evidence)
    assert PRIVATE not in repr(frag)
    assert "private_msg" not in repr(pieces[0])
    assert_valid(frag, conv)


def test_multibyte_unicode_split_and_pagination_are_lossless(tmp_path):
    messages = [("user", "日😀本語" * 200 + "\n" + "🎉" * 100),
                ("assistant", "This is the answer."), ("user", "終")]
    conv = chosen(tmp_path, messages=messages)
    cursor = SourceCursor()
    accumulated = [""] * len(messages)
    units = []
    for _ in range(1500):
        frag = page(conv, cursor=cursor, max_units=2, max_unit_bytes=15,
                    max_page_bytes=25, max_segments=2)
        assert_valid(frag, conv)
        assert frag.text_bytes <= 25
        assert all(u.context_bytes <= 15 for u in frag.units)
        for unit in frag.units:
            for sl in unit.evidence:
                accumulated[sl.message_index] += sl.text
        units.extend(frag.units)
        if frag.complete:
            assert frag.next_cursor == SourceCursor(len(messages), 0)
            break
        assert frag.next_cursor != cursor
        cursor = frag.next_cursor
    else:
        pytest.fail("pagination did not reach the end")
    assert accumulated == [x[1] for x in messages]
    assert len({unit.unit_id for unit in units}) == len(units)


def test_adjacent_hashes_reference_neighbor_not_text(tmp_path):
    conv = chosen(tmp_path, messages=[
        ("user", "A"), ("assistant", "B"), ("user", "C"),
    ])
    result = page(conv, max_segments=1, max_unit_bytes=256)
    assert len(result.units) == 3
    assert result.units[0].previous_source_message_sha256 is None
    assert result.units[0].following_source_message_sha256 == conv.messages[1].text_sha256
    assert result.units[1].previous_source_message_sha256 == conv.messages[0].text_sha256
    assert result.units[2].following_source_message_sha256 is None
    assert_valid(result, conv)


def test_metadata_only_revision_changes_provenance_not_unit_identity(tmp_path):
    old = chosen(tmp_path, title="first private title")
    original = page(old)
    newer = chosen(tmp_path, title="second private title")
    refreshed = page(newer)
    assert old.source_revision_sha256 != newer.source_revision_sha256
    assert old.content_sha256 == newer.content_sha256
    assert [u.unit_id for u in original.units] == [u.unit_id for u in refreshed.units]
    assert [u.source_revision_sha256 for u in original.units] != [
        u.source_revision_sha256 for u in refreshed.units
    ]
    with pytest.raises(SourceContractError, match="context_contract_mismatch"):
        assert_valid(original, newer)


def test_context_cannot_impersonate_vault_content_or_permission(tmp_path):
    conv = chosen(tmp_path)
    fragment = page(conv)
    assert fragment.source_kind not in ("11-Knowledge", "10-Project", "03-AI")
    assert "generation_authorized" not in fragment.__repr__()
    with pytest.raises(SourceContractError, match="context_authority_forbidden"):
        assert_valid(replace(fragment, generation_authorized=True), conv)
    with pytest.raises(SourceContractError, match="context_authority_forbidden"):
        units = (replace(fragment.units[0], generation_authorized=True),) + fragment.units[1:]
        assert_valid(replace(fragment, units=units), conv)
    with pytest.raises(SourceContractError, match="invalid_context_metadata"):
        assert_valid(replace(fragment, source_kind="11-Knowledge"), conv)


def test_tampering_text_hash_span_and_gap_fail_closed(tmp_path):
    conv = chosen(tmp_path)
    original = page(conv, max_segments=1)
    unit0 = original.units[0]
    sl = unit0.evidence[0]
    forged = replace(sl, text="tampered")
    with pytest.raises(SourceContractError):
        assert_valid(replace(original, units=(replace(unit0, evidence=(forged,)),)+original.units[1:]), conv)
    new_sl = replace(sl, start_codepoint=1, text=sl.text[1:])
    with pytest.raises(SourceContractError, match="context_gap_or_overlap"):
        assert_valid(replace(original, units=(replace(unit0, evidence=(new_sl,)),)+original.units[1:]), conv)
    with pytest.raises(SourceContractError, match="context_next_cursor_mismatch"):
        assert_valid(replace(original, next_cursor=SourceCursor(0, 0)), conv)
    with pytest.raises(SourceContractError, match="context_adjacent_source_mismatch"):
        assert_valid(replace(original, units=(replace(unit0, following_source_message_sha256=KEY),)+original.units[1:]), conv)


@pytest.mark.parametrize("params,code", [
    ({"scope": "../../not-allowed"}, "invalid_source_scope"),
    ({"source_hmac": "PRIVATE"}, "invalid_source_hmac"),
    ({"max_unit_bytes": 16385}, "invalid_context_bounds"),
    ({"max_units": 0}, "invalid_context_bounds"),
    ({"max_page_bytes": 65537}, "invalid_context_bounds"),
    ({"max_segments": 0}, "invalid_context_bounds"),
    ({"cursor": SourceCursor(-1, 0)}, "invalid_source_cursor"),
    ({"cursor": SourceCursor(999, 0)}, "invalid_source_cursor"),
    ({"cursor": SourceCursor(0, 9999)}, "invalid_source_cursor"),
])
def test_untrusted_options_rejected_with_constant_codes(tmp_path, params, code):
    conv = chosen(tmp_path)
    kwargs = {"scope": "private-scope", "source_hmac": KEY}
    kwargs.update(params)
    with pytest.raises(SourceContractError, match=code) as err:
        build_source_context_page(conv, **kwargs)
    assert PRIVATE not in str(err.value)


def test_source_digest_mismatch_never_becomes_a_context(tmp_path):
    conv = chosen(tmp_path)
    forged = replace(conv, content_sha256="0" * 64)
    with pytest.raises(SourceContractError, match="semantic_source_digest_mismatch"):
        page(forged)
    altered = replace(conv.messages[0], text="modified")
    with pytest.raises(SourceContractError, match="source_text_digest_mismatch"):
        page(replace(conv, messages=(altered,) + conv.messages[1:]))


def test_empty_visible_conversation_requires_no_units(tmp_path):
    conv = chosen(tmp_path, messages=[])
    fragment = page(conv)
    assert fragment.complete is True
    assert fragment.units == ()
    assert fragment.text_bytes == 0
    assert fragment.generation_authorized is False
    assert_valid(fragment, conv)


def test_role_and_unverified_content_cannot_be_promoted(tmp_path):
    conv = chosen(tmp_path)
    forged = replace(conv.messages[0], role="system")
    with pytest.raises(SourceContractError, match="invalid_source_message"):
        page(replace(conv, messages=(forged,) + conv.messages[1:]))
    result = page(conv)
    with pytest.raises(SourceContractError, match="context_coverage_mismatch"):
        assert_valid(replace(result, unverified_child_links=0), conv)


def test_forged_none_authority_flags_rejected(tmp_path):
    conv = chosen(tmp_path)
    fragment = page(conv)
    with pytest.raises(SourceContractError, match="context_authority_forbidden"):
        assert_valid(replace(fragment, generation_authorized=None), conv)
    with pytest.raises(SourceContractError, match="context_authority_forbidden"):
        units = (replace(fragment.units[0], generation_authorized=None),) + fragment.units[1:]
        assert_valid(replace(fragment, units=units), conv)


def test_forged_incomplete_empty_page_not_accepted(tmp_path):
    conv = chosen(tmp_path)
    fragment = page(conv)
    with pytest.raises(SourceContractError, match="context_no_progress"):
        assert_valid(replace(
            fragment, units=(), start_cursor=SourceCursor(0, 0),
            next_cursor=SourceCursor(0, 0), complete=False, text_bytes=0,
        ), conv)


def test_forged_boolean_or_number_metadata_type_fails_closed(tmp_path):
    conv = chosen(tmp_path, alternate=True)
    original = page(conv)
    assert original.complete is True
    assert original.off_branch_nodes == 1

    # bool subclasses int in Python, but page metadata must retain the
    # exact source contract field type. A truthy success marker is not enough.
    with pytest.raises(SourceContractError, match="context_completion_mismatch"):
        assert_valid(replace(original, complete=1), conv)
    with pytest.raises(SourceContractError, match="context_coverage_mismatch"):
        assert_valid(replace(original, off_branch_nodes=True), conv)


@pytest.mark.parametrize("invalid", [None, False, 0, "opaque", {}, [], [None], (None,)])
def test_untrusted_page_units_fail_with_fixed_source_error(tmp_path, invalid):
    conv = chosen(tmp_path)
    fragment = page(conv)
    with pytest.raises(SourceContractError, match="invalid_context_units") as exc:
        assert_valid(replace(fragment, units=invalid), conv)
    assert PRIVATE not in str(exc.value)


@pytest.mark.parametrize("invalid", [None, False, 1, "opaque", {}, [], [None], (None,)])
def test_untrusted_unit_evidence_fail_with_fixed_source_error(tmp_path, invalid):
    conv = chosen(tmp_path)
    fragment = page(conv)
    forged = replace(fragment.units[0], evidence=invalid)
    with pytest.raises(SourceContractError, match="context_segments_invalid") as exc:
        assert_valid(replace(fragment, units=(forged,)), conv)
    assert PRIVATE not in str(exc.value)


@pytest.mark.parametrize("invalid", [None, False, 2, "opaque", {}, []])
def test_untrusted_selected_messages_fail_with_fixed_source_error(tmp_path, invalid):
    conv = chosen(tmp_path)
    with pytest.raises(SourceContractError, match="invalid_source_messages") as exc:
        page(replace(conv, messages=invalid))
    assert PRIVATE not in str(exc.value)


@pytest.mark.parametrize("field,value", [
    ("selected_path_nodes", True),
    ("off_branch_nodes", -1),
    ("unverified_child_links", None),
    ("omitted_reasons", []),
])
def test_untrusted_selected_coverage_fails_closed(tmp_path, field, value):
    conv = chosen(tmp_path)
    with pytest.raises(SourceContractError, match="invalid_source_coverage"):
        page(replace(conv, **{field: value}))


@pytest.mark.parametrize("field", [
    "message_index", "role", "start_codepoint", "end_codepoint", "text_sha256",
])
def test_non_json_span_fields_rejected_before_unit_digest_hash(tmp_path, field):
    conv = chosen(tmp_path)
    fragment = page(conv)
    first = fragment.units[0]
    forged_slice = replace(first.evidence[0], **{field: object()})
    forged_unit = replace(first, evidence=(forged_slice,) + first.evidence[1:])
    with pytest.raises(SourceContractError) as exc:
        assert_valid(replace(fragment, units=(forged_unit,) + fragment.units[1:]), conv)
    assert PRIVATE not in str(exc.value)
