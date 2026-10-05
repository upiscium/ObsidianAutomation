# Production canonical-writer authority topology v0

## Purpose

Phase 2 introduces canonical Vault writes while preserving strict authority separation. The Phase 1 Snapshot LXC remains permanently read-only and must not be upgraded into a canonical writer.

Production v0 uses one dedicated AI Writer host/LXC. Sync Transport, Reader/Indexer, Generator, Validator, Evaluator, Human Review, and Executor are separated by Linux identities and filesystem ACLs. Canonical remote creation is performed only by Sync Transport with a conditional WebDAV request.

## Host boundary

```text
Nextcloud Live Vault
        ^
        | conditional WebDAV create
        | dedicated writer credential
        |
AI Writer host/LXC
├── obsidian-ai-sync
├── obsidian-ai-reader
├── obsidian-ai-embedder
├── obsidian-ai-generator
├── obsidian-ai-validator
├── obsidian-ai-evaluator
├── obsidian-ai-status
├── <human reviewer account>
└── obsidian-ai-executor

Snapshot LXC
└── existing Nextcloud read-only credential only
```

Production v0 requires exactly one AI Writer host. The shared production lock is host-local and is not a distributed lock.

## Credential boundary

Only `obsidian-ai-sync` may hold the canonical Nextcloud **writer** credential.

Rejected-projection cleanup uses a separate Nextcloud credential that is also
readable only by the Sync transport identity, but its remote share is restricted
to `04-AI` and grants only Read + Delete. It cannot reach canonical
`11-Knowledge` or other Vault roots. The ordinary AI writer credential does not
gain Delete permission.

Review Intake may hold a separate read-only Nextcloud credential scoped to the
private Vault so it can observe the Human-edited `04-AI/50-Review` projection.
That credential cannot create, update, or delete canonical Vault content.

The following identities have no Nextcloud writer credential:

- `obsidian-ai-reader`;
- `obsidian-ai-embedder`;
- `obsidian-ai-generator`;
- `obsidian-ai-validator`;
- `obsidian-ai-evaluator`;
- the Human review account/tool;
- `obsidian-ai-executor`;
- the Phase 1 Snapshot LXC account.

`obsidian-ai-sync` does not accept LLM prompts or semantic mutation instructions. It consumes an exact durable transport request prepared by Executor, independently rechecks the validated mutation / Human approval / execution intent binding, performs `PUT` with `If-None-Match: *`, verifies remote bytes, and writes only the transport-result stage.

## Mirror and AI state are separate

Production must not store the AI lifecycle journal inside the pull-only Vault mirror.

Recommended layout:

```text
/var/lib/obsidian-ai/
├── vault/                   # Nextcloud -> local pull-only mirror
│   ├── 00-DailyNote/        # Reader-only Semantic Corpus input
│   ├── 05-Idea/             # Reader-only Semantic Corpus input
│   ├── 10-Project/
│   └── 11-Knowledge/
└── state/                   # local-only; never rclone-sync this tree
    ├── 00-Untrusted/
    ├── 02-Orchestration/
    │   ├── recipes/
    │   ├── semantic-selections/   # Reader-only semantic planning records
    │   ├── pre-review-jobs.sqlite3
    │   └── status/pre-review-status.json
    ├── 04-Index/
    │   ├── semantic-corpus/
    │   ├── semantic-embedding-requests/
    │   ├── semantic-embedding-plans/
    │   ├── semantic-embedding-results/
    │   ├── semantic-embedding-result-sets/
    │   └── semantic-index/
    ├── 05-Context/
    ├── 10-Validation/
    ├── 12-Evaluation-Request/
    ├── 14-Evaluation-Context/
    ├── 15-Evaluation/
    ├── 16-Human-Projection/       # role-scoped bounded Markdown requests
    ├── 17-Human-Projection-Result/ # Sync-only transport attestations
    ├── 20-Review/
    ├── 24-Locks/
    ├── 25-Execution/
    ├── 27-Transport/
    └── 30-Receipts/
```

This separation is required. A Nextcloud pull must never be able to delete local-only index/context/validation/evaluation/review/execution/transport/receipt artifacts.

## Derived retrieval state

`04-Index` remains non-authoritative derived state, but Semantic Embedding Index
v1 narrows provider access with a dedicated `obsidian-ai-embedder` identity.

Reader/Indexer owns Semantic Corpus manifests, embedding requests/plans, and the
final semantic index. Embedder may read only the bounded request/plan subtrees
and may write only embedding result/result-set subtrees. Embedder cannot read the
Vault mirror, Semantic Corpus manifest, final semantic index, or Generator
Context. Generator still cannot read any `04-Index` content.

This split prevents Reader from needing embedding-provider credentials/network
authority while preventing the provider-facing identity from acquiring Vault
authority. See
[Semantic Embedding Index v1](semantic-embedding-index-v1.md).

`05-Context` is the non-authoritative Reader -> Generator boundary. Reader is the only writer. Generator and Evaluator may read exact Context Bundles but cannot rewrite them. Context never grants validation, evaluation, approval, execution, transport, or receipt authority.

`12-Evaluation-Request` is a bounded Validator -> Reader bridge. It exists so Reader does not need read access to Generator proposals or Validation. Validator deterministically projects the accepted proposal/mutation binding, target path, and retrieval query. Reader is read-only on this stage.

`14-Evaluation-Context` is a non-authoritative Reader -> Evaluator boundary. Reader uses canonical Knowledge plus Reader-private Index to produce exact candidate bytes for redundancy/consistency evaluation. Evaluator may read but not rewrite it.

## Pre-review operational metadata

`02-Orchestration` is non-authoritative scheduling/progress metadata shared only
by Reader, Generator, Validator, and Evaluator. Successful stage rows store
selected hashes, not semantic artifact bodies. Validation, Evaluation, Human
Review, Execution, Transport, and Receipt artifacts remain authoritative in
their existing stages.

`obsidian-ai-status` may read only bounded orchestration metadata: the job
database plus Reader-owned Planner cadence state. It writes
`02-Orchestration/status/pre-review-status.json`. The projection contains
aggregate counts/health/reminder/backpressure and bounded cadence metadata
(interval/reason/eligibility plus selection/objective policy names and future
novelty-skip reason). It does not contain job IDs, selection artifact SHA,
Context/Proposal/mutation/recipe hashes, content, endpoints, credentials, or
Review decisions.

The Human reviewer may read the aggregate status projection but cannot rewrite
it. Sync and Executor do not receive orchestration write authority. Reader may
read authoritative `20-Review` and `30-Receipts` solely to reconcile terminal
scheduler metadata; Reader still cannot write either authority stage.

## Reader / Generator sequence

```text
Reader / Input Planner / Indexer
  current production generation:
    read canonical 11-Knowledge + active Project Notes
    create mixed immutable Generation Context
    submit bounded pre-review jobs
  offline Semantic Planner Phase B:
    read Daily + Idea + Project + Project Note + Knowledge
    create exact Semantic Corpus / embedding requests
        ↓ bounded request-only boundary
Embedder
  resolve pinned embedding model identity
  write bound embedding results/result-set
        ↓ result-only boundary
Reader / Indexer
  revalidate exact corpus + result bindings
  create content-addressed semantic index
  keep automatic selection unchanged
        ↓
Reader / Indexer
  create existing BM25 04-Index/<sha>.index.json as needed
  create immutable 05-Context/<sha>.context.json
        ↓ read-only boundary
Generator
  read exact Context Bundle
  call approved LLM provider
  produce 00-Untrusted/<sha>.proposal.json
  produce 00-Untrusted/<sha>.generation.json
        ↓
Validator
```

Generator deliberately has no direct path to canonical Knowledge or Reader's Index. Generation provenance is audit provenance written by an untrusted identity; it is not a validation or approval attestation.

Semantic Planner Phase F reuses the same authority boundary rather than adding a
new privilege. Reader verifies one exact Semantic Selection and materializes only
the selected bounded chunk bytes into a content-addressed
`05-Context/<sha>.objective-context.json`. Generator may read that Context and
write only objective candidate/generation artifacts under `00-Untrusted`.
Those artifacts bind the Selection and Semantic Index identities but do not give
Generator read access to either store.

`idea-discovery-v0` and `project-adoption-proposal-v0` remain Human-facing
candidate paths only and never enter Validator/Executor as Knowledge
create-note mutations.

The controlled production integration admits only `deep-knowledge-v1`.
Reader pins an exact Semantic Index/Selection and writes one Objective Context.
Generator creates an Objective Candidate/Generation, deterministic code derives
the ordinary Knowledge proposal, and Generation Record v2 binds that complete
provenance before the existing Validator chain begins.

`02-Orchestration/semantic-selections` is a Reader-only subdirectory even
though the orchestration DB directory is shared by pre-review workers. Generator,
Validator and Evaluator cannot directly inspect or rewrite Selection Records.

## Validator / Evaluator / Human sequence

```text
Validator
  read proposal + canonical Knowledge
  apply deterministic create_note + Knowledge Note policy
  write accepted/rejected 10-Validation
        ↓ accepted only
Validator
  write deterministic 12-Evaluation-Request
        ↓
Reader
  read request + 04-Index + canonical Knowledge
  write recall-biased 14-Evaluation-Context
        ↓
Evaluator
  read proposal/generation provenance
  read original 05-Context
  read accepted 10-Validation
  read 14-Evaluation-Context
  write advisory 15-Evaluation
        ↓
Human reviewer / Review Intake
  read Validation + Evaluation + published Review projection
  write exact-artifact decision in 20-Review
  write immutable case/evaluation/mutation projection binding in 20-Review
```

Evaluator assesses groundedness, Knowledge quality, epistemic-status
preservation, redundancy, and consistency using output contract
`knowledge-note-evaluator-output-v7` and current prompt
`knowledge-note-evaluator-v10` (SHA
`b50b9cdbb1a141da4d3cc61fbc2459a061fce1b8189467c5107864b8aff13ebc`).
Groundedness, Knowledge quality, and epistemic status each compare the proposal
with the exact Generation input. Redundancy and Consistency remain isolated
pairwise passes against canonical Knowledge candidates. Recommendation is
advisory deterministic machine output under `conservative-five-v0`. For
Consistency, deterministic code builds exact proposal/candidate excerpt tables
and the model selects excerpt IDs. Code resolves those IDs back to exact source
bytes before independent verifier calls. The verifier receives only the exact
quote pair and no proposer-generated rationale; only a verifier
`contradiction` becomes persisted evidence. Deterministic code also binds each
`candidate_path` outside the model. Current Evaluation Records are v3, while
historical v1/v2 records remain readable without schema upgrade.

Human-facing Obsidian views are projected through separate non-authoritative stages.
Reader, Generator, Validator, Evaluator, Reviewer, Executor and Sync may write only
their own `16-Human-Projection/<role>` request queue. Review Intake persists the
exact case / Review-projection / Evaluation / mutation mapping as an immutable
reviewer-owned `20-Review/<mutation>.projection-binding.json`; Executor and Sync
consume that binding rather than reading `02-Orchestration`. Sync may read the
projection queues but cannot forge producer requests; only Sync writes
`17-Human-Projection-Result` and performs conditional WebDAV CREATE below the
fixed `04-AI/**` stage allowlist.

After Approve, Executor, Sync, and Executor respectively derive
`60-Execution`, `70-Transport`, and `80-Completed` from authoritative
Execution, verified Transport, and Receipt artifacts. Sync has read-only access to
Receipts solely to verify terminal projection cleanup; Receipt write authority
remains Executor-only. Terminal cleanup waits for the exact `80-Completed`
projection result, revalidates the reviewer-owned binding, Approve Review and
Receipt, then deletes only `00-Input` through `70-Transport`; the Completed
projection is retained.

For an exact evaluation-bound Reject or Keep as Idea Review, Reviewer may
enqueue the same bounded pre-terminal projection cleanup intent; only Sync can
execute the derived fixed-path WebDAV DELETEs, using the dedicated
`04-AI`-scoped cleanup credential rather than the canonical writer credential.

`keep_as_idea` is never a canonical write request from Automation. ObsidianCore
first performs the Human client-side `05-Idea` save and then sets the Review
request. Automation records that attestation as terminal
`human_kept_as_idea`, does not grant Executor approval, and never gains
`05-Idea` read/write authority. These projection artifacts never substitute
for Validation, Human Review, Execution, Transport, or Receipt authority.

```text
Evaluation != Validation
Evaluation != Human approval
Evaluation recommendation != execution authority
```

Executor remains authorized by deterministic Validation plus exact Human approval, not by an Evaluator recommendation.

## Canonical write sequence

```text
shared per-mutation lock
        ↓
Executor
  validate local mirror + Human approval
  persist durable intent
  write 25-Execution/<sha>.transport-request.json
        ↓
Sync Transport
  read exact request + validation + approval + intent
  conditional WebDAV PUT (If-None-Match: *)
  remote GET byte verification
  write 27-Transport/<sha>.transport-result.json
        ↓
Executor
  verify exact transport-result binding
  write 30-Receipts/<sha>.receipt.json
```

Executor never writes the local Vault mirror as the canonical effect. The mirror remains a read replica and later observes the successful Nextcloud write through the normal pull path.

## Why `27-Transport` is separate authority

Transport results must not live in Executor-writable `25-Execution`. Otherwise Executor could forge a `created_verified` result and manufacture a success receipt without contacting Nextcloud.

Therefore:

- Executor writes `25-Execution` and reads `27-Transport`;
- Sync reads `25-Execution` and writes `27-Transport`;
- Reviewer reads both when resolving an ambiguous remote outcome;
- all three share only `24-Locks` for host-local mutual exclusion;
- only verified `created_verified` allows Executor to create a success receipt.

## Actor permissions

`r` means content/listing may be read, `w` means artifacts may be created, and `-` means no direct access is required.

| Resource | Sync | Reader | Generator | Validator | Evaluator | Human reviewer | Executor |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Vault `11-Knowledge` | rw | r | - | r | - | - | r |
| State `00-Untrusted` | - | - | rw | r | r | - | - |
| State `04-Index` | - | rw | - | - | - | - | - |
| State `05-Context` | - | rw | r | - | r | - | - |
| State `10-Validation` | r | - | - | rw | r | r | r |
| State `12-Evaluation-Request` | - | r | - | rw | - | - | - |
| State `14-Evaluation-Context` | - | rw | - | - | r | - | - |
| State `15-Evaluation` | - | - | - | - | rw | r | - |
| State `20-Review` | r | r | - | - | - | rw | r |
| State `24-Locks` | rw | - | - | - | - | rw | rw |
| State `25-Execution` | r | - | - | - | - | r | rw |
| State `27-Transport` | rw | - | - | - | - | r | r |
| State `30-Receipts` | - | r | - | - | - | r | rw |

Human reviewer does not receive canonical write permission through this mechanism. Human editing through normal Obsidian remains a separate existing authority path.

Additional pre-review operational permissions:

| Resource | Sync | Reader | Generator | Validator | Evaluator | Status | Human reviewer | Executor |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `02-Orchestration` DB | - | rw | rw | rw | rw | r | - | - |
| `02-Orchestration/recipes` | - | rw | r | r | r | - | - | - |
| `02-Orchestration/status` | - | - | - | - | - | rw | r | - |
| `24-Locks/read-view` | rw | rw | - | - | - | - | - | - |


## Evaluator-specific isolation

Evaluator cannot read canonical Knowledge or `04-Index`. This prevents the LLM evaluation stage from independently expanding its knowledge visibility beyond Reader-selected exact artifacts.

Evaluator cannot read `12-Evaluation-Request`; it consumes only the Reader-produced `14-Evaluation-Context`. This keeps candidate selection under Reader authority.

Evaluator cannot write Validation, Review, Locks, Execution, Transport, or Receipts. Therefore it cannot convert an advisory judgement into a canonical effect.

Reader cannot read Generator proposals or Validation. It receives only the deterministic bounded request needed for evaluation retrieval.

## Remote crash semantics

A remote conditional PUT and local transport-result persistence are not one atomic transaction.

- If PUT never succeeds, no result is written and retry remains possible.
- If PUT succeeds and verified `created_verified` is durable, receipt creation can be retried safely.
- If PUT succeeds but the process crashes before result persistence, the next conditional PUT returns 412. Sync observes remote bytes and records `target_exists_matching` or `target_exists_conflict`.
- `target_exists_matching` is not converted automatically into success because the actor that created the bytes can no longer be proven. Human recovery may explicitly adopt the observed effect without creating a success receipt, or abandon it.

Remote Human recovery is bound to exact durable intent and exact transport-result bytes.

## POSIX ACL requirement

Simple owner/group/mode bits cannot express this matrix cleanly. Production v0 requires a local filesystem with Linux POSIX ACL support and `setfacl`/`getfacl`.

State stage directories are root-owned; named-user ACL entries grant only the required stage capability. Default ACLs ensure newly created immutable artifacts inherit the same reader boundaries.

## Required negative guarantees

Production acceptance requires proving at OS level that:

- Generator cannot read canonical Knowledge or `04-Index`; it can read but not write `05-Context`; it writes only `00-Untrusted`.
- Reader can read canonical Knowledge, read/write `04-Index`, write `05-Context`, read `12-Evaluation-Request`, and write `14-Evaluation-Context`; for post-review scheduler reconciliation it additionally reads but cannot write authoritative Review/Receipts. It cannot read Generator proposals or Validation and cannot write the Vault.
- Validator can read canonical Knowledge and Untrusted, write Validation and Evaluation Request, but cannot read Index/Context or write canonical Knowledge/Evaluation/Review/later stages.
- Evaluator can read Untrusted, original Context, Validation, and Evaluation Context; it cannot read the Vault, Index, Evaluation Request, Human Review, or later execution stages; it writes only Evaluation.
- Human reviewer can read Validation/Evaluation plus the exact Evaluator review projection and its projection result, write Review and operational Locks, but cannot write canonical Knowledge, machine-produced Validation/Evaluation, Execution, Transport, Receipts, or orchestration metadata.
- Executor cannot write the Vault mirror, Index, Context, Untrusted, Validation, Evaluation Request/Context/Evaluation, Review, or Transport; it writes only Locks, Execution, and Receipts.
- Sync can write the local Vault mirror, Locks, and Transport results, but cannot forge Index, Context, Untrusted, Validation, Evaluation, Review, Execution, or Receipts.
- no identity other than Sync can read the Nextcloud writer credential; Review Intake's separate credential is read-only and readable only by Reviewer.
- Status cannot read Context, Untrusted, Validation, Evaluation, Review, Execution, Transport, Receipts, or provider/Nextcloud credentials.
- Reader/Generator/Validator/Evaluator may update orchestration metadata but Status/Reviewer/Sync/Executor cannot turn that metadata into semantic authority.

## Health marker

The transport health marker remains:

```text
98-System/.rclone-bisync/RCLONE_TEST
```

The historical `.rclone-bisync` namespace name is retained for compatibility, but canonical writes do not use bisync. Public Exporter excludes `98-System/.rclone-bisync/**`.

## Production deployment sequence

1. Create the dedicated unprivileged AI Writer LXC.
2. Create separate Linux identities for Sync, Reader, Generator, Validator, Evaluator, Status, Reviewer, and Executor.
3. Create separate `vault` and `state` roots.
4. Create all lifecycle stage directories, including `12-Evaluation-Request`, `14-Evaluation-Context`, and `15-Evaluation`.
5. Apply and verify the POSIX ACL matrix.
6. Initialize the Vault mirror with Nextcloud -> local pull only.
7. Install all tools at one exact reviewed ObsidianAutomation revision using the non-editable production updater described in `pre-review-production.md`.
8. Only after local Gates pass, install the Nextcloud writer credential readable solely by `obsidian-ai-sync`.
9. Run disposable Generator/Validator/Evaluator and remote conditional-create E2Es before enabling real automatic flow.
10. Keep the Phase 1 Snapshot LXC unchanged and read-only.

## Out of scope

- multi-host Executor coordination;
- automatic Human approval;
- semantic/vector retrieval and LLM reranking;
- automatic index scheduling;
- update/merge/delete/rename canonical mutations;
- cryptographic identity proof for Human review records.
