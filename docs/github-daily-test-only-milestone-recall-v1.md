# Daily v2: test-only progress recall — nonpublishing source-only experiment

Issue: #386. Base: Draft PR #391, post-#417 exact parent
`4640b581a196e9b86251098eaf76e3ed8947117a`.

## Goal

A change to **test-only code** remains a real Issue/PR progress milestone.
Previously, source-verified test-only PRs could be selected by C3 yet
return zero C5 sentences. An empty summary is not evidence of no progress.

This is deliberately **not** an attempt to solve general contradictions
or declare feature implementation complete. It is a minimal improvement to
baseline C5 editorial guidance, preserving the existing baseline source
window and full original C4/CAS/Grounding. The optional feature-first path,
d1 system, root Collector, Writer/Timer, model installation and publication
are not changed.

## Private live-model pilot, different GitHub PRs

The frozen 2026-10-18 real-source capture has 71 GitHub events from two
repositories, 53 verified source routes, 15 immutable C1a/C4 contexts, 15
source-grounded C1b replies and 19 C2a replies. A separate local, opt-in
pinned-Gemma4 inference trial changed **only an in-process editorial prompt**
and then reran original C4/CAS C5 and post-edit Grounding.

| Real PR | Prior baseline C5 | Pilot baseline C5 | Qualified interpretation |
|---|---|---|---|
| PROJECT-SUZKA #330 | zero sentences | one supported sentence | reports a `test(r15)` preflight code change, not a completed product or successful test |
| PROJECT-SUZKA #327 | zero sentences | one supported sentence | reports a `test(r15)` replay/prototype test change, not a shipped feature |

The pilot also explored a **different, feature-first prompt** on PR #155,
#158, #328, and #331. These are *not* included in this code change:
#155 still had a merged-status/caveat sentence rejected by Grounding,
and #158 still yielded no supported feature-first sentence. Retaining this
counterevidence is intentional: simply adding prompt instructions is not
sufficient to certify the whole feature-first architecture.

The trial's original source and model receipts are retained under
`~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-Acceptance-20261020/`,
not committed to this repository or sent to Writer.

## Implementation

- Version the opt-in recent-source **prompt policy** from v3 to v4.
- Explicitly treat a `pull_request_commit` with `test(...)` as a possible
  test-only **code-change** milestone. Request a narrow Japanese summary
  citing only model-visible source refs.
- Never infer that tests passed, product feature completed, commit was
  pushed during the observed window, PR merged, or production was deployed.
- Preserve existing `RECENT_SOURCE_CONTRACT` and all event validation,
  source citation admission, unsupported sentence filtering and pending refs.
- Keep Issue/PR changes, tests, reviews, decisions, blocked conditions and
  non-test milestones equally eligible; no test-only exclusive routing.
- On an empty or invalid model response, leave original events pending as
  before. Never fabricate a deterministic Japanese sentence or enable
  automatic publication.

## Acceptance limits

This is **SOURCE-ONLY, nonpublishing** integration readiness, not
semantic-quality GA acceptance. The live-model positive evidence covers two
real test-only PRs. Before any installed/production promotion, independently
adjudicate meaningful-milestone recall and negative cases across other
repositories and days, as well as exact original source fidelity, C5
post-edit Grounding, latency/throughput and existing Writer/Timer gates.

A separate assistant-proposed, source-ref-anchored **12-milestone lexical
proxy** over the 2026-10-18 real set found low recall in frozen C5 baseline
and feature-first outputs. It is **not** human-labeled semantic ground
truth, not a quality PASS, and is kept local pending independent adjudication.

No `main`, upstream Draft merge, Writer/Timer/LXC/Vault/Nextcloud,
installed model profile changes or live production rollout is authorized by
this experiment.


## Frozen non-test holdout with the same v4 prompt

To test that the additional test-only editing instruction does not blindly
promote every PR to a test milestone, a second isolated experiment used the
**exact v4 prompt** on five real, source-verified non-test Issue/PR contexts,
with the same pinned Gemma4 model, options, original C4/CAS and post-edit
Grounding. It kept all original source refs and did not call Writer/Timer.

| Real source (2026-10-18) | Previous supported C5 sentences | v4 supported C5 sentences |
|---|---:|---:|
| TSUMGI PR #155 | 1 | 2 |
| TSUMGI PR #158 | 1 | 2 |
| PROJECT-SUZKA PR #328 | 2 | 2 |
| PROJECT-SUZKA PR #331 | 2 | 2 |
| TSUMGI Issue #151 | 2 | 2 |

All five v4 paths completed with **2 supported / 0 unsupported** after
original post-edit Grounding. This is a narrow, positive non-test regression
signal, **not** evidence of perfect milestone recall. The existing
source-only audit still flags PR #155's **unreported merge transition** and
**missing no-GA-cutover qualifier**, plus any incomplete source scan.
These flags are deliberately retained even when Grounding accepts a sentence.

In the two independent actual test-only PR contexts #327 and #330, the
same v4 editing policy produced **1 supported / 0 unsupported** each,
where the frozen v3 baseline returned no sentences. Thus the private pilot
tested **seven** real entities (two test-only and five non-test) from two
different projects, with no PR-specific output rewrites or model installation.
The independently human-adjudicated holdout remains an explicit blocker.
