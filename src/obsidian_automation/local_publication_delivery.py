"""#511 P2/A: strictly synthetic, opt-in non-force Push/readback proof.

No CLI, scheduler, production credentials or HTTPS Push support. Source records
come from read-only SnapshotJob SQLite, never from caller-constructed JSON.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import local_publication_preview as lp

_RUN_ID = re.compile(r"[a-f0-9]{32}\Z")
_MARKER = "OA511-P2-ISOLATED-FIXTURE-v1\n"
_REVISION = 1


class DeliveryError(lp.PreviewError):
    """A non-secret, fixed reason code for P2 fixture admission failure."""


@dataclass(frozen=True)
class FixtureDeliveryConfig:
    """Never use for production: file-only, under an explicit disposable root."""

    preview: lp.PreviewConfig
    b2_state: Path
    b2_run_id: str
    b2_connection_generation: str
    fixture_root: Path
    enabled: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    run_id: str
    source_sha: str
    core_before_sha: str | None
    candidate_sha: str | None


def _relative_under(path: Path, root: Path) -> Path:
    try:
        return path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise DeliveryError("fixture_out_of_bounds") from exc


def _fixture_repo(url: str, root: Path, expected: str) -> Path:
    parsed = urlsplit(url)
    if (parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment
        or parsed.username is not None or parsed.password is not None
        or not parsed.path.startswith("/")):
        raise DeliveryError("fixture_url_only")
    raw = Path(unquote(parsed.path))
    if raw.is_symlink():
        raise DeliveryError("fixture_out_of_bounds")
    # A matching basename under b2-state or a nested directory is not
    # the fixed disposable endpoint. Admit only the root's direct sibling.
    if _relative_under(raw, root) != Path(expected):
        raise DeliveryError("wrong_fixture_repository")
    return raw


def _validate_fixture(config: FixtureDeliveryConfig) -> None:
    if not config.enabled or not config.preview.allow_file_fixture:
        raise DeliveryError("fixture_disabled")
    root_path = config.fixture_root
    if root_path.is_symlink() or not root_path.is_dir():
        raise DeliveryError("untrusted_fixture_root")
    root = root_path.resolve(strict=True)
    if root.stat().st_mode & 0o077:
        raise DeliveryError("untrusted_fixture_root")
    marker = root / ".oa511-p2-disposable-fixture"
    if (marker.is_symlink() or not marker.is_file()
        or marker.read_text() != _MARKER
        or marker.stat().st_mode & 0o077):
        raise DeliveryError("untrusted_fixture_root")
    _fixture_repo(config.preview.source_url, root, "source.git")
    _fixture_repo(config.preview.core_url, root, "core.git")
    # Lexical is_relative_to is insufficient for '..' or an ancestor symlink.
    try:
        local_state = config.preview.state_dir.resolve().relative_to(root)
        b2_state = config.b2_state.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise DeliveryError("fixture_out_of_bounds") from exc
    # The isolated fixture has fixed sibling paths. Merely checking that
    # state is under root would permit writes inside source.git/core.git or
    # the B2 owner's directory, violating the no-extra-effects boundary.
    if local_state != Path("p2-state") or b2_state != Path("b2-state"):
        raise DeliveryError("fixture_out_of_bounds")
    if not _RUN_ID.fullmatch(config.b2_run_id):
        raise DeliveryError("invalid_b2_run_id")
    if (not isinstance(config.b2_connection_generation, str)
        or not 1 <= len(config.b2_connection_generation) <= 128
        or any(ord(char) < 32 for char in config.b2_connection_generation)):
        raise DeliveryError("invalid_connection_generation")


def read_verified_b2_run(
    state: Path, *, run_id: str, connection_generation: str
) -> lp.SnapshotEvidence:
    """Read the latest terminal B2 run and connection binding, with zero writes.

    This proves the current SQLite run/connection contract in the same service
    trust domain. A deployed B2-origin identity/credential gate is still P2/B.
    """
    if not _RUN_ID.fullmatch(run_id):
        raise DeliveryError("invalid_b2_run_id")
    if state.is_symlink() or not state.is_dir() or state.stat().st_mode & 0o077:
        raise DeliveryError("untrusted_b2_state")
    dbfile = state / "snapshot.sqlite3"
    if dbfile.is_symlink() or not dbfile.is_file():
        raise DeliveryError("untrusted_b2_state")
    try:
        conn = sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            names = conn.execute(
                "SELECT source_name,destination_name FROM settings WHERE id=1"
            ).fetchone()
            verified = conn.execute(
                "SELECT verified FROM connection_write WHERE id=1"
            ).fetchone()
            binding = conn.execute(
                "SELECT generation FROM connection_binding WHERE id=1"
            ).fetchone()
            latest = conn.execute(
                "SELECT run_id,trigger,status,stage,remote_ref,completed "
                "FROM runs ORDER BY created DESC,rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise DeliveryError("b2_state_unreadable") from exc
    if (
        names != ("nextcloud", "gitea")
        or verified != (1,)
        or binding != (connection_generation,)
    ):
        raise DeliveryError("b2_binding_unverified")
    if latest is None or latest[0] != run_id:
        raise DeliveryError("b2_not_latest")
    if (
        latest[1] not in {"scheduled", "manual"}
        or latest[2] not in {"saved", "no_changes"}
        or latest[3] != "verify"
        or not isinstance(latest[5], (int, float))
    ):
        raise DeliveryError("b2_run_not_verified")
    try:
        lp._sha(latest[4])
    except lp.PreviewError as exc:
        raise DeliveryError("b2_run_not_verified") from exc
    return lp.SnapshotEvidence(latest[2], latest[3], latest[4], "verified")


def _readback(
    url: str, *, state: Path, env: dict[str, str], before: str,
    candidate: str, local_core: Path,
) -> bool:
    """Fresh independent fetch; confirm exact ref, commit ancestry and tree."""
    with tempfile.TemporaryDirectory(prefix=".independent-readback-", dir=state) as temp:
        target = Path(temp) / "core.git"
        lp._git(["clone", "--quiet", "--bare", "--no-tags", "--single-branch",
                 "--branch", "main", url, str(target)], cwd=state, env=env)
        if lp._git(["rev-parse", "refs/heads/main"], cwd=target, env=env) != candidate:
            return False
        parents = lp._git(
            ["rev-list", "--parents", "-n", "1", candidate], cwd=target, env=env
        ).split()
        if parents != [candidate, before]:
            return False
        remote_tree = lp._git(
            ["rev-parse", f"{candidate}^{{tree}}"], cwd=target, env=env
        )
        local_tree = lp._git(
            ["rev-parse", f"{candidate}^{{tree}}"], cwd=local_core, env=env
        )
        return (
            remote_tree == local_tree
            and lp._remote_head(url, env=env, cwd=state) == candidate
        )


def _write(path: Path, fields: dict[str, object]) -> None:
    lp._atomic_receipt(path, fields)


def _load_record(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise DeliveryError("invalid_delivery_receipt")
    try:
        raw = path.read_bytes()
        if len(raw) > 4096:
            raise ValueError("oversized")
        obj = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise DeliveryError("invalid_delivery_receipt") from exc
    if not isinstance(obj, dict):
        raise DeliveryError("invalid_delivery_receipt")
    return obj


def _bound_fields(
    *, config: FixtureDeliveryConfig, evidence: lp.SnapshotEvidence,
    run_key: str, before: str, candidate: str | None, policy: str,
) -> dict[str, object]:
    return {
        "version": _REVISION,
        "run_id": config.b2_run_id,
        "connection_generation": config.b2_connection_generation,
        "source_sha": lp._sha(evidence.remote_ref),
        "core_before_sha": lp._sha(before),
        "candidate_sha": lp._sha(candidate) if candidate else None,
        "run_key": run_key,
        "policy_sha256": policy,
        "publisher_revision": lp._sha(config.preview.publisher_revision),
    }


def _result_from(fields: dict[str, object], status: str) -> DeliveryResult:
    return DeliveryResult(
        status, str(fields["run_id"]), str(fields["source_sha"]),
        str(fields["core_before_sha"]),
        str(fields["candidate_sha"]) if fields["candidate_sha"] else None,
    )


def _same_authority(
    fields: dict[str, object],
    config: FixtureDeliveryConfig,
    evidence: lp.SnapshotEvidence,
) -> None:
    if (
        fields.get("version") != _REVISION
        or fields.get("run_id") != config.b2_run_id
        or fields.get("source_sha") != evidence.remote_ref
        or fields.get("connection_generation") != config.b2_connection_generation
        or fields.get("publisher_revision") != config.preview.publisher_revision
        or fields.get("policy_sha256") != lp._policy_sha(config.preview.policy_path)
        or not isinstance(fields.get("run_key"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", fields["run_key"])
        or not isinstance(fields.get("core_before_sha"), str)
        or (fields.get("candidate_sha") is not None
            and not isinstance(fields.get("candidate_sha"), str))
    ):
        raise DeliveryError("delivery_authority_changed")
    try:
        lp._sha(fields["core_before_sha"])
        if fields["candidate_sha"] is not None:
            lp._sha(fields["candidate_sha"])
    except lp.PreviewError as exc:
        raise DeliveryError("invalid_delivery_receipt") from exc


def _result_saved(run_dir: Path, fields: dict[str, object], status: str) -> DeliveryResult:
    _write(run_dir / "result.json", {**fields, "status": status})
    return _result_from(fields, status)


def _reconcile_existing(
    run_dir: Path,
    config: FixtureDeliveryConfig,
    evidence: lp.SnapshotEvidence,
    *,
    env: dict[str, str], state: Path,
) -> DeliveryResult:
    """Reconcile an existing intent/receipt. Never resend an unknown push."""
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise DeliveryError("unsafe_delivery_state")
    intent = _load_record(run_dir / "intent.json")
    _same_authority(intent, config, evidence)
    fields = dict(intent)
    result_path = run_dir / "result.json"
    reconcile_path = run_dir / "reconcile.json"
    if reconcile_path.exists() or reconcile_path.is_symlink():
        repaired = _load_record(reconcile_path)
        _same_authority(repaired, config, evidence)
        if repaired.get("status") != "reconciled_verified":
            raise DeliveryError("invalid_delivery_receipt")
        return _result_from(intent, "already_published_verified")
    if result_path.exists() or result_path.is_symlink():
        outcome = _load_record(result_path)
        _same_authority(outcome, config, evidence)
        status = outcome.get("status")
        if status == "published_verified":
            return _result_from(intent, "already_published_verified")
        if status == "no_changes_verified":
            return _result_from(intent, "already_no_changes_verified")
        if status == "blocked_conflict":
            return _result_from(intent, "blocked_conflict")
        if status != "unknown":
            raise DeliveryError("invalid_delivery_receipt")
    candidate = intent.get("candidate_sha")
    if candidate is None:
        return _result_from(intent, "unknown")
    core = config.preview.state_dir / "runs" / str(intent["run_key"]) / "core"
    if core.is_symlink() or not (core / ".git").is_dir():
        return _result_from(intent, "unknown")
    # After a possible send, only an independent fetched exact ref/tree
    # can turn an unknown outcome into a verified effect.
    try:
        verified = _readback(
            config.preview.core_url, state=state, env=env,
            before=str(intent["core_before_sha"]),
            candidate=str(candidate), local_core=core,
        )
    except (lp.PreviewError, OSError):
        verified = False
    if not verified:
        return _result_from(intent, "unknown")
    _write(reconcile_path, {**intent, "status": "reconciled_verified"})
    return _result_from(intent, "reconciled_verified")


def deliver_disposable_fixture(config: FixtureDeliveryConfig) -> DeliveryResult:
    """Opt-in, marked disposable FILE-only E2E. Never push a real endpoint.

    No credentials. No production entrypoint. Only a validated exact-SHA
    local candidate can reach the single-ref, non-force fixture push.
    """
    _validate_fixture(config)  # No state writes or Git calls before this gate.
    evidence = read_verified_b2_run(
        config.b2_state, run_id=config.b2_run_id,
        connection_generation=config.b2_connection_generation,
    )
    state = config.preview.state_dir
    lp._private_dir(state)
    lp._private_dir(state / "deliveries")
    try:
        fd = os.open(
            state / "delivery.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
        )
    except OSError as exc:
        raise DeliveryError("unsafe_delivery_lock") from exc
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise DeliveryError("unsafe_delivery_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeliveryError("delivery_busy") from exc
        with tempfile.TemporaryDirectory(
            prefix=".delivery-git-home-", dir=state
        ) as home:
            env = lp._git_env(Path(home), fixture=True)
            run_dir = state / "deliveries" / config.b2_run_id
            if run_dir.exists() or run_dir.is_symlink():
                return _reconcile_existing(
                    run_dir, config, evidence, env=env, state=state
                )
            preview = lp.prepare_local_preview(evidence, config.preview)
            before = preview.core_before_sha
            candidate = preview.candidate_sha
            if candidate is not None and preview.status not in {
                "candidate_prepared", "already_prepared"
            }:
                raise DeliveryError("invalid_preview_result")
            core = state / "runs" / preview.run_key / "core"
            source = state / "runs" / preview.run_key / "source"
            policy_file = state / "runs" / preview.run_key / "policy.toml"
            if (
                lp._candidate_sha(
                    core, before, env=env, policy_path=policy_file
                ) != candidate
            ):
                raise DeliveryError("candidate_changed")
            lp._assert_projection_converged(source, core, policy_file)
            # P1 validation may take time. Re-admit the latest B2 run and its
            # connection generation immediately before any potential write.
            refreshed = read_verified_b2_run(
                config.b2_state, run_id=config.b2_run_id,
                connection_generation=config.b2_connection_generation,
            )
            if refreshed != evidence:
                raise DeliveryError("b2_changed_before_delivery")
            if lp._policy_sha(config.preview.policy_path) != preview.policy_sha256:
                raise DeliveryError("policy_changed_before_delivery")
            # Durable authority and intent are written BEFORE any write effect.
            try:
                run_dir.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise DeliveryError("unreconciled_delivery") from exc
            fields = _bound_fields(
                config=config, evidence=evidence, run_key=preview.run_key,
                before=before, candidate=candidate,
                policy=preview.policy_sha256,
            )
            _write(run_dir / "intent.json", {**fields, "status": "started"})
            if (
                lp._remote_head(config.preview.source_url, env=env, cwd=state)
                != evidence.remote_ref
                or lp._remote_head(config.preview.core_url, env=env, cwd=state)
                != before
            ):
                return _result_saved(run_dir, fields, "blocked_conflict")
            if candidate is None:
                return _result_saved(run_dir, fields, "no_changes_verified")
            # This is a fixture ONLY: no "+" refspec, no --force, no automatic
            # retry. The Core cloned by P1 has no remotes or write credentials.
            try:
                lp._git(
                    ["push", "--porcelain", "--no-verify",
                     config.preview.core_url, f"{candidate}:refs/heads/main"],
                    cwd=core, env=env,
                )
            except lp.PreviewError:
                # Transport failure after send may still have applied.
                pass
            try:
                verified = _readback(
                    config.preview.core_url, state=state, env=env,
                    before=before, candidate=candidate, local_core=core,
                )
            except (lp.PreviewError, OSError):
                verified = False
            return _result_saved(
                run_dir, fields,
                "published_verified" if verified else "unknown",
            )
    finally:
        os.close(fd)
