# Production canonical-writer authority topology v0

## Purpose

Phase 2 introduces canonical Vault writes. The Snapshot LXC from Phase 1 remains read-only and must not be upgraded into a canonical writer.

Production v0 uses one dedicated AI Writer host/LXC. Generator, Validator, Human review, Executor, Reader/Indexer, and Sync Transport are separated by Linux identities and filesystem ACLs. Canonical remote creation is performed only by the Sync Transport with a conditional WebDAV request.

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
- the Human review account/tool;
- `obsidian-ai-executor`;
- the Phase 1 Snapshot LXC account.

`obsidian-ai-sync` does not accept LLM prompts or semantic mutation instructions. It consumes an exact durable transport request prepared by the Executor, independently rechecks the validated mutation / Human approval / execution intent binding, performs `PUT` with `If-None-Match: *`, verifies the remote bytes, and writes only the transport-result stage.

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
    ├── 20-Review/
    ├── 24-Locks/
    ├── 25-Execution/
    ├── 27-Transport/
    └── 30-Receipts/
```

This separation is required. If AI state were kept inside a directory managed by `rclone sync` from Nextcloud, local-only index/context/intent/review/receipt artifacts could be deleted by a pull when they are absent remotely.

`04-Index` is Reader-private, non-authoritative derived state. Reader/Indexer is the only identity that may read or write it. The index never grants Generator direct Vault visibility.

`05-Context` is a non-authoritative Reader -> Generator boundary. Reader/Indexer is the only writer and reads exact bytes from the canonical Knowledge mirror. Generator may read immutable Context Bundles but cannot write or replace them. Context does not grant validation, approval, execution, transport, or receipt authority; Generator output remains untrusted regardless of Context contents.

`24-Locks` is operational state only. It carries no approval, mutation, transport-attestation, or receipt authority. Executor, Sync Transport, and the Human recovery tool share write access to this directory solely so the three processes can serialize one mutation on the single production host without granting write access to each other's semantic stages.

The reusable Python APIs take `vault_root` and `ai_root` separately.

## Reader / Generator sequence

```text
Reader / Indexer
  read canonical 11-Knowledge
  create immutable 04-Index/<sha>.index.json
  select exact index SHA
  deterministic lexical retrieval
  create immutable 05-Context/<sha>.context.json
        ↓ read-only boundary
Generator
  read exact Context Bundle
  produce 00-Untrusted/<sha>.proposal.json
        ↓
Validator
```

Generator deliberately has no direct path to canonical Knowledge or Reader's index. A Context Bundle contains the exact selected Markdown bytes, each source path, and a SHA-256 of each source. It is retrieval evidence for generation, not a trusted mutation artifact.

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

The Executor never writes the local Vault mirror as the canonical effect. The mirror remains a read replica and later observes the successful Nextcloud write through the normal pull path.

## Why `27-Transport` is a separate authority stage

Transport results must not live in the Executor-writable `25-Execution` directory. Otherwise the Executor identity could forge a `created_verified` result and manufacture a success receipt without contacting Nextcloud.

Therefore:

- Executor writes `25-Execution` and reads `27-Transport`;
- Sync reads `25-Execution` and writes `27-Transport`;
- Reviewer reads both when resolving an ambiguous remote outcome;
- all three share only `24-Locks` for host-local mutual exclusion;
- only a verified `created_verified` result allows the Executor to create a success receipt.

## Actor permissions

`r` means file content/listing may be read, `w` means artifacts may be created in that directory, and `-` means no direct access is required.

| Resource | Sync | Reader | Generator | Validator | Human reviewer | Executor |
| --- | --- | --- | --- | --- | --- | --- |
| Vault `11-Knowledge` | rw | r | - | r | - | r |
| State `00-Untrusted` | - | - | rw | r | - | - |
| State `04-Index` | - | rw | - | - | - | - |
| State `05-Context` | - | rw | r | - | - | - |
| State `10-Validation` | r | - | - | rw | r | r |
| State `20-Review` | r | - | - | - | rw | r |
| State `24-Locks` | rw | - | - | - | rw | rw |
| State `25-Execution` | r | - | - | - | r | rw |
| State `27-Transport` | rw | - | - | - | r | r |
| State `30-Receipts` | - | - | - | - | r | rw |

The Human reviewer does not receive canonical write permission through this mechanism. Human editing through normal Obsidian remains a separate existing authority path.

## Remote crash semantics

A remote conditional PUT and local transport-result persistence are not one atomic transaction.

- If PUT never succeeds, no result is written and retry remains possible.
- If PUT succeeds and the verified `created_verified` result is durable, receipt creation can be retried safely.
- If PUT succeeds but the process crashes before the result is durable, the next conditional PUT returns 412. The Sync Transport observes the remote bytes and records `target_exists_matching` or `target_exists_conflict`.
- `target_exists_matching` is not converted automatically into success because the actor that created the bytes can no longer be proven. Human recovery may explicitly adopt the observed effect without creating a success receipt, or abandon it.

Remote Human recovery is bound to the exact durable intent and the exact transport-result bytes.

## POSIX ACL requirement

Simple owner/group/mode bits cannot express the required matrix cleanly. Production v0 requires a local filesystem with Linux POSIX ACL support and `setfacl`/`getfacl`.

State stage directories should be root-owned; named-user ACL entries grant only the required stage capability. This prevents the Sync Transport from modifying Validation/Review/Execution, prevents the Executor from writing Transport results, prevents Reviewer from modifying machine-attested artifacts, and prevents Generator from reading the Vault or Reader-private Index or rewriting Reader-produced Context.

## Required negative guarantees

Production acceptance requires proving at OS level that:

- Generator cannot read or write canonical Knowledge or `04-Index`; it can read but not write `05-Context`; it cannot write later lifecycle stages.
- Reader can read canonical Knowledge, read/write `04-Index`, and write `05-Context`, but cannot read/write Generator proposals or later lifecycle stages and cannot write the Vault.
- Validator can read canonical Knowledge and Untrusted but cannot read Index/Context or write canonical Knowledge or later stages.
- Human reviewer can write Review and operational Locks but cannot read Index/Context or write canonical Knowledge, Validation, Execution, Transport, or Receipts.
- Executor cannot write the Vault mirror, Index, Context, Untrusted, Validation, Review, or Transport.
- Executor can write only Locks, Execution, and Receipts.
- Sync can write the local Vault mirror, Locks, and Transport results, but cannot forge Index, Context, Untrusted, Validation, Review, Execution, or Receipts.
- no identity other than Sync can read the Nextcloud writer credential.

## Health marker

The transport health marker remains:

```text
98-System/.rclone-bisync/RCLONE_TEST
```

The historical `.rclone-bisync` namespace name is retained for compatibility, but canonical writes do not use bisync. The Public Exporter excludes `98-System/.rclone-bisync/**`.

## Production deployment sequence

1. Create the dedicated unprivileged AI Writer LXC.
2. Create separate Linux identities for Sync, Reader, Generator, Validator, Reviewer, and Executor.
3. Create separate `vault` and `state` roots.
4. Apply and verify the revised POSIX ACL matrix, including `04-Index`, `05-Context`, `24-Locks`, and `27-Transport`.
5. Initialize the Vault mirror with Nextcloud -> local pull only.
6. Install Reader index/retrieval/context tools, validator, executor, and transport worker at one immutable ObsidianAutomation revision.
7. Only after all local Gates pass, install the Nextcloud writer credential readable solely by `obsidian-ai-sync`.
8. Run a disposable remote conditional-create E2E before enabling any real `create_note` proposal.
9. Keep the Phase 1 Snapshot LXC unchanged and read-only.
10. Establish the deterministic lexical retrieval baseline before connecting Generator/Evaluator LLMs or semantic retrieval.

## Out of scope

- multi-host Executor coordination;
- automatic Human approval;
- Generator/Evaluator LLM integration;
- embedding/vector retrieval and LLM reranking;
- automatic index scheduling;
- update/merge/delete/rename canonical mutations;
- cryptographic identity proof for Human review records.
