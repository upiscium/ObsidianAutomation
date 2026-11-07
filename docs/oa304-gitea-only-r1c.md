# #304 R1/C — Gitea-only Setup Form using saved shared Nextcloud (source-only)

Status: **source-only Draft PR publication authorized**; no Ready, Merge, deployment or service activation.

## Current-state correction

The earlier R1/A documentation assumed a Gitea Snapshot form needed the full combined eight-field B2 POST. This is **outdated for current main**: the backend already accepts a Gitea-only payload with exactly git_url, git_user, git_token and branch provided a validated private Nextcloud source has already been saved. It internally reads the canonical Nextcloud source and uses the existing bounded, CSRF-protected POST and read-only connection probes.

R1/C aligns the UI with the *existing* server contract. It does **not** introduce a new credential API, token readback, cross-form password copy or storage, write authority, or scheduler/Job path.

## UX scope

Under **Global System Settings**, the shared Nextcloud form continues to capture the four Nextcloud connection fields. The optional Gitea Snapshot form now contains only the four Gitea destination fields, a fixed-text prerequisite status, and a Save button initially disabled until the existing redacted Nextcloud connection GET reports configured.

- Missing/invalid/unavailable Nextcloud status blocks Gitea Save and explains the prerequisite.
- Saved Nextcloud credentials are reused server-side; **configured is not current live connectivity**.
- A stale older status request cannot re-enable Gitea after a newer missing/failed observation.
- Initial checked status and in-progress Gitea save state both affect Save availability.
- Duplicate Gitea form submissions remain blocked. Existing save, validation, CSRF, and status-refresh routes remain unchanged.
- No new automatic remote health probe, Job activation or Vault effect is introduced.

## Technical verification

tests/test_gitea_only_guide_ui.py asserts the original Nextcloud/GitHub forms remain available and that the Gitea form contains only four Gitea fields with a disabled-by-default Save and accessible prerequisite status. A controlled Node VM executes extracted **real inline JS** against isolated synthetic status responses: missing/unavailable/invalid shared Nextcloud, saved status, stale in-flight status, four-field Gitea POST, and duplicate-submit protection.

The existing tests/test_nextcloud_only_setup.py and tests/test_301_gitea_only_toctou.py cover the underlying Gitea-only POST behavior and source-rotation race with disposable stores and synthetic providers. The #530 guard retains its exact #301 source-code normalization and adds precisely bounded R1/C inline-JS normalization, asserting all other Python AST nodes and other inline JavaScript remain unchanged. The client test also covers failed Gitea POST recovery without form reset or secret echo. Real Nextcloud/Gitea I/O is not performed by this source prototype.

## Dependency on merged #301 TOCTOU correction

The backend race previously identified in #301 was corrected in merged [PR #550](https://github.com/upiscium/ObsidianAutomation/pull/550) (merge commit `a6019e8832790e5c137719f6e5b2d533f149e7fd`). The four-field Gitea-only handler now reads the canonical saved Nextcloud source **inside** its existing Writer-gate / Snapshot / Daily / Formatter lock interval, after busy checks. Its deterministic local concurrency regression originally failed before that correction and passed afterward. The R1/C candidate was reapplied from a separate uncommitted worktree onto that exact merged main, preserving the #301 backend changes.

R1/C modifies only the existing HTML and two bounded inline-JS regions, not the handler or its security properties. Its tests assert the corrected lock order. The historical #530 integrity guard is retained: it first reconstructs the exact known #301 code change, then normalizes only the two R1/C inline-JS regions and compares all remaining Python AST nodes and other inline JavaScript against the frozen baseline. The local concurrent test does **not** prove real remote, multi-process or crash recovery behavior.

This slice does not complete AI Provider setup (#301), Vault root/binding selection (#303), full Setup Wizard (#304) or fresh 1-LXC B1–B5 MVI (#298).

## Authorization boundaries

The owner explicitly authorized the bounded five-file Commit/Push/Draft PR publication. This does not authorize Ready/Merge, obsidian-control rollout/restart, credential mutation, Nextcloud/Vault/Gitea/GitHub write, scheduling, Run Once, or unrelated effects. Each later gate requires separate approval.
