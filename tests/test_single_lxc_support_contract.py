"""Prevent accidental removal of the approved single-active-LXC support notice.

This is a documentation contract, not a runtime singleton detector or proof
that a second active writer is harmless.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_readme_explicitly_states_single_active_lxc_and_unsupported_concurrency():
    readme = _read("README.md")
    assert "exactly one active ObsidianAutomation service-execution LXC per managed Vault" in readme
    assert "Concurrent multi-instance access to the same Vault is unsupported" in readme
    assert "behavior and data integrity are not guaranteed" in readme
    assert "does not provide cross-host distributed locks" in readme
    assert "Single-LXC does not remove local safety requirements" in readme


def test_adr_preserves_local_guards_and_does_not_claim_full_acceptance():
    adr = _read("docs/adr/0001-single-host-management-dashboard.md")
    assert "operator-approved boundary, 2026-10-16" in adr
    assert "a second independently running ObsidianAutomation instance" in adr
    assert "unsupported" in adr
    assert "Within the one active LXC, preserve strong local exclusion" in adr
    assert "not an acceptance of the entire Proposed ADR" in adr


def test_broker_scope_does_not_demote_local_exclusion():
    broker = _read("docs/admin-operation-broker.md")
    socket_contract = _read("docs/admin-broker-socket.md")
    assert "does not need to implement cross-LXC" in broker
    assert "maintenance gate must exclude" in broker
    assert "same-host" in broker
    assert "A local flock does not" in socket_contract
    assert "all **local** active" in socket_contract
    assert "service controls remain" in socket_contract


def test_legacy_docs_are_labeled_historical():
    legacy = _read("docs/production-lxc-consolidation.md")
    readme = _read("README.md")
    assert "Historical trust-domain topology (superseded by #298 target)" in legacy
    assert "legacy" in readme.lower()
    assert "Snapshot role runs" in readme


def test_cutover_requires_drain_and_final_transfer_not_legacy_early_copy():
    guide = _read("docs/production-lxc-consolidation.md")
    section = guide.split("## Cutover — active single-LXC target", 1)[1].split(
        "## Final acceptance and retirement", 1
    )[0]
    stop = section.index("stop old scheduling and admission of new effect claims")
    drain = section.index("drain or safely contain ALL in-flight jobs")
    reconcile = section.index("reconcile remote writes")
    transfer = section.index("MANDATORY final authoritative durable-state transfer AFTER quiescence")
    activate = section.index("enable ONLY the new service LXC")
    assert stop < drain < reconcile < transfer < activate
    assert "Concurrent active instances are unsupported" in section
    assert "preliminary mirror" in section


def test_historical_snapshots_and_runner_are_not_target_implementation_requirements():
    guide = _read("docs/production-lxc-consolidation.md")
    assert "former topology the Snapshot LXC was separate" in guide
    assert "separate Unix identity in the sole active service LXC" in guide
    assert "historical-only" in guide
    assert "local publisher (not a generic runner)" in guide


def test_installation_requires_one_active_host_and_local_exclusion():
    install = _read("docs/installation.md")
    assert "one active ObsidianAutomation service-execution" in install
    assert "unsupported" in install
    assert "no guarantee of" in install
    assert "disable and drain old services" in install
    assert "coordinate service control with local update processes" in install


def test_installation_lists_all_five_managed_timers():
    install = _read("docs/installation.md")
    lifecycle = _read("docs/host-runtime-lifecycle.md")
    daily = "obsidian-github-daily-progress.timer"
    assert install.count(daily) == 3
    assert "all five managed timers" in lifecycle
    assert daily in lifecycle


def test_public_publisher_runner_is_historical_for_new_target():
    readme = _read("README.md")
    heading = readme.split("## Public Publisher v0", 1)[1].split(
        "## Live Vault Snapshot v0", 1
    )[0]
    assert "historical" in heading
    assert "narrowly scoped local Publisher role" in heading
    assert "not yet accepted for production" in heading


def test_adr_timer_count_matches_documented_runtime():
    adr = _read("docs/adr/0001-single-host-management-dashboard.md")
    assert "contains an older count of four" not in adr
    assert "same five timers" in adr
    assert "GitHub Daily Progress" in adr
