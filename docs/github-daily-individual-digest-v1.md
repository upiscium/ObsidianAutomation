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
Each supplied editorial/Grounding answer must exactly match the **original
accepted C5 attempt** (raw and admitted-response SHA plus receipt binding),
and each resulting per-entity row must reproduce its **original** C5 preview.
A structurally valid substituted `supported` reply must not promote an
originally `unsupported` statement. Duplicate targets, foreign citations,
changed capture/provenance, unsupported input types, or unsafe publication
flags fail closed. The metadata report
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


## One explicit all-selected-day operation (opt-in, offline/manual only)

\`run_complete_private_daily(...)\` is an operator-controlled helper using the
**existing C5 inference** and the same composer, not a new model stage. It
enumerates every verified C3-selected (repository, Issue/PR type, positive
number) in source order, reserves the full worst-case four model calls
per entity before any inference, then invokes C5 once per entity. A shared
execution-time budget may stop before a later entity; those unrequested entries
must remain **visible and pending** in the composed private Markdown.

\`\`\`python
from obsidian_automation.github_daily_individual_digest import (
    run_complete_private_daily,
)

result = run_complete_private_daily(
    authorized_capture_sha, partial_replies, grounding_replies,
    materiality_decisions,
    infer=approved_native_ollama_infer,
    max_entities=64, max_total_calls=256,
    max_total_seconds=3600, per_entity_seconds=120,
    require_day_policy=True,
)
private_markdown = result.markdown
run_accounting = result.report()
\`\`\`

The strict default accepts only #358 \`github-day-snapshot-v1\` per-event
projected source within an independently verified immutable CAS capture; this
presence/identity check does not prove original resource edit time or model
execution authenticity. **An old API snapshot is not historically day-safe.**
\`require_day_policy=False\` is an explicit **legacy offline diagnostic only**
and can never authorize Daily publication. The policy is not magically present
in the original #359 branch: #358 must be integrated and adopted before
strict production-like usage.

\`selected_entity_count\`, \`requested_entity_count\`, \`unrequested_entity_count\`,
\`pending_summary_entity_count\`, \`all_inference_responses_complete\`, and
\`complete_and_all_selected_have_supported_text\` are separate. An exhausted
time budget is reported and leaves pending source items; it does not fabricate
empty/no-progress content. All source, semantic, factuality and human approval
remain advisory/unverified. A zero selected-entity day requires independent
C3/source completeness evidence before declaring a genuinely empty GitHub day.

This helper never fetches new GitHub data, persists artifacts, contacts
Nextcloud/Vault or enables a systemd Timer. Original C5 model calls occur only
because the operator supplied \`infer\` and invoked this function. A large day
may require many sequential C5 single-target runs; there is **no implicit
infinite retry**, and extra CAS reconstruction could be resource-intensive.
The bounded size, 4-calls-per-target reservation and elapsed limits are meant
to keep manual previews auditable, not attest throughput at 64-entity scale.
Independent model-selection/preflight/production UID/ACL/Writer approval remain
separate.


### Selection is not the same as source coverage

The complete-day report now separately includes C3's original source entity
count, original event count, C1/C2 response-set completeness, and the bounded
identities/count of **unselected entities still pending materiality review**.
These are *not* automatically added as progress because the user wants
meaningful updated Issue/PR items, not routine messages. However a day with
zero selected items and unresolved candidate materiality must never be treated
as a positively verified empty day. The operator must inspect those pending
source entities before claiming all important daily updates were included.
No additional model or semantic contradiction stage is introduced.
