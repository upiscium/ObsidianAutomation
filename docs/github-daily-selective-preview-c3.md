# C3: selective Japanese private review preview (#324 / #342)

This opt-in child of C2b #353 implements selection and safe presentation, not
production Daily synthesis. It never calls a model, writes CAS or a Daily Note,
alters original Evidence, invokes Nextcloud/Writer/Timer or changes v1/v2 paths.

## Do not equate three different questions

1. Does the original source belong to the selected Issue/PR? C0/C1 validate it.
2. Does a candidate follow the cited source? C2a provides a model-advisory
   Grounding verdict, reconstructed again through C2b.
3. Is this a material advance worth a daily item? Grounding support alone does
   not establish importance, nor completeness. C3 requires separate explicit
   materiality decisions for narrative progress; unclassified candidates remain
   pending. Decisions are caller-supplied advisory records, **not authenticated
   human review or proof of model execution**.

`build_witnessed_progress_preview(capture, partial_replies, grounding_replies,
materiality_decisions=())` reconstructs C2b from original raw reply records and
then reopens the exact fixed-namespace source CAS once for titles and lifecycle
facts. A normalized rollup, candidate object or self-hashed JSON cannot be
supplied in place of those raw records. Existing C2b quotas remain unchanged:
C3 incurs **one additional bounded CAS triplet read**, not another replay for
each entity or decision. No source-admission checks are bypassed or cached.

Each `MaterialityDecision(rollup_sha256, candidate_sha256, classification)`
selects `progress` or `routine` for an existing supported candidate. All exact
primitive fields are copied before CAS reads. Duplicate IDs, unrecognized
classifications, foreign candidates and decisions tied to old rollup bytes
fail as a whole request. In particular a changed raw partial or Grounding reply
invalidates the previous decision even when the candidate text is unchanged.

## Selection and unresolved coverage

Actual same-day lifecycle events (create/close/reopen/merge/readiness/draft) and
human PR approvals/changes requested supply fixed Japanese status wording from
original Evidence, not model summaries. Same-day PR merges always qualify as
an entity item even with no candidate reply; later or conflicting lifecycle
records cause qualified wording, not an invented final state. Chronology and
latest-title selection compare parsed UTC datetimes, not ISO strings whose
fractional-second precision can differ. Each entity is
one `(repository, issue|pull_request, positive number)` item, and repositories
with no selected item have no heading. No repository-wide narrative is added.
A public `/issues/N` comment does not become an Issue merely from the path.
Linked Issue mentions in a PR do not automatically add an unrelated Issue.
PR commits alone are not automatically material; cosmetic commits can be routine.

Only exact short routine commands/acknowledgements in a complete excerpt are
automatically routine. Truncated or missing excerpts and substantive bot content
remain pending rather than being silently discarded. Routine-only sources
cannot be selected as narrative progress even with a supported model verdict.
A separately classified `progress` candidate adds a Japanese **unapproved
candidate-count** phrase. Model summary/reason prose is never printed; there is
no keyword filter pretending to prevent hallucinated merge/deploy language.
This is deliberately **not yet the fluent Japanese summary synthesis** of #283.

Missing replies, empty candidates, unsupported/unresolved candidates and
unclassified supported candidates remain visible in private diagnostics, even
when an actual lifecycle item is selected. Per-entity diagnostic source IDs
plus unattributed default-branch commits exactly account for original Evidence.
Citation and response completeness never establish complete semantic coverage.
An empty display says no item was selected, or that unresolved evidence prevents
a conclusion. It does not certify 'no progress'.

## Output, privacy and authority

The returned object contains independently owned canonical JSON bytes and
Markdown. JSON binds all source SHAs, C2b rollup SHA, canonical sorted decision
SHA and rendered Markdown SHA. It retains selected entity IDs, lifecycle
Evidence IDs, selected candidate IDs and unresolved/source-coverage diagnostics.
It copies no original comment bodies, model summaries or model reason prose.
Source titles remain private label text; markup, HTML and Obsidian brackets are
escaped for Markdown. Markdown escaping precedes HTML entity generation;
Obsidian comment delimiters (`%%`) are emitted as literal entities. A diagnostic title is not a verified progress assertion.
Very long titles may be visually shortened; original Evidence remains intact.
Limits are 4 MiB JSON and 2 MiB Markdown, with fail-closed overflow, no silent
item cap and no partial-success preview.

`publication_authorized=false`, `materiality_provenance_verified=false`,
`model_verdict_is_advisory=true`, `requires_human_quality_review=true` and
`semantic_coverage_review_required=true` remain fixed. These ordinary Python
objects and advisory decisions are not capabilities. A trusted scheduler must
admit the capture and response selection, and a future human-review adapter
must authenticate editorial decisions before any production use. The preview
is never accepted directly by an existing Writer or projection consumer.

## Verification and outstanding work

Tests use synthetic GitHub input and real isolated POSIX-ACL CAS. They cover
routine-only, unknown bot, supported-but-unclassified, explicit selection,
unsupported rejection, same-day merge, next-JST-day exclusion, readiness vs
merge, reopened-after-merge, malicious title escaping, stale/duplicate decisions,
caller mutation, absent CAS, exact source accounting, no effect calls and bounds.
The review sample is synthetic, never a current-day report from the user's repos.

Outstanding: authentic materiality provenance and a human-calibrated omission
baseline (#290), factual Japanese narrative synthesis (#283), real-date private
inference/quality acceptance, large-day profiling, #337 HTTP409 semantics,
production source UID/ACL/checkpoint acceptance, parent exact-range review
limitations, and separately authorized Summary/Projection/Writer/Timer rollout.
All ancestors and this preview stay Draft until their own gates are satisfied.
