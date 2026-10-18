# ChatGPT Export selected-branch normalization v0 (Issue #62)

Status: **private real-Export read-only dry-run completed; not production accepted,
not deployed**. Synthetic tests and private aggregate reports validate the
observed input shape only, not Knowledge quality or downstream safety. This
branch remains a preparatory slice of #62, independent of the private intake,
Source/Context contract, Generation/Validation/Evaluation and Human Review.
It does not close #66.

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
  cycles/missing parent, mismatched IDs and inconsistent selected parent/child
  links **when children exists**. Real 2026-10 data has no node.children:
  its parent chain can be reconstructed, but reciprocal edge verification is
  explicitly flagged incomplete with unverified_child_links counts.
- An empty object {} in the conversations array is explicitly quarantined and
  counted (not assigned a fabricated conversation ID). Other non-empty
  malformed conversations fail closed. Quarantine is a production acceptance
  blocker, not automatic data loss.
- Reconstructs **only** current_node -> parent then reverses the chain.
  Alternate branches are neither merged by timestamps nor imported; off-branch
  graph structure is not claimed to be fully verified.
- Keeps messages on the chosen chain only when role=user or assistant with
  recipient unset/all and assistant channel unset/final; visibly hidden
  messages, internal/system/developer/tool messages, unresolved rich content,
  oversize content, and placeholders are omitted **with fixed reason counts**.
  Actual Export content types thoughts and reasoning_recap are explicitly
  excluded as internal assistant reasoning. Unknown author roles are errors.
  text/multimodal_text content is accepted only when every part is a string.
  Mixed image/text records are deferred, never partially reconstructed.
  Because real text records lack explicit channel/visibility confirmation,
  selected-branch assistant text is a **candidate**, not independently proven
  to be final/visible. This must be accepted before job submission.
- Preserves selected node ID, parent, message ID, role, timestamp, content
  type and SHA-256 of rendered text. Text is kept in memory for future *private*
  bounded extraction units, not emitted to logs.
- Maximum 128 KiB per retained text message, 24 MiB per invocation for
  retained text, along with existing inspector ZIP/JSON/node limits. An
  oversized text message is omitted and counted rather than truncated.
- Import-run digest binds **raw archive SHA**, selection, parser version and
  source scope. Separate per-conversation content_sha256 considers only the
  selected path's **retained role/content-type/exact visible text sequence**:
  non-visible reasoning, opaque node/message IDs, timestamps, omissions and
  off-branch content do not cause unnecessary LLM regeneration. The separate
  source_revision_sha256 binds selected raw message JSON and node lineage for
  provenance; metadata-only changes may affect it without changing content.
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

**Do not enable any automation** based on this dry-run. The private real-Export
structural report is available locally, but one quarantined slot, missing
child-edge reciprocity, unresolved multimodal records, visibility assumptions
and full downstream acceptance still require explicit disposition. CI success
and a parser that completes without exceptions are insufficient.

## Next gated work

1. Completed: run the read-only #66 inspector on a private real Export,
   aggregate shape only; no archive upload. A quarantine/review flag remains.
2. Completed in the preparatory branch: adapt to observed parent-only graph,
   known internal reasoning types, empty placeholder and semantic content hash;
   test sanitized synthetic variants. CI and independent review remain gates.
3. In a separate slice, implement the private non-privileged intake root,
   atomic ready publication, durable import journal, immutable selected
   revisions, and restart-safe no-op/changed handling.
4. Add a versioned external Source/Context reader and bounded extraction units;
   test Generator/Validator/Evaluator linkage and Review-only acceptance.
5. Complete real private end-to-end dry-run inventory and bounded backfill,
   with Human Review backpressure and explicit operational rollout approval.

Keep each capability's security and merge/deployment gates separate.
