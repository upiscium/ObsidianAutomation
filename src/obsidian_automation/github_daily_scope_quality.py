"""Read-only, bounded C5 development-scope/production-qualification audit.

This deliberately cannot certify factual correctness, edit C5 output, withhold
an Issue/PR, or authorize Writer/Timer. It checks two known omission classes
using exact original C4 excerpts, not model claims or inferred GitHub state.
"""
from __future__ import annotations

import re

from . import github_daily_feature_stage as feature

VERSION = "daily-c5-scope-quality-review-v1"
_TEST_TITLE = re.compile(r"^test(?:\([^\r\n)]*\))?:", re.IGNORECASE)
_FALSE_PRODUCT_COMPLETION = re.compile(
    r"(?:機能|製品|プロダクト)(?:.{0,10})(?:実装(?:は|が)?(?:完了|済み)|完成)|"
    r"(?:機能実装|製品実装|プロダクト実装)(?:は|が)?完了|"
    r"(?:[A-Z][A-Za-z0-9_-]{2,30})(?:.{0,12})"
    r"(?:実装|開発)(?:が|は)?(?:完了|済み)"
)
_TEST_SCOPE_JA = re.compile(
    r"(?:テスト専用|テストのみ|テストだけ|テスト用|事前検証|検証専用|試験専用|"
    r"製品コード[^。]{0,20}変更(?:は|を)?(?:していない|なし|されていない))"
)
_NOT_LIVE_JA = re.compile(
    r"(?:本番未(?:導入|反映|適用|展開)|"
    r"本番.{0,38}(?:未|していない|されていない|保留|変更なし|承認が必要|別途承認)|"
    r"(?:デプロイ|反映).{0,20}(?:本番|稼働GA|稼働サービス)|"
    r"稼働(?:中|環境|GA|サービス).{0,38}"
    r"(?:変更なし|未変更|変更されていない|変更していない|承認が必要|別途承認)|"
    r"(?:running (?:GA )?services|production site).{0,48}"
    r"(?:unchanged|not changed|approval required|not deployed))",
    re.IGNORECASE,
)


class ScopeQualityError(ValueError):
    """Caller did not provide a bounded original-C4/source-grounded view."""


def inspect_scope_quality(context: dict, sentences: list[dict]) -> dict:
    """Return advisory *risks*; an empty risk list is not a quality PASS."""
    if (type(context) is not dict
            or context.get("stage") != "github_daily_narrative_context_v4"
            or context.get("publication_authorized") is not False
            or type(context.get("entity")) is not dict
            or type(context.get("sources")) is not list
            or not 1 <= len(context["sources"]) <= 256
            or type(sentences) is not list or len(sentences) > 8):
        raise ScopeQualityError("invalid original C4 scope check")
    content = []
    for sentence in sentences:
        if (type(sentence) is not dict or type(sentence.get("text")) is not str
                or type(sentence.get("source_refs")) is not list
                or len(sentence["text"]) > 720
                or any(type(ref) is not int or ref < 0
                       or ref >= len(context["sources"])
                       for ref in sentence["source_refs"])):
            raise ScopeQualityError("invalid cited model sentence")
        content.append(sentence["text"])
    summary = " ".join(content)
    # C4 owns this selected-entity display title as immutable context data.
    # The original PR-created event may be outside the target-day source set,
    # so its title is not necessarily present in original_events. Treat the
    # selected title as a *risk-detection hint only*, never evidence that the
    # feature itself was completed or deployed.
    selected_title = context.get("title")
    if selected_title is not None and (type(selected_title) is not str
                                       or len(selected_title) > 512):
        raise ScopeQualityError("invalid selected-entity title")
    test_title = bool(type(selected_title) is str
                      and _TEST_TITLE.match(selected_title.strip()))
    explicit_test_only = []
    product_unchanged = []
    site_not_promoted = []
    site_promotion_reports = []
    source_scan_incomplete = False
    for idx, src in enumerate(context["sources"]):
        if type(src) is not dict or src.get("source_ref") != idx:
            raise ScopeQualityError("original source order is not bound")
        event = src.get("original_event")
        if type(event) is not dict:
            raise ScopeQualityError("invalid original source")
        if event.get("kind") == "pull_request_created":
            title = feature._field(event, "title")
            if (title is not None and not title["truncated"]
                    and _TEST_TITLE.match(title["text"].strip())):
                test_title = True
        if event.get("kind") in ("pull_request_snapshot", "issue_snapshot"):
            continue
        for field in ("body", "message"):
            value = feature._field(event, field)
            if value is None:
                continue
            if value["truncated"] or len(value["text"].encode("utf-8")) > 65536:
                # Unknown context; mark review, never silently certify.
                # The full original C4 remains available independently.
                source_scan_incomplete = True
                continue
            if any(len(line) > feature.MAX_SOURCE_LINE_CHARS
                   for line in value["text"].splitlines(keepends=True)
                   if line.strip()):
                # The bounded status lexer omits such lines. Without this
                # guard a no-GA-cutover condition buried on one long line
                # can disappear while the risk audit reports no concerns.
                # Do not invent a cropped/unsafe quote; require review.
                source_scan_incomplete = True
            for line in feature._status_source_lines(value, idx, field):
                normalized = line["verbatim"].lower()
                # A title alone is insufficient to identify the change's scope.
                if ("test only" in normalized or "test-only" in normalized
                        or "tests only" in normalized or "テスト専用" in normalized):
                    explicit_test_only.append(line)
                if ("no product code" in normalized or
                        "no product implementation" in normalized or
                        "製品コード" in normalized and "変更" in normalized and "なし" in normalized):
                    product_unchanged.append(line)
                if feature._SITE_NOT_PROMOTED.search(normalized):
                    site_not_promoted.append(line)
                if "site" in feature._positive_status_scopes(line["verbatim"]):
                    site_promotion_reports.append(line)
    # These are source-level reports, never proof of a real site transition.
    # A later qualified positive source report makes an older negative stale
    # for this *advisory*. Inconsistency remains a human-review reason.
    site_uncertainty = any(
        any(feature._status_after(older, later)
            for later in site_promotion_reports)
        for older in site_not_promoted)
    active_site_negatives = [older for older in site_not_promoted
        if not any(feature._status_after(older, later)
                   for later in site_promotion_reports)]
    test_only = test_title and bool(explicit_test_only) and bool(product_unchanged)
    deployment_condition = bool(active_site_negatives)
    risks = []
    if source_scan_incomplete:
        risks.append("original_source_scan_incomplete")
    if site_uncertainty:
        risks.append("later_source_site_transition_requires_human_review")
    if test_only and _FALSE_PRODUCT_COMPLETION.search(summary):
        risks.append("test_only_pr_claimed_product_feature_complete")
    if test_only and summary and not _TEST_SCOPE_JA.search(summary):
        risks.append("test_only_scope_not_explicit")
    if deployment_condition and summary and not _NOT_LIVE_JA.search(summary):
        risks.append("original_no_deploy_condition_omitted")
    if (test_only or deployment_condition) and not summary:
        risks.append("empty_summary_requires_review")
    refs = {
        "test_only": sorted({r["source_ref"] for r in explicit_test_only+product_unchanged})
                     if test_only else [],
        "no_site_promotion": sorted({r["source_ref"] for r in active_site_negatives}),
        "superseded_site_reports": sorted({r["source_ref"] for r in site_not_promoted
            if r not in active_site_negatives}),
    }
    return {
        "version": VERSION,
        "status": "needs_human_review" if risks else "no_known_scope_violation_detected",
        "known_rule_violations": sorted(set(risks)),
        "source_claims": {"test_only": test_only,
                          "no_site_promotion": deployment_condition,
                          "source_scan_incomplete": source_scan_incomplete,
                          "site_transition_reported": bool(site_promotion_reports)},
        "original_source_refs": refs,
        "model_or_grounding_is_semantic_authority": False,
        "semantic_completeness_certified": False,
        "publication_authorized": False,
    }
