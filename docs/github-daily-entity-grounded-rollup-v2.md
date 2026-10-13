# GitHub Daily v2 — Issue/PR entity rollup, read-only candidate preview

- **Issue:** [#332](https://github.com/upiscium/ObsidianAutomation/issues/332)
- **User output contract:** [#324](https://github.com/upiscium/ObsidianAutomation/issues/324)
- **Input:** [Draft PR #331](https://github.com/upiscium/ObsidianAutomation/pull/331) singleton candidate Grounding, built on [#327](https://github.com/upiscium/ObsidianAutomation/pull/327) entity contexts and [#325](https://github.com/upiscium/ObsidianAutomation/pull/325) preview
- **Route source dependency:** [#328 / Draft #329](https://github.com/upiscium/ObsidianAutomation/pull/329) NOT wired; v1 ambiguity remains quarantined
- **Status:** Experimental nonpublishing source/preview primitive. **Not** a final Daily Summary/Projection, model invocation, live WebDAV Writer or scheduling change

## Why this layer exists

The accepted Daily format has **only** materially progressed Issue/PR entities
and verified same-day PR merges, one entry per Issue/PR under its repository
heading, without repository-wide narrative. The v1 pipeline groups by
repository/kind and can omit evidence or combine several entities in one claim.
The new #327 extracts each entity's candidate text and #331 grounds **one**
candidate at a time, but neither guarantees a complete **per-entity** preview.

This layer consumes the **original exact SHA-named v1 Evidence bytes** in memory
and two complete, content-addressed answer maps:

1. `v2 PartialContext SHA -> untrusted model candidate response bytes`
2. `v2 SingletonGroundContext SHA -> untrusted model verdict response bytes`

No model call occurs in this layer. Both maps must contain exactly the
reconstructed source and singleton Grounding contexts, **even when candidate
responses are empty**. Missing/extra/swapped responses and duplicate identities
fail closed; never convert missing work into "no progress." Each answer is
independently checked through #327 and #331 against the original Evidence bytes.
There is an explicit **512 MiB source verification-replay work budget** to avoid
unbounded repeated full-Evidence parse costs; exceeding it blocks the preview.

## What can be displayed

A typed key `(repository, issue|pull_request, positive_number)` is verified
from original Evidence, so an Issue and PR with the same number remain
distinct. For each entity, the preview retains:

- The most recent observed, source-derived lifecycle event within the exact
  JST day, including Issue created/closed/reopened, PR created/closed/reopened,
  ready/draft/merged and human PR review approved/changes requested. These are
  **evidence-record observations**, not remote readback of current GitHub status.
- A same-day PR `pull_request_merged` event flag independently of any model
  text. A later contradictory lifecycle transition sets an unresolved conflict
  and never labels the model summary as verified merged/current-state fact.
  "Merge Accept," green CI and Ready are never upgrades to merged.
- Zero or more **model-supported but still advisory** candidate summaries,
  with exact original Event IDs and candidate digests. Exact repeated text
  deduplicates for the visual *candidate preview* only; original input and
  provenance are retained in their immutable sources. A candidate cannot
  insert its own URL, an unverified merged/deployed statement, another
  Issue/PR reference or nonprintable Unicode layout control.
- Unsupported candidates and possibly material uncited human events remain
  unresolved. Empty model responses or a model that calls everything
  "unsupported" **cannot certify that the entity was idle**.
- Unresolved Issue-vs-PR conversation events, PR commit associations and
  default-branch commits are accounted separately. Old v1 Evidence lacking
  #328's accepted source-route witness **stays quarantined**.

The Markdown preview is explicitly marked **未承認** and prefixes every
model-derived candidate with **要確認**. It renders **one link per entity**
under repository headings and has a fixed 2 MiB output bound. Empty results
are never represented as a verified proof that nothing changed; unresolved
source windows explicitly say review is incomplete.

Result JSON is a private read-model only:

- `requires_human_quality_review = true`
- `publication_authorized = false`
- `incomplete_source_or_materiality` and bounded counts/IDs of sources that
  still require investigation

**A model supported verdict is not proof of semantic materiality**, nor does
it make model-generated Japanese wording final. The preview is **not** a
`# Project Progress` authorized section and has no credential, Writer,
Nextcloud, Daily CAS or timer access.

## What remains before v2 is eligible for a Daily Note

- Independent review, trusted Collector route receipts and operator-approved
  role-isolated persistence for #328/#329, then a new audited admission
  contract that can safely unquarantine verified API-bound comments/PR commits.
- Real entity-scoped model inference (model name/digest/options/prompt SHA),
  singleton Grounding provenance and immutable crash-safe storage, including
  #288/#291 verified resume and #292/#293 atomic CAS.
- Human-calibrated semantic materiality and omission evaluation (#290);
  concise fluent Japanese editorial merging per entity (#283), including
  later evidence resolution and links only from the exact Issue/PR object.
- Versioned v2 Final Summary and deterministic canonical Markdown Projection,
  date-scoped read-only operator preview and separately authorized Nextcloud
  CAS Writer/Timer promotion. The already-published 2026-10-07 immutable v1
  refs and existing v1 production path must remain unchanged.

## Tests and security boundary

All examples use synthetic GitHub Evidence, no network access or production
paths. Coverage includes same-number Issue-vs-PR, multiple repositories, one
Issue spread across multiple Context batches, complete typed response sets,
missing/extra/swapped replies, malformed Grounding, model-invented merge/URL/
different Issue, bot/unresolved sources, zero-progress vs unknown, supported and
unsupported inputs, date-bound verified merge, later lifecycle conflict,
approved/changes-requested human reviews, and deterministic replay.

This code does not call a model or assign publication authority. A successful
local test or second-party review authorizes **neither** merging to main nor
running the Daily Writer, altering a Vault, or re-enabling systemd timers.
