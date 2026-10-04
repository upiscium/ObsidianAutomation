# Automatic Semantic Index refresh v1

## Purpose

Automatic Semantic Index refresh keeps Semantic Planner bound to the current
pull-only Vault mirror. Semantic Corpus, embedding request/plan/result/result-set,
refresh-plan and Semantic Index artifacts remain immutable and content addressed.

The Reader-owned current index binding is:

```text
04-Index/semantic-active-index.json
```

It names one exact finalized Semantic Index SHA and repeats the corpus/model
identity needed to validate the binding. Separate bounded Reader and Embedder
handoffs coordinate each refresh. None of these controls is source authority.

## Authority flow

A successful pull-only Vault refresh starts the following finite service chain:

| Stage | Unix identity | Work |
| --- | --- | --- |
| `obsidian-semantic-index-refresh-prepare.service` | `obsidian-ai-reader` | Build the current corpus and incremental plan using the active index's exact model and revision. |
| `obsidian-semantic-index-refresh-embed.service` | `obsidian-ai-embedder` | Reuse verified unchanged vectors and embed only new or changed inputs. |
| `obsidian-semantic-index-refresh-finalize.service` | `obsidian-ai-reader` | Validate the handoffs and immutable artifacts, check currentness and publish the new binding. |

Embedder cannot read raw Vault sources or write the Reader handoff or active
binding. Reader cannot write the Embedder handoff. Planner does not start a
refresh service as a dependency; a new Selection must independently pass the
existing current-corpus check.

## Active binding and pending recovery

Initial activation is explicit:

```bash
sudo -u obsidian-ai-reader \
  /opt/obsidian-automation/venv/bin/obsidian-semantic-index-refresh activate \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault \
  --semantic-index-sha <reviewed-current-index-sha>
```

Activation revalidates the exact index corpus against the current mirror and
writes the binding while still holding the mirror read-view lock.

The Planner can then use:

```text
AI_INPUT_MODE=semantic-deep-knowledge
AI_INPUT_SEMANTIC_INDEX_SHA=active
AI_INPUT_SEMANTIC_SELECTION_POLICY=semantic-project-distill-v3
```

For a new transaction, `active` is resolved once and becomes an exact SHA before
Selection, Context or job state is created. Durable jobs store the exact SHA.
An exact-SHA configuration remains supported for controlled canaries.

Pending recovery happens first. A journaled transaction retains its immutable
Context, index, selection and policy identities even if the active binding has
rotated or is missing. Recovery verifies those saved identities and completes
that snapshot without selecting new sources or rereading the Vault. An explicit
fixed-SHA configuration change remains a conflict. This preserves the existing
[Planner transaction contract](ai-input-planner.md); fresh Selection still
fails when its index is stale against the current mirror.

## Refresh opt-in and success guards

Automatic refresh requires systemd 251 or later and this deployment-private file:

```text
/etc/obsidian-ai/semantic-index-refresh.env
```

Example:

```text
SEMANTIC_EMBEDDING_BASE_URL=http://127.0.0.1:11434
```

Private config transfer preserves the file as optional, root-owned `0600`
configuration. Importing a bundle without it does not create it. The file is
read by systemd for Embedder's environment, not directly by Reader or Embedder.
Installing the units alone does not opt production into automatic refresh.

The endpoint does not choose a model. Refresh inherits the exact provider/model
identifier/model revision from the active finalized index. A provider identity
mismatch fails before publication.

`obsidian-ai-vault-pull.service` uses
`OnSuccess=obsidian-semantic-index-refresh-prepare.service`; prepare and embed
similarly start their successors on success. Each successor also checks all
four predecessor values in `ExecStartPre`:

- `MONITOR_UNIT` is its one expected predecessor;
- `MONITOR_SERVICE_RESULT` is `success`;
- `MONITOR_EXIT_CODE` is `exited`;
- `MONITOR_EXIT_STATUS` is `0`.

These variables are supplied to success handlers by
[systemd](https://github.com/systemd/systemd/blob/v251/man/systemd.exec.xml).
A skipped, stopped, failed or unrelated predecessor cannot start the next
refresh operation. Direct acceptance of an individual stage uses the CLI;
manually starting a successor unit without its predecessor evidence fails the
guard. Each stage also requires the env file and the absence of the runtime
update inhibitor described below.

## Publication and concurrency

Reader serializes `activate`, `prepare` and `finalize` with an exclusive lock at:

```text
04-Index/semantic-refresh-reader/refresh.lock
```

The lock order is Reader refresh lock, then mirror read-view lock. Provider
inference runs outside both locks. Existing immutable artifact builders acquire
their own mirror lock; callers do not nest that non-reentrant lock.

Finalization first validates the immutable result set and creates the immutable
index. Before publishing, it reacquires the mirror lock and checks that:

1. the Reader handoff bytes still have the captured SHA;
2. the entire active binding still matches the one read at the start;
3. the finalized corpus is still current against the mirror.

The atomic binding replacement occurs before releasing that mirror lock. A
mirror update between immutable finalization and publication therefore cannot
publish a stale binding, and another Reader operation cannot overwrite an
intervening activation.

Overlapping successful pulls may supersede a prepared handoff while embedding
is running. Such a run fails closed; the next successful pull retries against
the current mirror. A continuously changing corpus may require a quiet interval
to complete. This finite chain does not queue an unbounded backlog of snapshots.

## Unchanged mirror

If the rebuilt Semantic Corpus SHA equals the active index corpus SHA:

- Reader records phase `unchanged`;
- Embedder performs no provider request;
- finalization rechecks the handoff, binding, model and current corpus;
- the active binding bytes remain unchanged across repeated no-op cycles.

## Changed mirror

For a changed corpus:

1. Reader builds a new immutable embedding plan using the active index model
   identifier and exact model revision.
2. Reader builds an incremental refresh plan against the active index.
3. Embedder reuses vectors whose exact embedding input and model contract match.
4. Only new or changed embedding inputs call the provider.
5. Reader validates the exact result-set/refresh-plan association, model,
   provenance and refresh counts before finalization.
6. The publication checks above pass before the active binding is replaced.

## Control files

Reader and Embedder use separate bounded mutable controls:

```text
04-Index/semantic-refresh-reader/current.json
04-Index/semantic-refresh-embedder/current.json
```

They contain artifact identities, phase and bounded refresh counts, without
Vault source text or vectors. Embedder records `reader_control_sha256`, the
SHA-256 of the complete Reader file bytes it consumed. Reader rejects a result
from different control bytes, including a metadata-only or representation-only
change. Matching a subset of plan identities is insufficient. Identical Reader
bytes have the same identity across retries.

## Failure semantics

Refresh never replaces the previous active binding when:

- the binding is missing or inconsistent with its finalized index;
- a prepared or finalized corpus is stale against the mirror;
- provider/model identity changes or provider inference fails;
- the result set is incomplete, reordered or bound to another refresh plan;
- the handoffs or their counts do not match the validated immutable artifacts;
- the Reader handoff or active binding changes concurrently;
- final Semantic Index validation fails.

If the mirror has changed and the previous active index is now stale, new
Selection fails before job submission. The already journaled immutable Context
recovery described above remains available.

## Runtime updates

Both the consolidated host lifecycle and the legacy pre-review updater include
all three refresh workers in their stop/drain/install/smoke contracts. The legacy
updater also installs the Vault pull unit containing the success trigger and
provisions the two handoff directories' narrow Reader/Embedder ACLs on existing
hosts. Staging rewrites the service executable prefix for the selected layout.

Use the target-owned `obsidian-automation-update` path for the first upgrade
introducing refresh. An already installed older legacy updater runs its old
managed-unit list and cannot install the new services merely by replacing the
checkout beneath itself. Follow the
[consolidated host update procedure](host-runtime-lifecycle.md) with one exact
reviewed merge SHA.

Before stopping timers or draining workers, either updater creates:

```text
/run/obsidian-automation/semantic-refresh-inhibited.json
```

The root-owned `0600` marker contains the exact target SHA and updater owner
(`host-runtime` or `pre-review`). A separate stable `.lock` file serializes both
updater paths. Symlinks, hardlinks, unsafe directory ownership or write modes,
and a marker belonging to another target or updater are refused. PID1 checks
the marker before starting each refresh stage, so late success callbacks cannot
restart a worker during package or unit replacement.

The marker remains in place through safe smoke, timer-state restoration and
receipt persistence. Success removes it; a caught failure or interruption
retains it and attempts timer containment. Repair the reported failure and retry the same
updater with the same target SHA. The consolidated lifecycle recovers the
original timer states from `pending-runtime.json`; the legacy updater preserves
the states it observes when each retry begins. Both capture new timer states
inside the shared lock, after any prior updater has released it. The marker is
under `/run`; durable recovery and uncatchable interruption follow the
[host lifecycle recovery contract](host-runtime-lifecycle.md#failure-and-recovery).
Do not remove a marker to bypass an owner/target conflict.

## Controlled rollout

Keep both production timers disabled while accepting this feature. Code/CI
acceptance does not replace the production-host checks below.

1. Deploy one exact reviewed merge SHA through the target-owned consolidated
   updater with both timers disabled/inactive. Confirm systemd supports the
   success-handler variables, the installed units contain the guards, and the
   handoff ACLs are in place.
2. Verify the existing reviewed Semantic Index is current and explicitly
   `activate` that exact index.
3. Run `prepare`, `embed` and `finalize` directly for a no-op cycle. Confirm zero
   provider calls and unchanged active-binding bytes.
4. In an isolated disposable fixture, use the production embedding provider to
   verify incremental reuse after a source change, then provider failure and a
   stale corpus. Failures must preserve the last valid binding. The fixture has
   its own Vault root, AI root and active binding.
5. Set `AI_INPUT_SEMANTIC_INDEX_SHA=active` and install the optional refresh env
   file. Manually start one Vault pull and observe successful prepare, embed and
   finalize invocations. Confirm the binding names an index current against the
   mirror.
6. Run one controlled Planner canary and verify Selection, Context and job bind
   the exact resolved index SHA. Confirm a new canary refuses a stale index.
7. Verify the deployment receipt and both timers' disabled/inactive states.
   Only after the host acceptance evidence is reviewed may the operator restore
   normal mirror/pre-review recurrence.

This stage changes derived-state refresh and Planner binding. The accepted
Selection v3, Generator v6 and Evaluator v9 quality policies retain their
existing versioned contracts.
