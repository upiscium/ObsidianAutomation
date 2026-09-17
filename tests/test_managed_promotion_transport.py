from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from obsidian_automation.core_promotion import build_promotion_plan
from obsidian_automation.core_promotion_transport import (
    HTTPResponse,
    PromotionTransportConflict,
    PromotionTransportError,
    initialize_checkpoint,
    load_checkpoint,
)
from obsidian_automation.managed_promotion_transport import (
    APPEARANCE_PATH,
    execute_managed_promotion,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _appearance(snippets: list[str], **extra) -> bytes:
    value = {
        "theme": "obsidian",
        "cssTheme": "Tokyo Night",
        "enabledCssSnippets": snippets,
        **extra,
    }
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _fixture(tmp_path: Path):
    repo = tmp_path / "core"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Managed Promotion Test")
    _git(repo, "config", "user.email", "promotion@example.invalid")

    (repo / ".obsidian/snippets").mkdir(parents=True)
    (repo / "98-System/90-config/styles").mkdir(parents=True)
    (repo / "98-System/marker.txt").parent.mkdir(parents=True, exist_ok=True)
    (repo / APPEARANCE_PATH).write_bytes(_appearance(["obsidian-core"]))
    (repo / ".obsidian/snippets/obsidian-core.css").write_bytes(b"/* base */\n")
    (repo / "98-System/90-config/styles/obsidian-core.css").write_bytes(b"/* base */\n")
    (repo / "98-System/marker.txt").write_bytes(b"v1\n")
    base = _commit(repo, "base")

    (repo / APPEARANCE_PATH).write_bytes(
        _appearance(["obsidian-core", "obsidian-core-mobile"])
    )
    mobile = b"/* ObsidianCore mobile layout overrides. */\n@media (max-width: 600px) {}\n"
    (repo / ".obsidian/snippets/obsidian-core-mobile.css").write_bytes(mobile)
    (repo / "98-System/90-config/styles/obsidian-core-mobile.css").write_bytes(mobile)
    (repo / "98-System/marker.txt").write_bytes(b"v2\n")
    head = _commit(repo, "head")

    config = tmp_path / "public.toml"
    config.write_text(
        """version = 1
strict_missing = true
include = [
  "98-System/**",
  ".obsidian/appearance.json",
  ".obsidian/snippets/**",
]
exclude = []
repository_owned = [".github/**", ".gitignore", "README.md", "LICENSE"]
""",
        encoding="utf-8",
    )
    plan = build_promotion_plan(repo, base_ref=base, head_ref=head, config_path=config)
    checkpoint = tmp_path / "checkpoint.json"
    policy_sha = hashlib.sha256(config.read_bytes()).hexdigest()
    initialize_checkpoint(
        checkpoint,
        core_commit=base,
        policy_sha256=policy_sha,
        initialized_at="2026-09-17T00:00:00Z",
    )
    receipts = tmp_path / "receipts"
    return repo, config, plan, checkpoint, receipts


def _git_blob(repo: Path, commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def _base_remote(repo: Path, plan) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for change in plan.changes:
        if change.before_sha256 is not None:
            files[change.path] = _git_blob(repo, plan.base_commit, change.path)
    return files


class FakeDAV:
    def __init__(self, files: dict[str, bytes], *, reserialize_appearance: bool = False):
        self.files = dict(files)
        self.versions = {path: 1 for path in files}
        self.mutations: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.concurrent_before_mutation: dict[str, bytes] = {}
        self.reserialize_appearance = reserialize_appearance

    def _path(self, target_url: str) -> str:
        raw = unquote(urlsplit(target_url).path)
        marker = "/vault/"
        assert marker in raw
        return raw.split(marker, 1)[1]

    def _etag(self, path: str) -> str | None:
        if path not in self.files:
            return None
        return f'"v{self.versions.get(path, 1)}"'

    def __call__(
        self,
        *,
        method: str,
        target_url: str,
        username: str,
        password: str,
        headers,
        body,
        timeout: float,
        response_limit: int,
    ) -> HTTPResponse:
        assert username == "promoter"
        assert password == "secret"
        path = self._path(target_url)
        headers = dict(headers)
        if method == "GET":
            if path not in self.files:
                return HTTPResponse(404, b"", None)
            return HTTPResponse(200, self.files[path], self._etag(path))

        self.mutations.append((method, path, headers, body))
        if path in self.concurrent_before_mutation:
            self.files[path] = self.concurrent_before_mutation.pop(path)
            self.versions[path] = self.versions.get(path, 1) + 1

        current_etag = self._etag(path)
        if headers.get("If-None-Match") == "*" and path in self.files:
            return HTTPResponse(412, b"", current_etag)
        if "If-Match" in headers and headers["If-Match"] != current_etag:
            return HTTPResponse(412, b"", current_etag)

        if method == "PUT":
            assert body is not None
            stored = body
            if self.reserialize_appearance and path == APPEARANCE_PATH:
                value = json.loads(body)
                stored = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            self.files[path] = stored
            self.versions[path] = self.versions.get(path, 0) + 1
            return HTTPResponse(201 if current_etag is None else 204, b"", self._etag(path))
        if method == "DELETE":
            self.files.pop(path, None)
            self.versions.pop(path, None)
            return HTTPResponse(204, b"", None)
        raise AssertionError(method)


def _execute(repo, config, plan, checkpoint, receipts, dav):
    return execute_managed_promotion(
        plan=plan,
        core_repository=repo,
        config_path=config,
        base_url="https://nextcloud.example/vault",
        username="promoter",
        password="secret",
        checkpoint_path=checkpoint,
        receipt_directory=receipts,
        transport=dav,
    )


def test_semantic_appearance_update_preserves_private_state_and_unblocks_css_creation(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    files = _base_remote(repo, plan)
    files[APPEARANCE_PATH] = json.dumps(
        {
            "theme": "obsidian",
            "cssTheme": "Tokyo Night",
            "enabledCssSnippets": ["private-before", "obsidian-core", "private-after"],
            "accentColor": "violet",
        },
        separators=(",", ":"),
    ).encode()
    dav = FakeDAV(files, reserialize_appearance=True)

    _, _, receipt, advanced = _execute(repo, config, plan, checkpoint, receipts, dav)

    assert advanced.last_observed_core_commit == plan.head_commit
    assert load_checkpoint(checkpoint).last_observed_core_commit == plan.head_commit
    appearance = json.loads(dav.files[APPEARANCE_PATH])
    assert appearance["accentColor"] == "violet"
    assert appearance["enabledCssSnippets"] == [
        "private-before",
        "obsidian-core",
        "obsidian-core-mobile",
        "private-after",
    ]
    assert dav.files[".obsidian/snippets/obsidian-core-mobile.css"] == _git_blob(
        repo, plan.head_commit, ".obsidian/snippets/obsidian-core-mobile.css"
    )
    assert dav.files["98-System/90-config/styles/obsidian-core-mobile.css"] == _git_blob(
        repo, plan.head_commit, "98-System/90-config/styles/obsidian-core-mobile.css"
    )
    results = {item.path: item.result for item in receipt.outcomes}
    assert results[APPEARANCE_PATH] == "applied"


def test_semantically_applied_appearance_needs_no_rewrite(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    files = _base_remote(repo, plan)
    files[APPEARANCE_PATH] = json.dumps(
        {
            "enabledCssSnippets": ["private", "obsidian-core", "obsidian-core-mobile"],
            "cssTheme": "Tokyo Night",
            "theme": "obsidian",
            "futureKey": {"keep": True},
        },
        separators=(",", ":"),
    ).encode()
    dav = FakeDAV(files)

    _, _, receipt, _ = _execute(repo, config, plan, checkpoint, receipts, dav)

    appearance_mutations = [item for item in dav.mutations if item[1] == APPEARANCE_PATH]
    assert appearance_mutations == []
    result = next(item.result for item in receipt.outcomes if item.path == APPEARANCE_PATH)
    assert result == "already_applied"


def test_managed_appearance_divergence_blocks_all_mutations(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    files = _base_remote(repo, plan)
    files[APPEARANCE_PATH] = _appearance(["obsidian-core"], cssTheme="Other Theme")
    dav = FakeDAV(files)

    with pytest.raises(PromotionTransportConflict, match="appearance.json"):
        _execute(repo, config, plan, checkpoint, receipts, dav)

    assert dav.mutations == []
    assert load_checkpoint(checkpoint).last_observed_core_commit == plan.base_commit
    assert ".obsidian/snippets/obsidian-core-mobile.css" not in dav.files


def test_invalid_remote_appearance_fails_closed_before_mutation(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    files = _base_remote(repo, plan)
    files[APPEARANCE_PATH] = b"{not-json"
    dav = FakeDAV(files)

    with pytest.raises(PromotionTransportError, match="remote appearance"):
        _execute(repo, config, plan, checkpoint, receipts, dav)

    assert dav.mutations == []
    assert load_checkpoint(checkpoint).last_observed_core_commit == plan.base_commit


def test_concurrent_appearance_change_is_rejected_by_if_match(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    files = _base_remote(repo, plan)
    files[APPEARANCE_PATH] = json.dumps(
        {
            "theme": "obsidian",
            "cssTheme": "Tokyo Night",
            "enabledCssSnippets": ["obsidian-core"],
            "local": 1,
        }
    ).encode()
    dav = FakeDAV(files)
    dav.concurrent_before_mutation[APPEARANCE_PATH] = json.dumps(
        {
            "theme": "obsidian",
            "cssTheme": "Tokyo Night",
            "enabledCssSnippets": ["obsidian-core"],
            "local": 2,
        }
    ).encode()

    with pytest.raises(PromotionTransportConflict, match="conditional update rejected"):
        _execute(repo, config, plan, checkpoint, receipts, dav)

    assert load_checkpoint(checkpoint).last_observed_core_commit == plan.base_commit
    assert ".obsidian/snippets/obsidian-core-mobile.css" not in dav.files


def test_generic_divergence_still_blocks_semantic_transaction(tmp_path: Path) -> None:
    repo, config, plan, checkpoint, receipts = _fixture(tmp_path)
    files = _base_remote(repo, plan)
    files["98-System/marker.txt"] = b"local divergence\n"
    dav = FakeDAV(files)

    with pytest.raises(PromotionTransportConflict, match="98-System/marker.txt"):
        _execute(repo, config, plan, checkpoint, receipts, dav)

    assert dav.mutations == []
    assert load_checkpoint(checkpoint).last_observed_core_commit == plan.base_commit
