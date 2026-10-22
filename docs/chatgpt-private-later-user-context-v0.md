# #411 — Later selected-branch User context for existing Human Review

Depends on stacked Draft #438 (typed External Status) and prior #436
(reuse original Human Review Record/Reconcile). This is an explicitly
**private, read-only evidence adjunct**, not a new validation pipeline,
approval writer, second Review GUI or canonical Vault action.

## Why the additional evidence is useful

An exact, SHA-verified ChatGPT Export quote alone cannot prove that a
decision was adopted, a feature was actually implemented, or a prior
claim remains correct. The same selected branch may contain later
User statements retracting, contradicting, changing or confirming
the original. This module gives the existing private Human Reviewer
bounded **literal context** instead of predicting a correction.

`read_private_later_user_context` composes existing:

1. Private Journal/Source HMAC and full Revision-bound Reader;
2. Existing `read_private_review_evidence` and current
   `awaiting_human_review` Job/Evaluation/v3 Generation Record proof;
3. Pure selected-conversation message-index traversal for **User role**
   turns strictly after each evidence candidate's original message;
4. SHA-256 checks of full original User message text and bounded UTF-8
   prefixes (default at most 4 later turns per candidate, max 512 UTF-8
   bytes each, total chronology packet max 48 KiB).

Each private in-memory record includes only an index into the currently
selected conversation, full text hash, exact bounded text prefix,
source role `user`, and truncation/omission flags. No message ID,
conversation title/ID, node ID, raw private path, model key or new
Vault target is emitted. The complete packet has a distinct SHA-256
bound to the existing Review Evidence Packet and Source Revision.

## Epistemic and chronology guarantees

- `message_after_evidence=true` means **later position on the currently
  selected parent-linked path**, NOT a verified wall-clock timestamp.
- `semantic_correction_verified=false` and
  `user_confirmation_verified=false` remain unchanged even when the
  later User text literally says 'I approve' or 'I retract it'.
- The absence of later User messages on this branch is **not** proof
  that no correction exists elsewhere, that the User approved anything,
  or that the present Export has all relevant material.
- Truncation after a configured number of turns is explicitly signaled
  by `more_later_user_turns_unshown=true`; an incomplete excerpt carries
  `truncated=true`. This is never presented as exhaustive context.
- `complete_cross_conversation_correction_check=false` and
  `semantic_status=unknown` are unconditional. No LLM heuristic decides
  truth, implementation status, ownership or Human approval.

## Tests

The synthetic E2E fixture runs an immutable ZIP with a true later
User node on the selected parent path through Private Intake/Journal,
Reader, Fake Candidate Proposer, External Source Context CAS, the
strictly fake-only Job/Worker and real `awaiting_human_review` state.
Other tests cover zero later turns, later Japanese correction text,
literal 'I formally approve' without implied authorization, Unicode
byte-safe truncation, bounded later-turn omission, bad limits, stale
updated ZIP, forged quote/summary and packet digest stability.

No source text is persisted to the existing public Dashboard/status,
sent to a provider, logged or written to the canonical Vault. The
view runs only for the synthetic Scope `fake-e2e` in the authorized
in-process private Reader. Before actual live Export/GUI use, separate
nonroot Reviewer UID/ACL, private transport, real context semantic
assessment and controlled rollout remain required. Keep #411 open;
do not Merge/deploy without explicit maintainer authorization.
