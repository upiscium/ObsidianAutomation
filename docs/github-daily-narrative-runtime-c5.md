# C5: explicit nonproduction narrative inference (#342 / #283)

Parent: C4 #355. This adds a lazy native Ollama adapter and a bounded orchestration
function; it changes no existing source/Context/Grounding/preview or v1 runtime.
No CLI, scheduler, filesystem receipt writer, model download, host configuration,
Nextcloud, Vault, Summary/Projection or Writer integration is introduced.

## Invocation and scope

```python
from obsidian_automation.github_daily_narrative_runtime import (
    ollama_narrative_infer, run_witnessed_narrative_inference,
)

infer = ollama_narrative_infer(
    base_url=operator_approved_endpoint,
    model=operator_selected_installed_identifier,
    options=operator_options,
    timeout=60,
)
result = run_witnessed_narrative_inference(
    admitted_capture_sha, partial_replies, grounding_replies, materiality_decisions,
    infer=infer, max_entities=4, max_calls=16, max_elapsed_seconds=120,
)
private_preview = result.preview  # Never an approved Writer input.
```

C1/C2 replies and advisory materiality must already exist. This is NOT a new
end-to-end GitHub collector, semantic selector, or unattended Daily pipeline.
The endpoint, model identifier, source checkpoint and upstream raw response
selection belong to the trusted operator, not model-generated fields. Configuration
alone has no network effect; an explicit run invokes the backend only after
source admission and whole-request quotas. Installing or pulling models is absent.

C5 snapshots primitive upstream reply/decision fields before source access and
calls the unchanged C4 public API to create complete entity prompts. It sends
those exact canonical context bytes as the user message, plus the exact C4 system
prompt and structured-output schema. Each accepted editorial response is then
passed through the public C4 reconstruction to generate NEW per-sentence
Grounding contexts. Finally C4 rebuilds/validates the complete raw editorial and
verdict set for the private preview. No previous C2 Grounding support is inherited
by rewritten text. Invalid editorial replies never generate Grounding calls.

## Existing transport and model identity

The adapter reuses `ollama_generator` URL/options/model-list/transport behavior:
HTTP only on loopback, remote endpoints require HTTPS, credentials in URLs are
rejected, direct requests ignore proxy environment and refuse redirect following,
HTTP response bytes and request timeouts are bounded. It uses only `/api/tags`
and `/api/chat`; no shell/subprocess or endpoint from a source event.

`stream=false`, `think=false`, exact C4 schema and owned JSON options are used.
A completed assistant response must match the resolved identifier; a provided
completion reason other than `stop` is refused. Raw response text must fit the
editorial or Grounding byte limit and then pass C4 validation, without trimming
or silently repairing JSON. Tags are re-resolved before and after each chat; the
first observation pins identifier/digest for this adapter's lifetime. Ambiguous
model names, observed changes, wrong echoed models and incomplete completions
fail. A unique installed alias may be explicitly selected by the operator; the
adapter never silently picks one of conflicting entries.

These are **observations, not cryptographic inference attestation**. A server can
lie or swap and restore a tag between observations. Before/after tags do not
atomically prove the exact weights used; do not claim otherwise. A custom
`infer` callback/transport is trusted application code, not a sandboxed adversary.
It can fabricate receipts; source-bound hashes cannot cure that.

## Owned raw results and provenance

A successful adapter call returns immutable raw response and canonical receipt
bytes. The receipt records Context/request/raw-output SHA, prompt version and
system-prompt/schema/options SHA, observed model identifier/revision, effective
options, nonstreaming/nonthinking settings, and timeout. It contains no original
source text, reason prose, endpoint credentials or token. The full request hash
binds the actual canonical message/config payload passed to the transport.

The runner checks context/raw-output hash consistency and fixed false authority
flags before accepting a call. Final acceptance still occurs in unchanged C4.
Its run-level report contains only digests, states/counts, source day/mode and
preview identity. Raw replies and provider receipts live separately in private
`attempts`; accepted replies are also returned in typed C4 records for exact
replay. Untrusted callback receipt contents are not safe public logging data.
There is no durable storage or restart/resume guarantee from this in-memory API.

Provider/validation failures are bounded fixed diagnostic states. Exceptions
never copy their strings into the run report. Returned bounded invalid JSON is
retained privately for diagnosis but never accepted or repaired. Failure before
a backend result exists has no raw reply recorded. Missing/invalid replies leave
original Events pending in C4. Successful other entities may still be reviewed;
`inference_responses_complete=false` distinguishes incomplete runs. A `supported`
or `unsupported` valid verdict is a completed response, not a semantic truth
claim. Even all-complete or zero-entity runs never certify an inactive day.

## Resource bounds and chronology

Default four selected entities and 16 potential chat calls; absolute limits eight
and 32. Preflight reserves one editorial plus up to three verdicts per entity;
requests above the complete worst-case budget are rejected, never silently
truncated. No automatic retry. Tags metadata calls are additional: two per chat.
Individual HTTP requests have a maximum configured 120-second timeout.

C4 is reconstructed three times (prompt, Grounding, final preview), not once per
sentence. If P/G are upstream Partial/Grounding reply counts, planned C1a builds
per C4 invocation are `1+5P+3G`. C5 bounds three times that to 512 before source
access. C2b uses two CAS triplet reads per C1a build and one direct read; C3/C4
add one each. Combined planned reads are `3*(2*(1+5P+3G)+3)+1`, including C5's
one source-size measurement read. Original triplet size times that count must
fit 256 MiB before inference. Tests count actual dependency calls. These limits
can reject legitimate large days; no source checks are skipped to fit them.

Elapsed budget is checked before and after each model callback. Expiration stops
new calls and leaves outputs pending. It is **not** preemptive cancellation, a
hard wall-clock bound, or a guarantee against unbounded trusted callback code;
one in-flight HTTP call may finish after the elapsed budget. There is no automatic
model parallelism on a shared GPU. Test suites and independent reviews may run
in parallel without increasing inference resource contention.

Source date/mode is unchanged, including explicit historical reconfirmation.
All preview/publication/materiality/model-execution/semantic acceptance flags
remain inherited false or review-required. Actual-model compatibility, real-day
Japanese/omission quality (#283/#290), independent source producer identity,
production UID/ACL/checkpoint admission, #337 HTTP409 and explicit Writer/Timer
approval remain separate gates. Synthetic fixtures and local HTTP protocol tests
are not actual-model or production acceptance. Keep the feature Draft.
