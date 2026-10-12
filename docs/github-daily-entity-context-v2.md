# GitHub Daily Issue/PR v2: entity-scoped contexts and candidates

- **Issue:** [#326](https://github.com/upiscium/ObsidianAutomation/issues/326)
- **Parent:** [#324](https://github.com/upiscium/ObsidianAutomation/issues/324), [#325](https://github.com/upiscium/ObsidianAutomation/pull/325)
- **Status:** experimental read-only library, not wired to inference or any Writer
- **Dependency:** requires #325's immutable evidence entity mapping contract to pass its independent review before integrating or using this code in an active pipeline

## Boundary

The existing v1 GitHub Daily Partial Summarizer groups events **by repository**,
so one model input may contain many unrelated Issue/PR entities and citations
might be mixed. This follow-up library builds one or more strictly bounded
inputs for **one entity** instead. It neither changes the v1 Evidence collector
nor summarizes a whole repository into user-facing prose.

`build_entity_contexts(original_evidence_bytes)` parses the canonical
**original v1 Evidence** using the existing SHA-verified event reader. It
verifies the Asia/Tokyo date window and each event's typed GitHub source
identity and deterministic parent `EntityKey` mapping. A context contains
exactly one `(repository, issue|pull_request, number)`; Issue #7 and PR #7
can never collide. No prompt string, actor-controlled issue mention or LLM
prose may choose that identity.

**Source exclusions are visibility/identity exclusions, not collection loss:**
default-branch commits without an attested Issue/PR target are counted and
left in the original immutable Evidence; PR commits with a /commit/SHA URL
lack an authenticated /pull/N association in the v1 Evidence payload and are
also marked unverified/unattributed. A future immutable collector-route
receipt may qualify these; never guess a PR parent from the source's number
alone.

Contexts split deterministically with at most 64 original events and a
64-KiB **hard safety cap** per input; callers may use a smaller explicit
budget. Each original associated event appears once across the entity's
contexts, in its original per-entity Evidence order. A single oversized
event or too many contexts fails closed rather than truncating it or dropping
its identity. A context is canonical JSON with exact Evidence SHA, target JST
date, entity key, batch index/count and all original event fields, and is
content-addressed. This does not replace the v1 64-KiB production policy,
nor does it assert that 16/24/64-KiB model inputs have equal semantic
coverage or latency (#290).

## Model-facing candidate contract

The dynamic `model_candidate_schema(context)` permits only:

```json
{"candidates":[{"summary":"one plain line", "source_refs":[0,1]}]}
```

- `source_refs` must be true integers (not bool/string), unique, between
  zero and the exact last index of **this one entity context**, max eight
  cited events per candidate.
- No model fields for repository, issue/PR number, title, URL, merge,
  deployment status, author, publication permission, or Nextcloud action.
- Model prose is a **private unverified candidate**, not verified progress.
  It may not appear verbatim in a published Daily without source-by-source
  singleton Grounding, materiality and editorial review.
- The normalizer resolves each integer to the original immutable
  `evidence_id`; candidates bind their context digest, normalized text
  and original cited event identities into a deterministic candidate SHA.
  Unknown, duplicate, wrong-type, out-of-range, mixed-entity, oversized,
  multiline and extra-key responses fail closed.
- Empty claims, or events absent from the model's citations, cannot be
  interpreted as proof that an Issue/PR made no meaningful progress.
  `uncited_evidence_ids`, `grounding_required: true` and
  `semantic_coverage_review_required: true` remain visible to an authorized
  private quality reviewer for **every** candidate batch.
- All reports set `publication_authorized: false`. No Writer or effect
  endpoint reads this model. The JSON report intentionally contains private
  candidate text and Evidence hashes; do not send it to public CI logs or
  arbitrary web clients.

A model claiming “merged”, “deployed” or a related Issue number has no
authority to elevate a status. The only possible status promotion is through
the separate verified JST-dated original GitHub lifecycle Evidence, subject
to PR #325's identity contract and the future v2 projection reducer.

## Remaining steps before Daily v2 could run

1. Complete PR #325's Correctness/Security blockers and accept one shared
   **public** exact entity-resolution function; this prototype currently
   reuses its private event-key validator to prevent two divergent
   interpretation rules.
2. Add a real entity-scoped model adapter (exact model/digest/prompt/options
   provenance), separate typed singleton Grounding inputs, reliable
   per-entity evidence/review completeness and independent human
   calibration (#290). A syntactically valid candidate is not a grounded
   summary; a missing model citation can be a material omission.
3. Deduplicate multiple batches' supported claims **within one entity**;
   produce exactly one Japanese Issue/PR description with truthful last
   verified date-scoped state and no repository-level prose (#283/#324).
4. Version the approved v2 Summary and Projection schemas and integrate them
   with existing exact-source resume/atomic CAS designs (#291/#293), while
   preserving all existing v1 immutable references and the 2026-10-07 Live
   canary. Do not backfill historical note content implicitly.
5. Compare real-date, source-private preview against human materiality and
   omission judgments. Obtain independent Security/Correctness + CI and a
   separate operator-approved **Writer apply**, with another explicit
   decision for Daily Timer recurrence.

Neither the module nor its tests fetch GitHub, query Ollama, write a file,
open WebDAV, edit an Obsidian note, modify systemd, or contact a Vault.
