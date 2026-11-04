# #511 P2/A — isolated Local Publisher delivery proof

Status: **experimental fixture-only**. There is no CLI, scheduler, deployed
service, production credential integration or HTTPS/GitHub Core write path.
Merged PR #513 supplies only the P1 no-Push preview. #511 remains open.
P2/A never changes the existing Gitea Actions Runner or live Vault.

## Source authority and execution path

local_publication_delivery.read_verified_b2_run opens the B2 SnapshotJob SQLite
state with mode=ro and query_only=ON. It checks a **latest** 32-hex run ID,
terminal saved/no_changes + verify + valid 40-hex Git SHA, completed timestamp,
verified Gitea connection_write state, exact current connection-generation
binding and nextcloud -> gitea settings. Failed or superseded runs are refused.
This is the *local read model*, NOT cryptographic source attestation or a
per-run authenticated Gitea origin proof. Real source connection identity and
credential lifetime binding remain P2/B.

local_publication_delivery.deliver_disposable_fixture requires all of:

- explicit enabled=True and P1 allow_file_fixture=True
- a private 0700 disposable root with a 0600 fixed-content
  .oa511-p2-disposable-fixture marker
- two fixed file:// Git repositories source.git and core.git under that
  exact root, with separate sibling state directories b2-state and p2-state.
  State may not be placed inside the bare repositories or another job's tree

**All HTTPS destinations, including the real public ObsidianCore GitHub
repository, are rejected before Git or state writes.** No credentials or
normal HOME environment are passed. The P1 validator still runs Core-owned
Node code, so this is NOT an OS sandbox and must never be deployed to
process untrusted live source with write credentials.

Under one local delivery.lock, P2/A reads latest B2 evidence and reuses P1
exact-source/core SHA checkout, semantic Core drift protection, allowlist,
Node validation and local candidate. It independently checks candidate
ancestry, cleanliness and source Projection convergence. It **rechecks B2
latest-run and generation verification and policy digest after P1**, before
durable delivery intent and any possible Push. Both source and Core remote
refs are checked again before an effect.

The single Push uses no force flag, no plus refspec, no local pre-push hook:
a fixed candidate SHA -> refs/heads/main in the disposable core.git remote.
Its result is **not** trusted without an independent bare Git clone readback
of the exact new ref, one-parent ancestry and expected tree.

The local 0700 per-run state and 0600 fsynced JSON intent/results bind B2 run
ID, generation, private source SHA, Core-before SHA, candidate SHA, policy
digest, pinned publisher revision and P1 run key. No URL, secret or note path
appears in the receipt. A timeout or failed independent readback leaves
status **unknown**. A restart checks the frozen intent and fetches the
remote again, without automatically sending another Push.

## Result vocabulary

| Result | Meaning |
| --- | --- |
| published_verified | Independent fetched ref/parent/tree match |
| no_changes_verified | No candidate; refs unchanged; no Push |
| blocked_conflict | Source or target changed before the write gate |
| unknown | Possible effect with no exact readback proof; never auto-resend |
| reconciled_verified | Later independent fetch proved earlier unknown applied |
| already_published_verified | Historical verified receipt, no new Push |
| already_no_changes_verified | Historical no-op receipt, no new Push |

"Previously verified" is *not* an assertion that the ref is still current.
A mismatch of B2 run, source connection generation, policy revision, or
malformed local record fails closed.

## Test evidence and fixture-only scope

Tests use actual disposable bare file:// Git repositories and the real
SnapshotJob state machine producing SQLite records; simulated server races,
lost acknowledgements, unknown outcomes and independent fetches are tested.
All destinations are local fixture Git files. Neither private Gitea nor
GitHub ObsidianCore is written. Run in the Nix devShell:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider \
      tests/test_local_publication_delivery.py \
      tests/test_local_publication_preview.py \
      tests/test_public_publish.py tests/test_public_export.py \
      tests/test_snapshot_adapter.py

## Production boundaries NOT solved here

1. B2's current SQLite row does not authenticate the private Gitea
   repository used on each historical run, nor bind a signed per-run
   credential generation. P1 cannot currently fetch credentialed private
   HTTPS Gitea. Production requires a pinned read-only connection with
   transport identity and a durable per-run producer proof.
2. Node validator code executes Core-owned scripts, and scrubbed environment
   alone is NOT an OS sandbox. Future Core write credentials must never be
   accessible to the Node validator.
3. This adapter has no HTTPS destination Push or real GitHub scoped deploy
   credential. Do not install or enable it as a live publishing job.
4. A non-force Git Push blocks non-fast-forward overwrites, but is not a
   universal atomic expected-old-ref CAS (e.g. remote rewinds). Production
   needs branch protection/server-side policy and bounded explicit
   reconciliation for ambiguous outcomes.
5. Production WebUI/status, credential lifecycle, rollback and first live
   canary remain separate owner-approved P2/B and P3 tasks. Existing Runner
   must not be automatically deregistered or stopped. B2 WebDAV and
   managed-delete limitations remain upstream.

**Approval boundary:** Source-only P2/A implementation and disposable tests
do not authorize Commit/Push/PR, Merge, production deployment, live Core Push,
Vault writes, new LXC or the existing Runner shutdown.
