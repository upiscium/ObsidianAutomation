# Production canonical-writer authority topology v0

## Purpose

Phase 2 introduces canonical Vault writes. The Snapshot LXC from Phase 1 remains read-only and must not be upgraded into a canonical writer.

Production v0 therefore uses one dedicated AI Writer host/LXC. The host is the only machine allowed to run the canonical executor. Generator, Validator, Human review, Executor, Reader/Indexer, and sync transport are separated by Linux identities and filesystem ACLs.

This document defines the permission boundary. It does not define the Nextcloud synchronization implementation itself.

## Host boundary

```text
Nextcloud Live Vault
        ^
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

Production v0 requires exactly one canonical Executor host. The host-local `flock` in the execution orchestrator is not a distributed lock.

## Credential boundary

Only `obsidian-ai-sync` may hold the Nextcloud writer credential.

`obsidian-ai-sync` is a transport authority. It necessarily has broad access to the synchronized Vault, but it must not run an LLM, accept Generator/Evaluator prompts, or expose a semantic mutation interface.

The following identities must have no Nextcloud writer credential:

- `obsidian-ai-reader`;
- `obsidian-ai-generator`;
- `obsidian-ai-validator`;
- the Human review account/tool;
- `obsidian-ai-executor`;
- the Phase 1 Snapshot LXC account.

The Executor gets canonical write authority from the local filesystem permission boundary, not from a remote storage credential.

## Canonical entrypoint

Production must expose only the recovery-aware executor entrypoint:

```text
run_recovery_aware_create_note
```

The lower-level `run_approved_create_note` remains an implementation primitive and is not the production service entrypoint.

## Filesystem layout

The synchronized local Vault contains:

```text
<Vault>/
├── 11-Knowledge/
└── 20-AI/
    ├── 00-Untrusted/
    ├── 10-Validation/
    ├── 20-Review/
    ├── 25-Execution/
    └── 30-Receipts/
```

The reusable scripts in `examples/ai/` operate only on a disposable Vault carrying the marker file:

```text
.obsidian-ai-disposable-fixture
```

They intentionally refuse to run without that marker.

## Actor permissions

`r` means file content/listing may be read, `w` means artifacts may be created in that directory, and `-` means no direct access is required.

| Resource | Sync | Reader | Generator | Validator | Human reviewer | Executor |
| --- | --- | --- | --- | --- | --- | --- |
| `11-Knowledge` | rw | r | - | r | - | rw |
| `20-AI/00-Untrusted` | rw | - | rw | r | - | - |
| `20-AI/10-Validation` | rw | - | - | rw | r | r |
| `20-AI/20-Review` | rw | - | - | - | rw | r |
| `20-AI/25-Execution` | rw | - | - | - | - | rw |
| `20-AI/30-Receipts` | rw | - | - | - | r | rw |

The Generator deliberately does not receive direct canonical Vault read access. Retrieval/context should be supplied through the Reader/Indexer boundary rather than by giving the LLM filesystem access to the full Vault.

The Human reviewer does not receive canonical write permission through this mechanism. Human editing through normal Obsidian remains a separate existing authority path.

## Why POSIX ACLs are used

Simple owner/group/mode bits cannot express the required matrix cleanly. For example, `11-Knowledge` must be writable by Sync and Executor while remaining read-only to Validator/Reader. POSIX ACLs allow those identities to be separated without making Validator a member of a canonical-writer group.

The v0 deployment therefore requires a local filesystem with normal Linux POSIX ACL support and the `setfacl`/`getfacl` utilities.

No NFS/CIFS permission emulation is assumed by this contract.

## ACL inheritance

The fixture ACL script installs both access ACLs and default ACLs. New artifacts should inherit the same stage-specific authority boundary.

The sync transport remains the directory owner. Additional actor permissions are granted as named-user ACL entries.

This is an explicit trust decision: compromise of `obsidian-ai-sync` compromises the synchronized Vault. The mitigation is service isolation and absence of any LLM-facing interface, not pretending the transport credential is low privilege.

## Required negative guarantees

Production acceptance requires proving at OS level that:

- Generator cannot create or modify anything under `11-Knowledge`, Validation, Review, Execution, or Receipts.
- Validator cannot create or modify canonical notes, Review, Execution, or Receipts.
- Human review identity cannot write canonical notes, Validation, Execution, or Receipts.
- Reader cannot write canonical notes or any `20-AI` stage.
- Executor cannot write Untrusted, Validation, or Review.
- Executor can write only the approved canonical root plus Execution/Receipts.
- Sync transport can synchronize all required directories but is the only identity with the Nextcloud writer credential.

The disposable authority gate performs positive and negative write probes using `runuser` rather than inferring capability only from configuration.

## Production deployment sequence

1. Create a dedicated unprivileged AI Writer host/LXC.
2. Create separate Linux identities for Sync, Reader, Generator, Validator, and Executor.
3. Select the real Human reviewer account/tool identity.
4. Create a disposable Vault fixture on the same filesystem type intended for production.
5. Apply the ACL fixture policy.
6. Run the authority-separation Gate and require all probes to pass.
7. Only after the Gate passes, design and install the Nextcloud writer/sync transport.
8. Keep the Phase 1 Snapshot LXC unchanged and read-only.
9. Deploy the deterministic pipeline before connecting Generator/Evaluator LLMs.

## Out of scope

- multi-host Executor coordination;
- Nextcloud synchronization transport choice;
- production credentials;
- automatic Human approval;
- Generator/Evaluator LLM integration;
- update/merge/delete/rename canonical mutations;
- cryptographic identity proof for Human review records.
