# Daily v2 — C1b source-bound candidate schema and reply validation (#342)

**Nonpublishing.** This is an isolated child of the existing reviewed
C1a Context-v3 feature [Draft #348](https://github.com/upiscium/ObsidianAutomation/pull/348).
It avoids duplicating C1a's source Context builder and changes none of the
original v2 Context, singleton Grounding or entity Rollup code.

## Boundary

C1a `build_witnessed_entity_contexts(capture_sha256)` produces
`WitnessedEntityContext` with the exact Collector-owned Capture Index SHA,
route receipt SHA, Evidence SHA, derived C0 Closure SHA, target date and
capture mode, strict `(repository, issue|pull_request, number)` and
per-event original Evidence plus only its independently authenticated
source-route proof. Its `verify_witnessed_entity_context` reconstructs the
*entire* partition from the immutable Collector CAS before accepting an
in-memory Context. This derived Python Context and its own SHA are not
producer credentials; a trusted private scheduler must separately admit
the exact capture digest.

C1b adds **only** the private typed response boundary:

- `witnessed_model_candidate_schema(context, capture_sha256=...)`
- `normalize_witnessed_entity_candidates(context, raw_model_bytes,
  capture_sha256=...)`

Both call C1a's exact partition verifier, which **reopens trusted CAS**
and rebuilds the original partition. The trusted digest must be supplied
separately, not inferred from an untrusted model reply or silently selected
from the most recent pathname. A source input that is absent, corrupted,
moved, stale or unadmitted does not become an empty or completed Day.

## Model-facing contract

The only permitted model output is a bounded JSON object:

```json
{
  "candidates": [
    {"summary": "A concise source-groundable observation", "source_refs": [0]}
  ]
}
```

Up to 12 candidate entries per Context, one line and at most 2,048 characters /
8 KiB UTF-8 for each summary, with 1–8 unique positive-or-zero **true integer**
indexes into the exact one-entity Context events. No model-specified Issue/PR
identity, repository, event ID, GitHub URL, source witness, merge, deployment
status, Capture SHA, Context SHA, publication permission or Extra JSON field.
Malformed/duplicate JSON keys, bad Unicode controls/invisible format characters,
newlines, non-integer (including Boolean) refs, duplicates, out-of-range refs
and excessive payloads are rejected before normalization.

Each candidate's deterministic digest includes:

```text
record_version=3
Capture Index SHA
Route Receipt SHA
Evidence SHA
C0 Closure SHA
C1a Context SHA
strict repository + Issue/PR type + number
source-refs-resolved ORIGINAL Evidence IDs
bounded original model summary text
```

This means that the same original text and original event bundle with a
different authenticated Route Receipt/Capture Index cannot silently share
candidate identity. `WitnessedCandidateBatch` returns a fixed
`response_sha256`, one-entity key and the exact uncited original Evidence
IDs for later omission review. Empty candidates keep *all* uncited IDs;
they are **not** a verdict of zero progress.

All returned flags remain nonauthoritative:
`grounding_required=true`,
`semantic_coverage_review_required=true`,
`requires_human_quality_review=true`,
`publication_authorized=false`. The summary string is private model text;
do not publish or log it as a verified Issue/PR progress statement. A model
saying “merged” or “deployed” is not verified lifecycle evidence.

The normalized Python object, JSON and candidate SHA are **not**
transferable GitHub source attestation, capability, approval, Grounding result,
or permission to write a Daily Note. C2 must independently bind C1a/C1b
to source-witnessed singleton Grounding and Rollup, reopen the CAS and retain
the entire evidence/route/candidate SHA chain. Existing #331/#333 v2
Grounding APIs MUST NOT silently reinterpret C1b v3 candidates as v2.

## Testing and release boundary

Local POSIX ACL-backed CAS fixture and authenticated synthetic GitHub stub
exercise one-entity v3 model-output schema, valid/empty candidate normalization,
input revalidation, forged Context/Event/proof, changed Digest/Issue-vs-PR,
wrong Capture, duplicate/malformed JSON, Boolean/foreign/out-of-range refs,
injected status/permission, malicious Unicode/newline, oversized response,
model claims of merge, lossless uncited IDs, stable deterministic replay, and
absence of any Collector API, model invocation, Writer or CAS write on the
normalization path. All test data are private in the scratch fixture.

The module does **not** modify or call the live GitHub Collector,
Ollama, Nextcloud/Vault, Daily Writer, Timer, systemd, public API or historical
immutable v1 refs. Production Collector/summarizer UID/GID, named POSIX ACL,
trusted scheduler checkpoint digest, #337 bounded empty-repository HTTP409,
#290 material omission and #283 Japanese editorial review, v3
Summary/Projection/CAS and separately accepted Writer/Timer rollout remain
independent release gates. Neither C1a nor C1b is approval to merge main.
