# #301 — Gitea-only shared Nextcloud TOCTOU correction (source-only)

Status: source-only correction (Commit/Push/Draft PR separately authorized; no activation or deployment).

## Cause and precise correction

The existing Gitea-only POST to /api/v1/snapshot/connections accepts only the four
GITEA_FIELDS values and reuses the canonical private shared Nextcloud record.
Prior to this correction, it read and combined the Nextcloud record **before**
acquiring the Writer effect gate and the Snapshot/Daily/Formatter Job locks.
A concurrent Nextcloud source rotation could commit while Gitea waited on those
locks; the stale, already-expanded Gitea request could then overwrite the
newer canonical Nextcloud credentials and misbind the B2/B4 shared source.

The revised handler identifies only the Gitea-only payload **shape** before
acquiring locks. After acquiring the existing lock order:

1. Formatter Writer gate, when configured;
2. Snapshot Job lock;
3. Daily Job lock, when configured;
4. Formatter Job lock, when configured;

it performs the existing queued/running checks first and then loads the
**current** canonical Nextcloud credentials and combines the four Gitea fields.
The same existing SetupStore validation, read-only provider probe, atomic
credential write and schedule revocation remain in effect. A missing or invalid
canonical source still fails closed. The legacy explicit eight-field Snapshot
connection payload remains supported.

The order relative to SetupStore's private lock is not changed: Writer/Job locks
are held before calling SetupStore.load_nextcloud or SetupStore.save. No new
endpoint, credential cache, capability, provider network operation, scheduler,
Run Once path, or Vault/Gitea write operation is introduced.

## Reproducible concurrent HTTP regression

The new test file tests/test_301_gitea_only_toctou.py uses a disposable
127.0.0.1 ThreadingHTTPServer with the actual SetupStore, SnapshotJob,
DailyJob and FormatterJob. Provider probes are synthetic predicates; no real
Nextcloud, Gitea, GitHub, Vault or credentials are accessed.

Two separate POST requests are orchestrated by threading Events, **not sleeps**:
- Nextcloud rotation starts, enters the Formatter Writer gate and pauses inside
  its fake read-only probe before changing the canonical source.
- A Gitea-only POST begins. An observed gate wrapper signals the moment its
  real handler attempts to acquire that same gate.
- Only then does the test release the Nextcloud probe. The rotation commits
  before Gitea gets the gate.

Before the correction, the test fails reproducibly because Gitea restores the
old Nextcloud password. After the correction, Gitea reads the newly committed
record and its completed full B2 configuration retains the rotated source.
There are no submitted or executing Jobs; previously enabled synthetic B4 and
Formatter schedules are revoked by the source rotation as before.

Additional bounded tests cover:
- Gitea-only request with no saved Nextcloud source -> HTTP 400.
- Bad Gitea syntax -> HTTP 400; failed provider probe -> HTTP 422, no overwrite.
- Queued Snapshot, Daily or Formatter -> HTTP 409 before private source read.
- Existing explicit eight-field submission -> HTTP 200 without behavior change.

The historical #530 AST regression test is kept as a strict integrity gate:
it recognizes only the two exact changed source spans, reconstructs the
pre-correction handler and asserts that every other Python AST node (excluding
HTML as originally intended) matches its frozen baseline. This is not a
general exemption for backend edits.

## Remaining conditions and approvals

The test isolates the intended TOCTOU interleaving. It does not exercise
real provider availability, a live credential rotation, crash recovery,
all possible multi-process deployments or a production Vault.
It also does not implement full #301 Connections/AI-provider configuration.

This is a nonclosing source-only #301 slice. The separate #304 R1/C UI
simplification is a different uncommitted worktree; neither worktree is
published or deployed by this task. #304 and parent #298 stay Open.
The owner explicitly authorized Commit/Push/Draft PR publication of this
bounded source-only slice. Ready/Merge and any production rollout each remain
subject to separate Human Accept gates.
