# #444 — Grouped Project Progress index (deterministic v2)

## Motivation

The original B4 prose consisted mostly of generic sentences such as
"commit recorded", "review occurred", or "PR merged". The owner found these
sentences too low-information to justify Daily Note space. First prioritize a
scannable, linked activity **index**; natural-language summaries can later
replace the display label without disrupting event attribution or Writer.

## Exact Markdown surface

The Writer continues to own the sole existing \`# Project Progress\` H1.
B4's deterministic renderer supplies only its body:

\`\`\`markdown
## owner/repository
### Closed Issues
- [Issue #2](https://github.com/owner/repository/issues/2): Issue title
### Updated Issues
- [Issue #3](https://github.com/owner/repository/issues/3): Issue title
### Merged PRs
- [PR #4](https://github.com/owner/repository/pull/4): PR title
### Updated PRs
- [PR #5](https://github.com/owner/repository/pull/5): PR title
\`\`\`

- Repository sections sorted by repository key. Within each repository,
  categories use the fixed order above, numbers sorted ascending. Empty
  categories are omitted. Zero activity retains the prior one-line empty-day
  message, with no empty heading.
- Closed Issues: the **last dated typed Issue lifecycle transition** was
  \`issue_closed\`. An Issue reopened after closure is in Updated Issues.
- Updated Issues: an admitted material Issue update other than a final
  \`issue_closed\` (comment, creation, reopen, etc.).
- Merged PRs: **typed** \`pull_request_merged\` event, independent of Draft,
  review commentary or a reported intention to merge. It wins over all
  other PR activity that day, so a merged PR is not duplicated.
- Updated PRs: admitted material PR activity without a typed merge
  (commit, substantive comment, review, creation/reopen, unmerged closure).
  A closed-but-unmerged PR is **not** labeled "Merged".
- Each visible record is a single canonical GitHub Issue/PR link plus a
  source title display label. Nothing is inferred from \`Next\`, generic
  compliments, bot noise, or lack of an event.
- The title is selected from the most recent **nontruncated, nonempty,
  CAS-recorded title envelope for that entity**, if available. This is
  a label observed at source capture, not a claim about the moment the
  title was edited. If no title exists in the immutable original source,
  display \`タイトル未取得\`; do not pretend a commit subject or comment
  was the GitHub Issue/PR title.
- Rendered Markdown escapes untrusted title Markdown/HTML characters,
  normalizes control/newline characters, bounds labels and total bytes.

## Data preservation and later summary expansion

The same \`items[]\` records retain \`entity\`, \`status\`, \`sentences\`,
\`evidence_ids\`, \`source_event_count\` in the deterministic JSON receipt.
The title field is the replacement display slot; a later evaluated summary
can supply a different source-grounded display field without changing
which entities belong to which category. Do not use an LLM today.

The deterministic receipt version is bumped from
\`github-daily-deterministic-digest-v1\` to \`...-v2\` because Markdown
rendering and the title field semantics changed. Original GitHub CAS
capture/evidence/receipt bytes remain immutable and unchanged. Old v1
preview artifacts and canonical Daily Notes are not edited or migrated by
this source-only change. Regeneration intentionally yields a new Digest SHA
and new create-only Preview file names.

The first real 2026-10-24 Standard source (41 rendered entities) carried
only 29 nonempty Issue/PR title envelopes across its 41 entity groups.
Missing 12 titles require a **separate, provenance-preserving GitHub source
enrichment design** if completeness is needed; no network lookup from the
pure renderer and no unsupported invented title. The current branch does
not introduce a new title-fetching runtime path or new credentials.

## Operating boundary

Changes are limited to pure presentation and offline tests. Existing
previous-JST-day source collection, CAS admission, B4 timer, nonroot service,
strong-ETag/If-Match section-only Writer and real Vault remain unchanged.
The previous authorized live 2026-10-24 Daily Note already contains the v1
rendering; it is NOT retroactively rewritten in this branch. Deploying v2
and replacing a canonical Daily section requires a separate production
review and authorization. Old generated v1 receipts do not become v2
publication evidence.
