# #483 R3B2B2D/B2 — Nextcloud OCC / legacy LXC observation contracts

**Status: source-only; no real collector, no authenticated issuer or live Writer.**

Earlier B2D/A+B1 introduced MAC validity, local root-owned snapshot admission,
and an explicit refusal to authorize B3 cutover. This B2 contract establishes
a **bounded interpreter for the actual Nextcloud 34 OCC token-list field
schema** and a separate fixed inventory of **known legacy systemd unit
states**. They are pure parsers and never run a command, open a socket, read
a real credential, or operate a production host. Future trusted collectors
will have to obtain the inputs and prove their origin independently.

## Nextcloud 34 OCC token census: source-only contract

`github_b3_occ_token_inventory.py` defines
`fixed_occ_list_args(account)` returning **only** the read-only subcommand
`("user:auth-tokens:list", account, "--output=json")`, not PHP, sudo or
an executable. No process is launched. The exact account has to be
supplied by a future privileged, non-Writer operator-controlled scope.

`assess_nextcloud_occ_inventory(captures, expected_accounts,
prior_legacy_token_ids, expected_version, now)` consumes a bounded tuple of
untrusted per-account command results. It requires:

- An exact external `expected_accounts` census (1–32, sorted, unique),
  with at least one preexisting, independently known legacy token ID.
  The user-facing request can never authorize which principals are complete.
- The explicit `nextcloud-34-occ-auth-tokens-json/v1` format, exact
  installed `34.x.y` version, successful read-only command, empty stderr,
  each result fresh to 60 seconds, and exactly one capture per account.
- A strict max 64-KiB UTF-8 JSON array with at most 128 token rows per
  account and no duplicate JSON keys. Each row must contain **exactly**
  `id` (positive int), `name` (bounded printable string),
  `lastActivity` (nonnegative Unix seconds), `type` (int 0–3) and
  `scope` (bounded bool-only dictionary using recognized scopes).
  Unsupported versions/fields/types, missing accounts, malformed/stale
  output and command or account discrepancies are **unknown**, never
  an empty successful list.
- If any previously inventoried old token ID remains, state is **blocked**.
  Any *additional* token in a purported retired dedicated legacy account
  is also **blocked**, even if its display name or claimed scope looks
  harmless. Results reveal no account, token IDs, names or raw stderr.

A synthetic report with no listed tokens is merely
`candidate/provider_origin_and_password_auth_unverified`, with
**`cutover_authorized=false`**. The code does NOT prove that every
legacy principal has been inventoried, that account-password login has
been disabled, that other authorization paths do not exist, that the
command was executed on the actual Nextcloud instance, or that a
revocation action occurred.

The installed Nextcloud major/minor and the actual `--output=json`
structure must be requalified read-only before enabling real collection.
Official documentation lists `user:auth-tokens:list <user>` with JSON
output and shows the ID/name/lastActivity/type/scope fields. Nextcloud's
command code strips credential-bearing token fields. This contract is
intentionally **version pinned** and refuses unknown schema revisions:
- https://docs.nextcloud.com/server/stable/admin_manual/occ_users.html
- https://github.com/nextcloud/server/blob/master/core/Command/User/AuthTokens/ListCommand.php
- https://github.com/nextcloud/server/blob/master/lib/private/Authentication/Token/PublicKeyToken.php

No call to `user:auth-tokens:delete`, `user:disable` or a WebDAV
write-denial probe is authorized here. In particular, `--output=json`
does **not** prove old credentials lost their ability to write to Vault.

## Old-LXC systemd unit inventory: source-only contract

`github_b3_old_host_unit_inventory.py` offers only
`fixed_systemctl_show_args(unit)` for an **exact allowlist** of eight
source-derived legacy watcher, Writer, Daily Apply, compactor and Core
Promotion service/timer names. It never starts `systemctl`.

The typed input must contain one exact-command output for **every** known
unit, with exact `Id,LoadState,ActiveState,UnitFileState` fields.
Success requires `LoadState=masked`,
`ActiveState=inactive`, and `UnitFileState=masked` for each unit,
plus a plausible **claimed** different boot ID and monotonic
fence/restart/observation times. An absent unit, a merely disabled timer,
an active unit, malformed/missing fields or stale observations fail
closed. The only nominal result is
`candidate/old_host_origin_and_nonunit_writers_unverified`,
**never a cutover authorization**.

A caller can forge `systemctl show` output and an old boot ID. Masked
units do not rule out manually run binaries, cron entries, other
services/timers, containers, active processes, restored credentials
or unfenced network access. The authoritative old-LXC principal and
unit inventory must be established *independently*, and proven persistent
over reboot before live Writer activation.

## Next gates, still NYI

1. Qualify the real installed Nextcloud version/schema without collecting
   or publishing sensitive raw output; confirm its administrative
   authentication and complete principal inventory with a privileged
   operator. A source-only capture supplied by a caller is forgeable.
2. Separate trusted Nextcloud and old-LXC **administrative collectors**,
   protected transport, root-of-trust/key lifecycle and signed or pinned
   receipts. Keep token IDs out of WebUI; reject unrecognized fields and
   limit output before persistence.
3. A protected, atomic, serialized replay ledger with durable monotonic
   issuer generations and no rollback after restart. The prior read-only
   `replay-floor.json` cannot satisfy this gate.
4. Explicit administrative authorization for actual credential revocation,
   independent post-revocation denial observation, old-LXC reboot isolation,
   and all new-host canonical Writers/Core Promotion adopting one stable
   shared guard through effects and readback.
5. Human approval for all live host/provider operations, deployment,
   release and actual cutover. No such approval is implied by this source.

Both modules are **unattached**. No source/install writes,
Nextcloud/GitHub/Vault/Gitea API actions, LXC/systemd calls, credential
reads/changes, WebUI Sync Now or canonical Writer execution are performed.
Issue #483 and #316 remain OPEN.
