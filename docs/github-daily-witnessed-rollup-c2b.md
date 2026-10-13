# C2b: private source-bound Issue/PR rollup and coverage

Status: nonpublishing child of C2a #352. This adds only a rollup module, its
synthetic tests and this contract; C1a/C1b/C2a and all production callers stay
unchanged. No model calls, Collector requests, CAS writes, Daily Note,
Nextcloud/Vault, Writer, Timer or host configuration changes are introduced.

## Input and identity

`build_witnessed_entity_rollup(capture_sha256, partial_replies,
grounding_replies, max_context_bytes=...)` reconstructs the complete C1a
partition from the Collector-owned CAS. It accepts exact tuples of
`PartialReply(context_sha256, response_bytes)` and
`GroundReply(ground_context_sha256, response_bytes)`, with exact built-in string
and bytes fields. Records are copied to an owned primitive map before any CAS
read, so caller record mutation or virtual sequence indexing cannot change
which reply is later normalized. Duplicate records, even with identical bytes,
are rejected; no hidden newest/first/last-response selection is performed.

The capture digest is a locator supplied by a separately trusted scheduler
checkpoint. Raw reply selection likewise belongs to that trusted caller.
This module does not attest that an actual model produced a supplied reply,
nor defend against arbitrary Python code execution/root/Collector-UID
compromise. Normalized candidate/assessment objects, self-hashes and model
publication flags are not accepted as credentials.

Every present partial reply is re-normalized by corrected C1b. C2a regenerates
one immutable singleton per candidate; any supplied Grounding reply is keyed
by that exact singleton SHA and re-normalized against the rebuilt source.
Capture, Receipt, Evidence, C0 Closure, Context, raw partial reply, Candidate,
singleton and Grounding-response identities are retained. A whitespace change
to an otherwise semantically identical partial reply still changes the
singleton identity: old Grounding replies cannot silently follow it.

Bounds reuse C1a/C1b/C2a: at most 4096 partials and 12 Grounding replies per
partial; partial replies at most 64 KiB, Grounding replies at most 8 KiB;
combined incoming bytes at most 32 MiB; canonical private rollup at most
16 MiB. Oversized output raises an error instead of truncating entities or
sources. These are data/work-cardinality bounds, not a hard runtime deadline.
Repeated full-partition work is also checked before any per-partial or
per-verdict normalization. The conservative C1a-rebuild count is
`1 + 5 * partial_count + 3 * grounding_count` and may not exceed 512;
this count gate runs before CAS access. After the initial trusted source read,
that planned count multiplied by the combined Evidence/Receipt/Index byte size
must not exceed 256 MiB. A regression test instruments the actual C1a build
calls to detect an increased dependency multiplier. Either quota failure is a
typed whole-request error, never a partially complete rollup. These deliberately
conservative limits can reject large legitimate days; production profiling and
a separately reviewed bounded replay design are still required. They bound
amplified reconstruction work, not wall-clock time or arbitrary filesystem
latency. No cache bypasses source/identity admission.

## One row per entity, without confusing completeness with inactivity

The canonical version-3 output groups all attributed original Events into
exact `(repository, issue|pull_request, number)` rows, irrespective of model
candidate availability. It does not generate a repository-wide summary.
Each row retains the ordered original Evidence IDs, expected Contexts, present
partial-reply hashes, candidate text/IDs, raw reply and assessment hashes, and
these explicit states:

- A missing partial leaves the entire Context's Events **unprocessed**, not
  uncited, unsupported or inactive.
- A present empty candidate list leaves all its Events **uncited**. It is not
  an assertion that no meaningful progress occurred.
- Each candidate retains **supported**, **unsupported**, or **unresolved**.
  Unresolved means no Grounding reply was supplied. Unsupported text is not
  promoted into the supported set. Neither state is silently dropped.
- Verdict-specific Evidence-ID sets can overlap because different candidates
  may cite the same original Event. An unresolved candidate stays visible even
  if another supported candidate cites that Event. Candidate counts, distinct
  Event coverage and original totals are therefore separate concepts.
- Default-branch commits not attributed to a PR remain in the original CAS
  and have their exact IDs listed in `unattributed_default_branch_commit_ids`.

Every original ID must appear exactly once in either an entity's source census
or the unattributed-default-commit list. For each entity, the union of cited
(supported/unsupported/unresolved), uncited and unprocessed sets must equal
its original census; uncited and unprocessed are disjoint from all cited IDs.
Missing/changed CAS or invalid/foreign/duplicate replies fail with a typed
`DailyWitnessedRollupError`, not a successful empty result.

`response_set_complete` describes only the expected response inventory. It can
be true for all-empty candidate replies or zero entity Contexts, and never
means complete semantic coverage or an inactive day. Every output retains
`semantic_coverage_review_required=true`, `requires_human_quality_review=true`,
`model_verdict_is_advisory=true`, and `publication_authorized=false`.
Historical capture date/UTC/mode are preserved, not replaced by today's date.

## Disclosure and future consumption

The output contains private candidate summaries, including rejected or
unresolved candidates for audit; it must not be rendered as an approved Daily.
Original comment bodies, raw replies and Grounding reason prose are not copied;
reason and response SHA values are retained. An arbitrary summary can itself
contain private or misleading text. This is not a general redaction boundary.

`WitnessedEntityRollup` is owned canonical bytes and its SHA is deterministic,
but an instance or digest is not a transferable authority capability. Future
Summary/Projection/Writer consumers must independently admit the same source
checkpoint and raw response selection, rebuild/verify the rollup, enforce
material-change and Japanese editorial quality, and require separate rollout
approval. The remaining parent independent-review/production role/ACL gates,
#337 HTTP409 compatibility, #290 omissions and #283 quality are not waived.

Tests use synthetic GitHub responses and the actual existing local POSIX-ACL
CAS fixtures. They cover complete and incomplete inventories, overlap of
supported/unresolved evidence, raw-reply changes, cross-capture contamination,
wrong/duplicate identities, malformed model input, mutable caller records,
multiple batches per entity, empty/default-only captures, bounds and selected
network/write tripwires. Passing fixtures are not live model or production
UID/ACL acceptance.
