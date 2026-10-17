# C1b: compact candidate request v1 (Issue #378; private opt-in)

**Status: DRAFT / NOT NATIVELY ACCEPTED.** This is an alternative input
projection for the *existing* C1b candidate pass. It is **not** a new model
stage, automatic retry, prompt-repair algorithm, admission-policy relaxation,
source materiality authority, or production Writer/Timer cutover.

## Problem

The actual GitHub 2026-10-16 JST day had 87 dated events across 10 Issue/PR
entities. The original native C1b model emitted multiline, overlong candidate
strings on 6/10 entities, including Issue #300 and PR #311; the unchanged C1b
host normalizer correctly rejected those bytes. The original raw responses,
not reworded substitutes, are retained privately in the operator capture.
Those two Issue/PRs were left as *materiality pending*. #375 now shows the
source-linked pending identities in a separate private section.

At the subsequent check, the prior local model tag was missing on Adam, so
this source-only candidate-request prototype has not yet been shown to
increase real model contract adherence. Do not call it a native quality PASS.

## Opt-in request contract

The function build_bounded_candidate_request(context, capture_sha256=...)
independently validates the original **exact C1a Context** against immutable
Collector CAS through C1b, then reopens the same verified partition. It
constructs a private, canonical model input view containing:

- Original repository, Issue/PR key, target JST day and numbered
  source_refs. Links, publication status, and model-selected target IDs are
  never requested as model output.
- Up to the **last 12 original indexed events** (or fewer to meet the
  16-KiB JSON byte envelope), with their original indices unchanged and
  **all omitted indices explicitly listed**. A missing event is *not*
  considered routine, irrelevant or already reported.
- For each shown event: source event kind/time/actor/state and bounded
  title / body / message head/tail excerpts. The two spans are separate
  rather than concatenated as fictitious continuous source text. View-level
  clipping and original Collector truncation are separately indicated.
  The source is the *day-scoped projected Evidence*, never a withheld
  next-day mutable field held privately only in receipt v3.
- A versioned Japanese system prompt asking for at most **two short,
  single-line** factual or clearly unfinished candidate summaries
  (80 characters as a target, hard 160-character schema bound).
  It forbids treating quoted synthetic examples or plans as performed work.
  Empty candidates are allowed; they are **not** positive no-progress evidence.
- The schema offers source_refs only among actually shown original indices.

The returned source-view SHA, schema, prompt version and request fingerprint
are private **diagnostic identity only**, not host/model execution attestation.
The report() contains no original GitHub text and all authority/semantic
flags are false.

The function admit_bounded_candidate_reply recomputes the exact source
request and then first invokes the **original strict C1b normalizer**,
without changing its raw response, hashes, evidence IDs or uncited/pending
events. Only after that does it apply the extra opt-in bounds: maximum two
160-character candidates and citations only to shown indices. A server
ignoring the short-output JSON schema is rejected rather than fixed after
the fact. C2a still independently grounds every admitted claim against its
original full source Evidence.

## Non-claims and remaining acceptance

A compacted view is **not complete source reading**. The all_source_text_visible
flag is false whenever any event is omitted, a field is clipped, or the
Collector source excerpt itself is truncated. Even when all original
event indices are shown, source text may remain clipped. This is an
explicit coverage tradeoff, not an accuracy guarantee.

Source-only fixture regressions must cover stable exact source hashes,
wrong Capture/Route/Context, unavailable source, raw multiline/oversized
rejection, request tampering, citation subset, bounded cost and original
CAS byte immutability. Separately verify compatibility with C2a and C3
source accounting without producing false 'no updates'.

**The key release gate remains open:** on the *same pinned native model*
and unchanged archived authenticated source, compare original C1b and this
opt-in projection for Issue #300 and PR #311 plus at least two controls.
Record raw request/reply/receipt digests, admitted/failed candidates,
grounded verdicts, model truncation, original event coverage and Japanese
legibility. Do not substitute a different model, reinstall a removed
multi-GB model, or declare the prompts successful based only on code tests.
No default adoption, source-provenance relaxation, main merge, Writer,
Timer, Vault or Nextcloud modification is authorized by this Draft.

## v2 candidate-generation mode: most recent full events (opt-in)

The first compact v1 view retained many original event indices but clipped
every large body to a short head/tail fragment. In an independent native
Gemma 4 experiment on 2026-10-17 JST, the initial v1 model returned
empty candidates for actual Issue #300 and PR #311 despite progress-bearing
messages. Thus zero schema errors alone did NOT improve Daily coverage.

The new opt-in build_recent_full_candidate_request function uses the
versioned c1b-recent-full-source-ja-v2 prompt and the same C1b/C2a pipeline.
It preserves source indices but defaults to showing only the last **two**
substantial non-snapshot dated source events, including complete captured
title/body/message (subject to original Collector truncation). A bare
created-event with no retained title/body/message is omitted from the
model view; C3 still retains its structural status. Omitted original event
indices are always disclosed; no original event is deleted from CAS.

The caller can opt in to at most three recent full events. The JSON source
view must fit the same 16-KiB bound. If not, earlier whole events are omitted
and indexed; the original text is never silently clipped or rejoined.
If even one whole event cannot fit, request construction fails closed.
The strict original C1b normalizer first validates the raw model reply,
then the extra compact request policy checks at most two statements of
160 characters and citations to only actually displayed original refs.
C2a Grounding verifies the original complete source again.

admit_bounded_candidate_reply picks the expected source builder based on
the versioned request and recomputes exact bytes before accepting output.
A model cannot gain source or publication authority through either variant.

### New pinned-model comparison (not exact old-model recovery)

The earlier model Digest
a3d2b95350da03ff9b1943a753bb6617c49a3ee462b632a758518ec817743986
is absent from Adam's Ollama manifest/blob store and is not currently
recoverable from the checked remote manifest locations.
The available gemma4:e4b-it-q8_0 has a different pinned Digest:
3721c8128dc2e6cabed512039c22f63629c0ad6786b0fc852a9e4da47a6953f3.
These were NOT misrepresented as the same model.

A new one-shot controlled A/B used the latter revision, temperature=0,
num_predict=2048, identical saved 2026-10-16 JST Capture, and the exact
original C1b/C2a validators:

- Original long-view C1b rejected Issue #300 and PR #311 with multi-line
  summary. PR #367 admitted one model-Grounding-supported claim, while PR
  #374 admitted one that Grounding rejected.
- Compact v1 returned zero candidates for Issue #300, PR #311 and PR #367.
  For PR #374 it admitted two, of which one Grounding model-supported.
- Recent-full v2 with the last two substantial source events admitted
  two model-Grounding-supported claims each for Issue #300, PR #311 and
  PR #367; control PR #374 admitted one model-Grounding-supported claim.

An earlier three-event pilot produced Grounding rejections on one run,
so these results are NOT proof of repeatable semantic accuracy. Source
omission and current-day completeness remain subject to separate review,
and model-supported does NOT mean human factuality approval. No model
reinstall, production mode, Writer/Timer/Nextcloud/Vault, or main merge
is authorized by this Draft.

Private exact model requests/replies/receipts remain only on Adam at
/tmp/obsa379-native-ab-tmH7O4/ and are not GitHub attachments.

### v2 review correction: retain temporal and Draft semantics in input

Correctness and Security read-only source reviewers found a material gap in
the first v2 source projection: it omitted the source Draft marker and the
time scope of source observations despite asking the model to distinguish
Draft, performed work and deployment. The correction now includes these
**bounded, non-authoritative** event fields alongside each entire excerpt:

- Original source-projected draft (true/false/null); null does not prove
  non-Draft.
- The validated v3 temporal_source_status, including
  day_snapshot_not_event_instant, withheld_outside_target_day,
  event_transition_only, and content_addressed_commit_not_push_time.
- historical_instant_verified remains false. Withheld field names, when
  present, are reported, but their original withheld content is NOT added
  back from private receipt v3.
- A legacy source lacking v3 temporal metadata is explicitly rendered as
  legacy_unverified, not silently upgraded to a dated verified snapshot.

The versioned prompt now prohibits treating day-level snapshot body as the
text at the event instant, and prohibits inferring push/arrival time solely
from the immutable commit content. This is **prompt guidance**, not a new
semantic classifier or truth attestation. The original strict CAS/C1b/C2a
normalization remains in charge of raw bytes and citations.
