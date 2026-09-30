# Semantic Planner production acceptance v1

This runbook is the final gate before enabling
`AI_INPUT_MODE=semantic-deep-knowledge`.

The acceptance flow is intentionally split from mutation. It proves exact
deployment, authority, Semantic Index freshness, retrieval quality and one
selection observation, then emits an environment **plan**. It does not edit
`/etc/obsidian-ai/pre-review-input.env`, start a service or enable a timer.

## 1. Exact-SHA deploy while remaining legacy

Deploy one reviewed merge commit through the existing updater/lifecycle.

Canonical production paths are:

```text
/opt/obsidian-automation/app
/opt/obsidian-automation/venv
/usr/local/sbin/obsidian-automation-update
```

After the update, production must still have:

```text
AI_INPUT_MODE=legacy
```

or no explicit `AI_INPUT_MODE` in `pre-review-input.env`.

The systemd unit itself also defaults to legacy.

## 2. Preflight acceptance

Run as root so OS authority checks can use the real production identities:

```bash
sudo /opt/obsidian-automation/venv/bin/obsidian-semantic-production-acceptance \
  preflight \
  --expected-revision <reviewed-merge-sha>
```

The receipt proves:

- production checkout HEAD is the exact revision;
- checkout is `main` and clean;
- revision env matches the same exact SHA;
- semantic mode is not already enabled;
- the Input Planner unit defaults to legacy;
- `02-Orchestration/semantic-selections` is Reader-readable/writable;
- Generator/Validator/Evaluator cannot read/write the Selection Store;
- the pre-review timer is enabled and active.

Receipts are stored by default under:

```text
/var/lib/obsidian-ai/deployments/semantic-planner/
```

and are content-addressed.

## 3. Build one exact Semantic Index

Use the existing Phase B flow. Reader creates a corpus/index plan, Embedder
performs only the bounded embedding work, and Reader finalizes the index.

The index build is deliberately separate from acceptance so the rollout never
follows a mutable `latest` alias.

Keep the resulting exact Semantic Index SHA.

If the pull-only mirror changes before acceptance completes, the exact index
correctly becomes stale. Rebuild against the new Corpus or use the Reader-planned
incremental refresh flow in [Semantic Embedding Index v1](semantic-embedding-index-v1.md).
The Embedder consumes a bounded refresh plan and never gains finalized Semantic
Index read authority.
Do not bypass staleness verification and do not introduce a mutable `latest`
index pointer.

## 4. Verify the exact index against the current mirror

```bash
sudo /opt/obsidian-automation/venv/bin/obsidian-semantic-production-acceptance \
  verify-index \
  --semantic-index-sha <semantic-index-sha256>
```

This fails if the bound Semantic Corpus is stale relative to the current
pull-only Vault mirror.

The receipt contains only bounded metadata:

- exact index/corpus/plan/result-set identities;
- embedding provider/model identity;
- vector dimension/encoding;
- source-kind counts;
- vector count.

It does not contain Vault source text.

## 5. Run the Phase C retrieval benchmark

Prepare and embed the benchmark queries with the existing
`obsidian-semantic-retrieval` commands, then run:

```bash
sudo /opt/obsidian-automation/venv/bin/obsidian-semantic-production-acceptance \
  benchmark \
  --semantic-index-sha <semantic-index-sha256> \
  --benchmark <reviewed-production-benchmark.json> \
  --plan-sha <benchmark-plan-sha256> \
  --result-set-sha <benchmark-result-set-sha256> \
  --retrieval-profile semantic-retrieval-v1
```

The acceptance remains:

```text
hybrid semantic recall@K > BM25 semantic recall@K
AND
hybrid exact-technical top1 >= BM25 exact-technical top1
```

A failed benchmark writes a failed receipt and exits non-zero for acceptance
purposes. A failed receipt cannot be used to plan the canary.

The benchmark corpus remains Human-reviewed evidence. This tool never generates
ground-truth relevance labels.

## 6. Observe the intended Selection Policy

For the initial rollout:

```bash
sudo /opt/obsidian-automation/venv/bin/obsidian-semantic-production-acceptance \
  observe-selection \
  --semantic-index-sha <semantic-index-sha256> \
  --policy semantic-project-distill-v1
```

This may produce either `selected` or a legitimate novelty `skipped`
observation.

It creates no pre-review job and must not change the Planner submission clock.

## 7. Build the canary environment plan

Use the four exact receipt SHAs:

```bash
sudo /opt/obsidian-automation/venv/bin/obsidian-semantic-production-acceptance \
  plan-canary \
  --preflight-receipt-sha <sha> \
  --index-receipt-sha <sha> \
  --benchmark-receipt-sha <sha> \
  --selection-receipt-sha <sha>
```

The command cross-checks that index, benchmark and selection receipts bind the
same exact Semantic Index, that the benchmark passed, and that benchmark and
Selection observation bind the same versioned retrieval profile.

Output contains an inert plan equivalent to:

```text
AI_INPUT_MODE=semantic-deep-knowledge
AI_INPUT_SEMANTIC_INDEX_SHA=<exact-semantic-index-sha256>
AI_INPUT_SEMANTIC_SELECTION_POLICY=<versioned-policy>
```

The receipt explicitly records:

```text
mutation_performed = false
```

Human/operator action remains required to apply the environment change.

## 8. Canary enablement

Only after reviewing the plan should the production environment be edited and
the normal controlled service lifecycle used.

The first rollout remains limited to `deep-knowledge-v1`. Idea discovery and
Project adoption stay outside automatic durable jobs.

If canary behavior is not acceptable, restore:

```text
AI_INPUT_MODE=legacy
AI_INPUT_SEMANTIC_INDEX_SHA=disabled
```

Existing already-started jobs remain immutable; rollback affects future Planner
cycles only.
