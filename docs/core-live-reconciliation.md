# Core-tracked Live drift: detection and explicit recovery

Refs #125. This change is independent of the #109 and #110 deployment work.

## Authority and default behavior

Nextcloud remains user-editable authority. A changed Live file may be a legitimate
edit awaiting Vault -> Core publication. Therefore the periodic production command
**does not overwrite drift automatically**.

`obsidian-core-promotion-run` uses the managed deployment wrapper. Ordered Core
promotion is unchanged. When the fetched Core HEAD already equals the checkpoint,
the wrapper reacquires the same `promotion.lock`, rechecks HEAD/checkpoint/policy,
and audits every Core-tracked, allowlisted managed file through verified WebDAV GET.
It compares exact bytes except for the existing managed appearance projection.

- Every audited path converges: `result=up_to_date`, exit 0.
- A known path is missing or modified: `result=live_drift`, exit 3, immutable plan
  under `STATE/drift/plans/<digest>.live-drift-plan.json`. No canonical writes.
- Unreadable/ambiguous remote state, invalid appearance, limits or other failure:
  exit 2, never a successful convergence result.

The low-level `promotion_deployment` API still represents ordered Core advancement;
the production console entrypoint is `managed_promotion_deployment:main` and adds
the Live audit. Existing low-level callers must use the managed wrapper when they
need this stronger up-to-date contract.

## Scope and limits

The scope is `core_tracked_managed_paths`. Remote-only files are not enumerated,
claimed absent, deleted or moved. They may be legitimate unpublished user content.
Ordered Core deletes continue through the existing promotion transport; a drift
repair is not a remote garbage collector.

Limits: 4096 managed files, 2 MiB per Core/remote file, 64 MiB total content,
8 MiB plan/receipt input, bounded per-request timeout. Requests are serial. The
full audit adds one Nextcloud GET per tracked managed file; it is not a GitHub API
poll and consumes no GitHub REST quota. Measure the cycle on the actual Vault
before re-enabling recurrence after deployment. This is not an atomic remote
snapshot: concurrent edits can cause explicit drift or conflict, not blind repair.

Appearance comparison considers only `theme`, `cssTheme`, and the declared Core
snippet subset. Repair merges those fields into the exact observed Live document,
preserving unrelated keys and snippet names. Malformed appearance is rejected for
manual review rather than replaced wholesale.

Plans contain Core/policy digests, a hashed endpoint/account binding, public managed
paths, desired/observed content digests and bounded ETags. They contain neither Live
bodies nor credential values. Keep plans and receipts in the private promotion
state root. Do not share complete configuration or credential files.

## Recovery protocol

First decide whether the Live change is intentional. For an intentional edit, use
the normal reviewed publication/convergence procedure, not repair. Do not reset or
rewind `checkpoint.json` to force a resend.

For an explicitly approved restoration to the reviewed Core version:

1. Let the ordinary Core promotion checkpoint converge to fetched Core HEAD.
2. Run a read-only audit; inspect the private plan and its public desired paths.
3. Select the exact plan and its SHA-256 explicitly.
4. Run repair as the promoter identity, with the same private environment and
   password file as the ordinary promotion unit.
5. Re-run the audit; require convergence before normal operation is accepted.

The commands are modules in the installed package and do not need new identities,
credentials or a new timer. Run them in a child shell with the existing promoter
configuration loaded privately; never paste secrets or put passwords in argv.

```text
/opt/obsidian-automation/venv/bin/python -m obsidian_automation.core_live_reconciliation audit

/opt/obsidian-automation/venv/bin/python -m obsidian_automation.core_live_reconciliation repair \
  --plan /var/lib/obsidian-core-promotion/drift/plans/<digest>.live-drift-plan.json \
  --plan-sha256 <reviewed-digest>
```

The CLI reads `OBSIDIAN_PROMOTION_BASE_URL` and `OBSIDIAN_PROMOTION_USERNAME` from the
existing environment and uses `/etc/obsidian-core-promotion/nextcloud.password`.
It does not source shell files, register credentials, disable timers or enable jobs.
It serializes with the existing promotion lock. Use the real configuration bindings;
no secret values need to appear in commands or logs.

Repair fetches current Core, checks checkpoint/HEAD/policy, verifies the exact plan
hash, reconstructs desired bytes from the immutable Core commit, and preflights all
selected paths before any mutation. It never treats arbitrary JSON bodies as an
authorized desired state. A different endpoint, account, policy or Core HEAD requires
a new audit/approval.

- Missing path: `If-None-Match: *` conditional create.
- Modified path: exact observed body hash plus the same strong ETag, then `If-Match`.
- Already desired: no mutation (also supports same-plan crash recovery).
- Any preflight conflict: no PUT at all.
- A race after preflight: CAS rejection; never overwrite the competing change.
- Every successful write is exact-byte verified, including merged appearance.

This is per-file CAS, **not a multi-file atomic transaction**. Immutable started,
attempting, applied and final/failed receipts preserve partial or ambiguous results.
A failed request is not proof of no remote effect. Same-plan retry re-observes the
remote and skips already-desired content. No rollback/delete is attempted and the
ordered Core promotion checkpoint is never advanced by drift repair.

## Acceptance after merge

Code/fixture acceptance is not production acceptance. On a disposable or approved
managed-file canary: converge Core -> Live, remove or edit only that Live file, run
the managed production command with unchanged Core HEAD, observe `live_drift` and
no write, then explicitly approve repair. Verify conditional request protection,
private receipts, unchanged checkpoint, and a final converged audit. Also confirm
legitimate appearance-only user settings survive.

Normal unit failures during `live_drift` are intentional attention signals, not a
reason to weaken tests or grant wider credentials. A newly merged feature must not
be marked production-accepted from the earlier Runner migration logs alone.
