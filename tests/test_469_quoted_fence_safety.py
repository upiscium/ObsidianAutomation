"""Fail-closed quoted-fence grammar: synthetic inputs, no real Vault access."""
from obsidian_automation.vault_formatter import format_markdown


def test_explicit_quote_fence_protected_and_outside_blank_may_change():
    marker = chr(96) * 3
    raw = (
        "intro\n  \noutside\n\n"
        "> " + marker + "dataviewjs\n"
        ">   \n"
        "> source  \n"
        "> " + marker + "\n"
        "\nend\n  \nlast\n"
    ).encode()
    desired, positions, reason = format_markdown(raw)
    assert desired is not None, reason
    assert desired == raw.replace(b"intro\n  \noutside", b"intro\n\noutside").replace(
        b"end\n  \nlast", b"end\n\nlast"
    )
    assert b">   \n" in desired
    assert positions == (2, 11)
    assert format_markdown(desired)[1] == ()


def test_quote_fence_with_unquoted_lazy_continuation_fails_closed():
    marker = chr(96) * 3
    source = ("> " + marker + "\n" + "  \n" + "> " + marker + "\n").encode()
    assert format_markdown(source)[0] is None
    assert format_markdown(source)[2] == "unsupported-nested-fence"


def test_quote_fence_requires_explicit_close_and_matching_marker():
    marker = chr(96) * 3
    for raw in (
        "> " + marker + "\n>  \n",
        "> " + marker + "\n> ~~~\n",
        "> " + marker + "\n>> " + marker + "\n",
    ):
        assert format_markdown(raw.encode())[2] == "unsupported-nested-fence"


def test_nested_double_quote_fence_protected():
    marker = "~" * 3
    original = (">> " + marker + "\n>>  \n>> " + marker + "\nafter\n  \nend\n").encode()
    desired, lines, _ = format_markdown(original)
    assert desired == original.replace(b"after\n  \nend",b"after\n\nend")
    assert lines == (5,)


def test_unrelated_unclosed_plain_fence_still_fails_closed():
    fence=chr(96)*3
    raw=("> "+fence+"\n> contents\n> "+fence+"\n"+fence+"js\n  \n").encode()
    assert format_markdown(raw)[2]=="unterminated-protected-block"

def test_mixed_closing_delimiters_never_terminate_quoted_fence():
    marker = chr(96) * 3
    # A mixed marker must not end quoted code, even when followed by
    # a legitimate closing fence at a later line.
    source = (
        "> " + marker + "\n"
        "> literal-code\n"
        "> " + chr(96) * 2 + "~\n"
        "> " + marker + "\n"
        "outside\n  \nlast\n"
    ).encode()
    desired, changed, reason = format_markdown(source)
    assert desired == source.replace(b"outside\n  \nlast", b"outside\n\nlast")
    assert changed == (6,)
    # A truncated quoted sequence with only the fake close is unsafe.
    assert format_markdown(
        ("> " + marker + "\n> code\n> " + chr(96) * 2 + "~\n").encode()
    )[2] == "unsupported-nested-fence"


def test_quote_fence_with_protected_html_markers_fails_closed():
    fence = chr(96) * 3
    raw = (
        "> " + fence + "\n"
        "> <div> literal code\n"
        "> <!-- literal comment\n"
        "> <% literal templater\n"
        "> " + fence + "\n"
        "after\n  \nend\n"
    ).encode()
    assert format_markdown(raw) == (None, (), "unsupported-nested-fence")

def test_frontmatter_yaml_quote_fence_remains_unsupported():
    marker=chr(96)*3
    raw=(
        "---\n"
        "description: |\n"
        "  > " + marker + "\n"
        "  > literal YAML text\n"
        "---\n"
        "Body\n  \nEnd\n"
    ).encode()
    assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_unterminated_frontmatter_with_quote_fence_remains_skipped():
    marker=chr(96)*3
    raw=("---\ndescription: |\n  > "+marker+"\n  > literal YAML text\n").encode()
    assert format_markdown(raw)[2]=="unsupported-nested-fence"


def test_security_html_comment_mixed_transition_not_hidden_by_quote_fence():
    """Reviewer P2: quoted-looking text inside HTML comment may not skip context."""
    raw = (
        b"<!--\n"
        b"> " + bytes([96]) * 3 + b"\n"
        b"> --> <textarea>\n"
        b"> " + bytes([96]) * 3 + b"\n"
        b"-->\n"
        b"alpha\n"
        b"  \n"
        b"omega\n"
        b"</textarea>\n"
    )
    desired, lines, reason = format_markdown(raw)
    assert desired is None, (desired, lines, reason)
    assert lines == ()
    assert reason == "unsupported-nested-fence"


def test_context_hidden_quote_fence_inside_raw_html_does_not_bypass_protection():
    marker=chr(96)*3
    raw=(
        "<textarea>\n"
        "> "+marker+"\n"
        "> --> <div>\n"
        "> "+marker+"\n"
        "protected\n  \nbytes\n"
        "</textarea>\n"
        "after\n  \nend\n"
    ).encode()
    desired, lines, reason=format_markdown(raw)
    assert (desired, lines, reason) == (None, (), "unsupported-nested-fence")


def test_quote_fence_inside_active_templater_preserves_old_skip_policy():
    marker=chr(96)*3
    raw=(
        "<%*\n"
        "> "+marker+"\n"
        "> literal\n"
        "> "+marker+"\n"
        "%>\n"
        "after\n  \nend\n"
    ).encode()
    assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_policy_version_is_bumped_for_new_quoted_fence_admission():
    from obsidian_automation.vault_formatter import POLICY_VERSION
    assert POLICY_VERSION == "system-blank-lines-v2"

def test_invalid_quoted_backtick_info_cannot_hide_raw_html_container():
    marker=chr(96)*3
    raw=(
        "> "+marker+"bad"+chr(96)+"info\n"
        "> <textarea>\n"
        "> "+marker+"\n"
        "alpha\n  \nomega\n"
        "</textarea>\n"
    ).encode()
    desired,changed,reason=format_markdown(raw)
    assert desired is None
    assert changed == ()
    assert reason == "unsupported-nested-fence"


def test_invalid_quoted_backtick_info_cannot_hide_html_transition():
    marker=chr(96)*3
    raw=(
        "> "+marker+"bad"+chr(96)+"info\n"
        "> <!--\n"
        "> --> <textarea>\n"
        "> "+marker+"\n"
        "alpha\n  \nomega\n"
        "</textarea>\n"
    ).encode()
    assert format_markdown(raw)[2] == "unsupported-nested-fence"


def test_invalid_quoted_backtick_info_cannot_hide_templater():
    marker=chr(96)*3
    raw=(
        "> "+marker+"bad"+chr(96)+"info\n"
        "> <%*\n"
        "> "+marker+"\n"
        "code\n  \nnext\n"
        "%>\n"
    ).encode()
    assert format_markdown(raw)[2] == "unsupported-nested-fence"



def test_security_quoted_fence_must_not_hide_cross_boundary_templater():
    # Independent Security Reviewer P2: a Templater string value extends
    # from inside a quoted fence to an unquoted whitespace-only line.
    tick = chr(96)
    raw = (
        "> ~~~\n"
        "> <%* tR += " + tick + "alpha\n"
        "> ~~~\n"
        "before\n"
        "  \n"
        "after\n"
        + tick + "; %>\n"
    ).encode()
    desired, lines, reason = format_markdown(raw)
    assert desired is None
    assert lines == ()
    assert reason == "unsupported-nested-fence"


def test_quoted_fence_must_not_hide_cross_boundary_html_comment():
    raw = (
        "> ~~~\n"
        "> <!--\n"
        "> ~~~\n"
        "before\n"
        "  \n"
        "after\n"
        "-->\n"
    ).encode()
    desired, lines, reason = format_markdown(raw)
    assert (desired, lines, reason) == (None, (), "unsupported-nested-fence")


def test_quoted_fence_must_not_hide_cross_boundary_raw_html():
    raw = (
        "> ~~~\n"
        "> <script>\n"
        "> ~~~\n"
        "before\n"
        "  \n"
        "after\n"
        "</script>\n"
    ).encode()
    desired, lines, reason = format_markdown(raw)
    assert (desired, lines, reason) == (None, (), "unsupported-nested-fence")


def test_quoted_fence_opening_info_cannot_hide_template_marker():
    raw = (
        "> ~~~<%*\n"
        "> code\n"
        "> ~~~\n"
        "before\n"
        "  \n"
        "after\n"
        "%>\n"
    ).encode()
    desired, lines, reason = format_markdown(raw)
    assert (desired, lines, reason) == (None, (), "unsupported-nested-fence")


def test_quoted_fence_inside_plain_fence_does_not_hide_templater_context():
    # Second independently confirmed P2: source templating may outlive an
    # ordinary fence. Earlier v1 globally skipped the quoted-fence marker.
    tick=chr(96)
    raw = (
        "~~~~\n"
        "> ~~~\n"
        "> <%* tR += " + tick + "alpha\n"
        "> ~~~\n"
        "~~~~\n"
        "before\n"
        "  \n"
        "after\n"
        + tick + "; %>\n"
    ).encode()
    assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_quote_fence_in_plain_fence_without_template_remains_skipped():
    tick = chr(96)
    raw = ("~~~~\n> " + tick*3 + "\n> fenced literal\n> "+tick*3+
           "\n~~~~\ntext\n  \nend\n").encode()
    assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_source_context_started_inside_other_protected_block_then_quote_fence():
    # Independent Security Reviewer R3: a template opened *inside* a
    # protected Markdown/HTML/frontmatter region can outlive that region
    # even though the quoted fence appears later, after the region closes.
    tick=chr(96)
    preambles=[
        "~~~~\n<%* tR += " + tick + "alpha\n~~~~\n",
        "<!--\n<%* tR += " + tick + "alpha\n-->\n",
        "<pre>\n<%* tR += " + tick + "alpha\n</pre>\n",
        "---\nvalue: |\n  <%* tR += " + tick + "alpha\n---\n",
    ]
    suffix=("> ~~~\n> literal\n> ~~~\nbefore\n  \nafter\n"
            + tick + "; %>\n")
    for prefix in preambles:
        raw=(prefix+suffix).encode()
        assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_quote_fence_and_unrelated_source_context_still_fail_closed():
    # Source start on a distant line may be hidden or span boundaries.
    raw=(
        "ordinary quoted example\n"
        "> ~~~\n"
        "> safe code\n"
        "> ~~~\n"
        "paragraph\n"
        "  \n"
        "after\n"
        "<%* tR += 1; %>\n"
    ).encode()
    assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_spaced_multilevel_quote_marker_cannot_bypass_source_context_guard():
    # Independent Correctness R4: parser accepts spaced nested quote markers
    # that an earlier document-wide detector did not recognize.
    tick=chr(96)
    for quoted_marker in ("> > ~~~", "> > > ~~~"):
        raw=(
            "~~~~\n"
            "<%* tR += " + tick + "alpha\n"
            "~~~~\n"
            + quoted_marker + "\n"
            + quoted_marker.rsplit(" ",1)[0] + " literal\n"
            + quoted_marker + "\n"
            "before\n"
            "  \n"
            "after\n"
            + tick + "; %>\n"
        ).encode()
        assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_spaced_multilevel_quote_without_source_context_is_admitted():
    original = (
        "> > ~~~\n"
        "> > protected\n"
        "> > ~~~\n"
        "after\n"
        "  \n"
        "end\n"
    ).encode()
    desired, lines, reason = format_markdown(original)
    assert desired == original.replace(b"after\n  \nend",b"after\n\nend")
    assert lines == (5,)
    assert reason == "blank-line-whitespace"


def test_source_context_guard_recognizes_every_quote_parser_opener():
    # For all supported quote prefixes, a parser-admitted fence opener must
    # also trigger the conservative whole-document guard. Never maintain two
    # divergent grammars for admitting vs protecting source languages.
    from itertools import product
    import re
    from obsidian_automation.vault_formatter import (
        _FENCE_START, _QUOTED_FENCE_MARKER,
    )
    accepted=0
    for depth in range(1, 7):
        for gaps in product((0, 1), repeat=depth):
            prefix="".join(">" + (" " if gap else "") for gap in gaps)
            for leading in range(4):
                for space_after_prefix in range(4):
                    body=" "*leading + prefix + " "*space_after_prefix + "~~~"
                    parsed=re.match(
                        r"^ {0,3}(?P<prefix>(?:> ?)+)(?P<content>.*)$",
                        body,
                    )
                    if not parsed or not _FENCE_START.match(parsed.group("content")):
                        continue
                    accepted += 1
                    assert _QUOTED_FENCE_MARKER.match(body), repr(body)
    assert accepted > 500


def test_legacy_v1_quote_guard_is_subset_of_v2_document_guard():
    # The v2 conservative detector MUST accept every marker matched by v1;
    # admission grammar can be narrower, rejection coverage cannot regress.
    import re
    from obsidian_automation.vault_formatter import _QUOTED_FENCE_MARKER
    old=re.compile(r"^\s*>+\s*[\x60~]{3}")
    count=0
    for indent in ("", " ", "  ", "   ", "    ", "      ", "\t", " \t "):
        for depth in range(1, 6):
            for after in ("", " ", "   ", "      ", "\t"):
                for mark in ("~~~", chr(96)*3):
                    body=indent+">"*depth+after+mark
                    if old.match(body):
                        count += 1
                        assert _QUOTED_FENCE_MARKER.match(body), repr(body)
    assert count > 100


def test_overindented_or_tabbed_quote_marker_cannot_hide_templater_source():
    # R5 Security P2: legacy guard rejected 4-space and tab-indented markers.
    tick=chr(96)
    for indent in ("    ", "      ", "\t", " \t "):
        raw=(
            "~~~~\n"
            "<%* tR += " + tick + "alpha\n"
            "~~~~\n"
            + indent + "> ~~~\n"
            + indent + "> literal\n"
            + indent + "> ~~~\n"
            "before\n"
            "  \n"
            "after\n"
            + tick + "; %>\n"
        ).encode()
        assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_multiline_html_opener_is_not_hidden_by_quoted_fence():
    # R6 independent Security P2: HTML start-tag names may end at newline.
    raw=(
        "<pre\n"
        ">\n"
        "> ~~~\n"
        "> literal\n"
        "> ~~~\n"
        "alpha\n"
        "  \n"
        "omega\n"
        "</pre>\n"
    ).encode()
    assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_unrecognized_raw_markup_with_quote_fence_fails_closed():
    # A source language can start with markup not covered by a small HTML
    # tag-name recognizer. Quoted fences make that combination unsafe.
    for marker in ("<![CDATA[", "<?xml", "<pre", "comparison x < y"):
        raw=("> ~~~\n> code\n> ~~~\n"+marker+
             "\ntext\n  \nlast\n").encode()
        assert format_markdown(raw) == (None, (), "unsupported-nested-fence")


def test_multiline_pre_opening_without_quote_is_protected():
    raw=("<pre\n>\nalpha\n  \nomega\n</pre>\n").encode()
    desired,lines,reason=format_markdown(raw)
    assert desired is None
    assert lines==()
    assert reason=="unsupported-html-block"


def test_multiline_unrecognized_div_opening_without_quote_is_skipped():
    raw=("<div\n>\nalpha\n  \nomega\n</div>\n").encode()
    assert format_markdown(raw)==(None,(),"unsupported-html-block")


def test_legacy_quote_like_marker_not_admitted_by_parser_fails_closed():
    # R7 Correctness P2: a conservative detection hit is never a license
    # to treat the source as ordinary text when the quote parser disagrees.
    markers=[
        ">     ~~~",
        "    > ~~~",
        "\t> ~~~",
        "> \t~~~",
        "  > >     ~~~",
        ">    ~~~",
    ]
    for marker in markers:
        raw=(marker+"\n> literal\n"+marker+
             "\nordinary\n  \nend\n").encode()
        assert format_markdown(raw) == (None, (), "unsupported-nested-fence"), repr(marker)

def test_incomplete_raw_html_cannot_hide_cross_boundary_templater():
    tick = chr(96)
    raw = (
        "<pre\n"
        ">\n"
        "<%* tR += " + tick + "alpha\n"
        "</pre>\n"
        "before\n"
        "  \n"
        "after\n"
        + tick + "; %>\n"
    ).encode()
    assert format_markdown(raw) == (None, (), "unsupported-html-block")


def test_incomplete_html_attributes_cannot_hide_cross_boundary_template_string():
    tick = chr(96)
    for opening in (
        "<pre class=\"example\"",
        "<pre class=\"example",
        "<script lang=javascript",
        "<pre title=\">literal",
        "<pre title='>literal",
    ):
        raw=(opening+"\n>\n<%* tR += "+tick+"alpha\n</pre>\n"
             "before\n  \nafter\n"+tick+"; %>\n").encode()
        assert format_markdown(raw)==(None, (), "unsupported-html-block"), opening


def test_raw_html_opener_closed_same_line_lexer_handles_quoted_greater_than():
    # Syntactically complete tags may still be handled by the pre-existing
    # conservative raw HTML block rules; the lexical guard alone must not
    # mistake a quoted > for tag termination.
    from obsidian_automation.vault_formatter import _raw_html_tag_is_closed_on_line
    assert _raw_html_tag_is_closed_on_line('<pre title=">">', 4)
    assert not _raw_html_tag_is_closed_on_line('<pre title=">test"', 4)
    assert not _raw_html_tag_is_closed_on_line("<script lang=js", 7)
    assert _raw_html_tag_is_closed_on_line("<pre class='foo'>", 4)


def test_complete_html_opening_quoted_close_text_does_not_expose_pre_whitespace():
    source=b'<pre title="</pre>">\nalpha\n  \nomega\n</pre>\n'
    desired, lines, _ = format_markdown(source)
    assert desired == source
    assert lines == ()

def test_complete_html_opening_cannot_hide_cross_boundary_template_literal():
    tick=chr(96)
    for opener in ("<pre>", '<pre title=">">'):
        source=(opener+"\n<%* tR += "+tick+"alpha\n</pre>\n"
                "before\n  \nafter\n"+tick+"; %>\n").encode()
        assert format_markdown(source) == (None, (), "unsupported-html-block")

def test_raw_html_cannot_hide_cross_boundary_html_comment():
    source=b"<pre>\n<!-- start\n</pre>\nbefore\n  \nafter\n-->\n"
    assert format_markdown(source) == (None, (), "unsupported-html-block")


def test_nested_html_attribute_cannot_fake_raw_element_close():
    source=b'<pre>\n<span title="</pre>">\nalpha\n  \nomega\n</pre>\n'
    assert format_markdown(source)==(None, (), "unsupported-html-block")


def test_raw_html_closing_tag_is_required_to_stand_alone():
    for close in ("trailing </pre>", '<span title="</pre>">', '</pre> extra'):
        source=("<pre>\n"+close+"\nordinary\n  \nend\n</pre>\n").encode()
        assert format_markdown(source)==(None, (), "unsupported-html-block"),close


def test_multiline_nested_html_attribute_cannot_fake_standalone_raw_close():
    source = (
        b'<pre title="</pre>">\n'
        b'<span title="\n'
        b'</pre>\n'
        b'alpha\n'
        b'  \n'
        b'omega\n'
        b'">\n'
        b'</span>\n'
        b'</pre>\n'
    )
    assert format_markdown(source)==(None, (), "unsupported-html-block")


def test_nested_html_inside_raw_element_is_never_admitted_for_reformatting():
    for nested in ('<span title="test">', '<span title="', '<textarea>'):
        source=('<pre>\n'+nested+'\nalpha\n  \nomega\n</pre>\n').encode()
        assert format_markdown(source)==(None, (), "unsupported-html-block"),nested
