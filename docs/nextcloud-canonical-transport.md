# Nextcloud canonical create transport v0

## Purpose

The Live Vault on Nextcloud remains the canonical editing authority. The AI Writer host keeps a local read mirror for validation and execution planning, but a local filesystem write is not sufficient evidence that a canonical mutation reached the Live Vault.

For `create_note`, production transport must preserve the same create-only invariant at the remote effect boundary:

> create the exact target only if it does not already exist; never overwrite an existing remote object.

A general bidirectional synchronizer is intentionally not used as the canonical write primitive. It can propagate updates/deletes outside the narrow mutation contract and cannot express the exact `create-if-absent` precondition at the point of the remote PUT.

## Topology

```text
Nextcloud Live Vault
        |
        | pull-only mirror refresh
        v
AI Writer local mirror
        |
        | validated + Human-approved create_note
        v
conditional WebDAV PUT
If-None-Match: *
        |
        v
Nextcloud Live Vault
```

The existing Phase 1 Snapshot LXC remains read-only and is unchanged.

## Credential boundary

Only the `obsidian-ai-sync` production identity holds the Nextcloud writer credential.

The following identities must not receive that credential:

- Reader / Indexer
- Generator LLM
- Validator
- Human review tooling
- Executor

The reusable `obsidian-webdav-create` helper is intended to run under the Sync Transport identity. Later orchestration must hand it only an exact approved target path and exact bytes; it must not expose the credential to the Executor process.

## Remote effect primitive

`obsidian-webdav-create` sends:

```http
PUT <target>
If-None-Match: *
Authorization: Basic ...
```

The password is read from a file rather than passed on the command line.

Expected semantics:

- absent target + successful create -> HTTP 2xx, followed by GET verification;
- existing target -> HTTP 412, classified as conflict;
- any other HTTP status -> failure;
- successful PUT whose subsequent GET bytes differ -> failure.

The helper never performs DELETE, MOVE, PATCH, or an unconditional overwrite fallback.

## URL and path restrictions

Production requires HTTPS. The base URL must contain no embedded credentials, query, or fragment. The target is a Vault-relative POSIX path and rejects absolute paths, empty components, `.`, `..`, backslashes, and NUL.

Each target path component is URL-encoded by the helper.

## Mirror direction

The local production mirror is refreshed only from Nextcloud to local using a pull-only operation. Canonical AI writes do not use `rclone bisync`.

This distinction is important:

```text
remote -> local   mirror refresh
local -> remote   conditional create transport only
```

The pull and create phases must eventually be serialized by the production orchestration so that a mirror refresh cannot remove an in-flight local effect before its remote transport completes.

## Health marker

The operational health marker is fixed at:

```text
98-System/.rclone-bisync/RCLONE_TEST
```

The historical directory name is retained as an operational namespace even though canonical writes no longer use bisync. The marker may be used to verify that the expected Vault is mounted/reachable on both sides.

Public Exporter policy excludes:

```text
98-System/.rclone-bisync/**
```

so the marker is never part of the public `ObsidianCore` projection.

## Crash semantics

This transport primitive alone does not solve the full cross-system transaction.

Important crash window:

```text
remote conditional PUT succeeds
        ↓
process crashes
        ↓
canonical receipt not yet persisted
```

A retry will observe HTTP 412 because the target now exists, but filesystem/WebDAV state alone cannot prove whether this process or another authority created identical bytes. Therefore the existing Human recovery principle still applies: do not synthesize success provenance from matching state alone.

The next integration Gate must connect this transport to durable execution intent and Human recovery before production automatic writes are enabled.

## Out of scope

- update / merge / delete / rename;
- general bidirectional Vault synchronization;
- exposing Nextcloud credentials to Executor or LLM processes;
- automatic resolution of ambiguous post-PUT crashes;
- production timer/service wiring.
