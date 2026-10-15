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
prompt and structured-output schema. A versioned, fixed Japanese user-message
reminder restates the already-enforced disjointness of cited and pending refs;
it adds no source fact and changes no C4 validator or source Context. Its exact
version/hash and the complete actual request hash are recorded independently
of the original C4 system-prompt identity. Each accepted editorial response is then
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

Elapsed budget is checked before and after each model callback. The native
adapter receives the absolute deadline and checks remaining time before EACH
HTTP request, including after pre-chat model lookup and before post-chat lookup.
Each HTTP timeout is capped to that remaining budget; the receipt records the
actual per-request timeout separately from the configured upper bound. Therefore
a delayed model lookup cannot start a new chat after expiry. Custom callbacks
receive the same deadline keyword and must implement their own internal I/O
checks. This is **not** preemptive cancellation, a hard wall-clock bound or a
protection against unbounded trusted callback code; an in-flight request may
finish late. The request SHA covers the same compact JSON bytes serialized by
the native transport, without the canonical artifact newline. There is no automatic
model parallelism on a shared GPU. Test suites and independent reviews may run
in parallel without increasing inference resource contention.

Source date/mode is unchanged, including explicit historical reconfirmation.
All preview/publication/materiality/model-execution/semantic acceptance flags
remain inherited false or review-required. Actual-model compatibility, real-day
Japanese/omission quality (#283/#290), independent source producer identity,
production UID/ACL/checkpoint admission, #337 HTTP409 and explicit Writer/Timer
approval remain separate gates. Synthetic fixtures and local HTTP protocol tests
are not actual-model or production acceptance. Keep the feature Draft.

## Individual Issue/PR priority (2026-10-14)

The immediate delivery target is an individually usable Issue/PR summary,
not a synthesized repository-wide or cross-entity recap. Use the explicit
`target_entity=("owner/repository", "issue"|"pull_request", positive_number)`
runner argument to request exactly one already selected C4 entity. It must
match one verified context; a missing/ambiguous target fails before inference.
The target is snapshotted before source reads. No model selects that identity.
Only the target receives editorial and new grounding calls. Other selected
entities remain unprocessed/pending in the retained full private preview; they
are not silently deleted or declared inactive. Runtime reports distinguish
`run_scope`, requested vs selected counts and `all_selected_entities_requested`.
`inference_responses_complete` describes requested calls, not the whole day or
semantic completeness. Default all-selected behavior is unchanged. Full source
reconstruction quotas and C4 source/semantic checks remain in force; explicit
single-entity inference is not yet a large-day performance optimization.

### Opt-in simpler editorial wire contract

Configure `ollama_narrative_infer(...,
editorial_contract="c4-sentences-only-v1")`. Legacy `"c4-v1"` remains the
default and keeps its strict cited/pending disjointness check. The new **requested
provider format** contains only `{"sentences":[{"text":...,"source_refs":[...]}]}`.
Its fixed Japanese system prompt, prompt version and constrained JSON schema
replace the model-visible editorial contract; the original C4 context/digest
and source data are retained and the exact actual wire request is hashed.
Grounding's original response schema and host-side validator do not change.

The host validates exact keys/types/reference bounds, computes the complement
of cited source indices as `pending_source_refs`, then submits that projected
JSON to the **unchanged** C4 validator. This is deterministic format adaptation,
not semantic repair. An output containing `pending_source_refs` under the new
contract is rejected, even if its list happens to be consistent. Invalid refs,
Boolean refs, duplicate keys, extra permissions or text fail as before. An empty
sentences list conservatively leaves all original sources pending and triggers
no Grounding. Unsupported post-edit verdicts re-add their sources to pending.

Private attempts and native receipts retain the **verbatim provider response**
and its hash. Accepted attempts separately record `admitted_response_sha256`
for projected C4 bytes, which are retained in `narrative_replies`. Receipts
record `response_contract`, original C4 prompt hash and effective wire prompt/
schema hashes, plus exact wire request/model/options identities. The two hashes
are deliberately different when projection is used; never describe projected
JSON as provider-emitted bytes or repair a legacy failed output by relabeling it.
A trusted custom callback must explicitly declare its response contract; these
callbacks and receipts remain application code, not unforgeable attestation.

A citation proves only which original record was referenced, **not** that all
facts in that record were covered. This adaptation does not certify importance,
Japanese quality, truth, semantic exhaustiveness or human approval. Those flags
remain false/unapproved. No cross-entity synthesis, live GitHub collector,
Writer/Timer, main merge, new model installation or automatic retries are added.

## Individual factual phrasing policy (2026-10-14)

The optional `c4-sentences-only-v1` response schema remains unchanged. Its
editorial instruction now has the distinct effective prompt version
`c5-individual-facts-ja-v2`, recorded both in model-visible context and private
receipt. Original C4 prompt/version and raw provider/projection bytes remain
separate. The legacy default and the post-edit Grounding prompt are unchanged.

Prefer the source's own concise wording; put work and status/unfinished facts
in separate sentences. Do not infer causation from temporal order or co-occurrence.
Preserve negation and distinguish plans, approval, completed work and deployment.
This is guidance, not a semantic validator or a promise of factual output.
The host does not remove causal words or rewrite a bad response. An unsupported
sentence stays pending even when its JSON is valid. Citation accounting is not
semantic completeness; model-verdict and human-quality flags remain advisory.

Compare native-model outcomes on fixed synthetic sources, retaining failures,
raw replies, request/effective-prompt/model/options and verdict receipts. Positive
and deliberately unsupported negative controls are separate from pytest.
A sample that becomes `supported` is not human approval or real-day acceptance.
No cross-entity synthesis, model download, retry loop or Writer/Timer rollout is
part of this policy change.

For the explicitly selected sentences-only editorial contract, post-edit
Grounding now receives `c5-grounding-semantic-v1`: entity, target date, the exact
edited text, and every cited original event with only its opaque `evidence_id`
removed. No excerpt, negation, truncation marker, actor, event kind, number,
timestamp or other original field is changed. Full C4 context and host-side
validation/grounding identities remain unchanged. The original C4 Grounding system prompt and response schema are unchanged;
C5's optional effective system prompt may be versioned separately below.
No unsupported verdict is converted.
The legacy adapter's model-visible input remains the complete canonical context.

Receipts separately bind `model_input_version`/`model_input_sha256` to the actual
user-message bytes, alongside the exact full C4 context and wire-request hashes.
The semantic view omits unrelated provenance hashes and derived status labels;
it never substitutes a previous model summary for original source events.
This changes presentation to the model, not Grounding acceptance criteria.
Observed model support is advisory and can contain false positives/negatives;
compare positive and intentionally false claims and retain failures. Neither a
higher support count nor citation coverage establishes factual completeness.

## Private original-source companion (2026-10-15, #290)

`NarrativeRun.source_review` and `NarrativeRun.review_markdown` add an individual
source-comparison view without new inference, CAS reads, pipeline stage or
publication authority. The original `preview` and C4/model request behavior are
unchanged. Consumers performing human review should use `review_markdown`, not
interpret a zero pending-event count in `preview` as complete fact coverage.

Before the first model callback the runner owns a bounded JSON snapshot of each
requested entity's complete captured title/body/message fields. Exact text,
Evidence ID, source index, timestamp, field name, original byte/truncation
metadata and hashes are retained. Unrequested entities remain pending in C4;
the companion copies only requested entities. Fields already truncated by the
Collector remain explicitly truncated, not reconstructed. Nontext events are
counted, not certified from absent prose. No original source is posted to a
public log by the runtime; this companion and its Markdown are PRIVATE.

After final C4 reconstruction, matching is strictly lexical: each displayed
`supported` sentence is searched only in the original fields of the events it
actually cites, recording the first contiguous occurrence with Unicode-code-
point offsets. Overlapping matches are merged when calculating complementary
`unmatched_source_ranges`. No whitespace, text, event ordering or negation is
normalized; repeated passages beyond the first match stay unmatched. The full
captured original field is displayed even when every character matches. Thus
an omitted qualification inside a cited Event, such as `恒久対応は調査中です`,
remains present in the private source and unmatched ranges. A model-supported
invented causal phrase absent from its cited original is marked nonliteral,
not silently rewritten or reclassified.

**This is NOT semantic coverage, an automatic fact checker, or a new Grounding
verdict.** Legitimate paraphrases may be nonliteral; a substring inside a quote,
negation, hypothetical, superseded report or false source can match literally
while being misleading. Literal matches never override Grounding and never
establish truth. Unmatched text is not proof of an omitted important fact.
`semantic_coverage_verified`, `factuality_verified`, and `publication_authorized`
remain false, and `requires_human_quality_review` remains true, including when
all ranges match. Empty, rejected or unsupported drafts retain their original
fields and unmatched spans for review.

Raw exact text resides in the companion JSON; Markdown encodes ASCII punctuation
and represents nonprintable/control/bidi characters with visible `[U+XXXX]`
labels. Neither headings, links, comments, block IDs nor raw control characters
from source text are executable layout. This specifies emitted syntax, not an
Obsidian UI-rendering guarantee; no live UI was tested. The original text is
never silently truncated or modified in its JSON record.

Bounded limits: at most 1 MiB canonical source-review input checked before model
calls; 2 MiB companion JSON and 8 MiB combined review Markdown. An exceeded
output limit fails the run rather than reporting a truncated review complete.
Such final output failure is not cancellation or rollback of already finished
inference. The normal metadata-only run report contains companion hashes/version
and `source_review_required`, never source or reason prose. Bounds, native-call
caps, exact C4 identity replay, legacy response contracts and all production
UID/ACL/checkpoint/Writer/Timer gates remain in effect.


## Real-day English-source diagnostic (2026-10-15)

The opt-in sentences-only effective prompt is now `c5-individual-facts-ja-v3`.
It distinguishes faithfully retaining concise **Japanese** source wording from
translating an English source into Japanese output. English commit subjects must
not be copied as an entire output sentence. Proper names/API identifiers may
retain their spelling. Discovered defects, evaluation outcomes and unresolved
work are reportable progress, not grounds to return an empty array merely
because implementation is incomplete. Unsupported claims still must be omitted.

Only prompt instructions/version changed. The wire response contract, C4
Japanese/text/reference validator, source proof, semantic Grounding view/prompt,
model/call/deadline bounds and authority flags are unchanged. Provider bytes
are not translated or repaired after generation. These instructions are not a
proof that the model complied. Baseline and revised runs on the same captured
real sources must be retained separately; no general quality or production
acceptance follows from two examples.

## Quoted example versus completed work — #290 (2026-10-15)

In the first bounded real-source probe, the model promoted a synthetic
compatibility probe's *generated example text* about a validation fix and
regression tests into a claim that the PR actually performed that fix. A fresh
model Grounding verdict called this `supported`. Neither the cited Event
identity nor a supported verdict establishes the statement's attribution.

The optional `c4-sentences-only-v1` adapter now uses
`c5-individual-facts-ja-v4` as its effective editorial prompt and
`c5-grounding-quotation-scope-ja-v1` as its effective post-edit Grounding
system prompt. The extra instructions distinguish: (1) actual reported work,
(2) an example, quoted generated sentence, hypothetical test output or
expected value *describing* work, and (3) not-yet-completed or negated work.
A claim that the *example* was generated or inspected can be supported by
source evidence; the work asserted *inside the example* is not automatically
work done by the author. Ambiguous examples should remain unwritten/pending
rather than converted to implementation claims. Citation and response
contracts are unchanged.

The complete original C4 Grounding context, its original C4 system prompt
hash, source IDs and host validator remain identical. Only the optional C5
model-facing Grounding system prompt changes; the new effective version/hash
and full request bytes are recorded separately from the original prompt and
`c5-grounding-semantic-v1` user-message view. The legacy `c4-v1` path still
uses the unmodified C4 prompt, input and verdict schema. No model outputs are
post-hoc rewritten, verdicts overridden, source events silently removed or
new inference stages introduced.

**This is a prompt-level mitigation and must not be called a semantic fix
without independent real-model and human quality evidence.** A model may
still judge an example-derived implementation claim `supported`. Tests that
inspect the exact prompt/version/wire receipts only establish protocol behavior.
The private original-source companion and `requires_human_quality_review`
are still mandatory. In particular, independently label actual completed
actions, quoted examples, plans, negation, and exclusions on held-out individual
Issue/PR examples before any rollout or reliability claim. No publication,
production Writer/Timer, default-policy change, or cross-repository reducer.
