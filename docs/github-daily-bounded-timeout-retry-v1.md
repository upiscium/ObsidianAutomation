# GitHub Daily bounded inference timeout retry v1 (#288 follow-up)

## Scope

A **Draft source-only candidate** after PR #291's verified Partial/Grounding
resume. The standalone and production Summary CLIs accept optional
`--transient-retries` (integer 0–2, default **0**) and
`--retry-backoff-seconds` (finite 0–30 seconds, default **0**).
Both values apply to individual fresh Partial/Grounding inference contexts,
not to existing verified cache hits or deterministic Reduce.

A retry is allowed only if the provider wrapper preserved a `TimeoutError`
in the causal chain, its `URLError.reason` is a `TimeoutError`, the
callback directly raised `TimeoutError`, or its internal bounded HTTP response
reader reported the exact known deadline error. It never retries arbitrary
HTTP 5xx, SSL, DNS, connection, authentication, model alias/digest drift,
malformed provider output, citation/grounding integrity failure or corrupted
resume pointers. These other errors fail closed on the first attempt.

For an explicitly configured `N` retries, the maximum number of model calls
per fresh Context is `N+1`. Backoff is deterministic `backoff_seconds * retry_index`,
for retry indexes 1..N. The cache's Context/prompt/model/provenance identity
is **unchanged** by transport attempts and backoff; only fully validated
Output and Provenance can be indexed by the already reviewed immutable Resume
mechanism. The first successful, valid call is used; no stale or half-complete
output is adopted. Failed attempts do not produce new Resume pointers.
The existing per-request provider HTTP timeout continues to apply to each
attempt separately.

Error summaries include stage, batch index/count, provider exception *type*,
causal exception *type*, attempt count, and elapsed seconds. They do not print
prompts, source text, responses, model output or credentials.

## Deliberate remaining gate — full runtime budget and recurrence

This iteration **does not** implement a hard wall-clock limit across all
Partial/Grounding contexts. A 120-second request retried once can consume
over 240 seconds plus the adapter's own pre/post model alias verification;
many batches can still exceed the periodic schedule. A synchronous in-flight
HTTP operation cannot be preempted by checking a budget only between
contexts. Implement and independently review a global runtime/deadline
contract (including existing request timeout and Ollama alias checks), plus
operator-approved recurrence admission, **before** enabling the production
timer. This prototype deliberately keeps retry **disabled by default**.

Known real Batch 7 can fail repeatedly even with a retry: this does not
make a 64-KiB prompt faster. Evidence partitioning comparison (#289) and
semantic omission/calibration (#290) remain separate. No Writer, Timer, Vault,
Nextcloud, CAS mutation, historical ref rewriting, or deployment is authorized
by this document.
