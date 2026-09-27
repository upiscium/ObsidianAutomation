# Legacy 03-AI projection retirement

This runbook retires fully resolved historical `03-AI/**` projection artifacts
from active runtime queues before the runtime parser and cleanup compatibility
for `03-AI` is removed.

The migration preserves exact artifact bytes. It does not delete or rewrite
historical projection evidence.

## Preconditions

Run this only after all of the following are true:

- new Human-facing projections use `04-AI/**`;
- `pending_legacy_requests = 0`;
- `pending_legacy_cleanup = 0`;
- post-review projection publication and terminal cleanup have passed production acceptance.

The migration tool refuses unresolved or conflicted legacy projection requests.

## Archive layout

Resolved legacy artifacts are moved from the active runtime queues to:

```text
/var/lib/obsidian-ai/state/
└── 18-Human-Projection-History/
    └── 03-AI/
        ├── retirement-intent.json
        ├── retirement-completed.json
        ├── 16-Human-Projection/...
        └── 17-Human-Projection-Result/...
```

The original filenames and exact bytes are preserved below the archive root.
The retirement intent is persisted before any move. A repeated `--apply` resumes
the same intent and verifies already archived bytes.

Post-review projection bindings remain in `20-Review`. They contain case/evaluation/
mutation identities and the historical request SHA but no `03-AI` target path.
Runtime post-review execution can therefore retain those bindings after the
legacy request bytes leave the active queue.

## Check

After deploying a revision that contains the retirement tool:

```bash
sudo /opt/obsidian-automation/venv/bin/obsidian-ai-retire-legacy-projections \
  --ai-root /var/lib/obsidian-ai/state \
  --check
```

The check is read-only. It validates that every active `03-AI` projection request
has a matching successful projection result and that every related cleanup request
has a completed cleanup result.

A pending or conflicted artifact is a hard stop.

## Apply

Quiesce the pre-review dependency chain before moving active queue files.
Do not leave the timer disabled after a successful migration.

```bash
sudo systemctl stop obsidian-pre-review.timer
sudo systemctl stop obsidian-pre-review-status.service

sudo /opt/obsidian-automation/venv/bin/obsidian-ai-retire-legacy-projections \
  --ai-root /var/lib/obsidian-ai/state \
  --apply
```

Expected result:

```json
{"status":"completed", ...}
```

A repeated invocation after success returns `already_completed`.

## Verify

The active queue must contain no remaining retirement candidates:

```bash
sudo /opt/obsidian-automation/venv/bin/obsidian-ai-retire-legacy-projections \
  --ai-root /var/lib/obsidian-ai/state \
  --check
```

Expected:

```json
{"legacy_entries":0,"status":"ready", ...}
```

Verify the archive and completion receipt exist:

```bash
test -f /var/lib/obsidian-ai/state/18-Human-Projection-History/03-AI/retirement-intent.json
test -f /var/lib/obsidian-ai/state/18-Human-Projection-History/03-AI/retirement-completed.json
```

Then restore recurrence:

```bash
sudo systemctl start obsidian-pre-review.timer
systemctl show obsidian-pre-review.timer \
  -p LoadState -p UnitFileState -p ActiveState -p SubState
```

Expected timer state is loaded, enabled, active, and waiting.

Run one normal cycle and confirm Review Intake, post-review projection sync,
reconcile, cleanup, and status all succeed.

## Next phase

Only after the active queue check reports zero legacy entries should the runtime
remove:

- `LEGACY_PROJECTION_ROOT`;
- `03-AI` request/result parser compatibility;
- legacy cleanup root support;
- migration-specific tests and documentation.

The private history archive remains audit evidence and is not reintroduced into
runtime scanning.
