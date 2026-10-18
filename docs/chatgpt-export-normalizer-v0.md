# ChatGPT Export selected-branch normalization v0 (Issue #62)

Status: **synthetic contract only / not accepted on a real Export / not deployed**.
This branch is a preparatory slice of #62, independent of the private intake,
external Source/Context contract, Generation/Validation/Evaluation and Human
Review integration. It does not close #66.

## Trust boundary

Official Export acquisition, download and local placement remain manual.
Do not scrape undocumented ChatGPT APIs or store login cookies. Run locally
as an unprivileged identity without Vault/Nextcloud credentials. Raw archives,
normalized text and conversation/message IDs are **private**; never post them to
GitHub, normal logs, chat, or the canonical Vault. No on-disk import root,
journal, worker, systemd timer, LLM invocation, credential or transport is
created by this module.

The module obsidian_automation.chatgpt_export_normalize provides
normalize_export() as a **library function only**. It reads the locally supplied
file and returns private in-memory dataclasses; intentionally no CLI or stdout.
All dataclasses containing private values have disabled repr. Callers must
not serialize, log, or report their fields publicly.

The existing, strictly value-free inspector remains the first real-Export
acceptance step: python -B -m obsidian_automation.chatgpt_export_inspect
/path/to/export.zip. That tool emits aggregate shape, not content or IDs.

## Synthetic normalization contract

- Exact scope is required; this is an **identity namespace**, not a
  title/keyword-based Arca filter. Real account/project selection policy
  belongs to a later private caller.
- Reuses #66 ZIP preflight/selection/limit checks and duplicate-key-aware
  JSON decoder. No ZIP extraction, no recursive filesystem traversal.
- Reads only selected JSON members and demands a top-level conversation list,
  unique nonempty conversation IDs, mapping and current_node. Rejects
  cycles/missing parent, inconsistent selected parent/child edge and node IDs.
- Reconstructs **only** current_node -> parent then reverses the chain.
  Alternate branches are neither merged by timestamps nor imported; off-branch
  graph structure is not claimed to be fully verified.
- Keeps messages on the chosen chain only when role=user or assistant with
  recipient unset/all and assistant channel unset/final; visibly hidden
  messages, internal/system/developer/tool messages, unresolved rich content,
  oversize content, and placeholders are omitted **with fixed reason counts**.
  Unknown author roles are errors. text/multimodal_text content is accepted
  only when every part is a string. No partial multimodal reconstruction.
  The visibility rules require further checks against a real Export.
- Preserves selected node ID, parent, message ID, role, timestamp, content
  type and exact parts hash. Text is kept in memory for future *private*
  bounded extraction units, not emitted to logs.
- Maximum 128 KiB per retained text message, 24 MiB per invocation for
  retained text, along with existing inspector ZIP/JSON/node limits. An
  oversized text message is omitted and counted rather than truncated.
- Import-run digest binds **raw archive SHA**, selection, parser version and
  source scope. Separate per-conversation content_sha256 considers the
  selected path's IDs/role/content and omission decisions, ignoring off-branch
  content and titles. source_revision_sha256 additionally includes selected
  raw message JSON for provenance. Metadata-only changes to selected messages
  may change source revision without changing content, and should not force
  LLM regeneration.
- inventory_delta(previous, current) reports added, changed, unchanged,
  and absent_not_deleted counts. It **never interprets absence in a later
  Export as canonical note deletion**. The same Export is idempotent.
- SelectedBatch.real_schema_accepted is deliberately always false. It is
  not a production eligibility token.

The model's inputs remain untrusted data. Chat text must never be interpreted
as operational commands, instructions to the Generator, or authorization.

## What is NOT implemented

No durable import journal, atomic ready-file intake, source revision store,
backfill scheduling, extraction unit provenance, external Source/Context
contract, AI Job submission, Human Review UI, canonical writer, nor automatic
Export download. Do not claim the feature is end-to-end complete.

**Do not enable any automation** until a private real-Export structural report
is reviewed, unexpected role/content/metadata shapes are handled, branch
reconstruction is verified on sanitized examples, and an explicit production
acceptance gate is approved. CI success with synthetic fixtures is insufficient.

## Next gated work

1. Run the existing read-only #66 inspector locally against a finished *real*
   ChatGPT Export ZIP. Share only the vetted aggregate report, not the archive.
2. Compare observed shape with this experimental parser and add sanitized
   synthetic fixtures for actual role/content/branch variants; revise strict
   handling where justified and rerun CI.
3. In a separate slice, implement the private non-privileged intake root,
   atomic ready publication, durable import journal, immutable selected
   revisions, and restart-safe no-op/changed handling.
4. Add a versioned external Source/Context reader and bounded extraction units;
   test Generator/Validator/Evaluator linkage and Review-only acceptance.
5. Complete real private end-to-end dry-run inventory and bounded backfill,
   with Human Review backpressure and explicit operational rollout approval.

Keep each capability's security and merge/deployment gates separate.
