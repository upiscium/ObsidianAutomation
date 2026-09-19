# Dedicated Gitea Runner lifecycle

The runner is a separate execution-plane LXC. It must not share a host with
Core Promotion, AI, GitHub Sync or the read-only snapshot service. An
instance-level routing label is not an authorization boundary. Only trusted
repositories may use this host-mode runner. Do not add Docker/root/sudo groups,
Proxmox bind mounts, or automation credentials to it.

## Reproduce the host

Use Debian 13 with Git, Python 3.11+, python3-venv, Node.js 20+, OpenSSH client,
CA certificates and systemd. These are runtime requirements, not a resource
reservation. Size CPU/RAM/disk for job concurrency and workspace retention.

Fetch ObsidianAutomation from its canonical repository, verify the chosen full
commit belongs to fetched `origin/main`, and check out that exact commit in a
root-controlled source directory. Do not execute floating `main` installers.
Supply an independently verified runner binary with its expected SHA-256, plus
reviewed private runner YAML. Treat YAML as private: it can contain environment
values even when the current installation's config does not.

```sh
python3 /root/ObsidianAutomation/tools/bootstrap_gitea_runner.py \
  --source-root /root/ObsidianAutomation \
  --target-sha "$REVIEWED_SHA" \
  --runner-binary /root/staging/runner \
  --runner-sha256 "$REVIEWED_RUNNER_SHA256" \
  --config-source /root/staging/config.yaml
```

The source-side tool checks exact clean source, binary hash, a non-serving
runner and absence of writer/snapshot roots. It stages the binary, private
config and canonical unit atomically per file. It neither registers nor starts
the runner. An existing `.runner` is retained, never printed, and kept at 0600.
Unit/config updates require an explicitly disabled, drained runner; active jobs
are not killed. The tool is not a remote auto-updater or a registration-token
manager. Re-running with the reviewed sources is idempotent.

Keep runner workspaces under `/var/lib/gitea-runner` and use the private `/tmp`
provided by systemd for temporary work. `ProtectSystem=strict` intentionally
blocks workflows from mutating host system directories.

## Registration and cutover

Register interactively as `gitea-runner`, in `/var/lib/gitea-runner`, using
`runner register --config /etc/gitea-runner/config.yaml` and the routing label
`obsidian-publisher:host`. Never put the registration token in argv, shell
history, chat or logs. Use `umask 077` before registration; afterwards verify
`.runner` is owned by `gitea-runner:gitea-runner` with mode 0600. Do not copy old
registration state or automation credentials.

Keep the new service disabled until the old runner is paused/drained in Gitea
and then disabled/inactive. A process-count snapshot alone is not proof of an
idle runner: jobs can start after a check. Verify the old runner is no longer
serving before enabling the new unit. Then run a **fresh current-snapshot**
publication job on the new runner. Re-running an old snapshot is useful for
runner diagnostics but can legitimately fail current repository contracts.

The deploy key, known-hosts value and immutable `OBSIDIAN_AUTOMATION_REF` remain
in Gitea repository Actions configuration, not copied host files. A Green job
with no projection diff is valid acceptance. Retain a recoverable backup of
the old runner until the new runner's job and log evidence have been reviewed.

## Update and recovery

Repeat the same source-side staging command with the new reviewed source and
binary SHA after disabling/draining this runner only. Automation/snapshot hosts
are unaffected. If staging fails, leave the runner disabled, correct the input
or repeat the same exact operation; do not auto-start it in an error handler.
Binary/config/unit installs are individually atomic, not a multi-file atomic
transaction. Preserve `.runner` across updates, validate its metadata, and only
then deliberately enable the service and test a job.
