# Daily v2 — source-cited semantic risk and linked-day status recheck

Issue: [#386](https://github.com/upiscium/ObsidianAutomation/issues/386).
This is an **advisory-only, source-fidelity** extension to the parent Draft
[#391](https://github.com/upiscium/ObsidianAutomation/pull/391) at
`4640b581a196e9b86251098eaf76e3ed8947117a`. No output rewrite,
new installed model, automatic route selection, Writer/Timer or production
authority is created.

## Why

The project owner reviewed and explicitly confirmed a six-case AI-assisted
2026-10-18 TSUMGI/PROJECT-SUZKA quality assessment. Of 23 generated model
sentences, 4 were factually wrong or obsolete **at day-end**, and all six
Issue/PRs omitted important progress. This review is owner-confirmed, **not**
an independently blind human full-day ground truth. Full 15 entities/71 events
remain a separate scope of acceptance.

The four owner-confirmed negative cases are:

- **F02-02 / TSUMGI PR #155**: a prior Draft/unmerged snapshot treated as
  the end-of-day state despite a typed later same-day merge. The preexisting
  `merged_pr_claimed_unmerged` and `merged_pr_transition_omitted` advisories
  already detect this **when all original C4 event refs are retained**.
- **F03-01 / TSUMGI PR #158**: original Nix/runtime **profile** creation
  misreported as performance **profiling**. This code adds an advisory
  `runtime_profile_misread_as_performance_profiling` warning only when
  the **cited original fields** describe configuration profiles without
  source-cited profiling/performance measurement.
- **B05-02 / PROJECT-SUZKA PR #331**: a source statement saying
  "the user authorized a merge conditional on independent review,
  **not** installed production rollout or stacked-chain merge"
  rendered as an assertion about **what approval was based on**.
  `approval_scope_misread_as_approval_basis` warns on this lexical shape
  when its original source explicitly restricts approval.
- **B06-02 / TSUMGI Issue #151**: the last issue-local comment at
  **20:26 JST** still said Qwen3.8's actual upstream inference had not
  completed. A later **linked PR #155** event included a report of an
  active/ready Qwen3.8 upstream worker. The issue-local original C4
  **cannot** know that cross-entity development. The optional
  optional related-status audit accepts explicitly supplied,
  exact-C4 candidate linked PR contexts only.
  It binds repo, capture SHA, JST date, issue number in original related
  commit/comment, cited target timestamp, **later** original event
  timestamp, and a shared versioned technical token. It records **which**
  related event merits comparison but never declares the issue-local
  statement false, current live site changed, or production cutover
  complete. (The actual runtime flag is
  `later_linked_entity_progress_requires_recheck`.)

## Safety and generalization

Both new same-entity flags are added to the existing nonpublishing,
bounded `inspect_scope_quality` report. The related audit is **opt-in**:
`inspect_scope_quality(context, supported_sentences,
related_contexts=[verified_related_c4_context])`. The current C5 runtime
does **not** automatically discover a related Issue/PR nor mutate its
model-visible original source. This avoids promoting a suggestive
comment, project-wide model identity, or a source timestamp into
unverified release/production authority.

The cross-entity candidate result explicitly reports
`model_claim_contradiction_certified=false`,
`site_or_cutover_attested=false` and
`publication_authorized=false`. A source event timestamp orders recorded
events, but fields marked `day_snapshot_not_event_instant` cannot prove
the mutable body was identical at that instant; therefore **no final
contradiction verdict** is asserted by this logic.

The profile and approval patterns intentionally prefer conservative
human-review warnings over pretending to understand unrestricted
natural language. A missing lexical match does **not** mean semantic
correctness. The citation can be incomplete or truncated; the existing
`original_source_scan_incomplete` risk remains separately visible.
An original perf-profiling report prevents false profile warnings; a
correct "merge approved only, production not authorized" sentence does
not trigger the approval-basis warning.

## Verifications

- Generic synthetic tests cover unrelated repository names and PR/Issue
  numbers, config profile vs genuine profiling, approval scope vs approval
  basis, linked/unlinked, earlier/later, wrong model ID, other repo/day,
  mismatched capture SHA, malformed sources, oversized related sets and
  no auto-promotion. They do **not** hardcode #151/#155/#158/#331 into
  the application.
- In a **private** replay on Adam against original authenticated
  2026-10-18 C4/CAS and frozen C5 sentences, all **four** owner-identified
  negative IDs produced expected source-bound review candidates.
  The #151 related PR produced one *candidate* for cross-entity
  re-evaluation, **not** evidence of completed E2E rollout.
  No raw private GitHub body, model answer or owner notes are committed.

Private source and outcome receipts:
`~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-Acceptance-20261020/owner-confirmed-negative-tests-20261021/private-risk-replay.json`.

## Explicit non-acceptance

Passing the risk audit is **NOT** the Daily v2 semantic quality acceptance
gate. A source-cited model sentence can still omit essential progress;
Grounding can still support a false `profile → profiling` semantic leap;
a later related report is not a signed production probe. Actual content
remediation, independently adjudicated recall/precision across further
days/projects and true Collector/Writer/Timer admission remain pending.

PR #426 (baseline prompt) and #432 (offline metric evaluator) are separate
Draft candidates and must not be silently merged here. This source-only
change also remains Draft until separate human merge approval.
