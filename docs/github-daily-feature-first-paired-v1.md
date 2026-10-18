# Daily v2: feature mention detection and paired C5 evaluation

Tracking Issue #386. This is a private, opt-in, nonpublishing experiment stacked on Draft PR #385. No changes to original CAS, existing baseline C5, main, TSUMGI installed decision profile, Writer, Timer, or any service.

## Motivation

The previous C5 full-original Grounding supported Japanese statements even when the salient implementation itself was absent from the final Daily. PR #367 favored CI/reviews instead of the audited OperationBroker implementation. A non-exclusive two-stage approach tests whether it helps to detect feature mentions before preparing the final Japanese summary.

## Exact contracts

Stage 0: Build the already verified original C4 source context and immutable d1 PriorityPlan. Use exact original source refs and SHA256 identity. The earliest feature-defining PR created section or a unique feature commit provides a structural anchor. Identical Git commit SHA in older-numbered PRs is deterministic inherited-source information, not proof of push date or current PR implementation.

Stage 1: Pinned existing TSUMGI d1-3B, managed dual-systemone-v1 decision Profile, checks broad feature-mention YES/NO on the structural feature anchor. The vote must bind model/profile/semantics/deployment/serving-contract revisions, ordered finite normalized score vector, original feature view and source excerpt SHA256. All scores remain uncalibrated advisory observations. NO or abstention does not delete original source or prevent the baseline C5. Structural source evidence triggers fail-open additional summarization despite classifier NO, because the 16-case pilot missed material positives.

Stage 2: Always run unchanged baseline recent-source C5. For a model YES or verified structural feature anchor, also run feature-first C5 on the *same immutable original evidence*. The new model-facing view contains at most three exact verbatim feature-implementation fragments and one explicit no-live / not-installed / disabled status fragment. This avoids giving CI/test/review text in place of the actual implementation source. Every quote keeps its original source_ref, field, exact offsets and SHA256; unshown refs remain pending. No source deletion. The new C5 runtime verifies the rederived model-visible view and its receipt hash before admitting the draft. The original strict C4 normalizer and full-original post-edit Grounding remain authoritative.

Both drafts are *private alternatives for comparison*, not an automated selection, publication or completion verdict. If the optional path fails, the baseline path is retained. The paired experiment does not include any new contradiction detection, source rewriting or speculative model authority.

## 2026-10-18 preliminary pilot evidence

All tests use the saved authenticated 2026-10-16 GitHub capture (87 events, 10 Issue/PR entities, capture SHA256 043120ee2412fd01024c14ca37b7f1829ee4402c61cefcaf69d820b0592c964a) and pinned Gemma 4 model digest 3721c8128dc2e6cabed512039c22f63629c0ad6786b0fc852a9e4da47a6953f3.

A provisional 16-case original-source classifier test: the existing d1-3B binary feature-mention detector marked 7/10 provisional positives YES and 3 NO. A more specific three-way classifier detected only 2/7 implementation positives. Thus exclusive YES-only filtering is unsafe and is *not* used.

Initial paired C5 experiment:
- PR #367: gate YES; feature-first highlighted audited unwired OperationBroker implementation, which the previous prompt/view omitted. However its initial unfinished-state wording incorrectly inverted unwired; original Grounding did not catch the inversion. Original baseline attempt in this run failed to complete, so this execution was not a complete fair A/B.
- PR #373: gate YES; unchanged baseline completed; feature-first was initially rejected for returning three sentences instead of the required two.
- PR #374: gate YES; both paths completed; feature-first explained the durable pause/resume queue and unavailable privileged-worker effect. The original baseline also had a known conditional mistranslation of the Run once UI action.

Version 2 restricted the extra summarizer to one especially direct no-live quote and made the negation/output-budget instruction explicit. All three feature-first native replays then completed with two Grounded sentences:
- PR #367 now described OperationBroker and no impact on production service.
- PR #373 now described the Preview selector and the preview-only/no-execute restriction.
- PR #374 described the durable pause/resume queue and privileged-worker absence, but still mistranslated Run once into a conditional clause. This *remains a quality failure*, even though Grounding returned supported.

These tests are limited to one day and provisional labels. They do not establish that the full 10-entity Daily has materially complete coverage, that the baseline is fully reproducible in every run, or that separate-day human-adjudicated relevance passes.

## Required next gates

- Exact-head full regression, Github Actions CI and independent source review.
- An independent other-day/repository holdout with human-reviewed important milestones and plans vs implementations.
- Accurate Japanese negation and UI operation availability. A supported Grounding verdict alone is not enough.
- Side-by-side complete baseline and improved C5 on the same source and pinned model, with no deadline or runtime confounds.
- Only later and with human approval: consider production default selection, Writer/Timer access or any merge.

Private inputs, C5 raw model envelopes, managed d1 requests and Grounding replies are stored on Adam at ~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-FeatureMentionGate-20261018/. Never upload original private source material to public GitHub.

## Follow-up: original-source negation and cross-project checks (2026-10-18 JST)

The first independent read-only correctness and security reviews on Draft #387
identified three P2 failure modes. All received narrow corrections:

1. An unavailable optional feature infer, rejected gate vote or invalid optional
   source plan could prevent the baseline C5 from starting. The baseline now
   executes after verified C4 target selection but before any optional
   feature-view, model-vote or callback preflight. Optional preflight failure
   becomes a recorded optional error; the completed original baseline survives.
2. An earlier unavailable-worker statement was previously ranked ahead of a
   later explicit installed/enabled-worker update. The optional view now avoids
   presenting an outdated worker-not-installed statement when a later
   verified source explicitly reports the worker installed/enabled. This is
   conservative source exclusion only: it does not assert deployment or
   validate the later report's actual operational truth.
3. A collector-truncated final line could be promoted as a complete
   implementation claim. That incomplete last line is no longer a model-visible
   original quote, while earlier fully captured lines remain eligible.

The actual C5 prompt had mistranslated the unavailable Run once operation as
a conditional action. An opt-in source-exact UI availability glossary now
provides the correct operator label and narrow Japanese wording, plus a
host-only advisory quality guard against the observed inversion. The first
glossary-guided editorial output was semantically correct but the existing
semantic Grounding *misread the English coordinated unavailable status*
as an after-execution condition and rejected it. The revised **feature-only**
Grounding prompt explicitly disambiguates unavailable actions. It does not
override unsupported verdicts, and the baseline Grounding policy is unchanged.

Replays on the same frozen October 16 CAS and exact pinned Gemma 4 digest
demonstrated:
- PR #367: real disabled/unwired OperationBroker implementation and its
  test-only/nonproduction nature are stated without negation reversal.
- PR #373: Preview UI and preview-only / can_execute=false constraints remain.
- PR #374: durable pause/resume queue plus explicit Run once and Execute
  unavailability; both statements supported by original-evidence Grounding
  after the feature-only UI glossary fix. This passed twice in private
  independent invocations of the same model/settings; uncalibrated semantic
  quality across dates is not certified.
- A narrow source-bound guard flags the *old known wrong* PR #374 sentence
  with a conditional Run once as needs_human_review even if another
  model would Ground it. The guard and private quality report never
  edit source text, rewrite a normalized verdict or grant Writer authority.

Separate-project holdout: real, current October 18 GitHub PR metadata,
created body and discussion from upiscium/PROJECT-SUZKA #328 and #330
and upiscium/TSUMGI #155. This external-source probe is not an
ObsidianAutomation C4/CAS end-to-end capture. Three actual managed d1-3B
binary decisions marked feature PR #328 YES, test-only PR #330 NO, and
feature PR #155 YES, but the sample is much too small for model acceptance.
On unchanged pinned Gemma 4 settings, five summaries were admitted:
- SUZKA #328: feature-first supplied more concrete module-level source
  details than the already reasonable baseline and retained nonpersistent hold.
- SUZKA #330: test-only PR correctly did not trigger feature-first, but the
  baseline contained an ambiguous 'feature implementation complete' phrase
  despite explicit no-product-code limits. This is a real quality review item.
- TSUMGI #155: both named the opt-in Bonsai 2 Nix backend, but feature-first
  dropped the explicit Draft/no-GA-rollout qualifier that baseline preserved;
  choosing new feature-first for every YES would be a quality regression.

Therefore, cross-project evidence is **mixed**, not proof of overall quality.
The exact source, model messages and source-linked provisional judgments
remain private under the Issue #386 ResearchEvidence directory on Adam.
Future quality work must explicitly test product vs test-only wording,
status-caveat retention and other-day/other-project source provenance.
Do not auto-promote a feature-first candidate on model YES or Grounding PASS
alone. No human-independent labels or production-Writer qualification yet.

Final opt-in paired #374 run, with equal pinned model revision and identical
generation options on both C5 paths, completed independently and Grounded
both outputs. The unchanged baseline still translated Run once as an
after-execution condition, whereas the new feature-first path correctly
stated that Run once and browser Execute are both unavailable. The
source-bound protected-status check reported zero known inversions for
the new feature-first result, not a claim of generalized factuality.
The private guarded paired receipt and original post-edit Grounding are
saved in the Issue #386 ResearchEvidence directory under real_pair_guarded.
