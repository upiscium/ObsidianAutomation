# Daily Transport Result atomic publication and replay contract (#338)

Status: **source-only Draft, not an authorization for Writer/Nextcloud**.

## Failure paths fixed

The legacy Transport Result final path was created with `O_CREAT|O_EXCL`
*before* being written. A crash between these steps could leave a partial
canonical result, fail all later retries, or tempt an unsafe repair. The
existing in-place reader also had no inode/byte cap. The new producer uses
the already merged #293 atomic no-replace CAS helper for newly committed
receipts, staging complete/fsynced data before the final rename and fsyncing
the containing directory. Pre-existing receipts are admitted only as strict,
canonical, bounded (64-KiB), single-link, no-follow regular files. Partial,
corrupt, ambiguous or conflicting published receipts fail closed; no existing
receipt is silently overwritten. An interrupted private staging temp remains
an uncommitted orphan, not a cache hit or automatic repair candidate.

Before any new external CAS attempt, an existing receipt is admitted through
bounded/no-follow schema and exact Projection/date/target verification, and
its parent directory is fsynced. A corrupt or contradictory prior receipt
therefore fails before a new Writer operation, rather than allowing an
avoidable external effect before discovering the local corruption.

The transport stage can crash **after the Writer's external compare-and-swap
effect** or after the receipt but before publishing its date-scoped Stage Ref.
A replay can therefore observe `already_desired` even though the earlier,
immutable receipt says `applied`. Previous code hashed the old receipt but
put the **new** runtime outcome in the Stage Ref, so Live Smoke rejected an
otherwise completed stage.

## First committed receipt is the authority

For each projection SHA, the **first valid published receipt** is immutable.
On a replay, apply_projection must independently reobserve the remote desired
content before receipt admission. Reuse is allowed only if the *current*
observation matches the stored receipt on projection SHA, date, target path,
Evidence SHA, Grounded Summary SHA, projected section SHA and exact
`after_content_sha256`. A historical `before_content_sha256`, `outcome`
or timestamp may differ because the first CAS effect and a later read-only
observation have different chronology. The saved receipt's original outcome
is therefore authoritative for the corresponding date-scoped Stage Ref; its
SHA and outcome always describe **the same immutable metadata object**.

This does not authorize overwriting old published receipts, bypassing remote
ETag CAS, treating corrupt published records as misses, or assuming that
`already_desired` proves a previously attempted Writer succeeded. The
existing external state observation must still be correct and bound to the
same target. Conflicts remain explicit and fail closed.

## Acceptance boundaries

Synthetic fault tests cover prepublication failure, directory fsync failure,
unsafe/special inodes, corrupted final receipt, same-projection replay
outcome drift, concurrent publication, and the Stage Ref binding. The new
reader cap applies **only** to fixed-schema Transport Result metadata, not
model output, Evidence or user-facing Daily Notes.

This source PR alone is not enough to re-enable Daily:
- Independently review runtime/permission compatibility and the first
  receipt's existing mode/ACL with the writer identity.
- Perform separately authorized, isolated end-to-end interrupted Writer
  CAS/receipt/replay tests before a real date-scoped Writer acceptance.
- Resolve #290 semantic-coverage gates; retain #288 Summary runtime/retry
  guards and the stopped Daily timer.
- No Stage/Activate, live Writer, Vault, Nextcloud CAS, historical ref
  rewrite, or timer enable is approved by this document.
