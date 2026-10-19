"""Merged #390/#391 source-only boundaries (no live-site assertion)."""
import pytest

from obsidian_automation import github_daily_feature_stage as stage
from obsidian_automation import github_daily_scope_quality as quality
from obsidian_automation import github_daily_d1_priority_shadow as d1
from test_github_daily_feature_stage import make_original_feature_context
from test_github_daily_scope_quality import CAP, tsumgi, sentence
from test_github_daily_c5_source_view import event


@pytest.mark.parametrize("example_body", [
    ("## Current status: Example output (mock) ##\n\n"
     "## Notes\nRunning GA services have now been updated.\n"),
    ("## Model reply ###\n\n~~~~text\n"
     "~~~\n## Actual status:\nRunning GA services have now been updated.\n~~~~"),
    ("<!--\n## Actual status:\n"
     "Running GA services have now been updated.\n-->\n"),
    ("## Example output\n"
     "The output below is illustrative.\n## Notes\n"
     "Running GA services have now been updated.\n"),
])
def test_composed_example_fence_and_hidden_status_cannot_supersede_site(example_body):
    ctx=tsumgi()
    ctx["sources"].append(event(2,"pull_request_comment",body=example_body))
    view=stage.build_feature_first_view(ctx,d1._original_plan([ctx],CAP)).report()
    assert not view["excluded_obsolete_status_hints"]
    assert any("change to running GA services" in r["verbatim"]
               for r in view["latest_operational_caveat_evidence"])
    audit=quality.inspect_scope_quality(ctx,[sentence("Bonsai Nixを実装しました。")])
    assert audit["source_claims"]["no_site_promotion"] is True
    assert audit["source_claims"]["site_transition_reported"] is False
    assert "original_no_deploy_condition_omitted" in audit["known_rule_violations"]
    assert audit["publication_authorized"] is False
    assert view["source_deleted"] is False


def test_true_status_after_closed_nested_fence_and_blank_is_still_visible():
    ctx=tsumgi()
    fence=chr(96)*4
    ctx["sources"].append(event(2,"pull_request_comment",body=(
        "## Example output ##\n\n"
        +fence+"text\n"
        +chr(96)*3+"\n"
        "Running GA services have now been updated.\n"
        +fence+"\n\n"
        "## Actual status: ##\n"
        "Running GA services have now been updated.\n"
    )))
    view=stage.build_feature_first_view(ctx,d1._original_plan([ctx],CAP)).report()
    assert not view["latest_operational_caveat_evidence"]
    assert any("site" in row["superseded_scopes"]
               for row in view["excluded_obsolete_status_hints"])
    audit=quality.inspect_scope_quality(ctx,[sentence("機能を追加しました。")])
    assert audit["source_claims"]["site_transition_reported"] is True
    assert "later_source_site_transition_requires_human_review" in audit[
        "known_rule_violations"]
    assert audit["semantic_completeness_certified"] is False


def test_emphasized_real_status_after_comment_and_examples_remains_eligible():
    contexts,_=make_original_feature_context()
    current=contexts[1]
    current["sources"].append(event(4,"pull_request_comment",body=(
        "<!--\n## Actual status:\n"
        "The privileged effect worker is installed and enabled.\n-->\n"
        "## Example output ##\n"
        "The privileged effect worker is installed and enabled.\n\n"
        "**Actual status:**\n"
        "The privileged effect worker has now been installed and enabled."
    )))
    view=stage.build_feature_first_view(current,d1._original_plan(contexts,CAP))
    data=view.report()
    assert any("worker" in row["superseded_scopes"]
               for row in data["excluded_obsolete_status_hints"])
    assert data["publication_authorized"] is False
    assert data["source_deleted"] is False


@pytest.mark.parametrize("prefix,inner_heading", [
    ("## Example output", "## Actual status:"),
    ("## Example output ##", "## Actual status: ##"),
    ("## Model reply ###", "### Current status: ###"),
    ("## Current status: Sample output (mock)", "## Actual verified status"),
    ("Example output:", "Actual status:"),
])
def test_real_site_caveat_survives_nested_status_header_without_sample_payload(
    prefix, inner_heading,
):
    ctx=tsumgi()
    ctx["sources"].append(event(
        2, "pull_request_comment",
        body=(prefix+"\n"+inner_heading+"\n"
              "Running GA services have now been updated.\n"),
    ))
    view=stage.build_feature_first_view(ctx,d1._original_plan([ctx],CAP)).report()
    assert not view["excluded_obsolete_status_hints"]
    assert any("change to running GA services" in row["verbatim"]
               for row in view["latest_operational_caveat_evidence"])
    audit=quality.inspect_scope_quality(ctx,[sentence("Nix実装を追加しました。")])
    assert audit["source_claims"]["site_transition_reported"] is False
    assert "original_no_deploy_condition_omitted" in audit["known_rule_violations"]


def test_real_worker_status_after_immediate_nested_sample_header_stays_example():
    contexts,_=make_original_feature_context()
    current=contexts[1]
    current["sources"].append(event(4,"pull_request_comment",body=(
        "## Example output\n"
        "## Actual status:\n"
        "The privileged effect worker is installed and enabled.\n"
    )))
    data=stage.build_feature_first_view(current,d1._original_plan(contexts,CAP)).report()
    assert not any("worker" in row["superseded_scopes"]
                   for row in data["excluded_obsolete_status_hints"])
    assert any("not installed" in row["verbatim"].lower()
               for row in data["latest_operational_caveat_evidence"])
    assert data["source_deleted"] is False


def test_nested_immediate_status_inside_example_then_real_new_paragraph():
    ctx=tsumgi()
    ctx["sources"].append(event(2,"pull_request_comment",body=(
        "## Example output\n"
        "## Actual status:\n"
        "Running GA services have now been updated.\n\n"
        "Running GA services have now been updated.\n"
    )))
    data=stage.build_feature_first_view(ctx,d1._original_plan([ctx],CAP)).report()
    assert not data["latest_operational_caveat_evidence"]
    assert any("site" in row["superseded_scopes"]
               for row in data["excluded_obsolete_status_hints"])


@pytest.mark.parametrize("hidden_comment", [
    "<!--\n--> <!--\nRunning GA services have now been updated.\n-->\n",
    "<!--\n--> <!--\n## Actual status:\nRunning GA services have now been updated.\n-->\n",
    "<!-- note --> <!--\nRunning GA services have now been updated.\n-->\n",
    "<!--\n--> <!--\nRunning GA services have now been updated.\n"
    "--> <!--\nRunning GA services have now been updated.\n-->\n",
])
def test_reopened_html_comment_cannot_supersede_real_site_gate(hidden_comment):
    ctx=tsumgi()
    ctx["sources"].append(event(2,"pull_request_comment",body=hidden_comment))
    view=stage.build_feature_first_view(ctx,d1._original_plan([ctx],CAP)).report()
    assert not view["excluded_obsolete_status_hints"]
    assert any("change to running GA services" in row["verbatim"]
               for row in view["latest_operational_caveat_evidence"])
    audit=quality.inspect_scope_quality(ctx,[sentence("Nix backendを実装しました。")])
    assert audit["source_claims"]["site_transition_reported"] is False
    assert "original_no_deploy_condition_omitted" in audit["known_rule_violations"]


def test_html_comment_close_then_genuine_source_update_can_be_seen():
    ctx=tsumgi()
    ctx["sources"].append(event(2,"pull_request_comment",body=(
        "<!--\n"
        "--> <!--\n"
        "Running GA services have now been updated.\n"
        "-->\n"
        "Running GA services have now been updated.\n"
    )))
    data=stage.build_feature_first_view(ctx,d1._original_plan([ctx],CAP)).report()
    assert any("site" in row["superseded_scopes"]
               for row in data["excluded_obsolete_status_hints"])
    assert not data["latest_operational_caveat_evidence"]
