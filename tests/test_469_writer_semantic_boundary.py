"""Synthetic source-only semantic boundaries before a CAS Writer can be enabled.

No provider, WebDAV credentials, real Vault input or network operations.
"""
from pathlib import Path

import pytest

from obsidian_automation.vault_formatter import POLICY_VERSION, create_plan, format_markdown
from obsidian_automation.vault_format_writer import apply_plan, FormatWriteError

tick = bytes([96])


@pytest.mark.parametrize(
    "source",
    [
        b"~~~\n<%* tR += " + tick + b"alpha\n~~~\nbefore\n  \nafter\n" + tick + b"; %>\n",
        tick * 3 + b"\n<%* tR += " + tick + b"alpha\n" + tick * 3 + b"\nbefore\n  \nafter\n" + tick + b"; %>\n",
        b"---\nvalue: |-\n  <%* tR += " + tick + b"alpha\n---\nbefore\n  \nafter\n" + tick + b"; %>\n",
        b"<!--\n<%* tR += " + tick + b"alpha\n-->\nbefore\n  \nafter\n" + tick + b"; %>\n",
        b"    <%* tR += " + tick + b"alpha\nbefore\n  \nafter\n" + tick + b"; %>\n",
        b"   \t<%* tR += " + tick + b"alpha\nbefore\n  \nafter\n" + tick + b"; %>\n",
    ],
)
def test_templater_hidden_in_opaque_region_never_exposes_template_literal(source):
    result, changes, reason = format_markdown(source)
    assert result is None
    assert changes == ()
    assert reason == "unsupported-html-block"


@pytest.mark.parametrize(
    "source",
    [
        b'<pre>\n<x:y title="\n</pre>\nalpha\n  \nomega\n">\n</x:y>\n</pre>\n',
        b'<pre>\n<svg:foreignObject title="\n</pre>\nalpha\n  \nomega\n">\n</svg:foreignObject>\n</pre>\n',
        b'first\n<x:y title="unterminated\nalpha\n  \nomega\n',
        b'first\n<my_ns:x attr="hello">\nalpha\n  \nomega\n',
        'first\n<svg:ελληνικά attr="hello">\nalpha\n  \nomega\n'.encode(),
        'before\n<svg:e\u0301>\nalpha\n  \nomega\n</svg:e\u0301>\n'.encode(),
        b'<pre>\n<x:y\xcc\x81 title="\n</pre>\nalpha\n  \nomega\n">\n</x:y\xcc\x81>\n</pre>\n',
        b'<pre>\n<x:y! title="\n</pre>\nalpha\n  \nomega\n">\n</x:y!>\n</pre>\n',
    ],
)
def test_namespaced_raw_html_openers_are_never_normalized(source):
    result, changes, reason = format_markdown(source)
    assert result is None
    assert changes == ()
    assert reason == "unsupported-html-block"


def test_standalone_templater_stays_editable_outside_opaque_regions():
    source = b"before\n  \n<%* tR += 'demo'; %>\nafter\n  \nlast\n"
    desired, lines, reason = format_markdown(source)
    assert desired == b"before\n\n<%* tR += 'demo'; %>\nafter\n\nlast\n"
    assert lines == (2, 5)
    assert reason == "blank-line-whitespace"


def test_unrelated_standalone_fence_stays_editable_outside_code():
    source = b"before\n  \n~~~\n  \n~~~\nafter\n  \nlast\n"
    desired, lines, reason = format_markdown(source)
    assert desired == b"before\n\n~~~\n  \n~~~\nafter\n\nlast\n"
    assert lines == (2, 7)
    assert reason == "blank-line-whitespace"


def test_frontmatter_without_templater_stays_editable_outside_yaml():
    source = b"---\na: |\n  do not edit\n  \n---\nstart\n  \nend\n"
    desired, lines, reason = format_markdown(source)
    assert desired == source.replace(b"start\n  \nend", b"start\n\nend")
    assert lines == (7,)
    assert reason == "blank-line-whitespace"


def test_v3_plan_matches_new_policy_and_writer_rejects_old_policy(tmp_path: Path):
    assert POLICY_VERSION == "system-blank-lines-v3"
    note = tmp_path / "98-System" / "synthetic.md"
    note.parent.mkdir()
    note.write_bytes(b"before\n  \nafter\n")
    old_bytes = note.read_bytes()
    plan = create_plan(tmp_path)
    assert plan["policy_version"] == POLICY_VERSION
    assert plan["counts"]["would_change"] == 1
    # Recompute a *valid* digest for the obsolete v2 policy so rejection
    # specifically proves policy binding, not merely a broken hash.
    import hashlib
    import json

    obsolete = {**plan, "policy_version": "system-blank-lines-v2"}
    fixed = {k: v for k, v in obsolete.items() if k not in ("plan_sha256", "diffs")}
    canonical = json.dumps(
        fixed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    obsolete["plan_sha256"] = hashlib.sha256(canonical).hexdigest()
    with pytest.raises(FormatWriteError, match="invalid_plan"):
        apply_plan(obsolete, selected_paths=[])
    assert apply_plan(plan, selected_paths=["98-System/synthetic.md"])["status"] == "disabled"
    assert note.read_bytes() == old_bytes

def test_mixed_comment_closure_and_fence_marker_fails_closed():
    quoted = bytes([96])
    source = (
        b"<!--\n--> " + quoted * 3
        + b"\n<%* tR += " + quoted + b"alpha\n"
        + quoted * 3 + b"\nbefore\n  \nafter\n" + quoted + b"; %>\n"
    )
    assert format_markdown(source) == (None, (), "unsupported-html-block")


def test_same_line_closed_comment_followed_by_fence_marker_fails_closed():
    source = b"<!-- harmless --> ~~~\nplain\n  \nend\n"
    assert format_markdown(source) == (None, (), "unsupported-html-block")


@pytest.mark.parametrize("fence_info", (b"~~~ <!-- -->", b"~~~<!-- -->"))
def test_closed_comment_in_ordinary_fence_info_cannot_hide_templater(fence_info):
    source = (
        fence_info + b"\n"
        b"<%* tR += " + tick + b"alpha\n"
        b"~~~\n"
        b"before\n  \nafter\n"
        + tick + b"; %>\n"
    )
    assert format_markdown(source) == (None, (), "unsupported-html-block")


def test_spaced_extra_marker_is_not_an_ordinary_fence_close():
    source = (
        b"~~~dataviewjs\n"
        b"const s = " + tick + b"\n"
        b"~~~ ~\n"
        b"alpha\n  \nomega\n"
        b"~~~ ~\n"
        + tick + b";\ndv.paragraph(s);\n~~~\n"
    )
    desired, changes, reason = format_markdown(source)
    assert desired == source
    assert changes == ()
    assert reason == "no-safe-changes"


def test_contiguous_fence_closure_is_required():
    from obsidian_automation.vault_formatter import _ordinary_fence_closes

    assert _ordinary_fence_closes("  ~~~~ ", "~", 3)
    assert not _ordinary_fence_closes("~~~ ~", "~", 3)
    assert not _ordinary_fence_closes("~~ ~~~", "~", 3)
    assert not _ordinary_fence_closes("~~", "~", 3)
    assert not _ordinary_fence_closes("    ~~~", "~", 3)
    assert _ordinary_fence_closes("~~~", "~", 3)


def test_raw_templater_combination_short_circuits_expensive_scans(monkeypatch):
    from obsidian_automation import vault_formatter as mod

    def forbidden(_bodies):
        raise AssertionError("raw/Templater conflict must short-circuit")

    monkeypatch.setattr(mod, "_templater_with_opaque_context", forbidden)
    source = b"<pre>\n<%* %>\n" + b"~" * 40000 + b"\n" + b"\n" * 40000
    assert format_markdown(source) == (None, (), "unsupported-html-block")


def test_literal_namespaced_example_inside_fence_skips_entire_document():
    # Reject-only global guard intentionally sacrifices edit coverage even
    # for a literal example inside a protected ordinary Markdown fence.
    source = b"~~~\n<x:y>\n~~~\nbefore\n  \nafter\n"
    assert format_markdown(source) == (None, (), "unsupported-html-block")


def test_literal_namespaced_example_inside_yaml_skips_entire_document():
    source = b"---\nexample: '<x:y>'\n---\nbefore\n  \nafter\n"
    assert format_markdown(source) == (None, (), "unsupported-html-block")

def test_two_independent_templater_commands_and_fence_cannot_desync_state():
    source = (
        b"<%* const s = " + tick + b"\n"
        b"~~~\n"
        + tick + b"; %>\n"
        b"~~~\n"
        b"<%* tR += " + tick + b"alpha\n"
        b"~~~\n"
        b"before\n  \nafter\n"
        + tick + b"; %>\n"
    )
    assert format_markdown(source) == (None, (), "unsupported-html-block")


def test_colon_run_qualified_token_rejects_without_ambiguous_regex():
    source = b"<" + b":" * 65536 + b"<\n"
    assert format_markdown(source) == (None, (), "unsupported-html-block")


def test_templater_with_distant_fence_is_deliberately_skipped():
    source = b"<% code %>\nparagraph\n~~~\nexample\n~~~\nend\n  \nlast\n"
    assert format_markdown(source) == (None, (), "unsupported-html-block")

@pytest.mark.parametrize(
    "opening",
    [
        tick * 3 + b" " + tick * 3,
        tick * 3 + b" " + tick,
        tick * 3 + b"language" + tick,
    ],
)
def test_invalid_backtick_fence_must_not_hide_raw_html(opening):
    source = (
        opening + b"\n<pre>\n" + tick * 3
        + b"\nalpha\n  \nomega\n</pre>\n"
    )
    assert format_markdown(source) == (None, (), "unsupported-fence-syntax")


def test_valid_backtick_or_tilde_fence_remains_safe():
    valid = (
        b"before\n  \n" + tick * 3
        + b"dataviewjs\n  \n"
        + tick * 3 + b"\nafter\n  \nlast\n"
    )
    desired, changed, _ = format_markdown(valid)
    assert desired == valid.replace(b"before\n  \n", b"before\n\n").replace(
        b"after\n  \nlast", b"after\n\nlast"
    )
    assert changed == (2, 7)
    tilde = b"~~~lang" + tick + b"\n  \n~~~\n"
    desired2, changed2, _ = format_markdown(tilde)
    assert desired2 == tilde and changed2 == ()

def test_security_review_r5_exact_invalid_fence_cannot_expose_raw_html():
    # Exact R5 security regression: invalid backtick info is not a
    # CommonMark opener, and a later backtick line cannot rescue it.
    source = (
        tick * 3 + b"bad" + tick + b"info\n"
        + tick * 3 + b" " + tick + b"\n"
        + b"\n<pre>\n"
        + tick * 3 + b"\nalpha\n  \nomega\n</pre>\n"
    )
    assert format_markdown(source) == (None, (), "unsupported-fence-syntax")


def test_r6_security_tab_suffixed_closing_fence_preserves_raw_html_bytes():
    # Security R6 P2: trailing tab closes an ordinary fence in CommonMark.
    # Following raw HTML must not be hidden by a stale fence state.
    source = (
        b"~~~\n"
        b"~~~ ~\n"  # Embedded space does NOT close the original fence.
        b"<span>\n"  # Literal content within the fenced block.
        b"~~~\t\n"  # Valid close: trailing tabs are allowed.
        b"<pre>\n"
        b"~~~\n"
        b"alpha\n"
        b"  \n"
        b"omega\n"
        b"</pre>\n"
    )
    desired, changed, reason = format_markdown(source)
    assert desired == source, (changed, reason)
    assert changed == ()


@pytest.mark.parametrize(
    ("line", "is_close"),
    [
        ("~~~", True),
        ("~~~ ", True),
        ("~~~\t", True),
        ("~~~ \t \t", True),
        ("   ~~~~\t ", True),
        ("~~~ ~", False),
        ("~~~\tother", False),
        ("~~~x", False),
        ("    ~~~\t", False),
        ("~~~" + chr(96), False),
        ("~~", False),
    ],
)
def test_r7_ordinary_fence_close_tabs_and_near_misses(line, is_close):
    from obsidian_automation.vault_formatter import _ordinary_fence_closes
    assert _ordinary_fence_closes(line, "~", 3) is is_close


def test_r7_tab_suffixed_backtick_close_resumes_only_outside_opaque_block():
    fence = chr(96) * 3
    source = (f"before\n  \n{fence}python\n  \n"
              f"{fence} \t\n<pre>\n  \n</pre>\nafter\n  \nend\n").encode()
    desired, changed, _ = format_markdown(source)
    assert desired is not None
    assert b"<pre>\n  \n</pre>" in desired
    assert changed == (2, 10)


def test_r8_nearest_nonblank_cache_matches_legacy_neighbor_semantics():
    import random
    from obsidian_automation.vault_formatter import (
        _nearest_nonblank_risks,
        _in_ambiguous_markdown_context,
    )
    generator = random.Random(469)
    options = [
        "", " ", "  ", "   ", "\t", "plain", "after",
        "- task", "1. item", "> quoted", "    indented",
        "   \tinner", "nonblank  ", "    ", "x" * 70,
    ]
    for _ in range(80):
        lines = [generator.choice(options) + "\n" for _ in range(generator.randrange(1, 160))]
        bodies = [v[:-1] for v in lines]
        before, after = _nearest_nonblank_risks(bodies)
        assert len(before) == len(lines) == len(after)
        for index in range(len(lines)):
            expected = _in_ambiguous_markdown_context(lines, index)
            actual = bool(before[index] or after[index])
            assert actual is expected, (index, before[index], after[index])


def test_r8_long_blank_run_uses_precomputed_nearest_nonblank(monkeypatch):
    import obsidian_automation.vault_formatter as formatter

    def forbidden(*args, **kwargs):
        raise AssertionError("linear formatter must not rescan nearest nonblank")

    monkeypatch.setattr(formatter, "_nearest_nonblank", forbidden)
    source = b"start\n" + b"  \n" * 3500 + b"end\n"
    desired, changed, reason = formatter.format_markdown(source)
    assert desired == b"start\n" + b"\n" * 3500 + b"end\n"
    assert len(changed) == 3500
    assert reason == "blank-line-whitespace"


def test_r8_security_list_fence_close_cannot_expose_raw_html():
    # Security R7 P2 exact reproducer; old parser deleted line 8 inside raw pre.
    source = (
        b"- ~~~\n"
        b"  literal\n"
        b"  ~~~\n"
        b"\n"
        b"<pre>\n"
        b"~~~\n"
        b"alpha\n"
        b"  \n"
        b"omega\n"
        b"</pre>\n"
    )
    assert format_markdown(source) == (None, (), "unsupported-fence-syntax")


@pytest.mark.parametrize(
    "list_item",
    (
        "- ~~~", "+ ~~~", "* ~~~", "1. ~~~",
        "10) ~~~", "  - ~~~", "    - ~~~", "\t- ~~~",
        "> - ~~~", ">> - ~~~", "  > 2. ~~~",
    ),
)
def test_r8_list_container_fence_markers_fail_closed(list_item):
    source = (list_item + "\n  literal\n  ~~~\n\n"
              "<pre>\n~~~\nalpha\n  \nomega\n</pre>\n").encode()
    assert format_markdown(source) == (None, (), "unsupported-fence-syntax")


def test_r8_list_and_unrelated_fence_conservatively_skips_document():
    source = b"- item\n\noutside\n~~~\ncode\n~~~\ntext\n  \nend\n"
    assert format_markdown(source) == (None, (), "unsupported-fence-syntax")


def test_r8_list_without_fence_remains_eligible_outside_list():
    source = b"- item\n\noutside\n  \nend\n"
    desired, changed, reason = format_markdown(source)
    assert desired == b"- item\n\noutside\n\nend\n"
    assert changed == (4,)
    assert reason == "blank-line-whitespace"


@pytest.mark.parametrize("list_marker", (
    "-", "+", "*", "1.", "10)", "  -", "    *", "\t-", "> -", "> 2.",
))
def test_r9_empty_list_item_before_nested_fence_fails_closed(list_marker):
    source=(list_marker+"\n  ~~~\n  text\n  ~~~\n"
            "<pre>\n~~~\ninside\n  \nend\n</pre>\n").encode()
    assert format_markdown(source) == (None, (), "unsupported-fence-syntax")


def test_r9_long_indented_neighbor_is_classified_once_for_blank_run(monkeypatch):
    import obsidian_automation.vault_formatter as formatter
    original = formatter._ambiguous_nonblank_neighbor
    calls = []

    def counted(value):
        calls.append(len(value))
        if len(calls) > 3:
            raise AssertionError("neighbor risk must not be rescanned per blank line")
        return original(value)

    monkeypatch.setattr(formatter, "_ambiguous_nonblank_neighbor", counted)
    source = b"    " + b" " * 180_000 + b"code\n" + b"  \n" * 90_000 + b"end\n"
    desired, changed, reason = formatter.format_markdown(source)
    assert desired == source
    assert changed == ()
    assert reason == "no-safe-changes"
    assert calls == [180_008, 3] or calls == [180_008, 3], calls


def test_r9_risk_cache_preserves_ambiguous_neighbor_classification_for_long_indents():
    from obsidian_automation.vault_formatter import _nearest_nonblank_risks
    bodies = ["    " + " " * 30000 + "code", "  ", "   ", "end"]
    before, after = _nearest_nonblank_risks(bodies)
    assert before[1] is True and before[2] is True
    assert after[1] is False and after[2] is False


def test_r9_security_exact_bare_list_marker_phantom_fence_reproducer():
    # R8 Security P2: a bare list-item marker is still a valid list start.
    source = (
        b"-\n"
        b"    ~~~\n"
        b"    literal\n"
        b"  ~~~\n"
        b"\n"
        b"<pre>\n"
        b"~~~\n"
        b"alpha\n"
        b"  \n"
        b"omega\n"
        b"</pre>\n"
    )
    assert format_markdown(source) == (None, (), "unsupported-fence-syntax")


def test_r9_nonblank_risk_classification_preserves_legacy_list_semantics():
    from obsidian_automation.vault_formatter import _ambiguous_nonblank_neighbor
    for body in ("- item", "+ item", "1. numbered", "12) ordered", "> quote", "    code"):
        assert _ambiguous_nonblank_neighbor(body), body
    for body in ("plain", "not-list", "dash-word", "3.digit"):
        assert not _ambiguous_nonblank_neighbor(body), body
