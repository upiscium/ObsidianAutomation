"""Read-only, conservative formatter plans for Obsidian Vault system Markdown.

The formatter does not mutate the source. Any future WebDAV writer must bind the
plan to the exact current file bytes, policy and conditional remote revision.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

POLICY_VERSION = "system-blank-lines-v1"
PLAN_VERSION = 1
SCOPE = "98-System/**"
DEFAULT_MAX_FILES = 10000
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_SUPPORTED_SUFFIXES = frozenset({".md", ".markdown"})
_FENCE_START = re.compile(r"^ {0,3}(?P<mark>\x60{3,}|~{3,})")
_RAW_OPEN = re.compile(
    r"<(?P<tag>pre|script|style|textarea|template|xmp|iframe|noembed|"
    r"noframes|plaintext|listing)(?=[\s>])", re.I
)
_HARDBREAK_COMMENT = re.compile(r"^\s*>")


@dataclass(frozen=True)
class FormatResult:
    path: str
    status: str
    reason: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    changed_lines: tuple[int, ...] = ()
    before_size: int | None = None
    after_size: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "reason": self.reason,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "changed_lines": list(self.changed_lines),
            "before_size": self.before_size,
            "after_size": self.after_size,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _split_line(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def _nearest_nonblank(lines: list[str], index: int, direction: int) -> str | None:
    position = index + direction
    while 0 <= position < len(lines):
        body, _ = _split_line(lines[position])
        if body.strip():
            return body
        position += direction
    return None


def _in_ambiguous_markdown_context(lines: list[str], index: int) -> bool:
    """Avoid blank lines that may be structural parts of nested code/quotes."""
    previous = _nearest_nonblank(lines, index, -1)
    following = _nearest_nonblank(lines, index, +1)
    neighbors = [line for line in (previous, following) if line is not None]
    if any(_HARDBREAK_COMMENT.match(line) for line in neighbors):
        return True
    # Empty lines inside indented code blocks should stay byte-for-byte.
    if any(len(line) - len(line.lstrip(" ")) >= 4 for line in neighbors):
        return True
    # An indented whitespace-only line within a Markdown list could be
    # structural. Do not try to prove its semantics with nearest-line logic.
    if any(re.match(r"^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])\s", line) for line in neighbors):
        return True
    return False


def format_markdown(source: bytes) -> tuple[bytes | None, tuple[int, ...], str]:
    """Plan byte-preserving whitespace-only-line edits; never write to disk.

    Return (desired, changed lines, reason). desired=None indicates an unsafe
    input which the caller must skip, not an empty file.
    """
    if b"\x00" in source:
        return None, (), "binary-content"
    prefix = b"\xef\xbb\xbf" if source.startswith(b"\xef\xbb\xbf") else b""
    try:
        text = source[len(prefix) :].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, (), "invalid-utf8"
    if "\r" in text.replace("\r\n", ""):
        return None, (), "unsupported-line-endings"
    if "\r\n" in text and "\n" in text.replace("\r\n", ""):
        return None, (), "mixed-line-endings"

    if any(ch in text for ch in ("\v", "\f", "\x85", "\u2028", "\u2029")):
        return None, (), "unsupported-line-separators"

    lines = text.splitlines(keepends=True)
    if not lines:
        return source, (), "no-safe-changes"

    bodies = [_split_line(line)[0] for line in lines]
    # Nested block quote fences and multiline raw HTML are not parsed in the
    # minimal formatter; reject rather than guessing their Markdown semantics.
    if any(re.match(r"^\s*>+\s*[\x60~]{3}", body) for body in bodies):
        return None, (), "unsupported-nested-fence"
    if any(re.match(r"^\s*<(?:div|details|summary|table|ul|ol|li)(?=[\s>])", body, re.I) for body in bodies):
        return None, (), "unsupported-html-block"
    frontmatter = bodies[0].strip(" ") == "---"
    fence_char = ""
    fence_length = 0
    html_tag: str | None = None
    html_comment = False
    templater = False
    updates: list[str] = []
    modified: list[int] = []

    for index, line in enumerate(lines):
        body, ending = _split_line(line)
        # Frontmatter is protected in its entirety, including YAML block scalars.
        if frontmatter:
            updates.append(line)
            if index != 0 and body in {"---", "..."}:
                frontmatter = False
            continue

        if fence_char:
            updates.append(line)
            stripped = body.lstrip(" ")
            marker = fence_char * fence_length
            if (
                len(body) - len(stripped) <= 3
                and stripped.startswith(marker)
                and stripped[len(marker) :].strip(fence_char + " ") == ""
                and stripped[0] == fence_char
            ):
                fence_char = ""
                fence_length = 0
            continue

        if html_comment:
            updates.append(line)
            if "-->" in body:
                html_comment = False
            continue

        if html_tag:
            updates.append(line)
            if re.search(r"</" + re.escape(html_tag) + r"\s*>", body, re.I):
                html_tag = None
            continue

        if templater:
            updates.append(line)
            if "%>" in body:
                templater = False
            continue

        start = _FENCE_START.match(body)
        if start:
            updates.append(line)
            marker = start.group("mark")
            fence_char, fence_length = marker[0], len(marker)
            continue

        if "<!--" in body:
            updates.append(line)
            if "-->" not in body[body.find("<!--") + 4 :]:
                html_comment = True
            continue

        match = _RAW_OPEN.search(body)
        if match:
            # HTML plaintext consumes the rest of the document, including
            # literal "</plaintext>" text; there is no closing end tag.
            # Reject the whole input rather than resuming formatting.
            if match.group("tag").lower() == "plaintext":
                return None, (), "unsupported-html-block"
            updates.append(line)
            tag = match.group("tag")
            if not re.search(r"</" + re.escape(tag) + r"\s*>", body[match.end() :], re.I):
                html_tag = tag
            continue

        if "<%" in body:
            updates.append(line)
            if "%>" not in body[body.find("<%") + 2 :]:
                templater = True
            continue

        # Exactly 1–3 ASCII spaces on an otherwise empty Markdown line.
        # Longer indentation, tabs and other whitespace are not safe MVI edits.
        if (
            1 <= len(body) <= 3
            and body.strip(" ") == ""
            and not _in_ambiguous_markdown_context(lines, index)
        ):
            updates.append(ending)
            modified.append(index + 1)
        else:
            updates.append(line)

    if frontmatter:
        return None, (), "unterminated-frontmatter"
    if fence_char or html_tag or html_comment or templater:
        return None, (), "unterminated-protected-block"

    desired = prefix + "".join(updates).encode("utf-8")
    return desired, tuple(modified), "blank-line-whitespace" if modified else "no-safe-changes"


def _entry_collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _safe_directory_flags() -> int:
    """Require Linux/POSIX no-follow directory FDs for traversal."""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("no-follow-directory-open-unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _enumerate_system_files(vault: Path, max_files: int) -> list[tuple[str, Path, str | None]]:
    """Enumerate using dirfds, without following replaced intermediate links."""
    results: list[tuple[str, Path, str | None]] = []
    visited = 0
    dirflags = _safe_directory_flags()

    def walk(fd: int, relative_parent: str, depth: int) -> None:
        nonlocal visited
        if depth > 64:
            raise ValueError("max-depth-limit-exceeded")
        with os.scandir(fd) as scan:
            entries = sorted(scan, key=lambda e: (e.name.casefold(), e.name))
        for entry in entries:
            visited += 1
            if visited > max_files:
                raise ValueError("max-files-limit-exceeded")
            # POSIX may expose names containing arbitrary bytes through
            # surrogateescape. Never place unpaired surrogate code points
            # in a UTF-8 JSON plan or echo the raw name to public outputs.
            try:
                entry.name.encode("utf-8")
            except UnicodeEncodeError:
                fingerprint = _sha256(os.fsencode(entry.name))
                skipped_name = f"<invalid-utf8-name-{fingerprint}>"
                results.append((
                    relative_parent + "/" + skipped_name,
                    vault / PurePosixPath(relative_parent),
                    "invalid-utf8-filename",
                ))
                continue
            relative = relative_parent + "/" + entry.name
            path = vault / PurePosixPath(relative)
            try:
                metadata = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                results.append((relative, path, "source-unreadable"))
                continue
            if stat.S_ISLNK(metadata.st_mode):
                results.append((relative, path, "symlink"))
            elif stat.S_ISDIR(metadata.st_mode):
                if entry.name.startswith("."):
                    results.append((relative, path, "hidden-directory"))
                else:
                    try:
                        child_fd = os.open(entry.name, dirflags, dir_fd=fd)
                    except OSError:
                        results.append((relative, path, "source-unreadable"))
                        continue
                    try:
                        walk(child_fd, relative, depth + 1)
                    finally:
                        os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                results.append((relative, path, None))
            else:
                results.append((relative, path, "unsupported-file-type"))

    try:
        root_fd = os.open(vault, dirflags)
    except OSError as exc:
        raise ValueError("unsafe-vault-root") from exc
    try:
        try:
            system_fd = os.open("98-System", dirflags, dir_fd=root_fd)
        except OSError as exc:
            raise ValueError("98-System directory is absent, symlinked or not a directory") from exc
        try:
            walk(system_fd, "98-System", 0)
        finally:
            os.close(system_fd)
    finally:
        os.close(root_fd)
    return sorted(results, key=lambda r: r[0])


def _snapshot_file(vault: Path, relative: str, max_bytes: int) -> tuple[bytes | None, str | None]:
    """Read with O_NOFOLLOW on every component; never open through a symlink."""
    parts = PurePosixPath(relative).parts
    if len(parts) < 2 or parts[0] != "98-System" or any(
        part in ("", ".", "..") for part in parts
    ):
        return None, "unsafe-path"

    fd = -1
    parent_fd = -1
    try:
        dirflags = _safe_directory_flags()
        parent_fd = os.open(vault, dirflags)
        for part in parts[:-1]:
            child_fd = os.open(part, dirflags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        fileflags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(parts[-1], fileflags, dir_fd=parent_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None, "unsupported-file-type"
        if before.st_size > max_bytes:
            return None, "file-too-large"
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            data = handle.read(max_bytes + 1)
            during = os.fstat(handle.fileno())
        if len(data) > max_bytes:
            return None, "file-too-large"
        after = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        signature = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if (
            not stat.S_ISREG(after.st_mode)
            or signature(before) != signature(during)
            or signature(before) != signature(after)
        ):
            return None, "source-changed-during-read"
        return data, None
    except OSError:
        return None, "source-unreadable"
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def create_plan(
    vault_root: Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    include_diffs: bool = False,
) -> dict[str, Any]:
    if min(max_files, max_bytes, max_total_bytes) <= 0:
        raise ValueError("all resource limits must be positive")
    vault = Path(vault_root)
    if vault.is_symlink() or not vault.is_dir():
        raise ValueError("vault root is absent, symlinked or not a directory")
    entries = _enumerate_system_files(vault, max_files)
    collision_counts = Counter(_entry_collision_key(rel) for rel, _, _ in entries)
    results = []
    total = 0
    diffs: dict[str, str] = {}
    for rel, path, traversal_reason in entries:
        # A directory collision is conservatively rejected for each path.
        if collision_counts[_entry_collision_key(rel)] > 1:
            item = FormatResult(rel, "skipped", "path-name-collision")
        elif traversal_reason:
            item = FormatResult(
                rel,
                "error" if traversal_reason == "source-unreadable" else "skipped",
                traversal_reason,
            )
        elif any(part.startswith(".") for part in Path(rel).parts[1:]):
            item = FormatResult(rel, "skipped", "hidden-path")
        elif path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            item = FormatResult(rel, "skipped", "unsupported-extension")
        else:
            original, reason = _snapshot_file(vault, rel, max_bytes)
            if original is None:
                status = "error" if reason in {"source-unreadable", "source-changed-during-read"} else "skipped"
                item = FormatResult(rel, status, reason or "source-unreadable")
            else:
                total += len(original)
                if total > max_total_bytes:
                    raise ValueError("max-total-bytes-limit-exceeded")
                formatted, lines, reason = format_markdown(original)
                before_digest = _sha256(original)
                if formatted is None:
                    item = FormatResult(
                        rel, "skipped", reason, before_sha256=before_digest, before_size=len(original)
                    )
                else:
                    changed = original != formatted
                    item = FormatResult(
                        path=rel,
                        status="would_change" if changed else "unchanged",
                        reason=reason,
                        before_sha256=before_digest,
                        after_sha256=_sha256(formatted),
                        changed_lines=lines,
                        before_size=len(original),
                        after_size=len(formatted),
                    )
                    if include_diffs and changed:
                        before_text = original.decode("utf-8-sig").splitlines(keepends=True)
                        after_text = formatted.decode("utf-8-sig").splitlines(keepends=True)
                        diffs[rel] = "".join(
                            difflib.unified_diff(
                                before_text,
                                after_text,
                                fromfile="a/" + rel,
                                tofile="b/" + rel,
                            )
                        )
        results.append(item.as_dict())

    counts = dict(sorted(Counter(result["status"] for result in results).items()))
    payload = {
        "schema_version": PLAN_VERSION,
        "policy_version": POLICY_VERSION,
        "scope": SCOPE,
        "mode": "dry-run",
        "results": results,
        "counts": counts,
        "scanned_bytes": total,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    payload["plan_sha256"] = _sha256(encoded)
    if include_diffs:
        payload["diffs"] = diffs
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-vault-format-plan",
        description="Read-only format plan: Phase 1, 98-System Markdown only (no Vault writes).",
    )
    parser.add_argument("--vault", type=Path, required=True, help="Local Vault root")
    parser.add_argument("--diff", action="store_true", help="Include original text in local diff output")
    parser.add_argument("--check", action="store_true", help="Return 1 if safe changes are required")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    args = parser.parse_args(argv)
    try:
        plan = create_plan(
            args.vault,
            max_files=args.max_files,
            max_bytes=args.max_bytes,
            max_total_bytes=args.max_total_bytes,
            include_diffs=args.diff,
        )
    except (OSError, ValueError) as exc:
        # No user Vault content is included in failures.
        print("formatter plan rejected: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True))
    if plan["counts"].get("error", 0):
        return 2
    if args.check and plan["counts"].get("would_change", 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
