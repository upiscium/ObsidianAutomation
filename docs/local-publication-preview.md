# #511 P0/P1 — Runnerless local publication preview (source-only)

Status: **experimental, no production entrypoint, no remote push capability**.
Parent #298 effective 2026-10-24 Fresh-LXC MVI. Local Publisher is post-MVI opt-in;
#305's B2 Snapshot already has separate hourly production acceptance.
This document does not authorize PR, merge, production deployment, live Core
write, Vault mutation, generic Runner shutdown or schedule enablement.

## Existing source contract and gaps (P0)

- src/obsidian_automation/public_publish.py is the existing deterministic
  projection function / CLI. It applies allowlist and repository-owned guards,
  handles managed appearance semantics, checks Core-managed drift, invokes Core's
  validate-repo.mjs and Node tests and generates a **local** generated commit
  or no-op. The CLI never pushes.
- examples/gitea/public-projection.yml currently supplies the external
  Gitea host-mode Runner, private source checkout and scoped GitHub Core
  deploy key. It pushes only after the CLI emits a commit.
- #305's B2 snapshot_job records saved/no_changes, stage=verify,
  remote_ref=(40hex SHA); its live settings also expose gitea_write=verified
  and a private Gitea connection. Neither an HTTP 202 nor arbitrary
  user-submitted JSON proves that authority.
- No current source component owns an independently verified private Git
  commit -> public GitHub Core ref transaction with one-run idempotency,
  lock, stable receipts, write recovery and independent final readback.

## P1 read-only preview API

local_publication_preview.prepare_local_preview(evidence, config) is an
internal Python interface only. There is intentionally no CLI,
WebUI control, systemd timer, Runner hook, GitHub write key or push method.

The P1 configuration specifies a trusted private Git repository endpoint,
the fixed public https://github.com/upiscium/ObsidianCore.git endpoint,
an allowlist policy file, private local state root and a 40hex
publisher_revision identity. HTTPS Git endpoints with embedded
credentials or redirects are rejected. Local file:// remotes are allowed
only in explicit synthetic fixture mode. P1 tests exclusively exercise
two disposable local bare Git remotes.

SnapshotEvidence must be constructed by a future trusted B2 adapter,
binding the canonical saved|no_changes / verify result and Gitea write
verification to the actual configured B2 repository. **The dataclass itself
is not an attested credential.** A source SHA cannot gain authority merely
by being passed into this API. That authenticated production binding is
deferred to P2. Neither a publisher_revision 40hex string nor Git data
alone proves deployed code matches that revision; P2 must verify an
exact reviewed artifact at runtime.

For valid evidence, the preview:

1. Checks current private Git refs/heads/main against exact B2 source SHA;
   discovers Core refs/heads/main. A mismatch refuses work.
2. Acquires one nonblocking local advisory lock; computes a stable key from
   source SHA, Core SHA, exact policy SHA-256 and publisher revision.
3. Persists a 0600 fsynced intent in a 0700 run folder before checkout.
   An interrupted attempt without a success receipt stays blocked:
   **no automatic replay on restart**.
4. Makes isolated full-history no-checkout clones, verifies refs, detaches
   at exact SHAs, materializes the trees and **removes clone remotes**
   before Node validation. A frozen policy copy prevents source policy
   changes during the run.
5. Executes the existing guarded publisher in a bounded subprocess with
   stdout/stderr discarded, no ambient Git/SSH/HTTPS secrets or user HOME.
   Only local commit generation is possible via this adapter.
6. After validation, rechecks the source checkout and frozen policy bytes,
   then checks clean local Core tree and unchanged HEAD or exactly one marked
   generated child commit. Its changed paths must ALL match the managed
   allowlist (not repository-owned or excluded). This extra fence matters
   because the existing public_publish CLI uses git add -A, and Node
   validation may accidentally create unrelated files. Additionally the
   complete generated Core projection is recomputed against the pinned
   source with the same allowlist and semantic appearance rules: any
   remaining content drift (even within allowed paths) rejects the preview.
   Independently re-reads both remote refs before accepting a candidate.
7. Persists a 0600 fsynced candidate_prepared or no_changes receipt
   (only SHAs, policy digest, finite status; no URLs/notes). Repeated
   keys verify the local source/candidate and return already_prepared
   without rerunning validators. Cached source/Core Git metadata must be
   real directories, never symlinks into other repositories.
8. On failure after intent, writes a 0600 failure.json with a finite
   reason. No remote write is attempted.

Git operations have a 120s per-command timeout; the projection subprocess
has a 180s bound. These limits are for P1 preview, not a production
scheduling/resource budget. Preview success is never published_verified.

## Invariants and negative cases

- No reviewed Core change may be overwritten; path-wise drift and semantic
  appearance guards are reused unchanged.
- Repository-owned paths follow existing exporter policy. Even if a
  validator accidentally creates a repository-owned or excluded file,
  the generated candidate is rejected locally before a success receipt.
  Missing or failing Core validator/tests also produce no success.
- Source ref moved during checkout/after generation, Core HEAD changed,
  process timeout, validation failure, crashed prior intent, simultaneous
  duplicate, corrupt receipt, mutated candidate/policy => fail closed.
- A cache hit proves only exact remote refs and presence/validity of
  the local candidate; it does not certify public publication or
  point-in-time Nextcloud source consistency.

## Explicit P2/P3 unresolved gates

1. **Trusted B2 binding**: authenticated per-run Snapshot receipt,
   connection identity, private source ref. No user-configurable SHA
   impersonation.
2. **Credential/runtime isolation**: read-only Gitea and scoped Core
   write credentials without inheritance by Core Node validator.
   Node tests execute code from reviewed Core; clean environment alone
   is **not a sandbox**, and P2 must isolate future write credentials.
3. **Push/reconciliation**: pinned baseline, non-force single-ref push,
   non-FF/race detection and unknown-after-send recovery, independently
   fetched target ref and tree. No automatic resend of unknown mutation.
4. **Source consistency**: B2 managed-delete fence also rejects
   legitimate deletions and cannot prove point-in-time WebDAV listing
   completeness for new/updated notes. This adapter does not fix it.
5. **Cutover**: production deploy, first live Core canary, enabling
   local publishing and disabling external Gitea Runner each require
   separate Human approval and rollback verification.

## Test command

Inside the repository Nix devShell:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_local_publication_preview.py tests/test_public_publish.py tests/test_public_export.py

P1 consists of this module, disposable fixtures and design note,
uncommitted in an isolated Adam checkout.
