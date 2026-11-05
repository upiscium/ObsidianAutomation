# Issue #526: Dark Status Manager and native mobile Back gesture

Status: isolated source-only candidate. **Not published or deployed.**

The owner completed PC and smartphone acceptance of the existing
Obsidian Status Manager layout and basic controls on 2026-11-05.
Two follow-up requests are scoped here: a black/dark instead of white
base palette and use of mobile edge-swipe Back from Service Settings
and Global System Settings.

## Native navigation, no custom touch handler

The root remains the single-page manager. Each in-page transition
records a same-document, versioned History API entry. Browser/OS native
Back or Forward traverses these entries and reconstructs the right view.

- Home -> B2 Settings -> native Back goes Home; Forward reopens B2.
- B2 Settings -> Global Settings -> Back returns B2.
- The visible "サービス一覧に戻る" button returns to the recorded
  Home entry without creating a new Home history entry or Back loop.
- Reloading from Settings restores that exact Settings view.
- New navigation after browser Back discards stale Forward entries.
- Leaving Global Settings clears unsaved password fields, including
  transitions from native Back and Forward.
- Invalid historical app states resolve to Home. If History API is
  unavailable the existing button-only navigation remains usable.
- No manual touchstart/touchmove handlers are installed. Browser and
  OS edge gestures remain authoritative, avoiding duplicate swipes,
  scroll interference and selecting text.
- From Home, normal external browser Back remains unrestricted.

No URL routing change, Job API, OAuth flow or deep-link interface
was introduced. Real iOS/Android physical gesture acceptance remains
for the user after a separately approved production deployment.

## Black/charcoal palette

Existing 3/2/1 responsive grid, card dimensions and legacy operator
controls are unchanged. Explicit dark colors cover the full root app:
background, cards, nav menu, service details, Setup Readiness,
Nextcloud/Gitea/GitHub forms, form inputs and embedded B2/B4/Formatter
panels. Light text contrast and green/red/gray/running signal hues
retain explicit text labels. The theme is dark even with an OS light
preference; there is no cross-page dark-mode toggle.

The independent read-only /workflow-control diagnostic page has
separate assets and is not re-themed by this change.

## Evidence and boundaries

- Node history suite invokes the actual manager JS in a fake DOM and
  checks Back/Forward, nested navigation, Home return, reload,
  invalid state, secret cleanup and no effect requests.
- Python tests check dark palette contrast, unchanged exact 3/2/1
  layout, asset route and no newly introduced POST/PUT.
- Isolated localhost Chromium with no Job/credential/Vault access
  validates desktop/tablet/phone 1450/800/390/320 widths, navigation,
  reload in a nested view, form and status styling, and dark appearance
  under both OS light/dark preferences. Fixture-only HTTP 503s mean
  expected unavailable read-only Job data, not production failures.
- Original Snapshot/Daily/Formatter API/DOM controls and setup
  connection forms are retained. No backend jobs, credentials, Vault
  files, Gitea/GitHub effects or systemd services are changed here.

Next gates are owner-approved Commit/Push/Draft PR, exact-head CI,
review, separate Ready/Merge, a separate UI-only rollout preserving
the live backend Writer-gate differences, and owner phone validation.
