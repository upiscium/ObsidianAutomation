# C4: source-bound Japanese editorial drafts and private review (#283/#342)

**Status: nonpublishing, opt-in, no inference or production integration.**
Parent C3 #354 remains unchanged. This module replaces candidate-count-only
presentation with bounded Japanese model draft text, but only in a conspicuously
labelled private AI-draft preview. It is NOT the approved final Daily format.

## Public flow

1. `build_witnessed_narrative_contexts(capture, partials, grounds, decisions)`
   rebuilds C3 from exact raw C1/C2 replies and snapshotted advisory materiality
   records, then reads the original Collector-owned CAS triplet once more.
   A selected Issue/PR gets one canonical context containing all its original
   events, source-derived status/title/link, Capture/Receipt/Evidence/Closure/
   Rollup/decision/preview hashes, an explicit Japanese prompt and JSON schema.
   No model can create an extra entity or choose a different link or title.
2. A caller supplies exact `NarrativeReply(context_sha256, response_bytes)`
   records. `build_witnessed_narrative_grounding_contexts(...)` rebuilds the
   trusted source again and validates only `sentences:[{text,source_refs}]` and
   `pending_source_refs`. Every original entity event must be cited or explicitly
   pending, never silently dropped. Empty editorial replies leave all originals
   pending; missing replies do not certify an inactive day.
3. Each edited sentence is turned into a new singleton grounding prompt with
   the original cited events, correct Issue/PR identity/date/status and exact
   Context/draft/raw-editorial-reply/source hashes. Earlier C2a support for a
   candidate is NOT support for the rewritten sentence. Even whitespace changes
   to the raw editorial reply change its grounding identity.
4. `build_witnessed_narrative_preview(...)` rebuilds C3/CAS once per request and
   replays the raw editorial and `NarrativeGroundReply` records. It does not
   accept prevalidated Context/draft/assessment objects as trust credentials.
   Only sentences with a current `supported` advisory verdict are displayed,
   each labelled **AI下書き（未承認）**. Source-derived lifecycle status is a
   distinct fixed line; unsupported/unresolved edited sentences remain private
   diagnostics and their evidence stays pending, even when another supported
   sentence cites the same source. Missing sentences retain selected entities.

The output report binds the raw editorial/grounding response hashes; reason
prose is replaced by a hash. It preserves C3 diagnostics, unsupported/unresolved
candidate coverage, unattributed default-branch IDs and original source count.
Its own bytes/markdown hashes are deterministic. Raw source bodies never appear
in the rendered report, but selected source titles and AI draft text are PRIVATE,
not generally anonymized or safe to publish. Ref order is canonically sorted.

## Trust and semantic limits

The checkpoint digest and raw response selection still require a separately
trusted caller. This module does not authenticate actual model execution,
model revision/options, a human materiality judgment or human review. It does
not defend against root/compromised Collector UID or arbitrary Python execution.
All raw record primitive fields are copied before source access. Public
admission rejects foreign, repeated, stale, malformed, oversized, wrong-type or
incomplete reference records with a typed whole-request error.

A Japanese-script character check is only a format check, not language fluency
or factual truth. A cited event can contain more than the sentence preserves;
**structural accounting is not semantic completeness**. Grounding itself is an
advisory model judgment and can falsely support a hallucinated merge or deploy
claim. C4 therefore explicitly treats the entire narrative as an unapproved AI
draft, not a fact/status field. Human semantic/omission review remains required.
The immutable flags are `publication_authorized=false`,
`semantic_coverage_verified=false`, `materiality_provenance_verified=false`,
`model_execution_attested=false`, `model_verdict_is_advisory=true` and
`requires_human_quality_review=true`. Dataclasses and self-hashes confer no
capability. No C4 report may be accepted as an existing Summary/Projection or
Writer input merely because a model returned `supported`.

The Japanese prompt asks for latest-day milestones, precise pending/completed
boundaries, no inference from truncated excerpts, no source-text instructions,
and no invented relations/identities. These are model instructions, not proven
semantic enforcement. Grounding is rerun for the edited text against original
sources; actual-model and human quality acceptance still need execution.

## Bounds, rendering and cost

At most 64 selected entities, 256 original events per entity, 64 KiB per input,
4 MiB per context set, 16 KiB per editorial reply, 3 sentences (360 characters /
2 KiB each), 16 unique exact integer refs per sentence, 128 KiB per singleton
and 8 KiB per grounding reply. Output is at most 4 MiB. Oversize requests fail
rather than truncate evidence, entities or admitted sentence text.

C4 uses C3/C2b's admission and quotas plus one bounded CAS triplet read. It does
not rerun source reconstruction per edited sentence or received verdict.
The test suite instruments actual `cas.read_verified_capture` counts. These
limits are conservative work/data bounds, not wall-clock deadlines; legitimate
large days may need a separately measured batching design.

Markdown/HTML/Obsidian control syntax is escaped, including `%%`, without
truncating admitted prose. Unlike prose, existing title labels retain C3's
bounded display policy. Escaping prevents formatting effects, not false prose.
No test or public CI log should dump real source contexts, replies or drafts.

## Executed fixture versus release

Tests use synthetic GitHub data and the real local POSIX-ACL CAS fixture.
Japanese example replies and grounding replies are supplied test data, NOT a
live model, current-day report or human-reviewed quality baseline. They cover
owned input snapshots, new-edited-text grounding, citation/pending conservation,
foreign and changed raw identities, overlapping unsupported sources, raw reason
privacy, Unicode/JSON/ref abuse, safe rendering, lifecycle-only and empty cases,
byte limits, no effects, deterministic hashes and extra-read count.

No CLI/inference adapter, HTTP requests, model process, CAS write on C4 paths,
Nextcloud/Vault, Writer/Timer, host configuration or main change is introduced.
Outstanding release work: actual inference with attested model/prompt/options/
reply selection, real-day private quality and omission baseline #290, Japanese
editorial acceptance #283, large-day profiling, #337 HTTP409, production UID/ACL/
checkpoint audit, parent independent exact-range gates and explicitly approved
Summary/Projection/Writer/Timer integration. Keep the entire stack Draft.
