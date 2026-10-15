# Daily individual Issue/PR cross-event consistency audit v1 (#290)

This module is an **opt-in, read-only diagnostic**. It does not synthesize a
better summary, rewrite model text, override Grounding, fetch GitHub, run another
LLM, change stored Evidence, or publish anything to Writer/Timer/Obsidian.

## Inputs and exact trust boundary

`audit_entity_consistency(source_review_bytes, preview_bytes, target_entity,
candidates=(), require_day_policy=True)` consumes the **private, externally
verified** C5 source-review and C4 narrative-preview canonical JSON bytes for
exactly one Issue/PR. It checks their capture/content/context hashes and
publication-false flags. These hashes are consistency checks, **not** GitHub
signatures: the caller must separately establish Collector and CAS authority.

The normal strict path requires #358's `github-day-snapshot-v1` notes on
every event in the entity. Retained original fields must be eligible day
snapshots or content-addressed commits; withheld or out-of-period mutable fields
cannot be referenced. The recorded update timestamp must fall within the exact
selected JST calendar day for a day snapshot, never later than observation time;
missing-update and out-of-day statuses must agree with these timestamps. These
checks validate the C5 snapshot metadata's internal consistency, **not** its
authenticity or the precise historical edit order.
The optional `require_day_policy=False` is explicitly only for
historic/scratch diagnostics and is marked `legacy_unverified` in
metadata. Neither path certifies what text existed at the exact event time.

Each independently supplied `candidate` has exactly:

- `sentence_index`, `claim_start`, `claim_end`: Unicode code-point range
  of an atomic claim within a model-`supported` **displayed** sentence.
- `support`: `{source_ref, field, start, end}`, drawn **only** from the
  sentence's own cited Evidence IDs.
- `counter`: the same span shape, drawn from a different original Event
  in the **same** Issue/PR; the counter does not need to be cited.
- `relation`: one of `possible_contradiction`, `possible_supersession`,
  `consistent`, `uncertain`.

The source and preview keep each original SHA; the report retains only hashes,
bounded offsets, source-ref/field names, advisory relation, and counters.
Neither original source content nor model prose is emitted into report metadata.
Malformed, foreign, duplicated, overlapping, out-of-range or oversized input
rejects with `EntityConsistencyError`, not a silent fallback.

## Semantics: two checks, no implicit authority

**Cited supporting evidence** is a separate dimension from
**potential contradiction in another source Event**. The latter cannot be used
as a replacement citation: the original text, citations and model verdict
remain unchanged. The resulting `potential_cross_event_contradiction` state
is an operator/model *advisory*, never a final finding of fact.

All candidate statements keep `historical_order_unverified` because even
`updated_at` within the target day does not prove the event's exact earlier
field contents or the order of individual edits. A naive comparison of
`occurred_at` from a created Event versus a later comment must not establish
field-level supersession. Unannotated supported sentences remain `unreviewed`.
An Issue with no accepted editorial remains empty/pending, **not no progress**.

A positive `consistent` label only means no advisory conflict was submitted;
it does not prove factuality or exhaust all potentially material updates.
Source roles from #359 and materiality/semantic omissions from #290 require
separate human calibration; structural validity alone cannot compute precision,
recall, or reliability.

## Fixed adversarial and real-source examples

- **PR #354:** an earlier comment says regressions are *underway*, while
  another captured same-entity body reports 34/34 dedicated and 1,826/1,826
  full regressions PASS. The generated last sentence cited the earlier comment
  and was `supported`. This is a candidate **cross-event status
  contradiction**, subject to the historical-body timing caveat.
- **PR #355:** a quoted synthetic example's work is not real completed work.
  Quote attribution remains separate from chronological supersession.
- **Issue #290:** an important unfinished qualification can be missing even
  when `pending_evidence_ids` is empty. This module does not certify semantic
  recall or silently add the omitted clause.
- **Issue #283:** no admitted editorial is a valid pending state, not a
  successful no-progress summary.

Acceptance requires a separately reviewed human/materiality baseline, native
model false-positive/false-negative experiments, verified current policy-v3
source receipts, independent source review, and an explicit authorization to
integrate with Daily execution. The module introduces no new model calls or
changes to rollout defaults. All
`advisory_relations_independently_attested`,
`all_atomic_claims_reviewed`, `factuality_verified`,
`semantic_coverage_verified`, and `publication_authorized` remain false.
