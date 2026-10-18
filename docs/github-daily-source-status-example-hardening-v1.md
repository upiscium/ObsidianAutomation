# Feature-first C5 — example/status provenance guard

Tracks Issue #386, stacked on Draft PR #387. No production changes or source deletion.

The independent source-only follow-up review on #387 exact HEAD 1591432 identified two additional P2 risks. First, an unfenced `Example output:` paragraph followed by a standalone installation sentence could wrongly supersede a real not-installed worker blocker. Second, a line saying that a worker is installed on GitHub Actions could be interpreted as production installation.

The private original-source projection now excludes explicit unfenced example, generated output, sample output, and expected-output blocks until a blank line or Markdown heading; source events remain in C4 CAS/Grounding. It also treats direct GitHub Actions, workflow and Actions-runner references as nonproduction context. The original-CAS data, d1-3B Primary Profile, baseline C5, installed Worker, Writer/Timer and merge state remain untouched.

New tests cover seven example/CI false positives and confirm a real status report after an example's terminating blank line can still supersede an older negative in the matching operational scope. This is a conservative source-display safeguard, not a general-purpose semantic contradiction system or evidence that production changed. Human-reviewed other-day source quality and acceptance remain necessary.

A second independent Security source review found that Markdown headings such as ## Example output: were not recognized as example introducers. The parser now handles explicit example headings and still allows an ordinary subsequent Actual status heading to end the example block. Five additional negative/positive heading regressions were added. Original sources and baseline C5 remain intact.

A final Security source review identified a still broader case: arbitrary colon-ended source labels such as State excerpt: or Model reply: could introduce an unfenced, apparently standalone installation claim. The new policy conservatively treats unrecognized colon-ended headings as illustrative until a blank/new heading; only explicitly recognized Actual/Production/Current status headings can make the following line eligible. Eleven adversarial/true-status tests were added. This is still advisory input selection, not a truth assertion or deletion of original C4 evidence.
