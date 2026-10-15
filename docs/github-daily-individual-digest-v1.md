# C5-I: private complete individual Issue/PR Daily preview (#324)

**Delivery goal.** Render the target day's relevant, verified Issue/PR progress as
a single readable private Japanese Daily: a repository heading (grouping
only), then one source-derived Issue/PR item per entity. Do **not** generate
a repository-wide summary. Do not require a perfect contradiction-free text.

## Minimal composition API

```python
from obsidian_automation.github_daily_individual_digest import (
    compose_private_individual_digest,
)

daily = compose_private_individual_digest(
    capture_sha256=authorized_capture,
    partial_replies=original_partial_replies,
    grounding_replies=original_grounding_replies,
    materiality_decisions=original_decisions,
    individual_runs=tuple(single_entity_c5_runs),
)
private_markdown = daily.markdown
review_metadata = daily.report()
```

Each C5 run is explicitly scoped with
`target_entity=(repository, "issue"|"pull_request", number)` and uses the
existing `run_witnessed_narrative_inference` implementation. C5 model calls
may be executed independently or sequentially. This module adds **zero
model/GitHub requests**, and has no CLI/Writer/Timer/Vault/Nextcloud effect.

It intentionally does not introduce another model, cross-entity paraphrasing,
semantic completeness judge, contradiction review, retry, or competing Source
Authority. C4 re-reads the original verified CAS and reconstructs the
combined per-entity preview from **exact original C5 C4 replies and new
Grounding replies**, using the unchanged bounded source and identity checks.
Order of supplied entity runs cannot change the deterministic C4 source order.
Duplicate targets, foreign citations, changed capture/provenance, unsupported
input types, or unsafe publication flags fail closed. The metadata report
contains identifiers, hashes and bounded counts, **not raw GitHub or model
source text**.

## Status of an individual item

A selected entity with at least one Grounding-supported Japanese sentence
shows its original C4 `AI下書き（未承認）` text. This is a candidate, **not
independent human truth**.

A selected entity whose editorial/grounding failed, was unsupported, or
was not requested, **still appears exactly once**, with the existing
`本文は未生成、根拠不足、または照合待ちです。` notice, original Evidence
pending count and source-derived title/link/status. This must never be
silently turned into `進展なし`. Thus today's user-visible target is
**entity inclusion + useful Japanese draft**, while original Evidence remains
untouched even for incomplete model work.

The list of selected entities is already determined upstream by C3 materiality
and source-bound verified identities. This composer does not independently
claim that every changed Issue/PR was selected, or that all selected events
were semantically summarized. Review daily selection/omission separately by a
small representative real-date private preview. Summary revision alone is not
authorization to merge, Writer-apply or enable Timer.

## Regression/release boundary

Compare a C5 full-entity preview with the composition of separately generated
single-entity C5 runs on one identical synthetic Capture/CI fixture, including
reordered run collection. A failed or missing run must leave the entity visible
as pending. Adversarial tests cover duplicate targets, whole-entity runs,
foreign/mixed provenance, forged authority flags, invalid model grounding
identity and unsupported verdicts. Verify the exact GitHub HEAD in full Nix
Python suite, independent Correctness/Security source reviews, then a
real-source **offline**, date-scoped private acceptance sample, including
#358 day-snapshot constraints. Current code does not install a scheduler,
persist private reviews or enable production Writer.

Keep legacy v1 and published historical Daily artifacts unchanged. Explicit
human approval is required before any future main merge or Writer/Timer rollout.
