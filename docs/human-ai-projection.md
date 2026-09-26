# Human-facing AI lifecycle projection v0

## Purpose

The authoritative AI lifecycle remains private under:

```text
/var/lib/obsidian-ai/state/**
```

Obsidian users should not need shell access to inspect that lifecycle. v0 projects
bounded, sanitized Markdown views into the private Live Vault:

```text
04-AI/
├── 00-Input/
├── 10-Context/
├── 20-Generation/
├── 30-Validation/
├── 40-Evaluation/
├── 50-Review/
├── 60-Execution/
├── 70-Transport/
├── 80-Completed/
└── 90-Failed/
```

These notes are **human-facing projections**, not lifecycle authority. A note in
`04-AI` cannot validate a mutation, approve it, authorize Executor, attest a
transport result, or manufacture a Receipt.

The System UI that renders them is owned by ObsidianCore under
`98-System/02-embed/hub/ai-hub.md`.

## Private request/result stages

Projection transport uses two local-only stages:

```text
16-Human-Projection/
├── reader/
├── generator/
├── validator/
├── evaluator/
├── reviewer/
├── executor/
└── sync/

17-Human-Projection-Result/
```

Each semantic role may write only its own request subdirectory. Sync may read all
request subdirectories but cannot write producer-owned queues. Only Sync writes
transport results.

This preserves the existing semantic authority separation. There is no
all-seeing Projector identity.

## Pre-review producer mapping

The initial automatic path is:

```text
Input Planner / Reader
  -> 00-Input
  -> 10-Context

Generator
  -> 20-Generation

Validator
  -> 30-Validation

Evaluator
  -> 40-Evaluation
  -> 50-Review
```

Evaluator already has the exact read authorities needed to compare Proposal,
Validation and Evaluation Context. Therefore it can build the Human Review
projection without widening Reviewer access to Untrusted Generator artifacts.

The authoritative Human Review stage remains `20-Review`. Creating
`04-AI/50-Review/<case>.md` does not create an approval.

## Projection request contract

A request is immutable and content-addressed. It binds:

- one `ai_case_id` (the orchestration generation ID);
- one fixed projection stage;
- exact source artifact kind and SHA-256;
- deterministic `04-AI/<stage>/<case>.md` target path;
- exact Markdown SHA-256 and Markdown bytes;
- source-artifact timestamp.

Same artifact input therefore recreates the same request bytes and request SHA.

The Markdown frontmatter contains bounded machine bindings such as
`proposal_sha256`, `mutation_sha256`, `evaluation_sha256`, target path and
recommendation when they are available. Candidate Markdown is rendered inside an
inert dynamically-sized code fence so untrusted generated Markdown is not
executed as an Obsidian embed, Dataview block, or Meta Bind control.

## Review projection

`04-AI/50-Review/<case>.md` contains:

- exact target path;
- accepted deterministic Validation evidence;
- Evaluator groundedness / redundancy / consistency / recommendation;
- bounded findings;
- the proposed Knowledge Note as inert code;
- a Meta Bind `review_request` control for `approve` / `reject`.

The control updates only the Human-facing note. It does not write
`20-Review` and therefore is not approval authority.

`obsidian-ai-review-intake.service` runs as `obsidian-ai-reviewer`. It uses
a dedicated read-only Nextcloud credential to fetch the exact
`04-AI/50-Review/<case>.md` note. Intake accepts the Human edit only when:

1. the original immutable review Projection Request exists;
2. its successful Projection Result proves the expected note was published;
3. the remote note differs from the original projection only in the single
   `review_request` frontmatter value;
4. the requested value is blank, `approve`, or `reject`;
5. the exact Evaluation, accepted Validation, and validated mutation still
   satisfy the existing Review v2 binding contract.

Only then does the existing `obsidian-knowledge-review` logic create
`20-Review/<mutation_sha>.approval.json`. Any other Human edit, stale binding,
or replay mismatch fails closed. CRLF/LF representation variance is normalized
for comparison; semantic content is never repaired.

Approve then flows through the existing separated authorities:

```text
Review Intake (Reviewer)
  -> 20-Review
Executor
  -> 25-Execution
Sync
  -> conditional Nextcloud write + 27-Transport
Executor
  -> 30-Receipts
Reader
  -> reconcile terminal state into orchestration metadata
```

Reject creates authoritative Review and queues a content-addressed projection cleanup intent. After post-review reconciliation reaches `human_rejected`, a Sync-only cleanup service deletes the fixed `04-AI/00-Input` through `04-AI/50-Review` files for that exact case. Reject never invokes canonical Knowledge transport, and private lifecycle/audit artifacts are retained.

## Sync transport

`obsidian-ai-human-projection-sync.service` runs as `obsidian-ai-sync` after
Evaluator.

Only Sync holds the Nextcloud writer credential. It:

1. acquires the existing global canonical I/O lock;
2. reads immutable role-produced projection requests;
3. creates only the allowlisted `04-AI` collection and fixed stage collections;
4. performs conditional WebDAV CREATE for the deterministic note target;
5. verifies exact remote bytes;
6. persists `17-Human-Projection-Result/<request-sha>.projection-result.json`.

An existing target with identical bytes is adopted as idempotent
`already_matching`. Different bytes produce a durable conflict and the service
continues to fail closed until the conflict is resolved. It never overwrites a
Human-edited projection.

The service is enabled only when both private deployment files exist:

```text
/etc/obsidian-ai/human-projection.env
/etc/obsidian-ai/webdav-password
```

The env file contains only the non-secret Nextcloud base URL / username binding.
The password file remains readable only by Sync.

A separate `obsidian-ai-human-projection-cleanup-sync.service` runs after post-review reconciliation. It accepts no arbitrary path from Reviewer: cleanup targets are derived only from the exact `ai_case_id` and the fixed stage allowlist. Before DELETE, Sync revalidates the original published Review projection and the exact evaluation-bound authoritative Reject Review. DELETE is idempotent; an already-absent projection is accepted as success.

Cleanup does not reuse the canonical AI writer credential. It uses a dedicated Nextcloud account shared only the existing `04-AI` folder with Read + Delete permission (permission bitmask `9`). The account root must expose that share as `04-AI`, because cleanup paths remain fixed below `04-AI/**`.

Private deployment files:

```text
/etc/obsidian-ai/projection-cleanup.env
/etc/obsidian-ai/projection-cleanup-password
```

The cleanup service explicitly hides the canonical `webdav-password` from its systemd sandbox.

## Projection root migration

The canonical Human-facing projection root is `04-AI`. Historical immutable
Projection Request / Result artifacts created before this migration may still
bind exact `03-AI/**` target paths. Runtime parsers retain read/cleanup
compatibility for that legacy root so existing audit records are not rewritten.

New Projection Requests are always emitted below `04-AI/**`. No third root is
accepted. During migration, a legacy case continues to use its exact stored
`03-AI/**` target for Review Intake and Reject cleanup; new cases use
`04-AI/**`.

The old `03-AI` Vault folder can be removed after no active historical case
depends on it. It is not lifecycle authority; private state remains under
`/var/lib/obsidian-ai/state/**`.

## Folder creation boundary

The WebDAV transport may create only:

```text
04-AI
04-AI/00-Input
04-AI/10-Context
04-AI/20-Generation
04-AI/30-Validation
04-AI/40-Evaluation
04-AI/50-Review
04-AI/60-Execution
04-AI/70-Transport
04-AI/80-Completed
04-AI/90-Failed
```

Stage names come from a closed code allowlist. Projection requests cannot choose
an arbitrary collection.

## Public projection boundary

`04-AI/**` remains outside the ObsidianCore public-export allowlist. Regression
tests enforce that private AI lifecycle projections cannot appear in the public
Core repository.

## Input recursion boundary

AI Input Planner never reads `04-AI/**`. Human-facing generated projections
therefore cannot recursively become Generation evidence.

## Current scope

Implemented by this increment:

- Input, Context, Generation, Validation, Evaluation and Human Review projection;
- role-scoped private request queues;
- Sync-only conditional WebDAV projection transport;
- fixed collection creation;
- exact-byte idempotency and conflict detection;
- fail-closed Review Intake into authoritative `20-Review`;
- separated Executor / Sync / Executor post-review canonical path;
- scheduler reconciliation for approve/reject/completed terminal states;
- Sync-only automatic deletion of rejected-case Human-facing projections.

Reserved for later increments:

- Executor / Transport / Completed Human-facing projection emission;
- explicit Failed projections for orchestration failures;
- retention/GC policy for private lifecycle artifacts and non-rejected historical projections.
