# C5 recent-source view v1 — source presentation, not another model stage

**Status:** private, opt-in, experimental. Issue #283 / #290, Draft PR #379.
This addresses the specific real 2026-10-16 JST usability failure where C5
produced correct-looking, Grounding-supported Japanese about old design/reviews
but omitted the last important Issue #300 / PR #374 implementation milestone.

## Why a pure prompt change did not work

The earlier opt-in C5 latest-milestone editorial prompt (commit 06f6668)
failed the archived same-model/same-day three-entity native A/B: Issue #300
still preferred the old broad Product decision; PR #374 still preferred
source reviews/tests. PR #311 did improve the Draft/test-only qualification.
Thus prompt instructions alone are not evidence of substantive coverage.

The new c4-sentences-recent-source-v3 contract **changes only what the
model can read**. The existing C4 context and its original full numbered
Evidence remain source-bound and unchanged, and the post-edit C5 Grounding
still reconstructs the original full cited source from CAS. There is no
replacement CAS, inferred authority, automatic acceptance, new classifier
or Writer/Timer action.

## Deterministic, bounded original-source input

build_recent_c5_source_view receives only the already reconstructed
private C4 canonical context. The model sees up to two **whole captured
original source events** (no head/tail fabrication or truncation) with their
original C4 source_ref numbers. Source events not displayed are explicitly
listed as omitted_source_refs, and the original complete source count is
retained. The host's unchanged C4 validator builds the pending complement
of every original event, regardless of what the model has seen.

Snapshot bodies are not permitted to displace actual dated updates. An empty
PR-created lifecycle shell cannot displace a meaningful code/comment event;
the verified C3 status renderer still retains all structural transitions.
For an Issue, prefer its two most recent non-snapshot meaningful updates.
For a PR, prefer the most recent substantive feature/fix commit or non-empty
created scope, paired with its latest substantive comment/review. Conventional
commit prefixes are only lexical source-selection hints, **not** proof of an
implementation. If there is no such pair, use the last two non-snapshot
meaningful events. If none exists, the final structural event is represented
as such; it is not a false no-progress claim.

The primary_progress_source_ref is the selected implementation source
if available, otherwise the most recent selected original ref. It is a
presentation hint, **not a semantic importance verdict**.

explicit_limitation_quotes contains up to two **literal, unchanged lines**
from the displayed original title/body/message source text that match a
limited set of words such as unavailable, not installed, no live,
未マージ or 無効. Source_ref, field, original line_index and
source-truncation flag are retained. These are deliberately **weak literal
highlights** that can also capture irrelevant quotes or examples; they do not
certify anything. The model prompt says not to treat source quotations as
instructions or to promote simulated changes as real progress. C5
Grounding continues to judge the generated sentence against the original
full cited event, not the highlighted snippet.

The model input is capped at 16 KiB. If two whole events are too large,
the older one is explicitly omitted; if even the newest whole event cannot
fit, inference rejects the input. No operator error or schema failure
is reinterpreted as an empty/no-change day.

## Host-side authority / provenance

ollama_narrative_infer(..., editorial_contract=RECENT_SOURCE_CONTRACT)
uses a separately versioned Japanese prompt and compact source-ref enum
schema with at most two summary sentences. The model request, effective
prompt and model-facing input SHA256 are recorded in its ordinary C5 receipt.
After inference, _admit_editorial re-derives the exact model-visible
source refs from the original untouched C4 context. It rejects any model
claim whose cited source_ref was not displayed, as well as any foreign,
invalid, multi-line, oversized or extra-field reply. The original strict
C4 source/JSON validator is then run *unchanged*; its pending complement
still covers all omitted refs. C5 singleton Grounding remains the same
full-original-Evidence validation. The source view itself grants no
publication, factuality or cryptographic provider attestation.

No existing C5 contract or default has changed. There is no main merge,
Writer/Timer, Vault/Nextcloud, model installation or live GitHub re-fetch.

## Acceptance / limits

Compare the same pinned current Gemma 4 revision and same immutable
October 16 original 10-entity source for Issue #300, PR #311 and PR #374.
Accept a meaningful improvement only if the last supported implementation
milestone and explicit non-deployment / local-test-only qualifier are present
in the *supported* result; a valid JSON response, model Grounding verdict
or positive sentence count **alone** is not sufficient.

Then check held-out real updated PRs for accidental regressions. The view's
two-event budget may miss relevant earlier details; a held-out failure should
remain pending/flagged, not be silently normalized into a false completed
Daily. Independent read-only source review and exact-head full-suite CI are
required before considering the Draft complete. Human readability/factual
approval and release permissions remain separate.

Private original day/replay evidence is archived on Adam under
~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-20261016-PR379/.

## Native pilot on saved day (2026-10-17 JST)

Used authenticated immutable 2026-10-16 JST Capture
043120ee2412fd01024c14ca37b7f1829ee4402c61cefcaf69d820b0592c964a,
the same pinned already-installed Gemma 4 model Digest
3721c8128dc2e6cabed512039c22f63629c0ad6786b0fc852a9e4da47a6953f3
and the same original C4/C5 source and strict post-edit Grounding.

Source read-back showed exact whole source refs, no renumbering:

- Issue #300: original source refs 7 (Phase H) and 8 (Phase I / Draft #374),
  with the trailing larger Issue Snapshot ref 9 omitted from model view.
- PR #311: original refs 7 (isolated integration dry-run) and 8 (Draft #374,
  no production cutover).
- PR #374: original ref 10 (durable queue/receipt feature commit) and ref 12
  (review comment explicitly stating no privileged effect worker and Execute
  paths unavailable), while withheld created metadata stayed withheld.

The raw initial v3 two-whole-source experiment improved the earlier C5
latest-milestone prompt on all three examples: #300 foregrounded Phase I
Draft #374 and unresolved root/production gates; #311 foregrounded Draft
#374 and remaining real systemd deployment gates; #374 foregrounded the
pause/resume handoff feature and the absence of a privileged effect worker.
The later lexically highlighted limitation-line mode maintained this
coverage in three additional source-bound native runs. Each displayed
sentence received a model C5 Grounding verdict; this is NOT human
factuality or semantic-completeness certification.

Two held-out changed PRs showed why this remains **experimental**:
PR #373 summarized its non-effectful Preview UI feature and source-only
local dry-run integration correctly, but PR #367 still overemphasized
successful test/review facts rather than the original unwired OperationBroker
implementation (the PR's source title did identify it). In one #374 run,
the model mistranslated UI action name Run once into a conditional phrase
of the form 'even if Run once is executed', while the original source
actually states that Run once and all browser Execute controls are
*unavailable*. Subsequent prompt guidance did not reliably eliminate the
paraphrase. Valid JSON and a model Grounding-supported response alone
remain insufficient to authorize a production Japanese Daily.

The two weak spots are *reported*, not silently text-repaired, and no new
semantic contradiction model/stage was introduced. The accepted private
source-view API/host normalization keeps every non-shown original event in
the pending complement and any invalid/out-of-view citation is rejected.

Private operator diagnostic source and model outputs are under Adam
/tmp/obsa379-native-ab-tmH7O4/c5_recent_source_trial,
.../c5_recent_source_enhanced, .../c5_recent_source_finalv2,
and .../c5_recent_source_holdout. Preserve those with their model/provider
receipts before deleting any scratch data. Neither Writer nor production
Vault received these drafts.
