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
├── obsidian-ai-generator
├── obsidian-ai-validator
├── obsidian-ai-evaluator
├── <human reviewer account>
└── obsidian-ai-executor

Snapshot LXC
└── existing Nextcloud read-only credential only
```

Production v0 requires exactly one AI Writer host. The shared production lock is host-local and is not a distributed lock.

## Credential boundary

Only `obsidian-ai-sync` may hold the Nextcloud writer credential.

The following identities have no Nextcloud writer credential:

- `obsidian-ai-reader`;
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
│   └── 11-Knowledge/
└── state/                   # local-only; never rclone-sync this tree
    ├── 00-Untrusted/
    ├── 04-Index/
    ├── 05-Context/
    ├── 10-Validation/
    ├── 12-Evaluation-Request/
    ├── 14-Evaluation-Context/
    ├── 15-Evaluation/
    ├── 20-Review/
    ├── 24-Locks/
    ├── 25-Execution/
    ├── 27-Transport/
    └── 30-Receipts/
```

This separation is required. A Nextcloud pull must never be able to delete local-only index/context/validation/evaluation/review/execution/transport/receipt artifacts.

## Derived retrieval state

`04-Index` is Reader-private, non-authoritative derived state. Reader/Indexer is the only identity that may read or write it.

`05-Context` is the non-authoritative Reader -> Generator boundary. Reader is the only writer. Generator and Evaluator may read exact Context Bundles but cannot rewrite them. Context never grants validation, evaluation, approval, execution, transport, or receipt authority.

`12-Evaluation-Request` is a bounded Validator -> Reader bridge. It exists so Reader does not need read access to Generator proposals or Validation. Validator deterministically projects the accepted proposal/mutation binding, target path, and retrieval query. Reader is read-only on this stage.

`14-Evaluation-Context` is a non-authoritative Reader -> Evaluator boundary. Reader uses canonical Knowledge plus Reader-private Index to produce exact candidate bytes for redundancy/consistency evaluation. Evaluator may read but not rewrite it.

## Reader / Generator sequence

```text
Reader / Indexer
  read canonical 11-Knowledge
  create immutable 04-Index/<sha>.index.json
  deterministic production retrieval
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
Human reviewer
  read Validation + Evaluation
  write exact-artifact decision in 20-Review
```

Evaluator v0 assesses groundedness, redundancy, and consistency. Its recommendation is advisory machine output.

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
| State `20-Review` | r | - | - | - | - | rw | r |
| State `24-Locks` | rw | - | - | - | - | rw | rw |
| State `25-Execution` | r | - | - | - | - | r | rw |
| State `27-Transport` | rw | - | - | - | - | r | r |
| State `30-Receipts` | - | - | - | - | - | r | rw |

Human reviewer does not receive canonical write permission through this mechanism. Human editing through normal Obsidian remains a separate existing authority path.

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
- Reader can read canonical Knowledge, read/write `04-Index`, write `05-Context`, read `12-Evaluation-Request`, and write `14-Evaluation-Context`; it cannot read Generator proposals or Validation and cannot write the Vault.
- Validator can read canonical Knowledge and Untrusted, write Validation and Evaluation Request, but cannot read Index/Context or write canonical Knowledge/Evaluation/Review/later stages.
- Evaluator can read Untrusted, original Context, Validation, and Evaluation Context; it cannot read the Vault, Index, Evaluation Request, Human Review, or later execution stages; it writes only Evaluation.
- Human reviewer can read Validation and Evaluation, write Review and operational Locks, but cannot write canonical Knowledge, machine-produced Validation/Evaluation, Execution, Transport, or Receipts.
- Executor cannot write the Vault mirror, Index, Context, Untrusted, Validation, Evaluation Request/Context/Evaluation, Review, or Transport; it writes only Locks, Execution, and Receipts.
- Sync can write the local Vault mirror, Locks, and Transport results, but cannot forge Index, Context, Untrusted, Validation, Evaluation, Review, Execution, or Receipts.
- no identity other than Sync can read the Nextcloud writer credential.

## Health marker

The transport health marker remains:

```text
98-System/.rclone-bisync/RCLONE_TEST
```

The historical `.rclone-bisync` namespace name is retained for compatibility, but canonical writes do not use bisync. Public Exporter excludes `98-System/.rclone-bisync/**`.

## Production deployment sequence

1. Create the dedicated unprivileged AI Writer LXC.
2. Create separate Linux identities for Sync, Reader, Generator, Validator, Evaluator, Reviewer, and Executor.
3. Create separate `vault` and `state` roots.
4. Create all lifecycle stage directories, including `12-Evaluation-Request`, `14-Evaluation-Context`, and `15-Evaluation`.
5. Apply and verify the POSIX ACL matrix.
6. Initialize the Vault mirror with Nextcloud -> local pull only.
7. Install all tools at one immutable ObsidianAutomation revision.
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
