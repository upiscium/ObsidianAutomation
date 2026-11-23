# ChatGPT Export — Private Intake + Durable Inventory Journal v0

Parent: Issue #62. Depends on PR #393 selected-branch normalizer. This is a
**standalone, synthetic-tested, manually invoked source-only prototype**.
No production deployment, background timer, personal Archive migration,
account/identity provisioning, LLM generation, Review queue, Vault mutation,
WebDAV request or automatic approval is performed.

## Boundary / operator contract

- Official ChatGPT Export download is a manual user responsibility. Never
  automate private ChatGPT endpoints, cookies or browser account access.
- Private import root is **outside** the Vault, Nextcloud mirror and all
  existing lifecycle directories. The future production operator must
  explicitly provision a dedicated, non-root Importer service identity and
  storage root. That step **is not included** here.
- Must run with the account that owns source ZIP and root; refuses UID 0.
  Root and its state/staging/ready dirs must be 0700, archive files/DB/lock/
  HMAC salt must be 0600. Do not run under existing AI/transport identities
  or alongside their credentials.
- Treat **ALL** ZIP data, returned Python objects and the private SQLite DB
  as private. Never publish ZIP/member paths, original conversation IDs,
  text, titles, or hashes to public GitHub/chat/standard logs.
- Source ZIP is opened with O_NOFOLLOW/O_NONBLOCK and fstat; archive input
  remains unchanged. The prototype uses a private staging copy and verifies
  source descriptor metadata before publishing.
- Root ancestor symlinks, private inode ownership/mode errors, source hard
  links, ready archive mismatches and corrupt ZIP structures fail closed.
  Same-UID malicious actors, untrusted network mounts and simultaneous
  privileged maintenance **are outside the prototype security model**.
  A proper isolated Importer identity and host filesystem ownership are
  required before production.

## CLI (manual, opt-in)

With the project installed, or PYTHONPATH=src from the repository root:

    python -B -m obsidian_automation.chatgpt_export_intake publish \
      --root /private/export-intake --source /private/completed-export.zip

    python -B -m obsidian_automation.chatgpt_export_intake inventory \
      --root /private/export-intake --scope private-chatgpt

The inventory invocation is a **preview**: it does not create runs, update
Latest, or send anything to LLM. It may initialize a private DB/HMAC salt.
For an explicit durable inventory commit (still **NO** Knowledge jobs):

    python -B -m obsidian_automation.chatgpt_export_intake inventory \
      --root /private/export-intake --scope private-chatgpt --commit

Default is one archive per invocation; --max-archives N (1..16) is
supported only with --commit. For unusual Export JSON names use --member
privately (may repeat); not logged. Never share the command line or paths.

CLI stdout is a fixed-field, count-only JSON receipt. No archive digest,
private IDs, titles, message text, ZIP member names or arbitrary parse
exceptions are printed. Exit 0 is a successful *local inventory operation*,
not permission to run the Generator or modify Vault notes.

## Durable state and crash behavior

1. Publish copies a completed, regular, same-user source ZIP into private
   staging with O_EXCL, 0600, bounded reads and SHA-256, fsyncs the staged
   file and validates source descriptor stability.
2. The staged ZIP passes the existing bounded read-only inspector **before**
   publication, including ZIP-member safety and candidate JSON inspection.
   The inspector status schema_review_required is not an error, but remains a
   mandatory human/production Gate.
3. Within a local advisory flock, SQLite records an increasing publication
   sequence and a pending/publishing record (synchronous=FULL, rollback DELETE
   journal). A no-replace hard-link publishes the staged archive into
   ready/<sha256>.zip on the **same filesystem**. Ready directory fsync
   precedes marking the DB row ready. Original bytes are kept in private
   storage; staging temporary files are removed.
4. If publication fails after DB registration, the row remains publishing.
   Retrying **the same input** re-verifies the ready file (when present),
   recovers the registration and preserves its sequence. No mtime heuristic
   decides whether a half-written ZIP is ready.
5. Inventory processes ready archives by publication sequence and never
   replays a committed same-scope+same-selection+parser run. A new member
   selector cannot roll the Latest state back behind its committed archive
   sequence; use a new scope for a deliberate new ingestion policy.
6. Inventory validation returns two separated, local outcomes:
   inventory_only for structurally understood records and
   inventory_review_required where quarantined slots, missing reciprocal
   child-link evidence or deferred content exist. **Neither is generation
   eligibility.** Unsupported normalized schema is recorded as a fixed
   normalization_rejected receipt when --commit is given, without moving
   Latest. The next ready archive remains processable.
7. Each committed archive uses a single SQLite transaction to update:
   - immutable runs with count-only receipt/status;
   - revisions keyed by HMAC(scope + private conversation ID) and exact
     normalized source-revision hash;
   - latest with last seen content/revision digest per conversation;
   - run_items with added, changed, revision_only, unchanged.
   Each run commits atomically; a crash before commit can be re-run.
8. A conversation absent in a subsequent Export is absent_not_deleted.
   Its last known state remains recorded, **never** a Vault DELETE/MOVE.
   Repeated same ZIP and unchanged conversations produce no downstream jobs.

The HMAC key is a private random 32-byte salt stored as 0600 under state/.
It is not an account/token credential and must be preserved with the DB.
Only hashed conversation identity and hashes of source/semantic content
are stored in the DB. Actual conversation/message text is in the original
private ZIP, never as a SQLite field or public log.

## Intended permissions / delivery boundary

No feature in this implementation invokes the existing AI Generator,
Validator, Evaluator, semantic retrieval, Human Review, Executor or
Nextcloud transport. There are no new installed systemd timers/services,
credential scope changes, ACL changes, auto-import from ~/Downloads,
or standalone root bootstrap. Publication and --commit are manual local
operations. The DB inventory_review_required state is an explicit block
for downstream generation; an inventory_only state also does not grant
generation authority.

This module does **not** offer a replacement for existing Human Review.
Production must preserve an independent bounded Source/Context contract,
provenance per extraction unit, review queue backpressure, explicit scope
selection, and user-controlled approval before note publication.

## Test/acceptance boundaries

Tests use ONLY synthetic ZIP fixtures, no user's real Export or account data.
They cover duplicate staging, archive SHA, protected files, malicious ZIP,
symlink/hardlink rejection, concurrent publication, fsync-oriented
post-link recovery, parse rejection without changing Latest, out-of-order
replay refusal, metadata-only revisions, new/changed/unchanged inventory,
non-deletion, CLI sanitization and no hidden LLM/transport calls.

*Not yet accepted*:
- independent reviewer sign-off and exact-head Linux CI;
- real private physical staging/receipt acceptance after an explicit
  dedicated Importer identity has been provisioned;
- attachment/image/audio multimodal extraction, confirmation that
  assistant candidate text is truly visible/final, and disposition of
  one actual empty {} slot;
- production durable Source/Context reader, provenance, bounded
  backfill/Review queue, and true periodic worker/systemd service;
- End-to-end Generation→Validation→fresh Retrieval→Evaluation→
  awaiting_human_review, with explicit human review of output.

PR #393 and this intake slice remain Draft until independent validation.
Neither prototype is sufficient evidence for production-ready Importer.


## 2026-10-19 scoped review corrections

The following defects were identified in the first prototype and fixed before
acceptance consideration. They are not evidence of independent Reviewer PASS:

- Identity salt must be **bound to the existing Journal**. On replacement,
  deletion after prior use, or missing journal-binding metadata, refuse
  inventory rather than interpreting historical conversations as new IDs.
- A ready ZIP with an orphaned staging hard-link (interrupted publication
  before staging cleanup) is recoverable only when its sole other inode link
  is a verified 0600 regular file inside the private staging directory.
  Unexplained hard-links remain rejected. Directory fsync follows cleanup.
- If ready archives survive but the SQLite Journal is missing, refuse
  recreating an empty Journal: otherwise a prior import could be silently
  replayed as all-new.
- A Source scope binds one immutable selection policy and parser version.
  Changing the member selector under the same scope, even on the same ZIP,
  is rejected: create a new explicit scope for migration.
- Omitted empty/oversize/unsupported content is classified as an explicit
  inventory review gap; it is never a silent complete-coverage claim.

These are operational data-integrity guarantees in the **single-owned,
non-hostile UID** model, not guarantees against hostile same-UID processes,
disk corruption, power loss on all filesystems, or independent model-derived
acceptance. The service identity and production topology remain unconfigured.


## 2026-10-20 review hardening (pre-merge)

- A lost Journal may still have a **regular 0600 file of zero bytes**.
  If any ready archive exists and SQLite user_version is zero, the
  importer rejects it as journal_uninitialized_with_ready_archives rather
  than initializing a new inventory and replaying old conversations.
  A completely new intake without ready archives can still initialize.
- A structurally valid selected conversation that retains zero visible
  messages is always inventory_review_required, even when no other
  omission flag and no missing child-edge verification is present.
  Empty evidence is not a success marker for a future Knowledge job.

The review additions are synthetic-only and have no production effect.
These changes preserve source-only / no auto-LLM / no Vault authority.
