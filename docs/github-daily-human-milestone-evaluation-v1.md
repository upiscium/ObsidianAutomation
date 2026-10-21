# Daily v2 — independently adjudicated material-milestone evaluation

Issue: [#386](https://github.com/upiscium/ObsidianAutomation/issues/386).
Base: parent [Draft PR #391](https://github.com/upiscium/ObsidianAutomation/pull/391)
at `4640b581a196e9b86251098eaf76e3ed8947117a`.

This is an **opt-in, offline research/evaluation capability**, not an
operational Daily feature, quality gate, Writer/Timer authority or a source
of production changes. It does not import or depend on the installed local
model, Nextcloud, private Vault, or GitHub network. It never edits the
original C4/CAS/Grounding model records.

## Why a second judgment is necessary

A Grounding `supported` result says that the sentence *can be read as
source-supported*. It does not mean that the sentence is an important
change, that it describes the *latest* state of the Issue/PR, or that
the summary covers all the day's important milestones.

In the frozen private 2026-10-18 GitHub CAS from TSUMGI and
PROJECT-SUZKA, a source-grounded PR #155 summary omitted its own
later merge/GA caveat while a model-facing synthetic whitespace-only
snapshot change still received source-citable text. **These are distinct
recall/precision failures, not evidence of source corruption.**

## Manual annotation protocol

Open the private, noncommitted Adam file:

`~/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-Acceptance-20261020/provisional-recall-2026-10-21/human-adjudication-scaffold.private.json`

It contains six original-CAS Issue/PR identities, 12 **assistant-proposed,
not independently adjudicated** milestone candidates, and 18 frozen C5
sentences across 11 actually executed route/entity combinations.
The source excerpts and original model response contents remain in
private ResearchEvidence. No screenshot/text of private GitHub source
is moved to the public repository as part of this work.

An authorized independent reviewer must:

1. Verify every source milestone from the **full original C4/CAS event**,
   not only a cropped commit title. Record whether original evidence
   actually supports it and whether it is important, routine or uncertain.
   Snapshot dates and commit creation times must **not** become a claimed
   same-day push/deploy. Proposals are not authoritative.
2. **Add missing important milestones** rather than simply rubber-stamp
   all 12 hypotheses. In particular inspect merge transitions,
   unfinished GA/site gates, nonfeature fixes, experiments, decisions and
   existing pending events. Set `inventory_complete=true` only after
   independently checking the full selected source scope.
3. For every model-generated sentence, inspect the full sentence and
   original quoted refs, post-edit Grounding and its date/subject.
   Set `semantic_verdict` to `material`, `routine`, `unsupported`
   or `uncertain`; label its actual milestone coverage separately.
   Source-ref overlap is **necessary but never sufficient** to
   establish semantic coverage.
4. Only after actual human review, fill `review_status=adjudicated`,
   meaningful rationale and `reviewer_type=human`. A helper script
   cannot verify a claimed reviewer's independence or identity.
   Never auto-convert a preliminary model judgment into a human label.

The model's two summary routes are distinct. `baseline` is always
eligible. An absent `feature_first` attempt is marked *not applicable*
in the entity inventory and **must not count as a missed feature-first
milestone** or synthetic 0-sentence output. Compare the two only on
entities for which both actually ran.

## Evaluator and interpretation

Run inside the repository Nix environment:

```bash
nix develop --command python -m obsidian_automation.github_daily_milestone_eval \
  "$HOME/Documents/ResearchEvidence/ObsidianAutomation-DailyV2-Acceptance-20261020/provisional-recall-2026-10-21/human-adjudication-scaffold.private.json"
```

This JSON-only tool validates bounded input, entity identities,
original-source indices, source-ref scope, a complete route/entity
inventory and explicit milestone-to-sentence links. Cross-Issue/PR
matches, unsupported sentence claims and fake target attribution fail
closed. Raw source is never needed as a CLI parameter.

On **complete, human-self-reported annotations**, for each route:

- *Material milestone recall* = number of independently labelled,
  original-source-supported material milestones that a correctly
  attributed post-Grounding-supported sentence covers, divided by
  the total labelled material milestones in that route's applicable scope.
- *Post-Grounding material precision* = count of supported sentences
  separately judged material and tied to labelled milestones, divided
  by all Grounding-supported sentences in the route scope.
- *Over-inclusion* = Grounding-supported but independently judged
  routine sentences, reported separately from rejected unsupported
  sentences. An abstention with pending source is not proof of no progress.
- *F1* = harmonic mean only when both component ratios have a valid
  nonzero denominator. Undefined denominators are emitted as `null`.

No aggregate score is accepted with pending/uncertain labels,
incomplete Issue/PR coverage, missing route outcomes, or reviewer
type `model_provisional`. The only completed-status wording is
`ADJUDICATED_INPUT_SCORED_NOT_PRODUCTION_CERTIFIED`: this is
**self-reported human annotation**, not verified reviewer independence
or production qualification. Both the report's
`semantic_acceptance_automatically_granted` and
`publication_authorized` are always **false**.

## Evidence and current status

The current local scaffold correctly returns
`NOT_HUMAN_ADJUDICATED_OR_INCOMPLETE` and
`semantic_acceptance_automatically_granted=false`.
Actual human reference labels have **not** been obtained.

The separate 12-milestone lexical proxy in ResearchEvidence was
generated from assistant-proposed labels and is **not** independently
human-labelled Recall. Attempts at a read-only OpenCode review of the
private data were blocked by the Reviewer's external-directory
permissions. Do not bypass private source access restrictions by
committing raw source excerpts, weakening GitHub security or
conflating an external model review with human verification.

Known separate model-output limitations include #155's omitted merged
status/GA caveat and #158's unsupported feature-first `profiling`
wording; these are not solved by a measurement tool.

## Safeguards and release gate

This capability has no consumer in the current Daily runtime.
It does not modify source-view inclusion, baseline execution,
feature-first fallback, C4/CAS citation validation, post-edit Grounding,
installed models, any Worker/Writer/Timer, Vault/Nextcloud, or LXC.

Source-only tests must pass, independent Correctness/Security source
review must be completed, and any integration into Draft #391 requires
separate maintainer authorization. To ship semantic-quality assertions,
an independent **human** label set and actual representative
cross-day/project end-to-end/model evaluation remain mandatory.
