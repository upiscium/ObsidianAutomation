# Issue #530 — simplified Global Settings menu

Status: source-only candidate; not committed, pushed, merged or deployed.

## Owner feedback

The owner accepted dark appearance and the native mobile Back/Forward
operation of the currently deployed #527 Obsidian Status Manager on both
PC and phone. The owner then requested that the Settings navigation
show **only Global System Settings**, without separate shortcuts for
Nextcloud, Gitea and GitHub.

## Minimal change

The top-right hamburger now exposes only these two navigation entries:

- Service overview — non-Settings route to the current dashboard Home.
- Global System Settings — the sole Settings entry.

The former Nextcloud Settings, Gitea Settings and GitHub Settings
shortcuts are removed **only from the menu**. Their underlying sections
and existing forms remain inside Global System Settings, in the same
order, with existing validation, token-saving and write-only password
behavior unchanged. The separate optional Setup Readiness panel remains.

The menu's JavaScript handler is simplified to route those two existing
choices without the now-unused provider-specific scrolling behavior.
Browser-owned History API Back/Forward, Home return, native mobile edge
swipe, dark theme, responsive 3/2/1 layout and status display are
preserved. No new API endpoint, credential transport or job admission
path is introduced.

## Validation and next gates

Five targeted Python checks verify exactly two menu destinations,
retained forms/controls, unchanged backend AST, no new mutation
transport, and execution of the existing 14-case History API regression.

Full UI/Job regression tests are required before publication. Exact
main/HEAD review and any actual deployment require distinct
authorizations: Commit/Push/Draft PR, CI/independent review, Ready,
Merge, and production UI-only cutover.

No Nextcloud, Vault, GitHub, Gitea, Runner or systemd changes are
authorized by this source-only menu simplification. #298 and #304
remain open for broader workflow integration and Setup Wizard work.
