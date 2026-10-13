# GitHub Daily Summary managed runtime boundary (#288)

Status: **source-only; NOT approved for deployed Stage/Activate/Timer/Writer**.
This source-only candidate is independent of Draft PR #343 (optional transient
transport retries), and branches from merged #291 on main.

## Why an OS-level budget

`obsidian-github-daily-summary.service` uses `Type=oneshot`. systemd disables
the default `TimeoutStartSec` for this service type and `RuntimeMaxSec`
does not limit oneshot activation. A Python deadline checked between
Partial/Grounding contexts cannot interrupt a blocked `urllib` request or
a blocking model identity verification operation within a context.

The service template adds:

- `TimeoutStartSec=20min`, measuring **Summary service activation**, including
  initial provider resolution, Partial/Reduce/Grounding, any separately
  authorized retry/backoff, CAS publication and Summary stage-ref publication.
- `TimeoutStartFailureMode=kill` to request immediate final SIGKILL on
  expiration rather than an additional graceful termination interval.
- `KillMode=control-group` and `SendSIGKILL=yes` to stop the managed service
  process group, including subprocesses that remain in that service cgroup.
- `Restart=no` to avoid immediate, unapproved source-level restarts.

A timeout marks the Summary service failed. Existing `Requires` and `After`
dependencies prevent Renderer and Writer from succeeding as part of that
failed unit activation transaction. A Summary Stage Ref is never synthesized
to make the pipeline look complete.

## Crash behavior and verified Resume

The limit is enforced **outside Python**. SIGKILL may occur at *any* point,
including after a no-overwrite rename but before the parent-directory fsync.
Merged PR #291's immutable CAS/Stage Ref publication, fsync revalidation and
verified Resume are prerequisites:

- Previously committed Partial and Grounding output + provenance + Resume
  pointers survive interruption and must be checked again on retry.
- An interrupted fresh inference does not create an accepted Resume pointer.
  An invalid existing published object still fails closed.
- Local directory-inode flock ownership is kernel-managed and released when
  the owning process exits.
- Uncommitted private temps after an unclean kill are **not** automatically
  adopted or removed. Any operator cleanup needs separate review.
- This Summary service is not an authorized Vault, Writer, or Nextcloud CAS
  publisher. It changes no Evidence or previously published ref.

The unit timeout is a bound on **Summary only**, not total Collect/Render/
Apply chain execution. The timer currently fires at each hour's `:40` and
also at 00:10 JST; the shortest *scheduled start* interval is 30 minutes.
A 20-minute Summary limit leaves nominal headroom, but does not prove there
is any idle gap, because collection and scheduling also consume time.
Timer/recurrence admission, backlog control, and date-scoped acceptance are
separate operational gates. Keep the production Daily timer stopped.

## Verification and production rollout gates

- Unit contract regression verifies a oneshot activation timeout, immediate
  kill failure mode, service cgroup scope, disabled unapproved restarts, and
  existing fail-closed service dependency chain.
- A scratch Linux SIGKILL test interrupts the seventh Partial only after six
  indexed outputs, checks six surviving immutable Resume pointers and
  lock release, then retries and verifies only Partial 7 is recomputed. This
  tests the crash/recovery invariants, **not** systemd's actual unit behavior.
- A separately approved systemd integration canary must use an **isolated,
  nonproduction fixture and a temporary short timeout**, then verify
  actual `Result=timeout` (or matching systemd failure evidence), correct
  KillMode behavior, no surviving worker process, no Writer/Nextcloud
  action, lock release and Resume. No live service, timer or canonical
  filesystem should be modified in a source PR.
- An operator must verify exact installed unit bytes, `TimeoutStartUSec`,
  `TimeoutStartFailureMode`, `KillMode`, unit dependencies, effective service
  identity and filesystem permissions at a separate Stage/Activate gate.
- PR #343 still needs explicit source integration approval and should retain
  retries default OFF. #290 semantic omissions and #338 transport receipt
  crash/replay remain separate prerequisites for daily publication.
- No automatic timer enable, new systemd unit activation, Writer request, or
  historical ref mutation is authorized here.

References:
- https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
- https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html
