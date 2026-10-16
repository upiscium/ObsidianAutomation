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
