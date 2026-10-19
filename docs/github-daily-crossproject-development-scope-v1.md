# Daily v2: external PR development-scope quality (private, opt-in)

Tracking #386, stacked after Draft PR #391. **Source-only**, nonpublishing,
not Writer/Timer code. Parent baseline C5 is always executed regardless of
d1 Yes/No; this patch does not change baseline source selection or automatically
replace its text with a new summary.

## Observed failure cases (frozen 2026-10-18 GitHub snapshots)

- `upiscium/PROJECT-SUZKA#330`: a **test-only** PR for AgentState v10
  continuity/capacity preflight, adding only experiment/test files and expressly
  not modifying product code or real WAL. An earlier native baseline Gemma4
  summary wrongly said `機能実装は完了`. Only the preflight/tests had completed
  in that captured state. Do not conflate inherited P1b-A implementation
  or successful tests with product v10 implementation.
- `upiscium/TSUMGI#155`: an **opt-in** Nix Bonsai runtime implementation
  whose frozen source explicitly said `No merge, new release, or change to
  running GA services without explicit maintainer approval`. The prior
  feature-first native summary included the new backend but omitted that
  approval/running-site condition in favor of a reviewer-timeout detail.
  This is not a generic CI or review omission.

Historical PR snapshots must not be silently reinterpreted using their later
GitHub states. In particular, #330 was subsequently closed as superseded
after integration through SUZKA PR #333, and #155 has since been merged.
Neither later GitHub state by itself proves that the feature was deployed
into its running product.

## Change

1. The opt-in **feature-first** source projection retains exact original
   source_ref, field, character range and SHA256 for a running-GA-unchanged
   or no-production-promotion limitation, and prioritizes that limitation
   over a later review/CI-only update. This is a small source presentation
   priority rule, not a global contradiction solver, live-state observer or
   removal of complete original Collector CAS/Grounding.
2. The paired comparison now emits a bounded, **non-authoritative** scope-risk
   audit for both baseline and feature-first output. For an original
   test(...) / TEST ONLY PR that explicitly says product code is unchanged,
   it flags false product feature-completion statements. It also flags a
   summary that omits an explicitly sourced no-site-promotion caveat.
3. No risk flag modifies model text, replaces an existing C5 output,
   selects the winner, suppresses any entity or original source, or grants
   publication/merge/deployment authority. `no_known_scope_violation_detected`
   is **not** a semantic quality PASS or proof that a site is still unchanged.

## Verification and open gates

- Deterministic regression fixtures for both original Project PR patterns,
  source-ref binding, sample/quote exclusion, and invalid source/ref rejection.
- Independent native A/B output and corrected prompt/view replay are
  exploratory **model quality observations** until audited against exact C4
  source and original Grounding, with separate-day/other-repository
  independently labeled holdout.
- Where original material is truncated, ambiguous, contains a single
  source line beyond the 600-character status-excerpt limit, or includes a
  later contradictory operational assertion, mark the private audit as
  requiring human review rather than treating a missing model-visible quote
  as proof that no production caveat exists. The complete original C4 is
  unchanged.
- C4's selected PR title may still exist when the PR-created event belongs
  to an earlier day. Use this title solely as a test-only *risk-detection
  hint* alongside actual original test-only/no-product-change statements;
  never treat the title as evidence that the product was implemented.
- Keep all parent Draft PRs unmerged, d1-3B Primary unchanged; never
  touch TSUMGI installed profile, Clef, Writer, Timer, Vault, Nextcloud,
  production LXC or publication without separately approved gates.


## Narrow corrections from independent source review (2026-10-19)

The private source audit handles explicit Japanese `テストのみ` (with original
`製品コードの変更なし`) and flags vague `このPRの実装が完了` on a test-only
PR as **scope ambiguous** requiring human review, without asserting that the
product was actually implemented. The site-scope lexer now admits explicit
`running GA services remain unchanged` while preserving the existing
original-source role and quote/fence checks. Markdown illustration headings
like `## Example output (mock)` are treated as illustrations, not direct
deployed-site evidence. An affirmative `デプロイ済みで本番稼働中` cannot
satisfy a no-deploy omission check and is separately flagged as a
source-conflicting status if the original no-promotion caveat is active.
Conversely, `この実装はまだマージ、デプロイ、公開されていません`
retains its nondeployment meaning in the private comparison.

These are intentionally bounded heuristics for observed failure classes.
They are **not** a generic contradiction solver, verifier of running system
state, universal Japanese paraphrase recognizer, or production acceptance
gate. Ambiguity always remains eligible for full-original human review.
