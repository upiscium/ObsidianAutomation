# C5 latest-material-milestone editorial experiment (opt-in)

Motivation: the pinned Gemma 4 native 2026-10-16 private Daily covered
10/10 updated Issue/PR entities but the Issue #300, PR #311 and PR #374
Japanese summaries underweighted the last material implementation and
non-deployment gates. This is a source-selection and editorial-priority
problem, not a request for universal contradiction resolution.

An explicit new C5 editorial contract, c4-sentences-latest-milestone-v2,
retains the existing source-bound C4 context, same 3-sentence JSON shape,
original source refs, strict host-side normalization, and independent C5
post-edit Grounding. It adds a versioned Japanese instruction to prefer
the last substantive verified progress milestone and the related explicit
not-merged/not-installed/not-live qualification over older generic scope
descriptions or detailed test counts. It must not invent a latest milestone
when source evidence is absent. The existing sentences-only and legacy
contracts are unchanged, and this mode is NOT enabled by default.

The contract is a prompt-level hypothesis, not a deterministic ranking
or proof that the model will obey it. Native A/B on the archived real
Issue #300 / PR #311 / PR #374 plus independent review and completeness
measurements are required before a rollout decision. This experiment
must not mutate main, Writer/Timer, Vault, Nextcloud, or production data.

Original private evidence: Adam
~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-20261016-PR379/
