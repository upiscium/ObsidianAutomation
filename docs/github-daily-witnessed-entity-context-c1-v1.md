# Daily v2 — C1 witnessed Issue/PR partial Contexts (#342)

**Status: experimental, model-inert, nonpublishing.** This feature combines
the reviewed read-only CAS Source Closure (#346) with the independent #325
strict Issue/PR resolver and #327 v2 Context partitioning rules without
editing or relaxing those existing APIs. It is not a migration of their SHA
formats or a deployment change.

## Why a new versioned Context is necessary

A v1 Evidence comment at `github.com/owner/repo/issues/N` does not prove
whether it belongs to an Issue or PR. A public `/commit/SHA` link alone
cannot prove a PR parent. Existing #327 correctly quarantines these events,
rather than attaching them by kind, regex or LLM output. However, Collector
#329 can independently validate the exact GitHub API comment ID and
`/issues/N` classification, PR review-comment linkage, and bounded
`/pulls/N/commits` SHA membership. Source Capture Index #339, atomic
Collector CAS #341 and private source Closure #346 bind those fresh source
events together. The four route-sensitive kinds are:
`issue_comment`, `pull_request_comment`,
`pull_request_review_comment`, `pull_request_commit`.

A source route's `verified_api_path` records the fetched endpoint;
`linked_entity_api_path` is a canonical linked identity, not necessarily an
additional `GET /pulls/N` (see #346). Do not claim stronger remote proof
than the Collector actually obtained. Non-route lifecycle events are already
validated against immutable original Evidence but have **no independent C0
API-route witness**. Historical `explicit_historical_reconfirmation` remains
explicitly labelled, not backfilled into old v1 immutable Evidence or made
publication eligible.

## New C1 entrypoints and immutable identities

```text
Authorized scheduler/private caller supplies Capture Index SHA
   |
   v
build_witnessed_entity_contexts(capture_sha256, max_bytes=64 KiB)
   | re-open C0: read_verified_source_closure(capture SHA)
   | re-open #341 CAS reader by SAME SHA, never caller-supplied bytes/path
   | verify Capture/Receipt/Evidence SHA + capture JST day and mode
   | reuse #327 original JST window and #325 strict event→EntityKey checks
   | require exact C0 source-route proof on every route-sensitive event
   | preserve all original events and original per-entity order
   v
WitnessedContextSet: grouped v3 partial Contexts
   |
   +-- verify_witnessed_entity_context(context, capture_sha256=...)
           reopens CAS and rebuilds ALL batches, even if a single context
           or empty candidate would be used downstream
```

Each new `WitnessedEntityContext` encodes **version 3** in sorted-key,
compact canonical UTF-8 JSON, with a SHA-256 over those **exact bytes**,
independently binding:
- `capture_sha256`, `route_receipt_sha256`, `evidence_sha256`,
  `closure_sha256` and precise `date`, `captured_at_utc`, `collection_mode`
- canonical `(repository, issue|pull_request, number)` `entity`, plus exact
  `batch_index`, `batch_count`, `max_context_bytes`
- `events`: each object has `original_event` (exact v1 Evidence dictionary,
  including its unmodified original `evidence_id`) and a `verified_route`.
  The latter is the exact C0 route binding for one of the four sensitive event
  kinds; otherwise it is `null` for **original Evidence only**, not a falsely
  claimed API proof
- `publication_authorized:false`,
  `requires_human_quality_review:true`

The recorded Context SHA is *not* valid if only Evidence SHA matches a
different Capture/Receipt. Even two otherwise identical Evidence bundles
captured on separate authenticated source invocations produce different
Context SHA when their receipts/indices differ. Neither a freshly self-hashed
model response nor a constructed Python `WitnessedEntityContext` is an
authority: its partition must rederive from CAS on each admission.

The new `WitnessedContextSet.report()` accounts for every original event:
`grouped_event_count = witnessed_event_count + original_only_event_count`,
and `grouped_event_count + unattributed_default_commit_count =
original_event_count`. Unattributed default-branch commits remain in the
original immutable Evidence, not injected as Issue/PR progress. Empty
contexts/claims are **not** proof that a day lacked material changes. Never
drop an unsupported original event silently.

## Deterministic bounds and rejection

- Original v1 Evidence <=32 MiB and <=50,000 events; C0 closure <=2 MiB.
  #341 additionally caps each CAS object and attests file ACL, owner,
  regular single-link inode, nofollow, SHA, root ancestry and role.
- At most 10,000 entity identities, 4,096 partial contexts, 64 original
  events per context, 1,024–65,536 bytes requested per context. A single
  oversized event or Context causes **typed BLOCKED**, not silent truncation.
- Entity is resolved by reusing #325's strict kind/type/URL/number validator.
  No raw public URL substring, linked API path or candidate model text can
  assign an Issue/PR. The C0 binding must match exact event ID, repository,
  source kind, original source ID, occurred-at time and entity number.
- Context batching sorts by the strict `EntityKey`, preserves original
  Evidence order per entity, and uses the recorded budget in the Context hash.
  On readback, even a single Context requires a complete CAS re-open,
  reconstruction of all batches and byte-for-byte full object equality.
- Failure to read Collector CAS, incorrect process role, wrong/old digest,
  missing receipt/witness, out-of-range clock, changed source record, duplicate
  or cross-entity Evidence, unknown kind, source/reference mismatch and bad
  capacity all result in `DailyWitnessedContextError` or the documented
  stricter prerequisite typed error. No failure means an empty Day.

A Collector's own compromised UID, root, unstable host clock or unsupported
filesystem ACLs are outside this contract's cryptographic claims. C1 is an
in-memory private experimental API; its source-bound event list includes
**private original source prose** for later model evaluation, so do not
publish it to public CI logs or third-party telemetry.

## Explicitly not yet wired

This slice does **not** expose a candidate normalization API or call Ollama.
#327's existing `model_candidate_schema` and
`normalize_entity_candidates` accept **their original v2 class** and are
unchanged; they cannot be used to establish C1 v3 source-bound candidate
authority. A next separately reviewed C1b/C2 slice must define a v3 response
normalizer that re-opens CAS, binds every model `source_refs` integer to the
exact C1 Context/Event/Route/Entity and closes evidence-missing cases, then
propagate Capture/Receipt hashes into #331 singleton Grounding and #333
Japanese entity rollup. A valid candidate is never a verified substantive
progress update or merge/deploy fact.

Keep the older #325/#327 v2 quarantine for callers using historic Evidence
without fresh C0 Collector source proof. C1 must not change the old v1
Collector/Summary/Projection/CAS, append to any Daily Note or touch Nextcloud,
Vault, systemd, Writer or Timer. Reproducible local tests only; no live GitHub
source fetch, credential use or production UID/ACL admission.

Still required for a public Daily v2: production host service role and POSIX
ACL audit (#336), exact Source Route Source/CAS integration approval, #337
bounded HTTP409 semantics, full-day semantic omission review (#290), Japanese
editorial calibration (#283), approved versioned v2 Summary/Projection/CAS,
independent Security/Correctness + CI gates and separate explicit Writer and
Timer rollouts. This document is not an authorization to merge or deploy.
