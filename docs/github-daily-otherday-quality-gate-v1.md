# Daily v2 — 2026-10-21 nonpublishing other-day acceptance

Issue: #386. This is an **opt-in private quality evaluation** of the feature-first
and unchanged baseline C5, not a Daily v2 rollout authorization. The exact
original GitHub sources and model responses remain in private Adam
`~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-Acceptance-20261020/`.

## What was actually tested

- 2026-10-16 ObsidianAutomation real source: pinned 87 original events
  across 10 Issue/PR contexts, previously captured through C4/CAS.
  Re-admission of **saved** actual model responses under the current C4/CAS →
  C5 → Grounding code reproduced all 10 preview hashes, 20 previously
  Grounding-supported sentences and zero unsupported; **no fresh inference**
  in that replay and **no independently human-confirmed semantic recall**.
- 2026-10-18 PROJECT-SUZKA / TSUMGI real GitHub re-confirmation (not a
  historical-instant snapshot): 71 authenticated events, 53 verified source
  routes, content-identical private isolated CAS storage, 15 source contexts.
  C1b on pinned installed Gemma4 returned 15 admitted candidate responses;
  19 C2a Grounding responses, 17 supported candidates. C3 selected 15/15
  entities on source-based transitions and operator-diagnostic materiality
  decisions; **43 original events remained pending**. Isolated CAS fixture
  UID/GID is **not a production Collector authority test**.
- Complete paired original day: 15/15 baseline C5 callbacks complete,
  9/9 eligible feature-first C5 callbacks complete. An absent optional
  feature path did not remove the baseline or the source; **d1 feature model
  was not re-run in this day-pair** (verified structural fail-open only).
  Model-generated C5 sentences remain private and are not automatically
  selected or published.

## Actual quality failures — acceptance is NOT satisfied

1. **TSUMGI PR #155**: the original feature-first response called a PR
   `unmerged` despite a later verified, same-day `pull_request_merged`
   source event; the incorrect statement had a Grounding-supported verdict.
   A first revision prevented repeating stale `No merge` text, but still
   omitted the merge. A second, stronger prompt exposes the exact merged
   transition ref and produces a Grounding-supported explicit merge report,
   with *no inference about GA rollout*. **However the feature-implementation
   sentence was unsupported**, so this is a conditional improvement, not
   an accepted complete summary.
2. **PROJECT-SUZKA PR #330**: the baseline accepted an *empty* C5 response,
   despite the verifiable test-only preflight commit. A deterministic advisory
   now flags this as `test_only_commit_progress_unreported`. It does **not**
   fabricate a product-code implementation, a test success, or a commit push
   instant, and it does not generate a replacement sentence.
3. **TSUMGI PR #158**: optional C5 twice invented `profiling` when the
   verified implementation source did not support this claim; original
   Grounding correctly returned `unsupported`. Explicit source-bound
   prompting did not reliably cure it. The optional pair now marks **zero
   supported sentences** as `needs_human_review` instead of presenting
   `no_known_inversion_detected` as useful quality clearance. The baseline
   preserved one supported actual native runtime acceptance result.

These are known counterexamples. An automatic YES → feature-first promotion
or a Grounding `supported` verdict alone are **insufficient** acceptance
grounds. This work is *not* a general contradiction-detection system.

## Safe source-only remediation in this Draft

- The optional feature-first presentation binds typed original
  `pull_request_merged` lifecycle evidence (source_ref and evidence_id) and
  removes older contradictory whole-line `No merge` excerpts from its
  **model-visible hint only**. Mixed no-merge/no-GA lines are not silently
  rewritten. All full C4/CAS evidence and strict post-edit Grounding are
  unchanged. The source-scope audit reports `merged_pr_claimed_unmerged`
  and `merged_pr_transition_omitted` as advisory risks for both paths.
- The private test-only source-scope audit detects a dated `test(...)`
  commit with no C5 summary, even when a historical PR-created body is
  withheld. It never upgrades a test-only commit into a production feature.
- The paired private comparison does not report a quality-clean alternate
  when Grounding supports none of its sentences. Neither path gains
  publication, Writer/Timer, or winner-selection authority.
- **C5 source work budget:** the 256 MiB bound blocked this otherwise
  source-verified two-repository day before any model call; using the
  original source replay-count formula, 15 C1b + 19 C2a replies imply
  808 conservative CAS reads and 355,872,288 bytes of aggregate work.
  A **384 MiB finite bound** admits this workload without changing
  reconstruction limits, read accounting, or fail-closed preflight; boundary
  tests ensure one byte under the required work still fails before HTTP.
  This bound is logical I/O-work accounting, not peak RAM usage.

## Gates still required for final Daily v2 acceptance

- Get a verified material-milestone recall sample with **independently
  human-reviewed labels** across the above different day and projects.
- Make #155 merge-aware implementation text and #158 source-exact
  implementation wording actually Grounding-supported without fabrication,
  and recover #330's test-only milestone without false product completion.
- Confirm both paths' C4/CAS source refs, complete Grounding, original
  status qualifiers, abstain/failure fallback, and model latency on the
  **same exact deployed candidate**.
- Perform a separately approved full production-role/host admission and
  publication/Writer/Timer trial only after the above semantic quality
  gate, independent review, CI and user approval. No main merge,
  installed model-profile switch, Writer/Timer, LXC, Vault or Nextcloud
  action is authorized here.


## Independent source-security review correction (2026-10-21)

An independent read-only reviewer found two additional, concrete P2
source-presentation cases: an HTML `<pre><code>` example carrying a
fake positive GA-site update, and a **multi-paragraph** unfenced sample
whose paragraph separator previously ended the entire site's
illustrative-status suppression. Both could supersede a real older
no-GA-live caveat *inside the optional feature-first hint*, despite
unchanged original evidence.

The bounded status-line lexer now excludes HTML code/pre blocks
(including nested HTML tags and same-line open/close), and requires an
explicit new actual-status/Markdown heading to treat a later same-field
site transition as authoritative after an illustrative example's
blank paragraph. It retains existing independent worker-control
updates after appropriate boundaries and leaves all original C4/CAS
source text available. A previously positive synthetic example case
was intentionally tightened to **deny** an unlabelled post-example GA
assertion; an explicit `## Actual status:` positive control remains
eligible. No source text, Writer authority, or site state is rewritten.

A read-only source review, current exact-head CI, and independent Linux
regressions must be repeated on this corrective commit before any
technical merge decision. Semantic quality remains **not accepted**.
