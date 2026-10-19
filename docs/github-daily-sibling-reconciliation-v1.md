# Daily v2: reconcile sibling Draft #390 and #391 (source-only)

This is a **private nonpublishing integration candidate**, not a production
change or an approval to merge a parent Draft. Source branch starts at
`feat/389-citation-boundary-guard` (PR #391), accepted head
`b446cd016207721d8a92fc4d80dec0730b0323f7`; it incorporates sibling
`feat/389-status-hint-fence-boundaries` (PR #390), head
`43a9589be0ac0775d7de01f093f27f2ea5bf28fc`.
Both diverged from parent `ae2efd3abb960e5f666f81bfddb21d81458b42ce`.

## Conflict resolution

`github_daily_feature_stage._status_source_lines` is deliberately
**one combined lexer**, not two disjoint passes that would disagree about
whether a line in an example is authoritative. Preserve all of these:

- #391's Markdown blockquote/quoted-line exclusion, HTML-comment exclusion,
  original source refs, full-original C4 retention and advisory-only site gate
  prioritization; its arbitrary-heading-within-an-example behavior remains
  conservative.
- #390's Markdown fence marker family and opening length requirements; inner
  shorter markers and language-bearing lines cannot terminate a longer fence.
- #390's illustration recognition with/without colons, composite status plus
  sample labels, optional ATX closing hash markers, and a blank *before* the
  first sample payload never terminating the sample.
- Real original source updates after a valid fence, a blank following an
  actual example payload, or a genuine *exact* status heading may still be
  reported as later operational state. This report is not a live-site probe.

The existing regression suites from **both** siblings are carried forward;
additional integrated cross-boundary regressions test combinations of sample
headings, code fences, Markdown headings and HTML comments, as well as
legitimate subsequent source reports. No C4 event is dropped or rewritten.

## Integration gates

Source-only focused regression, full independent Linux regression and Canary
tests, exact-head hosted GitHub CI, and read-only independent Correctness and
Security review are separate evidence. If any finds a regression, keep this
candidate Draft. Model semantic recall, cross-day/project human adjudication,
full C4/CAS-Grounding replay, Writer/Timer and production are **outside** this
source-only merge acceptance. Keep both original sibling PRs untouched pending
their owners' explicit disposition; never double-merge conflicting siblings.

No source-only status heuristic authorizes deletion, summary publication,
current production-state assertions, Vault/Nextcloud/LXC updates or a
`main` merge. The parent #391 remains Draft/unmerged until separate approval.
