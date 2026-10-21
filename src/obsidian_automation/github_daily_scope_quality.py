"""Read-only, bounded C5 development-scope/production-qualification audit.

This deliberately cannot certify factual correctness, edit C5 output, withhold
an Issue/PR, or authorize Writer/Timer. It checks two known omission classes
using exact original C4 excerpts, not model claims or inferred GitHub state.
"""
from __future__ import annotations

import re

from . import github_daily_feature_stage as feature
from . import github_daily_related_status_audit as related_status

VERSION = "daily-c5-scope-quality-review-v1"
_TEST_TITLE = re.compile(r"^test(?:\([^\r\n)]*\))?:", re.IGNORECASE)
_FALSE_PRODUCT_COMPLETION = re.compile(
    r"(?:機能|製品|プロダクト)(?:.{0,10})(?:実装(?:は|が)?(?:完了|済み)|完成)|"
    r"(?:機能実装|製品実装|プロダクト実装)(?:は|が)?完了|"
    r"(?:[A-Z][A-Za-z0-9_-]{2,30})(?:.{0,12})"
    r"(?:実装|開発)(?:が|は)?(?:完了|済み)"
)
_AMBIGUOUS_PR_COMPLETION = re.compile(
    r"(?:この|当該|本)?PR.{0,14}(?:実装|開発).{0,8}(?:完了|完成|済み)"
)
_TEST_SCOPE_JA = re.compile(
    r"(?:テスト専用|テストのみ|テストだけ|テスト用|事前検証|検証専用|試験専用|"
    r"製品コード[^。]{0,20}変更(?:は|を)?(?:していない|なし|されていない))"
)
_NOT_LIVE_JA = re.compile(
    r"(?:本番未(?:導入|反映|適用|展開)|"
    r"本番.{0,38}(?:未|していない|されていない|保留|変更なし|承認が必要|別途承認)|"
    r"(?:この実装|本機能|今回の変更).{0,60}デプロイ.{0,30}"
    r"(?:されていません|されていない|していない|未実施)|"
    r"稼働(?:中|環境|GA|サービス).{0,38}"
    r"(?:変更なし|未変更|変更されていない|変更していない|承認が必要|別途承認)|"
    r"(?:running (?:GA )?services|production site).{0,48}"
    r"(?:unchanged|not changed|approval required|not deployed))",
    re.IGNORECASE,
)
# Model text may directly *invert* the original site condition. This only
# marks a scope-specific review risk, never overrides full-original Grounding.
_DEPLOYED_JA = re.compile(
    r"(?:デプロイ済み.{0,20}本番|本番.{0,24}"
    r"(?:稼働中|導入済み|反映済み|デプロイ済み))"
)
# C4-verified PR lifecycle transitions, not model claims or statements from
# an earlier Draft body, determine whether "unmerged" is a stale day summary.
_STILL_UNMERGED_JA = re.compile(
    r"(?:未マージ|まだ.{0,15}マージ.{0,18}(?:されていない|されていません|していない)|"
    r"マージ.{0,10}(?:未実施|保留)|Draft PR.{0,22}マージ.{0,15}されていない)",
    re.IGNORECASE,
)
_MERGED_DONE_JA = re.compile(
    r"(?:マージ(?:されました|された|済み|完了)|"
    r"統合(?:されました|された|済み|完了)|\bmerged\b)",
    re.IGNORECASE,
)
# A C5 sentence often mentions a related/sibling PR. Such a sentence does
# not discharge the *current* PR's merge / no-GA caveat obligation, even
# when the model cites the current merge alongside an unrelated source.
# This is a deliberately conservative warning heuristic, not NLP grounding.
_FOREIGN_PR_SUBJECT = re.compile(
    r"(?:別(?:の)?|他(?:の)?|関連(?:する)?(?:別(?:の)?)?|前(?:の)?|以前の)"
    r"\s*(?:PR|プルリク)|"
    r"\b(?:another|other|sibling|parent|child|previous|unrelated)"
    r"\s+(?:PR|pull request)\b", re.IGNORECASE)
# A trailing Unicode word boundary is not valid after a digit followed
# by Japanese text (e.g. "PR #505が"); only disallow more ASCII digits.
_NAMED_PR = re.compile(
    r"\b(?:PR|pull request)\s*#\s*([0-9]+)(?![0-9])",
    re.IGNORECASE,
)
_SELF_PR = re.compile(r"(?:この|本|当該|対象)\s*(?:PR|プルリク)",re.IGNORECASE)

# Lexically similar technical terms are not interchangeable:
# an installed/runtime Nix "profile" is not an attested performance profiling
# run. These warnings are deliberately NOT a semantic truth oracle.
_PROFILING_JA_CLAIM = re.compile(
    r"プロファイリング(?:を|が|のため|するため|した|実施)|"
    r"\b(?:performed|ran|conducted)\s+(?:performance\s+)?profiling\b",
    re.IGNORECASE,
)
_PROFILING_NEGATED = re.compile(
    r"(?:プロファイリング.{0,28}(?:未実施|行っていない|していない|していません|未確認|ではない)|"
    r"profiling.{0,30}(?:not performed|not run|unverified))",
    re.IGNORECASE,
)
_NIX_CONFIG_PROFILE = re.compile(
    r"(?:\b(?:nix|runtime|managed|installer|package)\b.{0,105}\bprofiles?\b|"
    r"\bprofiles?\b.{0,105}\b(?:nix|runtime|managed|installer|package)\b)",
    re.IGNORECASE,
)
_PROFILE_PERFORMANCE_SOURCE = re.compile(
    r"\b(?:profiling|profiled|profiler|flamegraph|performance profiling)\b|"
    r"(?:性能.{0,16}(?:計測|分析)|プロファイリング(?:を|した|実施))",
    re.IGNORECASE,
)
# A model's positive "profiling happened" statement is suspicious if the
# only cited source reports that profiling was NOT performed. A raw mention of
# the noun profiling must not count as proof that it actually happened.
_SOURCE_PROFILING_EXPLICITLY_NEGATED = re.compile(
    r"(?:\b(?:no|without|never)\s+(?:performance\s+)?profiling\b|"
    r"\bprofiling\b.{0,34}\b(?:not\s+(?:performed|run|done|completed)|"
    r"never\s+(?:performed|run|done))\b|"
    r"(?:プロファイリング|性能.{0,12}計測).{0,24}"
    r"(?:未実施|していない|されていない|行っていない))",
    re.IGNORECASE,
)
_APPROVAL_LIMIT_SOURCE = re.compile(
    r"(?:\b(?:approved|authorized|authorised|permission|allow)\b|承認|許可)"
    r".{0,195}?(?:\b(?:not|without|never)\b|承認(?:なし|しない)|許可(?:なし|しない|されない)|対象外)"
    r".{0,120}(?:\b(?:production|rollout|deployment|stacked)\b|本番|展開)",
    re.IGNORECASE | re.DOTALL,
)
_APPROVAL_BASIS_INSTEAD_OF_SCOPE = re.compile(
    r"(?:本番|(?:全|stacked|stack).{0,22}(?:チェーン|merge|マージ)|"
    r"rollout|deployment).{0,105}(?:に基づく|基づくものではない|"
    r"根拠(?:ではない|にならない))",
    re.IGNORECASE,
)


def _bounded_cited_original(context: dict, refs: list[int]) -> str:
    """Read ONLY cited original fields, with a strict token/byte bound."""
    chunks = []
    total = 0
    for ref in sorted(set(refs)):
        original = context["sources"][ref]["original_event"]
        for field in ("title", "message", "body"):
            val = feature._field(original,field)
            if val is None or val["truncated"]:
                continue
            data = val["text"].encode("utf-8")
            if len(data)>65536 or total+len(data)>131072:
                continue  # unknown is not proof of an absent signal
            total += len(data)
            chunks.append(val["text"])
    return "\n".join(chunks)


def _about_current_pr(text: str, number: object) -> bool:
    """Conservative attribution only; ambiguity still requires human review."""
    if _FOREIGN_PR_SUBJECT.search(text):
        return False
    mentions = {int(n) for n in _NAMED_PR.findall(text)}
    if mentions and number not in mentions and not _SELF_PR.search(text):
        return False
    return True


def _current_pr_clauses(text: str, number: object) -> tuple[str,...]:
    """Inspect target-bound clauses, not a mixed foreign/target sentence.

    This is a bounded advisory lexical split, NOT general semantic attribution.
    Ambiguous co-referents in the same clause are excluded for human review.
    """
    # Japanese enumeration commas join a shared predicate and cannot be
    # used as subject boundaries (e.g. "マージ、デプロイ、公開されていません").
    parts = re.split(r"[。！？;；\n]|(?<=[A-Za-z])\.(?=\s|$)",text)
    return tuple(part for part in parts
                 if part.strip() and _about_current_pr(part,number))


class ScopeQualityError(ValueError):
    """Caller did not provide a bounded original-C4/source-grounded view."""


def inspect_scope_quality(context: dict, sentences: list[dict], *,
                          related_contexts=()) -> dict:
    """Return advisory *risks*; an empty risk list is not a quality PASS.

    Related, source-linked Issue/PR observations are strictly opt-in and
    can only create a human-recheck candidate, not an inferred deployment.
    """
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
    merge_refs = []
    test_commit_refs = []
    source_scan_incomplete = False
    for idx, src in enumerate(context["sources"]):
        if type(src) is not dict or src.get("source_ref") != idx:
            raise ScopeQualityError("original source order is not bound")
        event = src.get("original_event")
        if type(event) is not dict:
            raise ScopeQualityError("invalid original source")
        if (event.get("kind") == "pull_request_merged"
                and context["entity"].get("entity_type") == "pull_request"):
            # Lifecycle event kind is a typed, C4-verified source observation.
            # Do not infer release, site promotion, or live service changes.
            merge_refs.append(idx)
        if event.get("kind") == "pull_request_commit":
            msg = feature._field(event, "message")
            if (msg is not None and not msg["truncated"]
                    and _TEST_TITLE.match(msg["text"].strip())):
                # A dated test-scope commit is a source-supported progress
                # candidate, NOT proof that tests passed, code deployed, or
                # the commit was pushed at the observed source instant.
                test_commit_refs.append(idx)
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
                        or "tests only" in normalized or "テスト専用" in normalized
                        or "テストのみ" in normalized or "試験のみ" in normalized):
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
    if test_only and _AMBIGUOUS_PR_COMPLETION.search(summary):
        risks.append("test_only_pr_completion_scope_ambiguous")
    if test_only and summary and not _TEST_SCOPE_JA.search(summary):
        risks.append("test_only_scope_not_explicit")
    own_number = context["entity"].get("number")
    # Check each source-cited sentence rather than matching a concatenation
    # across unrelated claims. A related/previous PR's production caveat is
    # not an acceptable substitute for the target PR's own original gate.
    target_site_refs = {r["source_ref"] for r in active_site_negatives}
    cited_own_site_gate = any(
        any(_NOT_LIVE_JA.search(clause)
            for clause in _current_pr_clauses(item["text"],own_number))
        and bool(target_site_refs.intersection(item["source_refs"]))
        for item in sentences
    )
    if deployment_condition and summary and not cited_own_site_gate:
        risks.append("original_no_deploy_condition_omitted")
    if deployment_condition and any(
        any(_DEPLOYED_JA.search(clause)
            for clause in _current_pr_clauses(item["text"],own_number))
        for item in sentences
    ):
        risks.append("original_no_deploy_condition_contradicted")
    if test_title and test_commit_refs and not summary:
        risks.append("test_only_commit_progress_unreported")
    if merge_refs and any(
        any(_STILL_UNMERGED_JA.search(clause)
            for clause in _current_pr_clauses(item["text"],own_number))
        for item in sentences
    ):
        risks.append("merged_pr_claimed_unmerged")
    # A sentence can cite several sources including a real target merge;
    # an explicit foreign-PR claim in that sentence still cannot discharge
    # the target's lifecycle obligation.
    cited_own_merge = any(
        any(_MERGED_DONE_JA.search(clause)
            for clause in _current_pr_clauses(item["text"],own_number))
        and any(ref in merge_refs for ref in item["source_refs"])
        for item in sentences
    )
    if merge_refs and summary and not cited_own_merge:
        risks.append("merged_pr_transition_omitted")
    # High-impact semantic ambiguities from source-cited text: never silently
    # treat "Nix runtime profile" as an actual profiling experiment, or a
    # limitation on the *scope* of approval as the *basis* for approving.
    # This is advisory only; a missing lexical match never certifies truth.
    profile_refs = []
    approval_refs = []
    for item in sentences:
        original_text = _bounded_cited_original(context,item["source_refs"])
        claim = item["text"]
        if (_PROFILING_JA_CLAIM.search(claim)
                and not _PROFILING_NEGATED.search(claim)
                and _NIX_CONFIG_PROFILE.search(original_text)
                and (not _PROFILE_PERFORMANCE_SOURCE.search(original_text)
                     or _SOURCE_PROFILING_EXPLICITLY_NEGATED.search(original_text))):
            risks.append("runtime_profile_misread_as_performance_profiling")
            profile_refs.extend(item["source_refs"])
        if (_APPROVAL_BASIS_INSTEAD_OF_SCOPE.search(claim)
                and _APPROVAL_LIMIT_SOURCE.search(original_text)):
            risks.append("approval_scope_misread_as_approval_basis")
            approval_refs.extend(item["source_refs"])
    if (test_only or deployment_condition) and not summary:
        risks.append("empty_summary_requires_review")
    linked = None
    if related_contexts:
        linked = related_status.inspect_related_later_progress(
            context,sentences,related_contexts)
        if linked["later_related_progress_candidates"]:
            risks.append("later_linked_entity_progress_requires_recheck")
    refs = {
        "test_only": sorted({r["source_ref"] for r in explicit_test_only+product_unchanged})
                     if test_only else [],
        "no_site_promotion": sorted({r["source_ref"] for r in active_site_negatives}),
        "superseded_site_reports": sorted({r["source_ref"] for r in site_not_promoted
            if r not in active_site_negatives}),
        "merged": sorted(set(merge_refs)),
        "test_only_commit": sorted(set(test_commit_refs)),
        "runtime_profile_semantic_risk": sorted(set(profile_refs)),
        "approval_scope_semantic_risk": sorted(set(approval_refs)),
    }
    report = {
        "version": VERSION,
        "status": "needs_human_review" if risks else "no_known_scope_violation_detected",
        "known_rule_violations": sorted(set(risks)),
        "source_claims": {"test_only": test_only,
                          "test_only_commit_progress_candidate": bool(test_title and test_commit_refs),
                          "merged_in_period": bool(merge_refs),
                          "no_site_promotion": deployment_condition,
                          "source_scan_incomplete": source_scan_incomplete,
                          "site_transition_reported": bool(site_promotion_reports)},
        "original_source_refs": refs,
        "model_or_grounding_is_semantic_authority": False,
        "semantic_completeness_certified": False,
        "publication_authorized": False,
    }
    if linked is not None:
        report["related_source_recheck"] = linked
    return report
