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


### Lexical HTML state correction after second independent review

A second read-only independent review found two new P2 counterexamples
against separate comment/tag passes: `<!-- </code></pre> -->` *inside*
an open HTML `<pre><code>` block incorrectly closed the code depth, and
`<pre></pre><!--` on one line failed to carry the open comment state.
The lexer now uses one bounded, ordered token scan for open/close
HTML comments and `pre`/`code` tags. Code tags inside comments are
ignored; comments following a same-line code close remain active, as do
tags following a same-line comment close. Every affected line stays out
of the optional source-status hints while full C4/CAS is untouched.
Negative and positive regression fixtures cover the two exact P2 cases,
their close/reopen variants, and explicit true status after closure.
Final CI/full-suite/independent review must be repeated for the new head.


## PR #417 additional cross-entity generalization gate (2026-10-21)

The acceptance review does not equate passing the three original failed
PRs (#155/#330/#158) with general performance. It adds a *new* synthetic
source-only holdout covering four unrelated repository names, multiple unseen
PR numbers/features, actual typed merge vs no merge, source-ref provenance,
test-only commits and an Issue case; none of the new identifiers is special
cased in application code. The new cases preserve complete original C4 refs
and require an advisory result to remain nonpublishing.

This uncovered a **real additional correctness risk**: a sentence about a
*different PR* having merged previously satisfied the current PR's
`merged_pr_transition_omitted` check solely because the text contained
`マージされました`. The revised advisory audit requires that the merge
sentence **also cite the current PR's exact typed C4 merge-event source_ref**.
An unrelated PR's merge cannot discharge the current PR's own unreported
state transition. This check is source binding only; even a correctly cited
Japanese sentence is not automatically certified truthful or complete.

For a separate 2026-10-18 real-source evaluation, all 15 original C4/CAS
contexts from two repositories were reconstructed from immutable private
GitHub Evidence/C1b/C2a. Both previously saved baseline and optional C5
results were audited under the new source-ref rule without text rewriting:
exactly one target had a typed merged-PR event (TSUMGI #155), and the other
14 entities were not fabricated as merged. Original raw source and model
responses stay in private Adam ResearchEvidence, not this repository.
This is a verified replay of saved text/grounding, not fresh generator
inference or independent human-labeled semantic-coverage proof.

Generalization acceptance remains **SOURCE ONLY**. The rule must not be
misrepresented as automatic quality acceptance; downstream human relevance
review and independently accepted live-role/Writer/Timer behavior remain
outside this development candidate.


### Independent cross-PR review: three advisory attribution corrections

An independent read-only Correctness review of the first generalization
candidate identified three new P2 blind spots. A foreign PR's merge with a
**co-cited target merge source**, a foreign PR's "unmerged" statement, or a
foreign PR's "not deployed" statement could be attributed to the target PR.
The revised **advisory-only** per-sentence audit now rejects explicitly
foreign-PR attribution (Japanese and English patterns), checks exact
`PR #number` mentions (including Japanese directly after the digits),
requires own typed C4 merge event source refs for merge completion, and
requires target-owned original no-live status refs for no-deploy caveats.
Unclear mixed-referent statements remain subject to human review; the
system does not parse arbitrary natural-language coreference or rewrite
model output. Synthetic holdouts cover each reviewer counterexample,
including a co-cited merge source_ref and positive target-owned controls.

This is a *risk detector*, not a proven solution to semantic attribution.
No source Evidence is omitted from original CAS; neither C5 path gains
automatic winner/publication authority. The real other-day corpus still
contains known incomplete or unsupported model text and no independent
human-confirmed relevance labels.
