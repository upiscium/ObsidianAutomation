# #300 Phase G — Core Promotion local lock hardening

**Source-only hardening. Not a privileged Dashboard Broker, browser control
operation, production deployment or actual Linux service acceptance.**

The Core Promotion role already uses a role-local promotion.lock file under
its dedicated StateDirectory. The old implementation took a blocking
flock(LOCK_EX), allowing a manually started second process to wait for the
first run to complete and then start another full remote-changing cycle.
That was *serialized*, but not an explicit no-duplicate-admission policy.

The reviewed new contract is:

- The Core role state directory must be owned by the process UID and not
  group- or world-writable. StateDirectoryMode=0700 from the service manifest
  satisfies this requirement.
- The existing promotion.lock inode must be a single-link regular file,
  owned by the current role identity and mode 0600. Symlink, hardlink,
  unexpected mode/owner or inode replacement fail closed.
- Take an exclusive, **nonblocking** flock. A competing Core Promotion or
  managed read-only Live audit using the same lock is rejected promptly
  with a fixed promotion_already_running result rather than queuing another
  remote-effectful invocation.
- Release the descriptor in all normal/failure exits. Existing Callers
  retain their existing transaction semantics: ordered promotion, CAS and
  checkpoint rules are not replaced by this host-local lock.
- The secondary managed Live drift audit deliberately reacquires the SAME
  lock only after the first cycle has released it. If another promotion
  advanced in the interim, its existing checkpoint/HEAD revalidation still
  rejects stale observations.

The Dashboard #300 Broker should not hold the Core Promotion role's lock
while synchronously invoking systemctl start on that role, because the
service itself must acquire that lock: doing so would cause a self-deadlock.
Instead, a future reviewed admission flow must separately prove that the
fixed Core service is idle and preserve proper root-to-nonroot ownership
boundaries, then leave the Core role to acquire its own mutex. A concurrent
independent process cannot cause two simultaneous Core effects, although
the remote CAS/reconciliation and manual retry gates remain necessary.

This does NOT claim that all Github/AI/OnSuccess service launchers share the
same gate, nor that an external arbitrary privileged process cannot trigger
a job. A single active execution LXC per Vault remains the supported topology.
Any production control activation still requires the privileged Broker,
CSRF/Origin-bound operator API and disposable actual-systemd/cross-UID E2E.

Synthetic tests cover lock contention/nonblocking failure, reentry,
private inode/owner/mode, symlinks/hardlinks, unsafe directory and guaranteed
release after a failure. No live remote/Nextcloud/Vault mutation is tested.
