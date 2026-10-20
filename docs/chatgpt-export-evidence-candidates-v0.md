# ChatGPT Export — Evidence-bound, unverified Knowledge candidates v0

Issue #62. **Dependent on Draft PRs #393 → #396 → #398.** This is
a source-only proposal decoding and source-binding contract. It does
not provide an LLM generator, production Job, Human Review submission,
canonical Vault writer, daemon, or new credential authority.

## Why a separate external-source contract is required

Existing production pre-review jobs consume versioned Vault ContextBundles
or semantic-objective Context. ChatGPT history must not be represented as
an active 11-Knowledge Markdown note, placed into the Vault, or stuffed
into a ContextBundle query.

User text is a **user report**: not necessarily a confirmed decision,
independently verified result, or approved instruction. Assistant text
is an **assistant claim**, not proof of user agreement or implementation.
Internal reasoning and unsupported multimodal/alternate branches remain
excluded, with explicit omissions tracked by the private SourceContext.
Later messages may correct or contradict earlier ones; a short quote alone
cannot establish the final state.

A candidate is therefore a *suggestion for investigation*, even with
an exact source quote and a passing deterministic hash check.

## Contract

The pure module chatgpt_export_candidates accepts:
1. A validated, in-memory private SourceContextPage and its exact
   SelectedConversation, scope and privacy-preserving HMAC source identity.
2. Zero or more untrusted EvidenceProposal objects (or strict proposal
   JSON decoded with parse_candidate_proposals).

Each proposal selects one exact Source slice, supplies the actual nonempty
quote as a string and codepoint start/end offsets (both checked against the
private original text), a bounded unverified interpretation, and one proposed
semantic kind. A matching offset without the exact quote is rejected:

- unclassified
- question_candidate
- hypothesis_candidate
- correction_candidate
- decision_candidate
- result_candidate
- feature_change_candidate

These labels are **only hypotheses**, never verified facts, accepted
decisions, completed results or binding project requirements. The
candidate review state is always needs_semantic_and_human_review.
Authority is always none. Generation, automatic approval and canonical
mutation remain prohibited regardless of kind.

The deterministic validator independently re-reads the exact private
Source page and conversation, validates all message and quote
coordinates, speaker attribution, source hashes, selected source
revision, unit ID, excerpt SHA-256 and bound contract fields. It
regenerates candidate identities and rejects tampering or a forged
authorized/completed status.

## Provenance and deduplication

The private candidate includes exact scope, Source semantic/revision
digests, unit ID, message/node ID, role and attribution, source text hash,
absolute codepoint offsets, excerpt text/hash and unverified proposed
interpretation.

- evidence_anchor_sha256 is stable for the same semantic source excerpt
  even if a model changes its summary or suggested classification.
- candidate_id includes the anchor, classification and interpretation
  hash; different proposals remain distinct, rather than overwriting
  each other.
- Metadata/title-only revisions can preserve evidence anchor while
  changing the recorded Source revision; stale revision binding is
  rejected when validating against the new exact source.

The strict JSON decoder permits only the complete allowlisted envelope,
no duplicate keys or unknown fields, exact enum and integer coordinates,
a 64 KiB raw byte limit and bounded UTF-8 excerpt/summary text.
Errors are fixed codes without input values, paths or raw content.

These caps cover raw input fields; they do not establish model-token
or complete pipeline budgets.

## Trust and authority boundaries

Potential future pipeline:
1. A private Reader reads a scope-filtered, immutable Source revision
   from the Import Journal, not arbitrary Vault paths.
2. A low-trust model can propose exact-quote candidates, NOT Knowledge
   Notes or approved project instructions.
3. This validator binds all proposed excerpts to their real Source.
   Semantic adjudication must separately consider uncertainty,
   corrections, chronology, user confirmation and conflicting evidence.
4. A separately reviewed external Source adapter may then submit
   bounded, semantically checked candidates to the existing Generator,
   Validator, fresh retrieval, Evaluator and Human Review pipeline.
5. Only Human Review can authorize subsequent canonical updates.

**Only the structural proposal decoder and evidence binding exist here.**
No model invocation, autonomous inference, Review job submission, private
candidate database, auto-backfill or canonical writes have been enabled.
A syntactically valid proposed interpretation may still be false.

## Remaining blockers and tests

- Independent review of #393, #396, #398 and this follow-up.
- Real Export policy for one empty entry, unverified reciprocal children,
  assistant final/visibility ambiguity and 8 deferred multimodal messages.
- Source-specific Job adapter without widening the existing Vault Context
  authority, semantic evaluator, version locking, Review backpressure
  and trusted Human Review transitions.
- Dedicated nonroot production Importer identity and user-approved rollout.

Synthetic tests cover quote/source integrity, role attribution, stable
anchors across paraphrases, changed metadata, forged approvals, Unicode,
malformed/double-key JSON and no-authority invariants. No actual private
Export content is included in any tests or GitHub files.
