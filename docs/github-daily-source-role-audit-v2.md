# Source-role audit v2 (Issue #290)

This is an optional read-only calibration tool for individual GitHub Daily summaries.
It does not change model replies, post-edit Grounding, source capture, publication, Writer or Timer.

## Why this exists

PR #355 contained actual completed Markdown escaping work and, within the same
cited source event, a generated synthetic example describing a different repair.
Native Grounding erroneously supported attributing the example's repair to the PR.
Changing Grounding instructions alone did not reject that fixed counterexample.

## Contract

`audit_individual_source_roles(source_review_bytes, preview_bytes, target_entity, annotations)`
accepts the exact private C5 source-review and C4 preview bytes. Each annotation
names a displayed sentence index, exact Unicode code-point claim_start/claim_end
within that sentence, claim kind, operator assessment, and one or more
source anchors (`source_ref`, `field`, Unicode code-point `start` / `end`, `role`).
The source field must belong to an event that the sentence actually cites.
Multiple nonoverlapping claim spans may be annotated inside a single sentence:
this prevents a genuine completed action in one clause from laundering a
quoted-example action in another. Every partial label set remains incomplete;
`all_atomic_claims_labeled_verified` stays false even when every sentence has
at least one label. Offsets alone cannot prove that all claims were found.

Claim kinds: `performed_action`, `example_report`, `unresolved_status`, `other`.
Source roles: `direct_action`, `quoted_example`, `proposed`, `negated`, `background`.
Assessments: `entailed`, `not_entailed`, `uncertain`.

A performed-action claim referencing only a quoted-example anchor is reported as
`quoted_example_only`, regardless of the model verdict or operator assessment.
Other performed-action claims with no direct-action anchor are reported as
`performed_action_not_established_by_anchors`. Absent labels are `unreviewed`.
`no_scope_conflict_detected_not_verified` never implies factual correctness.

Canonical bytes, preview/content hashes, entity identity and cited span bounds
are checked. Every preview sentence is type-checked before any annotation offset
is dereferenced; malformed fields fail with the typed audit error, while genuine
C4 `unresolved` verdicts remain permissible but cannot be annotated as displayed.
Operator labels remain untrusted semantic interpretations: verifying
their offsets does not verify their meaning or provide independently signed
human adjudication. Review metadata contains only hashes, roles, offsets and counts,
not source or model text. Every output has publication, factuality, semantic
coverage, and independent attestation flags set to false.

## Acceptance boundary

Use the fixed PR #355 false claim, a genuine completed-action positive,
ambiguous plans and unresolved-work omissions for independent calibration.
Preserve failed verdicts and unreviewed source records. Source-role annotations
do not themselves fix the model, justify a cross-entity summarizer, replace the
temporal provenance correction in #357/#358, or authorize deployment/merge.
