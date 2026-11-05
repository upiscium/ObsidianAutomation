# #298/#481 — Obsidian Status Manager wireframe redesign

Status: source-only candidate, not deployed. Exact owner-supplied visual reference:
the three PNG wireframes included in the uploaded \`Page 1(1).zip\`
(\`Group.png\` top view, \`Group-2.png\` service detail,
\`Group-3.png\` connection-settings layout). The ZIP is a design reference,
not a runtime asset and is not checked into the repository.

The user's intended flow supersedes treating the previous \`/workflow-control\`
read-only observation page as the finished Dashboard:

1. Root \`/\` is **Obsidian Status Manager**. It displays six service cards
   in a 3-column desktop, 2-column tablet, 1-column mobile grid, closely
   matching the gray rectangles, visible Status and Settings buttons of the
   supplied reference. Five are the B1–B5 product workflows and one is the
   already implemented Vault Formatter. The sixth is a real, separately
   labeled service rather than a synthetic global-system "service."
2. Each **Settings** opens a service-specific detail screen with current
   status, configuration, scheduling, execution, last run, verified outcome,
   and next run where applicable. B2 Snapshot, B4 Daily and Formatter reuse
   the **existing** service-specific guarded operation panels, including
   real Run once / enabled controls. B1 AI health remains advisory; B3 and
   B5 are observation-only until their effect routes are separately accepted.
   B5 is manual import with no next run or enabled timer.
3. The top-right hamburger opens **Global System Settings** and direct
   links to Nextcloud Settings, Gitea Settings and GitHub Settings. The
   existing write-only connection forms and validation/save operations are
   moved into this screen without changing their handlers, endpoint
   semantics or stored credentials. Advanced systemd observations and the
   existing guarded operation-preview UI are retained in an expandable
   section instead of being discarded.

## What was changed in the candidate

- \`admin_dashboard.HTML\` keeps all original form IDs, event targets and
  legacy controls but replaces the *visible root* with the new shell. The
  original panels remain in a hidden DOM compatibility holder; when the user
  navigates to a detail/global view, the existing elements are moved to
  that view **without cloning**. Existing event listeners and guarded
  POST/PUT routes are unchanged.
- \`webui_design_v1/status_manager.js\` uses the existing strict
  \`ObsidianDesign.controlReader("same-origin-http")\` for B1–B5 observation
  and fixed same-origin GETs for already deployed B2, B4 and Formatter
  status. These GET results are for rendering only, not action admission.
  Job status can show green only for explicitly known verified outcomes,
  red for explicitly known failures; unknown, disabled, stale and
  Preview-only outcomes remain gray. Each job has a fixed accepted terminal
  vocabulary: a Daily response cannot use a Snapshot-only success status.
  A newer unknown attempt stays gray even if older history says success.
  The available action controls are
  solely the original, backend-guarded forms, never synthesized from
  workflow GET \`availableActions\`.
- \`webui_design_v1/status_manager.css\` applies the neutral visual
  language of the supplied wireframe; status indication uses both label
  and color, not color alone. Layout uses minimum 44-pixel touch targets,
  keyboard-visible navigation, a movable service Settings view and a
  mobile-responsive single-column grid.
- The two new static assets use **exact fixed GET/HEAD allowlist routes**
  in the existing dashboard. No generic static file-serving directory,
  new form submission route, external font, JS package, iframe, auth
  bypass, additional UI server, Node build step or root worker is added.

## Validation already exercised in the isolated source worktree

- Python/HTTP + UI compatibility tests verify the root shell, preserved
  form IDs, exact static routes, HEAD and CSP, and Host/Origin rejection.
- Existing B1–B5 pure model and live observation JS suites remain green.
- Chromium headless used a **synthetic local-only server on Adam**, with
  no production credentials, Git provider or Vault. It verified desktop
  3-column six-card overview, B2 Settings to live legacy control panel,
  hamburger to Global Settings with Nextcloud/Gitea/GitHub forms, B4
  Settings to its existing controls, B5 no-fake-action detail, and
  single-column mobile mode. Synthetic screenshots are under a private
  scratch state directory, not part of repository or user Vault.

## Explicit limitations and follow-ups

- This is not a claim that B1/B3/B5 are already connected, nor that
  historical successful evidence proves the service is currently healthy.
- The legacy root DOM and original root JS still initialize beneath the
  new shell to preserve B2/B4/Formatter operations. This deliberately
  favors minimal-risk UI relocation over rewriting control transports.
  Later cleanup can factor the legacy panels into shared components and
  avoid duplicate read-only status GETs.
- Global Gitea/Snapshot connection retains the current backend requirement
  to supply Nextcloud fields again when changing the B2 connection; this
  should be simplified only with a reviewed backend DTO/settings change.
- Setup Wizard, B1/B3/B5 effectful operations, stronger menu deep-linking,
  broader locale consistency and full fresh-LXC end-to-end acceptance
  remain separate from this source-only visual redesign.
- No production service update, commit/push/PR/Ready/Merge, credential
  change, schedule change, writer enablement, Vault/Gitea/GitHub Core
  mutation or existing Runner shutdown is authorized by this source-only
  worktree. The current deployed LXC continues to serve its old root UI
  until an explicit deployment gate.
