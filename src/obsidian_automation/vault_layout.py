"""Role folders inside the active Vault root (#303 Phase 2, slice P2-0).

Every module that locates a role folder (daily, ai, idea, project, knowledge,
system) reads its name from here. P2-0 only centralises today's fixed names;
there is no mapping record yet, so behaviour is unchanged.

Not covered on purpose (see docs/oa303-phase2-path-inventory.md):
- approval/binding identities such as ``98-System/__binding__``: they are
  digests over a fixed string and must never follow a mapping change;
- stored history (receipts, projections, CAS entries) and JSON schemas;
- human-readable text, docstrings and Obsidian links inside note bodies.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Callable, Mapping

ROLES = ("daily", "ai", "idea", "project", "knowledge", "system")

DEFAULT_ROLE_FOLDERS: Mapping[str, str] = MappingProxyType({
    "daily": "00-DailyNote",
    "ai": "04-AI",
    "idea": "05-Idea",
    "project": "10-Project",
    "knowledge": "11-Knowledge",
    "system": "98-System",
})


def role_folder(role: str) -> str:
    """Return the folder, relative to the Vault root, that holds ``role``."""
    try:
        return DEFAULT_ROLE_FOLDERS[role]
    except KeyError:
        raise ValueError(f"unknown vault role: {role!r}") from None


def role_folders() -> Mapping[str, str]:
    """Return the full role -> folder mapping (read-only)."""
    return DEFAULT_ROLE_FOLDERS


def remote_folder(layout: Callable[[str], str] | None, role: str) -> dict:
    """Copier keyword for a role's physical folder (#584 P2-2).

    Empty while the folder is today's name, so copiers keep their two-argument
    call; ``{"folder": ...}`` only for a mapped folder. ``layout`` errors
    (``vault_bindings.BindingsError``, a ``ValueError``) propagate: a bad
    mapping record stops the pull.
    """
    if layout is None:
        return {}
    folder = layout(role)
    if type(folder) is not str or not folder:
        raise ValueError("invalid role folder")
    return {} if folder == role_folder(role) else {"folder": folder}
