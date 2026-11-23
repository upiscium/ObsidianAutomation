# ChatGPT Export — Private External Source/Context v0

Parent Issue #62. Depends on Draft PR #393 (selected-branch normalizer) and
Draft PR #396 (private intake and durable inventory). Source-only, not deployed.

## Purpose

Add a new **versioned, private, untrusted external source** contract without
weakening the existing ContextBundle v1 invariants. The original v1
ContextBundle only accepts active 11-Knowledge and 10-Project Markdown sources;
ChatGPT raw history must **not** be disguised as either type or written to the
canonical Vault or pull-only mirror.

The pure module obsidian_automation.chatgpt_export_source provides:

- build_source_context_page(conversation, scope, source_hmac, cursor, bounds)
- validate_source_context_page(page, conversation, scope, source_hmac)

Inputs are SelectedConversation from the previous private normalizer and a
privacy-preserving source_hmac from the private Importer Journal. This module
has **no CLI, filesystem access, model invocation, Queue transport, credential
access or background worker**. It does not store text or approve generation.

## Authority and trust model

source_kind: chatgpt-export
contract_version: chatgpt-external-source-v0
trust_level: untrusted-evidence

Every page and extraction unit has generation_authorized=False. This flag
cannot be set via build_source_context_page; the validator rejects forged
authoritative flags or Vault source kinds. The future Generator must treat
every word in evidence text as untrusted **source content**, never as an
instruction to the system/Reader/LLM. The contract itself cannot force a
model to ignore prompt injection: prompt isolation and output validation must
be demonstrated separately before actual LLM use.

Role attribution is preserved:

- user message -> user-report-unverified
- assistant message -> assistant-claim-unverified

These distinctions avoid treating a proposed assistant solution as a user
decision or established fact. They do not detect hypotheses, corrections,
task status, verified results or consent, which belong to future extraction
and Validation/Evaluation. Existing hidden messages, internal reasoning,
nonconversation roles and unsupported rich payloads are already excluded
by the upstream normalizer. The page also retains omission counts, unverified
child-edge counts and off-branch counts; these are not silently erased.

## Bounded extraction

- Single source conversation, existing selected branch only; **one
  conversation can result in multiple units/pages**.
- Default max 4 units/page, 8192 UTF-8 bytes/unit and 32768 text bytes/page.
  Hard caps 8 units, 16 KiB per unit, 64 KiB per page, 16 slices per unit.
  UTF-8 codepoint boundaries are preserved; oversize messages are split into
  exact, lossless [start_codepoint,end_codepoint) slices.
- Slices preserve private node/message ID, message index, role, text SHA-256,
  exact span coordinates and SHA-256 of the bytes in that slice. Text content
  exists only in the in-memory private contract, not in public artifacts.
- Each unit has the corresponding source semantic digest and full source
  revision digest plus hashes of the neighboring candidate messages
  (not their content). A downstream private Reader can retrieve adjacent
  context when justified; this slice does not assert it was read.
- unit_id is deterministic over exact semantic source, scope, source HMAC,
  positions, roles and slice digests. **Metadata/title-only revision changes
  do not regenerate units**. Unit source_revision_sha256 changes separately.
- Sequential SourceCursor (message_index, text_offset) supports replay-free
  paging. The validator independently checks source content, every byte/hash,
  contiguous coverage without gaps/overlap, unit ID, order, neighbors,
  contract/source binding, max byte limits, cursor and completion status.

Page bounds limit raw text payload only, not final serialized context JSON,
model tokens or downstream LLM inference budgets. This v0 must not be treated
as a production model context without additional token/serialization bounds.

## Evidence semantics and non-goals

No arbitrary path is accepted and no Vault Markdown Frontmatter is fabricated.
There is no automatic conversion of a source statement into Knowledge or
authority to mutate notes. A source message is a **candidate evidence span**
and can be wrong, outdated, adversarial, speculative, or contradicted later.

Not implemented in this phase:

1. Durable private Source/Context materialization, exact Reader ownership,
   source scope selection and archival revision snapshots.
2. Message-level semantic extraction (corrections, claims, decisions,
   uncertainty, user confirmation), cross-page neighboring context retrieval,
   provenance-backed Knowledge candidate generation.
3. LLM Generator, Validator, Evaluation, semantic retrieval and
   awaiting_human_review job integration/backpressure.
4. Stable private Importer service identity and production-ready timer,
   backfill selection, user-visible audit and source consistency acceptance.

Raw ChatGPT ZIP on Adam is **not** read by these tests. All fixtures are
synthetic. PRs remain Draft until independent review and human approval.

## Verification

The synthetic focused tests cover exact UTF-8 segmentation and multi-page
reconstruction, role labeling, stable unit IDs across metadata-only changes,
source digest binding, invalid cursor/bounds, forged spans/digests, tampering,
untrusted-to-Vault promotion, changed provenance and no-generation invariants.

The next gate is a private, strictly bounded candidate extraction experiment
from the admitted source pages, followed by real Generator/Validator/Evaluator
contract integration. **No automatic backfill of the full Export**.
