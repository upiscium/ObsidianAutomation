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


## Exact-head source reviewer corrective cases

The first independent read-only source reviewers of the combined Draft
`c03790b` found two **P2** illustrative-status escape paths:

1. An exact `## Actual status:` appearing immediately after an illustrative
   heading, *before any example payload*, could incorrectly end the example
   and promote an enclosed fake site-update sentence. The consolidated
   scanner now requires an actual example payload before treating that exact
   status heading as a new real source section. It still admits genuine
   explicit status headings *after* an illustrative payload, preserving both
   siblings' positive cases.
2. An HTML comment containing `--> <!--` on one line could close and reopen
   in a way the prior boolean scanner did not track; the next hidden
   `Running GA services have now been updated` statement became a false
   candidate. A bounded token-order state machine now handles multiple
   open/close markers on each original line. This does not provide a
   semantic or security certification for arbitrary malformed Markdown.

New regressions include these negative escape cases and positive legitimate
updates after the example paragraph or actual closing HTML-comment boundary.
The original C4 source remains complete. Source-only independent re-review
and exact-HEAD full-suite CI are required for the corrective commit, not
inferred from the earlier PASS results.
