# Knowledge note policy v0

## Purpose

This policy is the deterministic admission contract for AI-created notes under `11-Knowledge`.

It intentionally grants less authority than the full interactive Obsidian UI. Existing users may later move a note to `outdated`, `archived`, `deleted`, or a higher maturity through normal human-controlled Vault editing, but the AI create path cannot create those states directly.

## ObsidianCore compatibility target

The current ObsidianCore Knowledge template uses:

```yaml
---
type: knowledge-note
status: active
category:
maturity: draft
source_type: self
---
```

ObsidianCore currently recognizes these category values:

- blank
- `explanation`
- `manual`
- `troubleshooting`
- `spec`
- `reference`
- `summary`

The AI creation policy accepts these source types:

- `self`
- `official`
- `paper`
- `book`
- `web`
- `other`

## Fixed creation state

AI-created Knowledge notes must use:

```text
type      = knowledge-note
status    = active
maturity  = draft
```

`category` may be blank or one of the allowed categories. `source_type` must be one of the allowed source types and may not be blank.

The explicit string `none` is not accepted. ObsidianCore represents an unset optional category with an empty value, not the literal `none` value.

## Frontmatter grammar

Policy v0 deliberately accepts only a small YAML subset:

- frontmatter must start at byte/character zero;
- exactly five keys are allowed: `type`, `status`, `category`, `maturity`, `source_type`;
- each field must be a top-level plain scalar;
- duplicate keys are rejected;
- quoted values, lists, maps, anchors, tags, multiline scalars, and inline YAML comments are rejected;
- LF line endings are required;
- the Markdown body after frontmatter must be non-empty.

This is not intended to be a general YAML parser. A narrow grammar keeps the canonical artifact deterministic and avoids giving an untrusted generator access to YAML features the current Knowledge contract does not need.

## Path policy

The Knowledge-specific Validator and production CLIs hard-code the canonical root to:

```text
11-Knowledge
```

Additional constraints:

- target must be a `.md` create-note mutation below that root;
- hidden path components are rejected;
- components with leading/trailing whitespace are rejected;
- Windows-unsafe filename characters are rejected;
- content is limited to 256 KiB of UTF-8 bytes.

The generic create-note APIs remain available for reusable tests and other future mutation classes, but production Knowledge services should use the policy-bound CLIs below.

## Validator CLI

```text
obsidian-knowledge-validator
```

Inputs:

```text
--ai-root
--vault-root
--proposal-sha256
```

The Validator reads the immutable proposal from `00-Untrusted`, validates it with `knowledge-note-v0`, and writes either:

- accepted canonical mutation + validation record; or
- rejected validation record with a deterministic rejection reason.

Policy rejection is a normal workflow outcome and returns a JSON result. Infrastructure/artifact errors return process exit code 2.

An existing immutable validation result is reused idempotently. Accepted reuse also verifies that the referenced canonical mutation artifact still exists and matches its SHA-256.

## Policy-bound production CLIs

Production should use:

```text
obsidian-production-knowledge-executor
obsidian-production-knowledge-webdav-worker
```

instead of relying on callers to supply an `--allowed-root` or policy name correctly.

The Knowledge Executor fixes `allowed_roots` to `11-Knowledge` and applies the policy while preparing the durable execution intent/request.

The credential-holding Knowledge WebDAV Worker reloads the exact validated mutation and reapplies the same deterministic policy immediately before it is allowed to read the writer credential and perform the conditional remote create.

This gives three policy checkpoints:

```text
Validator
   ↓ policy v0
canonical mutation + Human approval
   ↓
Knowledge Executor
   ↓ policy v0
transport request
   ↓
Knowledge WebDAV Worker
   ↓ policy v0 immediately before credential use
conditional Nextcloud create
```

## Out of scope for v0

Policy v0 does not determine whether the note is factually correct, useful, novel, or well-written. Those are Generator/Evaluator/Human-review concerns.

It also does not require the Meta Bind embed or a particular Markdown heading structure. Those are presentation conventions rather than canonical safety properties and can be added later if they become a stable repository contract.
