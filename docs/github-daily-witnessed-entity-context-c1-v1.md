# Daily v2 — Source-witnessed Issue/PR Context C1 (#342)

**Status: experimental, read-only, nonpublishing.** Separate versioned source
contract; NOT a retrofit of the pre-existing v2 Context or a new production
Collector/Writer/Timer. This work is stacked on #346's Source Closure and a
clean merge of the existing entity preview/context/Grounding/rollup feature
line (#325/#327/#331/#333). All ancestors remain Draft.

## Why this version exists

Existing `github_daily_entity_context.py` (v2) necessarily quarantines
`issue_comment` and `pull_request_comment` on `/issues/N`, as the public
path is shared between Issues and PRs; it also quarantines PR commits whose
public `/commit/SHA` URL proves no parent PR. That was correct for original
v1 Evidence, which lacks authenticated route attribution.

Collector #329 re-verifies four API-sensitive kinds and seals exact Evidence
plus source-route receipt under Collector-owned CAS #341. C0 #346 constructs
a private route binding after reopening that CAS. C1 can now make a new,
**explicitly source-qualified** Context without rewriting historical v1
Evidence, changing v2 quarantine, or upgrading arbitrary hash strings to
GitHub signatures.

C1 is a new, independent module
`github_daily_witnessed_entity_context.py`. It exports:

- `build_witnessed_entity_contexts(capture_sha256, max_bytes=...)`
- `witnessed_model_candidate_schema(context, capture_sha256=...)`
- `normalize_witnessed_entity_candidates(context, reply, capture_sha256=...)`

**Each** entry point accepts an exact 64-hex Capture SHA from an otherwise
trusted caller checkpoint. It reopens and independently validates the source
CAS via #341 and C0, including the original Evidence bytes and reconstructed
route binding. It does not accept an in-memory `VerifiedCapture`,
`VerifiedSourceClosure`, model-supplied token, filename or arbitrary URL
as a trust credential. C1's immutable Python objects are ordinary in-memory
data and can be forged by same-process callers; they are rederived against the
CAS before model output is normalized.

The digest is still a *locator*, not independently authenticated provenance;
admission of the scheduler's checkpoint digest remains a separate operational
boundary. Root or compromised Collector UID, filesystem/ACL assumption
failures, and a spoofed host clock remain outside the existing source CAS
attestation model.

## New v3 Context and candidate identity

Every `WitnessedPartialContext` serializes canonical, sorted-key, newline-
terminated UTF-8 JSON with these exact anchor fields:

```json
{
  "record_version": 3,
  "stage": "github_daily_entity_witnessed_partial_context_v3",
  "capture_sha256": "<SHA-256>",
  "route_receipt_sha256": "<SHA-256>",
  "evidence_bundle_sha256": "<SHA-256>",
  "collection_mode": "explicit_historical_reconfirmation",
  "date": "2026-10-05",
  "entity": {"repository": "upiscium/Test", "entity_type": "issue", "number": 1},
  "batch_index": 0,
  "batch_count": 2,
  "max_context_bytes": 65536,
  "events": [],
  "route_proofs": [],
  "publication_authorized": false,
  "requires_human_quality_review": true
}
```

*The JSON above is a field-shape illustration only; an admitted batch must
contain between 1 and 64 actual original Events.* The `route_proofs` array
contains the exact matched #346 C0 proof records only for source types with
an independently reverified API route. It uses the original Event
`evidence_id` for deterministic membership; both the model Context SHA and
normalized candidate SHA cover the Capture/Receipt/Evidence triple. Context
SHA cannot collide with the old v2 stage for the same Evidence.

Every original Evidence Event on a witnessed capture is accounted for:

- Actual Source Route witness required for `issue_comment`,
  `pull_request_comment`, `pull_request_review_comment` and
  `pull_request_commit`. Each binding's kind, repository, Issue/PR type,
  number, source ID, timestamp and Evidence ID must match the original.
  Missing, extra, swapped or repeated routes are rejected, not guessed from
  public URLs and not silently treated as inactivity.
- Other **structural** Issue/PR events may form entity contexts using #325's
  already strict event-kind/URL/type/number validator and JST date window,
  but they are explicitly **not individually API route witnessed**. Their
  lack of route proof cannot become an assertion about a fetched PR record.
- Default-branch commits without authenticated PR membership remain
  unattributed and are preserved in the immutable source bundle (not silently
  attached to a model-selected PR).
- Counts reconcile exact original event total and the one-entity partitions;
  all route sources are included exactly once. A single event above the
  configured byte budget, exhausted batch capacity, bad role, damaged CAS
  object or missing receipt produces a typed failure, never a partial daily
  collection marked complete.

Per-entity context batches are deterministic and have max 64 original events,
64 KiB canonical input, 4,096 batches and 50,000 original event cap.
The chosen context byte budget is itself part of each Context SHA.

C1's response schema accepts only `{"candidates": [...]}` with 0–12 items,
each containing one bounded one-line summary and 1–8 integer `source_refs`
into that exact Context. The model cannot select repository, Issue/PR number,
GitHub URL, merge/deployment status, candidate SHA, source event ID or any
publication permission. Candidate normalization recomputes the *entire*
trusted partition from CAS before resolving refs, binds the exact source and
Context triple into the candidate digest, preserves uncited Evidence IDs, and
keeps `grounding_required=true`,
`semantic_coverage_review_required=true`,
`requires_human_quality_review=true` and
`publication_authorized=false`. Even an empty candidate response is not
evidence of no progress.

No model requests are made in C1. Private text may be carried inside
Context/candidate data, so do **not** send those bytes to public CI logs or
untrusted clients.

## Correct API route semantics

C0 supplies two different fields for each witnessed event:

- `verified_api_path`: the *actual* pinned source API endpoint fetched
  and verified by the Collector.
- `linked_entity_api_path`: the canonical Issue or PR association/link,
  **not necessarily independently fetched**.

Issue/PR discussion comments verify the comment and an `/issues/N`
classification resource. PR review comments validate the linkage on the
actual `/pulls/comments/ID` response; PR commits validate exact SHA
membership in `/pulls/N/commits`. The latter two flows do **not** separately
GET `/pulls/N`; C1 cannot infer they did, nor use them to establish a
new merge or deployment state.

## C2 and release gates

C1 does **not** change the v2 `build_entity_contexts`,
`normalize_entity_candidates`, `build_entity_grounding_contexts`,
`normalize_entity_grounding_verdict`, or existing rollup consumers.
Feeding a v3 `WitnessedPartialContext` to the v2 Grounding APIs is not an
approved migration: those rederive old Evidence-only v2 partitions and
would lose source-route authority. A later separately reviewed C2 stage must
propagate the new Capture/Receipt/Evidence and Context/Candidate SHAs through
singleton Grounding and entity rollup, and re-open trusted CAS again at
each model/effect boundary. The new v3 SHA cannot be silently relabeled v2.

The entire code path remains isolated/offline and does not fetch live GitHub,
call Ollama, write CAS, edit a Daily Note/Nextcloud/Vault, or start a Timer.
Historical reconfirmations retain their original day and are not promoted
to today's scheduled publication. Outstanding gates include #337 HTTP 409
compatibility, actual trusted service role/ACL/storage audit, independent
source-producer acceptance, #290 omission/semantic materiality audit, #283
Japanese editorial quality, v2/v3 Summary+Projection+CAS migration, and
**separate explicit** Writer/Timer rollout approval. Do not merge to main,
run a production Collector or backfill v1 historical evidence based on C1.
