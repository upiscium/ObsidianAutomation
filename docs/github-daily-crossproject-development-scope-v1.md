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
- Where original material is truncated, ambiguous, or includes a later
  contradictory operational assertion, prefer explicit uncertainty/human
  review over silently certifying current operational status.
- Keep all parent Draft PRs unmerged, d1-3B Primary unchanged; never
  touch TSUMGI installed profile, Clef, Writer, Timer, Vault, Nextcloud,
  production LXC or publication without separately approved gates.
