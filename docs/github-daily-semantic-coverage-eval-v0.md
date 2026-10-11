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
