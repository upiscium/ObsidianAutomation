# Daily v2 source-status fences: final source-only edge hardening

**Draft / nonpublishing, child of PR #388 → #387; no main merge.**

This small opt-in change addresses exact-head, source-only independent
Correctness/Security P2 findings from #388
(ae2efd3abb960e5f666f81bfddb21d81458b42ce).

The private feature-first C5 receives direct original-source operational
status quotes. It must not promote sentences from *examples* or from
Markdown *code blocks* to real production state updates. Original
Collector CAS, C4 source refs, Grounding and baseline C5 remain untouched.

## Previous errors

1. Markdown headings "Example output" and "Model reply" without a colon were
   not treated as illustrative headings. A following apparently standalone
   production worker-installed line could suppress an earlier actual
   worker-not-installed statement.
2. Markdown fenced blocks of four markers could be closed erroneously by a
   nested three-marker line. Example worker-installed text between the
   shorter inner marker and the proper outer closing marker could be
   promoted as a real state transition.
3. The label "Current status: Sample output:" began with a recognized
   operational heading prefix but was itself a composite **example** label.
   A prefix-based check could give its next line actual-status authority.

## Narrow implementation

- Match known example/model/reply/state-excerpt headings even without
  a colon. Unknown colon-ended labels remain conservatively illustrative.
- Admit an operational heading only on a **full exact label**, not a
  prefix. The next standalone line is still only an original source
  report, not independent proof of a production change.
- Keep the opening Markdown fence character and marker count. Only
  the matching marker character with at least the opening length can
  close it, and only on an otherwise-empty closing line. Entire fenced
  code blocks, including shorter nested markers and headings, remain
  excluded from optional status candidates.
- A genuine later report outside a properly closed fence, after a blank
  paragraph or under its own exact actual/current/production-status
  heading, can still supersede older same-scope negative source hints.
- Original full C4 and baseline/Grounding retain *every* source event and
  its complete original text; no source is deleted. Optional projected
  hints have no Writer or deployment authority.

Tests cover all three concrete reviewer reproductions, both backtick
and tilde nested fence marker lengths, subsequent legitimate status
outside a correctly closed fence, real operational headings and
baseline relevance/source safeguards. No general semantic contradiction
solver is added.

Human-reviewed other-day/project semantic quality, Daily Writer/Timer
production rollout and parent Draft merges remain separate approval
gates. d1-3B remains Primary System One; Clef and three-model production
co-residency stay deferred.

## Exact-head source review follow-up

The first independent read-only correctness/security reviews of this child
Draft identified two additional P2 escaping cases: a composite heading
such as "## Current status: Sample output" without a terminal colon and
a valid ATX Markdown heading such as "## Example output ##" with closing
hash markers. Both could previously expose illustrative worker-installed
text as a direct operational update.

The source parser now strips optional closing ATX marker runs from headings
and recognizes known illustration phrases even inside composite labels,
regardless of the final colon. Legitimate exact operational headings such
as "## Actual status: ##" still terminate illustrations and admit an
independent following original source report. This was verified with
additional source-only negative and positive regressions. Neither model
scores nor this parser can assert a real site deployment, delete CAS text
or authorize Writer publication.

A second read-only Security reviewer of the follow-up source found that a
blank line immediately after an illustrative heading (but **before** the
example body) could prematurely terminate the example and accept a fake
worker-installed line. The parser now tracks whether an example has
actually had a payload line or fenced payload. A blank before its first
payload remains inside the illustration; a blank *after* its payload can
end the example and allow a later genuine source status report.
Targeted negative and legitimate positive tests cover both cases.
This remains conservative optional source presentation only, not a
mechanism for changing the original event or claiming actual deployment.
