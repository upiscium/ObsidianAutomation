# Daily v2 — d1-3B exact-line shadow experiment (2026-10-18)

**Private / opt-in / no deployment.** Child experiment of Draft PR #383 and Issue #381. Existing d1-3B Primary remains, Clef-Flash deferred.

## Source and runtime

The verified October 16 GitHub CAS (87 source events, 10 Issue/PR entities, capture SHA256 `043120ee2412fd01024c14ca37b7f1829ee4402c61cefcaf69d820b0592c964a`) yields 241 original paragraph spans and 383 linewise spans. All linewise fragments have exact source_ref / evidence_id, original field SHA256 and source character offsets. Same-commit-SHA provenance is retained. Blank separators remain in complete C4 source and are not model-classified. Original lines exceeding 520 characters are not silently truncated; they become pending on d1 request construction. The installed TSUMGI d1 Profile is still limited to 128 state tokens and is the final token admission authority.

40 preselected short line spans across Issue #300 and PR #311, #367, #373, #374 were submitted via the **existing managed TSUMGI Control → Worker → d1-3B** serving path. Outcome: **40/40 HTTP 200, 19 recommendations, 21 policy abstentions, 0 HTTP errors**. Advisory choice scores remain uncalibrated. Full CAS retained; zero source deletions or C5/Writer/Timer authority changes.

## Exact same-CAS C5 comparison

A follow-up C5 native run on PR #367, #373 and #374 using the pinned Gemma 4 model digest `3721c8128dc2e6cabed512039c22f63629c0ad6786b0fc852a9e4da47a6953f3` and private d1 advisory view completed with original Grounding. #373 and #374 foregrounded feature work and explicit nonlive caveats; **PR #367 still foregrounded CI/reviews and omitted the actual audited OperationBroker core implementation.** Therefore this experiment **does not meet the C5 quality improvement gate**. More classifier segments alone are insufficient; a later source-first editorial policy experiment is needed. No artificial post-hoc rewrite or auto-source filtering was added.

## Implementation limits

Pure opt-in `build_line_priority_plan` subdivides the already-verified d1 C4 source plan only along exact original newline boundaries. The original field and offsets remain the authority. Any unrepresentable long line or model abstention stays pending; the partial original text cannot itself prove a milestone. Review/model abstention cannot authorize source exclusion, publication or merge. This small, one-day evaluation is not a general relevance benchmark.

Private source, actual managed requests and C5 model/grounding receipts on Adam: `~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-D1-LineShadow-20261018/`. Do not upload original GitHub text to public GitHub.

## Independent source-review finding and correction

The first Correctness review found that the *parent* d1 paragraph projector
could already split a single long physical line at 1500 characters. A short
remainder containing e.g. "not installed" could then be incorrectly
classified as an independent complete line in the new linewise view. This was
remediated before accepting this Draft: contiguous parent source spans in the
same original field are now conservatively grouped, and **all** members of a
group containing a 1500-character parent are marked unsafe/pending. This
ensures no isolated suffix is promoted after an arbitrary earlier cut. Tests
reproduce the 1500-character prefix plus short negative suffix failure.
The 2026-10-16 40 selected short line fragments remained unchanged under the
fix (none newly unsafe), so the previous native observations still bind to the
byte-identical fixed private line plan; qualitative C5 limits also remain.

Second independent Security review identified a whitespace-gap edge case:
the upstream original paragraph projector can omit whitespace-only spans
between two parts of the *same original physical line*. Merely checking
contiguous parent offsets would allow a short final 'not installed' suffix
to be classified independently. Because the projected plan cannot prove
the missing gap contains a line break, the final implementation conservatively
flags **every segment of the same original field** when any parent reached
the upstream 1500-character hard split cap. Such segments stay pending.
The regression includes a 1500-character prefix, a 1500-space gap, and a
short negative suffix. On the saved 2026-10-16 fixture this correction did
not alter the 40 previously sampled fragments or the canonical 383-line
plan SHA, but it remains a meaningful structural safety rule for other days.
