# Daily Project Progress v2 — Issue/PR progression preview

- **Issue:** [#324](https://github.com/upiscium/ObsidianAutomation/issues/324)
- **Parent:** #258 (Daily Project Progress), #283 (Japanese editing quality), #290 (semantic omission audit)
- **Status:** experimental / read-only / never production-authoritative
- **Compatibility:** keeps existing v1 Evidence, Grounding, Projection and Daily Writer unchanged

## User-accepted output contract (2026-10-12)

The Daily `# Project Progress` section should list only **repositories
containing at least one Issue or PR with verified meaningful progress during
the target JST date, plus PRs with verified merges on that date**. There is
no independent repository-level summary, no display of repositories that
have no qualifying entities, and no Decision/Implementation/Bugfix headings.

For each Issue/PR: one linked item, a concise grounded Japanese account of
what actually changed, and the **last verified state**, without upgrading
`CI Green`, `Merge Accept`, `Draft` or `Ready` into `merged`.
Multiple comments/reviews/commits for the same entity are combined in one
item. Routine acknowledgements and bot-only notices do not create an item.
An entity absent from the output does not authorize deletion of its Evidence.

## What this child does now

`obsidian-github-daily-issue-pr-preview` is an **inert new command**, not a
replacement for `obsidian-github-daily-progress-render` or any managed
systemd timer. It takes **only local SHA-256-named Evidence and Grounded
Summary files**, requires bounded nofollow regular-file reads, verifies the
canonical hashes and existing v1 summary/Evidence binding, then produces a
nonpersisted v2 preview on standard output. It does **not** open remote
GitHub/Nextcloud endpoints, request credentials, change the Daily Note,
create a CAS ref, re-run the model, set a timer or acquire Writer authority.

Example (authorized existing local immutable inputs only):

```sh
obsidian-github-daily-issue-pr-preview \
  --evidence /private/scratch/<sha>.github-daily-evidence.json \
  --summary /private/scratch/<sha>.github-daily-grounded-summary.json \
  --format markdown
```

`--format json` emits a **versioned read model** (v2), with
`publication_authorized: false` and
`requires_human_quality_review: true`. The raw input Evidence is preserved
in its preexisting store; this command publishes nothing. Its output may
contain summaries and Issue/PR titles, so run only in an authorized private
operator context; do not route it to public logs.

Exit codes: `0` means the preview was constructed without ambiguous or
unresolved entities (still **not** accepted for publication); `3` means
cross-entity claims or substantive-but-uncited human comments need a separate
semantic judgment; `2` means source/identity/bounds validation failed.

## Identity and status controls

- The key is **`(verified repository, issue|pull_request, positive number)`**.
  `Issue #7` and `PR #7` never share a progress entry.
- A claim can contribute only if all its original `evidence_ids` belong
  to the same entity, which is determined by its GitHub event type and
  verified repository/number/URL—not by model-generated prose or references
  to issue numbers in a comment.
- A model claim citing two distinct Issue/PR entities (or one entity plus an
  unassigned default-branch commit) cannot be guessed into one. It is
  reported as **ambiguous** and blocks an otherwise clean preview acceptance.
- Only a **verified `pull_request_merged` Event inside the JST day window**
  establishes `merged`. PR review approval, a human `Merge Accept`, CI
  success and a plain closing event are not equivalent to merge.
- Pure `issue_snapshot`/`pull_request_snapshot` timestamp updates and
  unattributed default-branch commits do not establish an Issue/PR's
  progress. Explicit create/close/reopen/merge/ready/draft/PR-commit and
  review approved/changes-requested transitions can establish it.
- A nonroutine human Issue/PR comment or review that has no grounded
  qualifying claim is recorded as an **unresolved candidate**, not silently
  certified as irrelevant. Neither that nor an ambiguous cross-entity claim
  can be turned into a verified entity summary automatically.
- Unsupported model language claiming a merge or production deployment
  without original lifecycle/production evidence fails closed.

The final JSON has **no mutation flag**, stores only bounded target IDs and
grounded-claim references, and always returns
`publication_authorized: false`. The existing v1 `# Project Progress`
section-only CAS writer remains untouched and must not consume v2 preview
JSON as a `github-daily-progress` projection.

## Known limitations / next mandatory gates

This is an initial entity identity and deterministic lifecycle-status
contract, **not** proof of the semantics of every Issue/PR comment. It
reuses already-grounded v1 claims, which can span multiple entities; those
do not automatically become a valid entity-level account. A grounded claim
about a complex design discussion does not itself prove the update was
material, nor that the Japanese editorial account is sufficiently clear.

For a production v2 Daily: build an **entity-scoped candidate/progress
assessment** with exact Evidence closure and independently validated
grounding, full per-entity coverage/omission metrics (#290), Japanese
deduplication and prioritization (#283), approved v2 Summary and Projection
schema migration, compatibility with #288/#291/#292/#293 resume/atomic CAS,
and a reviewed manual date-specific quality canary. Only then, in a
**separately authorized** operation, allow a new exact-bounded
`# Project Progress` section update, followed by a separate explicit
Daily Timer rearm decision. Historic 2026-10-07 source/receipts and the
uncompleted 2026-10-08 chain must not be silently rewritten.
