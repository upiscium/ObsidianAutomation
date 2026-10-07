from __future__ import annotations

from pathlib import Path


ROOT = Path("examples/github-sync")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_daily_timer_has_finalize_and_retry_calendars() -> None:
    timer = _read("obsidian-github-daily-progress.timer")

    assert "OnCalendar=*-*-* 00:10:00 Asia/Tokyo" in timer
    assert "OnCalendar=*-*-* *:40:00 Asia/Tokyo" in timer
    assert "Persistent=true" in timer
    assert "Unit=obsidian-github-daily-apply.service" in timer


def test_daily_service_chain_is_strictly_ordered() -> None:
    collect = _read("obsidian-github-daily-collect.service")
    summary = _read("obsidian-github-daily-summary.service")
    render = _read("obsidian-github-daily-render.service")
    apply = _read("obsidian-github-daily-apply.service")

    assert (
        "Requires=obsidian-github-daily-schedule.service "
        "obsidian-github-sync-vault-pull.service"
    ) in collect
    assert "Requires=obsidian-github-daily-collect.service" in summary
    assert "Requires=obsidian-github-daily-summary.service" in render
    assert "Requires=obsidian-github-daily-render.service" in apply

    assert "After=network-online.target obsidian-github-daily-collect.service" in summary
    assert "After=obsidian-github-daily-summary.service" in render
    assert "After=network-online.target obsidian-github-daily-render.service" in apply


def test_daily_roles_have_separate_unix_identities() -> None:
    assert "User=obsidian-github-sync" in _read(
        "obsidian-github-daily-collect.service"
    )
    assert "User=obsidian-github-summarizer" in _read(
        "obsidian-github-daily-summary.service"
    )
    assert "User=obsidian-github-renderer" in _read(
        "obsidian-github-daily-render.service"
    )
    assert "User=obsidian-github-daily-writer" in _read(
        "obsidian-github-daily-apply.service"
    )


def test_summarizer_has_provider_config_but_no_github_or_writer_config() -> None:
    summary = _read("obsidian-github-daily-summary.service")

    assert "EnvironmentFile=/etc/obsidian-github-summarizer/config.env" in summary
    assert "EnvironmentFile=/etc/obsidian-github-summarizer/revision.env" in summary
    assert (
        "ReadOnlyPaths=/etc/obsidian-github-summarizer "
        "/var/lib/obsidian-github-pipeline/daily-progress/00-Schedule "
        "/var/lib/obsidian-github-pipeline/daily-progress/10-Evidence"
    ) in summary
    assert "/etc/obsidian-github-sync" in summary
    assert "/etc/obsidian-github-daily-writer" in summary
    assert "/srv/obsidian-github-sync/vault" in summary


def test_renderer_is_offline_and_has_no_credentials() -> None:
    render = _read("obsidian-github-daily-render.service")

    assert "PrivateNetwork=true" in render
    assert "EnvironmentFile=" not in render
    assert (
        "ReadWritePaths=/var/lib/obsidian-github-pipeline/"
        "daily-progress/30-Projection"
    ) in render
    assert "/etc/obsidian-github-summarizer" in render
    assert "/etc/obsidian-github-daily-writer" in render


def test_daily_writer_reads_projection_only_and_has_no_create_surface() -> None:
    apply = _read("obsidian-github-daily-apply.service")

    assert "EnvironmentFile=/etc/obsidian-github-daily-writer/config.env" in apply
    assert (
        "--password-file /etc/obsidian-github-daily-writer/webdav-password"
        in apply
    )
    assert (
        "ReadOnlyPaths=/etc/obsidian-github-daily-writer "
        "/var/lib/obsidian-github-pipeline/daily-progress/00-Schedule "
        "/var/lib/obsidian-github-pipeline/daily-progress/30-Projection"
    ) in apply
    assert (
        "ReadWritePaths=/var/lib/obsidian-github-pipeline/24-Locks "
        "/var/lib/obsidian-github-pipeline/daily-progress/40-Transport"
    ) in apply
    inaccessible = apply.split("InaccessiblePaths=", 1)[1]
    assert "/daily-progress/10-Evidence" in inaccessible
    assert "/daily-progress/20-Summary" in inaccessible
    assert "/etc/obsidian-github-summarizer" in inaccessible


def test_schedule_and_renderer_do_not_need_network() -> None:
    assert "PrivateNetwork=true" in _read(
        "obsidian-github-daily-schedule.service"
    )
    assert "PrivateNetwork=true" in _read(
        "obsidian-github-daily-render.service"
    )
