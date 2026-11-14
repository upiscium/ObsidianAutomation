# #303 Phase 2 / P2-0 — role folder inventory

**Status:** P2-0 (#584). Inventory as of `main` `e211fcc` (2026-11-13).

## What P2-0 changes

- **One accessor:** `src/obsidian_automation/vault_layout.py`.
  `role_folder(role)` returns today's folder for each role.
- **No behaviour change:** there is no mapping record yet, so every value
  equals today's name. `tests/test_303_p2_0_vault_layout.py` pins this.
- **Guard test:** a new role-folder string literal anywhere in
  `src/obsidian_automation/*.py` fails the test, unless it is on the
  allow-list below. It also checks bytes literals and Python subpackages.
  Docstrings and the literal text of exception messages are skipped; a call
  nested inside a message is still checked. Binding identities must match
  the whole literal.

| Role | Folder |
| --- | --- |
| daily | `00-DailyNote` |
| ai | `04-AI` |
| idea | `05-Idea` |
| project | `10-Project` |
| knowledge | `11-Knowledge` |
| system | `98-System` |

The role list is complete for `src/`. Other numbered folders in code, such
as `00-Untrusted`, `04-Index`, `05-Context` and `16-Human-Projection`, are
stages in the AI state directory, not Vault folders.

## Modules now reading the accessor

**Kind** says what the path is used for:

- **read:** the module reads notes under the folder (local mirror or remote).
- **write:** the module creates or changes notes there, or builds the target
  path for a write.
- **gate:** the module accepts or rejects a path that it receives (review
  candidates, CAS records, evidence, benchmarks).

The slice column is where the module moves from the fixed default to the
mapping record.

Update (P2-2): modules on obsidian-control map only their Nextcloud pull and
keep logical paths inside. The AI-host modules marked P2-2 below
(`knowledge_index`, `retrieval_eval`, `context_bundle`, `ai_input_planner`,
`semantic_*`, `pre_review_production_canary`) are deferred to a separate
decision; see the design doc.

| Module | Role | Kind | Slice |
| --- | --- | --- | --- |
| `vault_formatter` | system | read (local scan) and gate | P2-2 |
| `vault_format_source` | system | read (rclone pull) | P2-2 |
| `vault_format_writer` | system | write (PUT) and gate for the Writer allowlist | P2-3 |
| `vault_format_effect_journal` | system | gate for journal records | P2-3 |
| `knowledge_index` | knowledge | read | P2-2 |
| `retrieval_eval` | knowledge | gate | P2-2 |
| `context_bundle` | knowledge, project | read and gate | P2-2 |
| `ai_input_planner` | knowledge, project | read | P2-2 |
| `semantic_corpus` | daily, idea, project, knowledge | read | P2-2 |
| `semantic_retrieval` | daily, idea, project, knowledge | gate | P2-2 |
| `semantic_objective` | daily, idea, project, knowledge | gate | P2-2 |
| `github_project_watcher` | project | read (scan) | P2-2 |
| `github_project_watch_view` | project | gate | P2-2 |
| `github_b3_watcher_bridge` | project | gate (watcher warnings) | P2-2 |
| `b3_project_cache` | project | read | P2-2 |
| `github_daily_setup` | project | read (rclone pull), writes the watcher config | P2-2 |
| `github_daily_runonce` | project | gate (watcher config check) | P2-2 |
| `github_daily_summary` | project | gate | P2-2 |
| `github_pipeline_evidence` | project | gate | P2-2 |
| `github_b3_verified_registration` | project | gate | P2-3 |
| `github_project_overview` | project | write (Status note PUT) | P2-3 |
| `github_project_status_mutation` | project | write (Status note PUT) | P2-3 |
| `github_daily_progress` | daily | write (Daily Note PUT) | P2-3 |
| `generator_contract` | knowledge | write (new note target) | P2-3 |
| `knowledge_note_policy` | knowledge | Writer allowlist; used by `knowledge_validator` and `knowledge_production` | P2-3 |
| `evaluator_contract` | knowledge | gate for review candidates | P2-3 |
| `evaluation_artifact` | knowledge | gate for review candidates | P2-3 |
| `human_projection` | ai | write (projection notes) | P2-3 |
| `pre_review_production_canary` | knowledge | synthetic Vault for the canary | P2-2 |

## Kept literal on purpose (allow-list)

| Module | Literal | Why |
| --- | --- | --- |
| `core_live_reconciliation` | `98-System/__binding__` | Approval binding identity. It is a digest over a fixed string, so it must not follow a mapping change. |
| `vault_format_writer` | `98-System/__binding__.md` | Same as above. |
| `public_publish` | `98-System/99-dev/...` | Layout of the ObsidianCore repository, not the Vault. |
| `github_production_smoke` | `10-Project/Terreate/Terreate.md` | Fixed smoke fixture in a stored-history shape. |
| `human_projection` | `[[98-System/...]]`, `` `05-Idea` `` | Text inside a generated note body. |
| `admin_dashboard` | `Vault Formatter / 98-System` | Heading shown to people. |

Not in the guard test, and unchanged:

- **Docstrings and exception messages:** for example `vault_format_source`,
  `vault_format_canary_binding`, `chatgpt_export_source`, and messages such
  as "project must be below 10-Project". Messages will name the mapped folder
  in P2-2/P2-3.
- **`webui_design_v1/status_manager.js`:** the card subtitle
  "98-System · Dry-run / Formatting". UI text; P2-4.
- **`tools/provision_automation_authority.py`:** directories and ACLs under
  `/var/lib/obsidian-ai/vault/` on the AI host. It provisions a separate host,
  so a mapping change there needs its own approval. Decide in P2-3.

## Outside `src/` (unchanged; owned by later slices)

- **JSON schemas:** path patterns in `knowledge-index-v0`,
  `retrieval-eval-v0`, `context-bundle-v0`, `github-daily-evidence-v1`,
  `github-daily-progress-*-v1`, and `semantic-retrieval-benchmark-v1`.
  These describe stored history, which stays immutable. New schema versions
  would be needed for mapped paths.
- **Configs and examples:** `configs/public-export.example.toml`,
  `configs/github-project-watcher.example.toml`, `examples/ai/*`
  (rclone filters, gate scripts, systemd `ReadOnlyPaths`) and
  `examples/github-sync/*`.
