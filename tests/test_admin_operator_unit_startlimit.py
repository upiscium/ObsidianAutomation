"""#300 root broker service rate-limit options belong to systemd [Unit]."""
from pathlib import Path


def test_operator_start_limit_is_in_unit_section_not_service():
    source = (
        Path(__file__).resolve().parents[1]
        / "examples/admin/obsidian-admin-operator.service"
    ).read_text(encoding="utf-8")
    section = ""
    fields = {}
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        key, sep, value = stripped.partition("=")
        assert sep
        if key.startswith("StartLimit"):
            fields[key] = (section, value)
    assert fields == {
        "StartLimitBurst": ("Unit", "3"),
        "StartLimitIntervalSec": ("Unit", "60s"),
    }
    assert "Restart=on-failure" in source
    assert "TimeoutStopSec=180s" in source
