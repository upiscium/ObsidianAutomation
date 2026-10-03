# Automatic Semantic Index refresh v1

## Purpose

Automatic Semantic Index refresh keeps Semantic Planner bound to the current
pull-only Vault mirror without introducing a mutable Semantic Index manifest.

All Semantic Corpus, embedding request/plan/result/result-set, refresh-plan and
Semantic Index artifacts remain immutable and content addressed.

The only mutable state introduced here is a small Reader-owned control binding:

```text
04-Index/semantic-active-index.json
```

It names one exact finalized Semantic Index SHA and repeats the corpus/model
identity required to validate that binding.

## Authority flow

```text
successful pull-only Vault refresh
        |
        v
obsidian-semantic-index-refresh-prepare.service
  user: obsidian-ai-reader
  - read current active binding
  - build current Semantic Corpus
  - inherit exact embedding model/revision from active index
  - prepare normal embedding plan
  - prepare incremental refresh plan
        |
        v
obsidian-semantic-index-refresh-embed.service
  user: obsidian-ai-embedder
  - read Reader refresh control
  - embed only new/changed inputs
  - reuse already verified unchanged vectors
  - write result set + Embedder control
        |
        v
obsidian-semantic-index-refresh-finalize.service
  user: obsidian-ai-reader
  - cross-check Reader/Embedder controls
  - verify active index did not change concurrently
  - finalize against the current mirror
  - atomically replace active binding only after success
```

Embedder cannot write the Reader control directory or the active binding.
Reader cannot write the Embedder control directory.

## Active binding

Initial activation is explicit:

```bash
sudo -u obsidian-ai-reader \
  /opt/obsidian-automation/venv/bin/obsidian-semantic-index-refresh activate \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault \
  --semantic-index-sha <reviewed-current-index-sha>
```

Activation revalidates the exact index corpus against the current mirror before
writing the binding.

The Planner can then use:

```text
AI_INPUT_MODE=semantic-deep-knowledge
AI_INPUT_SEMANTIC_INDEX_SHA=active
AI_INPUT_SEMANTIC_SELECTION_POLICY=semantic-project-distill-v3
```

`active` is resolved once at the start of each Planner invocation and becomes an
exact SHA before Selection/Context/job state is created. Durable jobs never store
the literal word `active`.

An exact SHA remains supported for controlled canaries and historical replay.

## Refresh opt-in

Automatic refresh is disabled unless this deployment-private file exists:

```text
/etc/obsidian-ai/semantic-index-refresh.env
```

Example:

```text
SEMANTIC_EMBEDDING_BASE_URL=http://127.0.0.1:11434
```

The endpoint does not choose a model. The refresh always inherits the exact
provider/model identifier/model revision from the currently active finalized
index. A provider identity mismatch fails before a new index can be activated.

A successful `obsidian-ai-vault-pull.service` has:

```text
OnSuccess=obsidian-semantic-index-refresh-prepare.service
```

The prepare unit itself has `ConditionPathExists` for the refresh env file, so
installing/deploying the units does not opt production into automatic refresh.

## Unchanged mirror

If the rebuilt Semantic Corpus SHA equals the active index corpus SHA:

- Reader records phase `unchanged`;
- Embedder performs no provider request;
- finalization rechecks that the active corpus is still current;
- active binding remains unchanged.

## Changed mirror

For a changed corpus:

1. Reader builds a new immutable embedding plan using the active index model
   identifier and exact model revision.
2. Reader builds an incremental refresh plan against the active index.
3. Embedder reuses vectors whose exact embedding input and model contract match.
4. Only new/changed embedding inputs call the provider.
5. Reader finalization reloads all bindings and verifies the new corpus against
   the current mirror.
6. Only then is `semantic-active-index.json` atomically replaced.

## Failure semantics

The previous active binding is never changed when:

- the active binding is missing or inconsistent with its finalized index;
- the mirror changes while prepare/finalize is running;
- a prepared corpus becomes stale before finalization;
- provider/model identity changes;
- provider inference fails;
- the result set is incomplete or reordered;
- Reader/Embedder control files do not match;
- active binding changes concurrently;
- final Semantic Index validation fails.

If the Vault mirror has changed while the previous active binding remains stale,
Semantic Planner cannot successfully build Selection from that index. This is
intentional fail-closed behavior; it must not silently generate from stale
vectors.

## Control files

Reader and Embedder use separate bounded mutable controls:

```text
04-Index/semantic-refresh-reader/current.json
04-Index/semantic-refresh-embedder/current.json
```

They contain only artifact identities, phase and bounded refresh counts. They do
not contain Vault source text or vectors.

These controls are orchestration state, not source authority and not Semantic
Index manifests.

## Controlled rollout

Keep both production timers disabled while accepting this feature.

Recommended acceptance sequence:

1. deploy one exact reviewed SHA while timers remain disabled/inactive;
2. verify the existing reviewed Semantic Index is current;
3. explicitly `activate` that exact index;
4. set `AI_INPUT_SEMANTIC_INDEX_SHA=active`;
5. install `/etc/obsidian-ai/semantic-index-refresh.env`;
6. manually start one Vault pull and observe prepare -> embed -> finalize;
7. verify the active binding resolves to an index current against the mirror;
8. modify/add one disposable semantic source through the normal Vault path and
   repeat the pull to verify incremental reuse plus activation;
9. verify a forced provider/finalization failure leaves the old active binding
   unchanged;
10. only after those gates pass, restore the normal mirror/pre-review timers.

This stage changes derived-state refresh and Planner binding only. It does not
change Generator, Evaluator, Human Review or canonical Vault write authority.
