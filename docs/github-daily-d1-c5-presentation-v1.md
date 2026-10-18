# d1-3B Primary → C5 opt-in advisory source presentation (experimental)

Tracking [Issue #381](https://github.com/upiscium/ObsidianAutomation/issues/381).
Parent [Draft PR #382](https://github.com/upiscium/ObsidianAutomation/pull/382).
This child only experiments on the C5 text model input. It does not alter
Collector/CAS, C1b/C2a/C3, the original C4 evidence, or C5 full-original
Grounding and Writer/Timer authority. No main merge, production deploy,
Clef-Flash usage or third-model co-residency is in scope.

## Why

In the 2026-10-16 saved GitHub day (87 original events, 10 Issue/PRs), the
existing C5 recent-full-source view preserves the last two substantive
updates per entity. It nevertheless sometimes foregrounds source review /
CI / test counts while omitting the feature core, notably PR #367.

The installed d1-3B system-one Profile
`dual-systemone-v1` (pinned revision
`sha256:949c53c95f5d9cde5b457a9c41f1a54823f2aa36f7e5d5f243523caa540f4ee7`)
was exercised via the actual TSUMGI managed Control → PostgreSQL → Worker
path in the preceding Draft #382. On 15 selected short source spans:
five returned recommendations, ten abstained, zero HTTP errors. The
configured state token cap is 128; unrepresentable long paragraphs remain
unclassified, not silently truncated or marked irrelevant. d1 scores
are uncalibrated and never authorize discarding material source events.

## New model-facing view, always C4-source-bound

`build_private_advisory_c5_view(context, plan, shadow)` takes only an
unchanged original verified C4 context, the immutable d1 source-span
plan, and the normalized private d1 shadow record. It verifies that
the plan matches the original CAS Capture identity, target Issue/PR,
source_ref, evidence_id, field SHA256, exact character offsets, contents,
collection truncation and temporal/Draft status. It validates pinned
model/serving identities, score shape, advisory mode and nonauthority.

**Both full original sources shown by the existing recent C5 view remain
present**; d1 cannot drop or downgrade either. When it has an admitted
`must_include` classification for a literal fragment, the renderer can
include up to three precise span hints. At most ONE additional original
event `source_ref` can be exposed if the fragment came from an event
outside the original recent two. It is presented as a *partial original
source excerpt*, not a fabricated whole event or proof of push time.
Inferences may cite this original ref only within the model-visible
fragment and are subsequently Grounded against the actual full C4 event.

The C5 runtime accepts the opt-in
`D1_ADVISORY_SOURCE_CONTRACT` only when its caller supplies the same
source-bound d1 plan and shadow. Before inference, the C5 runner derives
the exact allowed reference set from original verified C4; it checks the
returned model input SHA256, plan SHA256 and shadow SHA256 receipt claims
against that trusted view. It rejects citations to any other source ref;
the original strict C4 normalizer and post-edit model Grounding remain in
place. No third-party model score becomes evidence or publication authority.

If no appropriate d1 hint exists, the entire model source view and
system prompt revert byte-for-byte to the previous recent C5 mode.
If the additional hint cannot fit the explicit 16KiB view budget, it is
not half-shown or synthesized; full baseline inputs are preserved.
A malformed plan/decision or contradictory source-provenance view fails
closed. All unpresented source refs stay in the original C4 pending
complement.

The model output is JSON `sentences:[{text,source_refs}]`, at most
two Japanese one-line sentences; for hinted views the text JSON schema
also limits each sentence to 180 chars and prohibits embedded newlines.
The editorial policy prioritizes verified substantive changes before
secondary test results, then the explicit Draft/no-live/other gate.
Source comments, model-generated priorities and injected instructions
inside source are not trusted.

## Native same-CAS test results and limitations

Pinned C5 Gemma 4 tag `gemma4:e4b-it-q8_0`, digest
`3721c8128dc2e6cabed512039c22f63629c0ad6786b0fc852a9e4da47a6953f3`,
was used as in the existing recent-C5 acceptance. The saved Capture was
`043120ee2412fd01024c14ca37b7f1829ee4402c61cefcaf69d820b0592c964a`;
no live GitHub download or Writer was used.

With only the 15 previously classified d1 spans, the opt-in new view
provides changed input to FOUR of the 10 Issue/PRs:
PR #367: supplemental original commit ref 0 alongside existing refs 1,2;
PR #369: emphasis on existing commit ref 4;
PR #373: supplemental feature commit ref 8 alongside existing refs 9,11;
PR #374: emphasis on existing feature commit ref 10.
Other six model source views and prompts revert to previous C5, unchanged.

First system-prompt probe had all-C4-admitted Japanese C5 response for
#367, #369, #374, but #373 returned one long verbatim English source
passage and was rejected by the unchanged C4 validator.

Second, shorter editorial prompt with model JSON maxLength 180 yielded
four complete source-validated C5 runs:
- #369: latest fail-closed local cooperating updater-lock feature and
  the Draft / not-merged qualification.
- #373: Preview UI / IPC feature plus disabled/authority caveat.
- #374: durable pause/resume queue and no installed privileged worker;
  browser Execute was still paraphrased as *becoming unavailable*,
  so temporal semantics need careful review before production.
- **#367 remains a quality failure**: generated sentences focused on
  CI/test pass and source-only limitations, omitting the actual feature
  implemented in the original broker-core PR. The supplementary source
  ref 0 was displayed but not selected by the final two-sentence editor.

These results are an **exploratory, same-day qualitative comparison**.
They do not prove an incremental contribution of d1-3B beyond a changed
editorial prompt, or population importance Recall. The source review and
full test suite check code safety, not prose relevance. Therefore do
not promote the new contract to the production default or claim that
Issue #381's end goal is complete.

Private raw source, model envelopes, C5/grounding receipts and earlier
comparison are on Adam at
`~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-D1-Presentation-20261018/`.
Do not upload private source or API receipts to GitHub.

## Remaining Primary work

A useful next iteration should segment the original source into smaller,
exact, original-line/text spans that fit the existing d1 128-token Profile
without silently losing negative/completion context; increase evaluation
coverage beyond 15/241 segments. Retain deterministic same-commit-SHA
inheritance and latest-original milestone/operational caveat protection.
Before any automatic priority-based input reduction, verify must-include
Recall and false negatives on independent human-labelled other dates /
repositories. Avoid universal contradiction-solving and per-sentence
self-correction stages; the task is useful progress summaries.

Until then: d1-3B remains Primary, Clef deferred, C5 original default
unchanged, and this child remains Draft/unmerged.
