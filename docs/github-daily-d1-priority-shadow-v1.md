# Daily v2: d1-3B Primary paragraph-priority shadow v1

**Scope/authority:** opt-in, read-only, private and nonpublishing. Tracking:
[#381](https://github.com/upiscium/ObsidianAutomation/issues/381), parent
[#283](https://github.com/upiscium/ObsidianAutomation/issues/283);
stacked upon Draft [#379](https://github.com/upiscium/ObsidianAutomation/pull/379).

The user selected **Liquid AI d1-3B as the Primary System One model**
(2026-10-17 JST). Clef-Flash and its simultaneous third-model residency
are deferred unless a later concrete requirement justifies them. Neither
the existing TSUMGI serving profile nor installed models/Workers should be
changed for this feature.

## Motivation and actual comparison evidence

2026-10-16 real GitHub CAS captured 87 original events for 10 changed
Issue/PR entities. C5 already creates source-supported Japanese narrative
for 10/10, but misses recent material milestones/remaining operational
gates in some examples. Model-only filtering is not safe: on 90 disjoint
assistant-labelled source spans from the same day, d1-3B preserved 31/38
provisionally important segments (81.6% recall) even after adding a
deterministic exact-SHA duplicate-commit hint. Clef preserved 30/38;
neither model has demonstrated perfect recall. Labels are unverified
human-ground-truth and same-day-only. This is **not** a claim of
population-level model accuracy.

Many stacked PRs carry the exact same commit SHA, although each PR
can show a new commit list to the Collector. Repeated identical commit
SHA is authoritative evidence of the same Git object, unlike only a
matching commit subject; **it is not evidence of commit push time or
automatically a semantic irrelevance label**. Deterministic source
reconciliation should precede model priority recommendations.

Private initial research data and scripts are on Deneb at
/root/Documents/ResearchEvidence/TSUMGI-SystemOne-Compare-20261017/
and the full original Daily CAS/replay archive is on Adam under
~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-C5-RecentSource-20261017/.

## Construction

The only public-style entry is build_private_d1_priority_plan, which calls
the existing C4 verified context reconstruction, itself based on immutable
Collector CAS, C1b/C2a, and C3. The new module never changes that
source or its trust/citation indexes.

- Collect PR commit SHA only from original verified pull_request_commit
  events. Group within repository, compare exact lowercase 40-hex values,
  and report all other-numbered PRs carrying the same SHA. The
  earlier-numbered relation is not a formal parent/child claim.
- Segment *all original displayed* title/body/message fields into
  contiguous exact offsets, paragraph breaks, and bounded spans
  (currently 1500 characters maximum). Source offsets and whole-field SHA256,
  original evidence_id, source_ref, capture index SHA256, Draft/temporal
  semantics, and byte-unmodified fragment SHA256 are retained.
  Pure whitespace separators are not model classification targets, but the
  full Collector source remains unchanged. If the source/plan exceeds
  bounded budgets, fail closed instead of fabricating head/tail text.
- Compile one existing TSUMGI POST /v1/decisions contract per fragment:
  the reviewed existing d1 Profile ID and exact pinned profile_revision,
  Semantics ID, Deployment revision, and Serving Contract revision,
  original data state, one three-way choice question, and ordered category IDs
  must_include / supporting / background. A verified reused commit SHA is
  kept in host provenance metadata; the model receives a short
  duplicate-PR hint rather than bulky SHA strings.
- Admission checks TSUMGI's normalized result shape: profile revision,
  decision-semantics identity syntax, shadow/recommendation-only mode,
  uncalibrated choice-set_probability scores, ordered finite normalized
  scores, legal abstention, and zero output tokens. Any other otherwise
  syntactically valid TSUMGI/Clef profile is rejected rather than
  mislabelled as d1-3B. Huge integer/NaN responses are rejected as
  pending instead of triggering float-conversion exceptions. Ordinary
  transport exceptions including RuntimeError also remain pending.
- The bounded shadow runner uses an **injected transport**. No network or
  durable Job is submitted merely by importing/building a plan. An invalid
  request, abstention, invalid result, or transport error remains pending,
  not "no progress" and not a candidate for source deletion.
- No original segment is removed or automatically hidden. Existing C4
  source_refs, C5 editorial input, post-edit Grounding, Writer/Timer,
  Nextcloud/Vault, and main branch remain unchanged.

## Existing managed TSUMGI gateway bounds

The only admitted profile in this exact source revision is the already
running d1-based dual-systemone-v1, with these **pinned identities**:

- Profile Revision: sha256:949c53c95f5d9cde5b457a9c41f1a54823f2aa36f7e5d5f243523caa540f4ee7
- Decision Semantics ID: sha256:a205bcadf619d8173bd56f091e9d5bfcf043c2bc5d05c10b2a72767cf691c4cd
- Deployment Revision: sha256:cb3c03a5a9bd4b917b7d1e2fa3ef6b23b8cb2e21c9a75705d31df2f606bc621d
- Serving Contract Revision: sha256:5898c20d4f6cd02844180bb91925e0a42b199768dc62560555afd0161bdf55c9

Observed by private read-only GET /v1/decision-profiles on the existing
Control host on 2026-10-17 JST:

- 128 state tokens; 2048 state bytes
- 128 question tokens; 512 question bytes
- 384 aggregate input tokens
- at most 4 choice labels; each label at most 256 bytes / 64 tokens
- request at most 8192 bytes
- mode shadow; status uncalibrated; authority recommendation-only
- the profile binding is versioned, not guessed from the model alias.

The new compiler respects these byte/choice limits; token admission remains
the TSUMGI provider's authority. It does **not** truncate overly long
captured sentences or silently retry other models. Model request inputs
that exceed this exact profile become pending. A future larger-context
Daily-specific d1 profile would require separate immutable binding,
deployment compatibility/latency/capacity review and operator approval,
and must not overwrite dual-systemone-v1.

## Live managed protocol acceptance — **not** quality certification

The C4 source from saved 2026-10-16 CAS was reconstructed and produced
241 exact original source spans over all 87 events and 10 entities;
51 spans carried a commit SHA also present in at least one other selected
PR. Its original C4-derived private canonical plan SHA256:

    91315eb549fcc113d8174f28122fe68638c70631492c70d9c73b9c25fd5a1a2d

Read-only instructions compiled 15 predetermined test source fragments for
managed Control → PostgreSQL Job → GPU Worker → d1-3B, without altering
service/profile/configuration or deployment. Initial long metadata sent
9 valid managed inference responses with 6 genuine 400 state-token-limit
rejections, which were retained as evidence. A versioned compact request
removed redundant SHA/metadata **without truncating actual source text**:

- 15/15 HTTP 200, all normalized responses admitted through the new
  host-side d1 validator.
- 5 choice recommendations; 10 policy threshold abstentions; 0 HTTP errors.
- The 10 abstentions, unlike wrong sources, are not treated as "no progress".
- 241 original fragments continue to exist; 15 were sampled, not 241
  confirmed-quality classifications. Most sampled fragments were short
  progress headings or commit titles. This is only API/authority smoke.

Private receipts, error bodies, source samples and plan on Adam:
~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-D1-Shadow-20261017/

## P2 review and resolution

The first exact-source read-only correctness and security reviewer runs found
three P2 advisory-boundary defects: a malicious 400-digit integer model score
could raise uncaught OverflowError instead of being pending; a RuntimeError
from a callback could abort all remaining shadow observations; and a caller
could bind an unrelated otherwise valid TSUMGI decision Profile while the
output still asserted d1-3B. All three were fixed with focused regressions
before progressing this Draft. The pinned existing d1 profile and four
identity values are now mandatory. An independently re-reviewed final HEAD
and full Ubuntu/Nix test are separate gates.

## Remaining acceptance

1. Independent source follow-up and full test suite on exact fixed Draft HEAD.
2. Human-reviewed relevance labels covering other dates/repositories and
   harder short/long/negated source passages; measure must-include Recall,
   especially no-live/Draft caveats. A score is not calibrated correctness.
3. Model a conservative, non-destructive C5 *priority presentation* based
   on the recorded observations and exact source refs; compare to the
   existing recent whole-source v3 mode. Reject regression in newest
   material milestones rather than optimizing raw unconfirmed count.
4. Only consider deployment or any source-selection authority after
   separate user approval. Do not add general-purpose contradiction
   detection or a new broad semantic judge.

No permission is requested or implied for source deletion, main merge,
production Writer/Timer, or Clef-Flash rollout.
