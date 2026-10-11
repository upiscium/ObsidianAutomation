# Daily Project Progress semantic coverage evaluation v0 (#290)

Status: **DRAFT / metadata-only, read-only prototype.**

The deterministic partitioner must retain every original Evidence ID in order.
Citation coverage asks whether a source is explicitly referenced; it is **not**
equivalent to preserving every material fact. A single original event may
produce zero useful claims, and a supported standalone claim may be semantically
entailed by a batched output without citing that exact source event.

The pure function `github_daily_coverage_eval.evaluate_coverage` accepts
externally validated immutable Evidence, partition/output identities, normalized
batched claims, Grounding-supported single-event claims and independent
`human` versus `model` entailment/adjudication labels. It rejects duplicate,
missing, reordered or cross-repository identities. It returns bounded metadata
counts and sample-level verdicts; it never returns original source text,
generated summaries or individual source/claim IDs in the human-facing report.

The caller **must independently verify** original canonical Evidence bytes,
claim identities/Output/Provenance digests, Grounding verdict bytes and any
adjudication provenance. A supplied Grounding digest is a declared external
binding, **not independently attested by this pure function**. Model judgments
are advisory and cannot satisfy human review. A `no_omission_in_reviewed_sample`
result does not establish comprehensive day-level quality or authorize Writer
and does not justify Daily timer activation.

For #289 acceptance, next collect representative original events across sizes
and repositories, validate independently grounded singleton claims, calibrate
human semantic comparisons and acceptable material omission rate, and measure
whole-day Ollama call count/token/time. Keep the exact 2026-10-08 historical
Evidence immutable. Do not alter the production partitioner or Writer via
this diagnostic prototype.

## Canonical Evidence structural adapter (read-only)

The `verified_partition_report(evidence_file, max_partial_context_bytes=...)`
entrypoint accepts a **bounded, absolute, canonical SHA-named Evidence file**,
uses `load_evidence_bundle` to revalidate its bytes and each original event
identity, and derives actual `partition_evidence` SummaryContexts for that
exact proposed byte budget. It returns only the Evidence SHA, partition SHA
(which binds the context SHA list and byte budget), counts and maximum context
size; it never returns event titles, actors, issue bodies, summaries or source
text. Validation necessarily reads the private original Evidence into memory,
but there is **no external upload, logging of source text, model call or write**.

The production partial partitioner groups by repository while preserving
**per-repository event order**. Original globally interleaved streams may
therefore legitimately be reordered across different repositories. The pure
evaluation function now checks exact preservation of each repository's event
sequence, forbids cross-repository model batches, and detects missing or
duplicated Evidence IDs, instead of incorrectly requiring the flattened model
batch order to equal global chronological Evidence order.

This structural report is **not** semantic coverage: to evaluate real model
outputs one still needs exact partition/provenance-bound Grounding and
independent human entailment/missing assessments. The adapter does not silently
fabricate batched claims or standalone Grounding approvals. Both functions
return `publication_authorized: false`. The canonical 2026-10-08 Evidence
remains immutable, and no production Daily Note or Writer is affected.
