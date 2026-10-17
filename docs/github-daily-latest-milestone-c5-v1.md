# Daily v2 C5 — latest material milestone view (Draft / opt-in)

Parent #283 (Japanese per-updated-Issue/PR progress), #290 (coverage), #379
(C1b bounded source input); integrated source stack #375.

## Scope and cause

The actual 2026-10-16 JST private Daily generated one summary for each of ten
updated Issue/PR entities, but a bounded independent source review found:

- Issue #300 described overall broker responsibilities rather than the day's
  latest Draft #374 durable pause/resume intent handoff (implemented but not
  installed or live).
- PR #311 should distinguish local test-only integration from main merge or
  production cutover.
- PR #374 emphasized review/test evidence rather than the durable handoff
  and the unavailable privileged worker / Run once / browser Execute gate.

The existing C4 input includes every original source event, even cumulative
Issue snapshot bodies containing older policy. The prompt verbally asks for
the latest outcome, but does not prioritize actual dated event evidence in
the model-facing input. This is editorial salience, not a reason to add a
global contradiction checker or alter C3 materiality/source selection.

## Optional adapter contract

The full C4 context and its original CAS evidence are verified as before.
Operators explicitly selecting C5 editorial contract
c4-sentences-latest-milestone-v1 get a MODEL-ONLY bounded projection.
The original c4-v1 and c4-sentences-only-v1 adapters remain unchanged.

The view contains at most two latest action-bearing dated original sources,
prioritizing comments/commits over a cumulative later Issue/PR snapshot and
skipping empty creation shells when useful. It preserves original source_ref
integers and fully enumerates omitted original indices. Its maximum serialized
JSON size is 16 KiB: earlier whole events may be omitted with their refs
reported, but source text is never silently chopped/rejoined. If even one
event cannot fit, the request fails closed.

The focused Japanese prompt prioritizes the latest material
implementation/decision/experiment in the first sentence, and any genuinely
unresolved acceptance/deployment gate in the remaining sentences. Explicit
warnings preserve Draft versus merged, local tests versus production,
day-snapshot versus event-instant text, and immutable commit content versus
proof of push time. Missing evidence must never be invented.

Output is the existing sentences:[{text,source_refs}] JSON. The model schema
and host admission additionally constrain citation refs to only the shown
original event indices. Unsupported refs are refused; no silent normalization
or fabricated success. Original C4 admission keeps the exact raw-output hash
and records ALL unshown/uncited original events in pending_source_refs, while
C2a/C5 Grounding reads the complete original CAS source. Focus ref indices,
effective prompt and exact model-visible source SHA are recorded in the
provider receipt, but are not cryptographic model-execution attestation.

## Non-claims

This is NOT a second model stage, global contradiction model, new semantic
judge, production Writer/Timer path, source selection change, or permission to
publish to Vault/Nextcloud. A two-event view can omit an important older
milestone. A displayed model-supported sentence is still a private AI draft,
not proof of human factuality, overall event coverage, or a GitHub source
signature. Require held-out, source-bound examples to verify usefulness.

The initial model pilot uses the exact saved authenticated 2026-10-16 JST
Capture Index 043120ee2412fd01024c14ca37b7f1829ee4402c61cefcaf69d820b0592c964a
and the already-installed fixed Gemma 4 model revision
3721c8128dc2e6cabed512039c22f63629c0ad6786b0fc852a9e4da47a6953f3.
The missing historical a3d2... model has NOT been recovered and is not
equivalent. A new Draft requires exact-head regression tests, GitHub CI,
read-only independent source review and the actual native sample before any
separate human approval of merge or Writer/Timer activation.

### Implementation-versus-review salience in the model-only view

The first native three-entity pilot improved Issue #300 and PR #311, but
PR #374 still led with Security/Correctness review evidence instead of the
durable pause/resume handoff feature. Therefore the **effective** v2 prompt
(c5-recent-milestone-ja-v2, still one C5 request with the same C4 response
contract) now prioritizes an original implementation commit as the first
model-visible item if the two newest whole events include a commit and a
later review-only comment. If both inputs are comments, newest comment is
first. Model display order is never represented as chronological evidence;
original source_ref, occurred_at and temporal_observation remain unchanged,
and omitted source indexes are still explicitly counted.

The new model-visible shown_implementation_commit_refs is derived solely
from original source event kind, not a semantic judge. Source comment text
may still establish a later development; the prompt must not invent a push
timestamp or live deployment. Post-edit C2a Grounding continues to see the
entire cited original Evidence and rejects unsupported details.

## Small fixed-model acceptance evidence — 2026-10-17 JST

A read-only scratch test used the *same archived real GitHub day*, original
C1b v2/C2a admissions and C3 selection as the earlier baseline, plus the
same pinned local Gemma 4 revision noted above. No new model installation,
main branch, Writer/Timer, Vault, production status or C3 selection changed.
Raw replies and receipts are private on Adam under
/tmp/obsa283-c5-milestone-Hi924Y/private-native*.

The first opt-in trial used two newest events in original chronological
order. It improved Issue #300 (the Draft #374 durable handoff and remaining
live-effect gates were mentioned) and PR #311 (nonproduction status), but
PR #374 still led with source-only review/tests and underreported the added
queue/receipt feature.

The effective prompt was revised to c5-recent-milestone-ja-v2. Only the
MODEL-VISIBLE presentation order changed: an explicit implementation commit
precedes a later review comment when both are present; otherwise the newest
actual dated update precedes older ones. This ordering is never evidence
of event time, and the exact unchanged original source_ref is retained.
With this view, model/C2a supported text for Issue #300 and PR #374 led with
the actual durable handoff and said it is not installed/live; PR #311 kept
the Draft/test-only status but did not always name the original integration
branch itself as unmerged. The latter remains an explicit human quality
issue, not a new gate or automatic source repair.

A further wording correction, effective prompt version
c5-recent-milestone-ja-v3, reminds the model not to confuse the target
Issue/PR merge status with that of related child PRs. A separate PR #311
native test retained the latest handoff and no-live-cutover qualifier, but
the phrase about the actual integration branch/main still was not
reliably emitted. Do NOT claim perfect source completeness as a result.

Two previously unused updated PR examples were also tested with v3:
PR #369 retained the fail-closed updater lock feature and limited local
scope (two model-supported draft sentences); PR #372 retained the
fail-fast Core Promotion lock change and Draft/no-production qualifier
(two model-supported draft sentences). The three selected quality-review
entities #300, #311 and #374 also produced accepted/supported drafts
with their omitted original event refs left pending; source-bound model
Grounding remains advisory, not human factuality confirmation.

Because the process is intentionally only opt-in for private previews, the
original C5 v4 input and other model contracts remain unchanged. Release
still depends on exact-source full regression, independent read-only source
review, another complete private day in the opt-in mode, human-calibrated
readability and separate main/Writer/Timer approval.

## Review correction: omitted-source status, updated snapshots, superseded commits

The first independent source reviews identified three narrow P2 risks
in the initial opt-in presentation (without claiming C4 source tampering):

1. An unshown lifecycle merge could still appear to the model as the
   full-context source_status_ja label, allowing it to cite an unrelated
   shown comment. The corrected focused model view **does not include that
   derived label**. C3/C4's separately source-authenticated UI lifecycle
   status remains available when rendering original Evidence.
2. Excluding every Issue/PR snapshot could hide a new decision made in
   an actually updated target-day Issue body. The corrected input can
   **add the latest independently verified day-updated snapshot after
   the last action/comment** as an auxiliary, citable original source with
   unchanged source_ref and time-scope caveats. It remains distinct from
   the actual action events and may contain accumulated historical text.
   If the most recent action plus an eligible updated snapshot cannot
   jointly fit the 16-KiB view, the opt-in path fails closed; it does not
   silently truncate either into an apparent complete source.
3. Leading with a commit regardless of later comments could misreport an
   abandoned/reverted implementation. The corrected view orders *actual
   action events newest first* (by independently verified original event
   timestamp), supplies earlier implementation commits as additional
   supporting refs, and explicitly says later correction/retraction may
   supersede the commit. An auxiliary updated snapshot appears separately
   after those action events. This never claims source order itself proves
   a completed production effect.

The new effective source/prompt version is c5-recent-milestone-ja-v4.
The original full C4 context, original source refs, raw response hashes,
C2a grounding and all omitted-event accounting are unaffected. These
changes mitigate the identified cross-source citation and salience risks;
they do not guarantee flawless model judgment about a complex source.

## Source-only P2 correction follow-up: effective v5

A first P2 correction v4 was safe in its structure but reduced actual native
quality: the model was shown a full current-day Issue snapshot *after* its
latest action events, causing Issue #300 to regress to old responsibility
discussion. A read-only diagnostic changed only model-facing event order:
auxiliary snapshot first, then original action events from earlier to later.
Original source-ref identities, event times, original C4 admission, C2
Grounding and omitted-ref accounting did not change. The diagnostic restored
Draft #374 handoff progress while keeping the current-day Issue snapshot
available as separate citable context.

The effective opt-in source-view version is c5-recent-milestone-ja-v5.
When a later independently verified target-day updated snapshot exists, it
appears FIRST as supplementary cumulative context. The final meaningful
action/comment is placed last. An older implementation commit cannot
suppress a newer correction or retraction. Presentation order never proves
historical snapshot content at the event instant. A full-source derived
source_status_ja is never copied into the narrowed model view, because the
lifecycle source for that state might be omitted. C4 still separately displays
the lifecycle status independently derived from the original full Evidence.

This is a source-view salience preference, NOT a semantic truth or
complete-coverage guarantee. Omitted original events are always pending
and checked against the unchanged CAS in post-edit Grounding. If a required
whole recent source and a newer updated snapshot exceed the 16-KiB budget,
the opt-in request fails closed rather than silently discarding that
independently verified snapshot.

### Actual native v5 source-bound sample (fixed model and fixed day)

On Oct 16 JST actual GitHub CAS, the C5 view for Issue #300 uses original
source refs [9,7,8] (supplemental updated Issue body, then Phase H/Phase I);
PR #311 uses [7,8], PR #374 uses [10,12].
On the pinned current Gemma 4 model, their private per-entity drafts were
admitted and model-Grounding-supported, saying respectively:

- Issue #300: latest Draft #374 durable operator-intent handoff implemented,
  but no privileged worker/systemd acceptance or production cutover.
- PR #311: only the local integration test-only branch was changed; the
  actual integration and main branches were not modified or merged.
- PR #374: durable pause/resume queue and receipt handoff implemented,
  but privileged effects / Execute remain unavailable.

The earlier unavailable a3d2... model has not been restored. These are
controlled limited samples, not human factuality or full-source coverage
certificates. All raw source/model receipts remain in private operator
evidence, not GitHub text attachments. No production, main, Writer/Timer,
Nextcloud or Vault privileges have been changed.
