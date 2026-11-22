"""Fixed-purpose Core Promotion adapter for the minimum three-service runtime.

Only an explicitly initialized checkpoint bound to BOTH its Core policy and
its canonical Vault target can enable effects. The existing conditional
WebDAV transport, not the WebUI, performs and verifies writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from .core_promotion import CORE_REPOSITORY, PROMOTION_POLICY_VERSION
from .core_promotion_transport import load_checkpoint
from .managed_promotion_deployment import run_promotion_cycle
from .promotion_bootstrap import bootstrap_production_checkpoint
from .public_export import load_config
from .vault_bindings import physical_folder, run_verified
from .vault_root_activation import current_pin, read_private_json


_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TARGET_KEYS = ("webdav_url", "nextcloud_user", "vault_folder")


class Invalid(ValueError):
    pass


class CorePromotionRuntime:
    """Fixed private paths. No dynamic path, policy or credential HTTP inputs."""

    def __init__(self, app_state: Path, setup_store, guard):
        self.root = app_state / "core-promotion"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.policy_path = self.root / "public-export.toml"
        self.checkpoint_path = self.root / "checkpoint.json"
        self.target_binding_path = self.root / "vault-target-binding.json"
        self.quarantine_path = self.root / "target-change.quarantine"
        self.store = setup_store
        self.guard = guard
        self.policy_bytes = files("obsidian_automation").joinpath(
            "three_services", "public-export.toml"
        ).read_bytes()
        self.policy_sha256 = hashlib.sha256(self.policy_bytes).hexdigest()

    def _policy_verified(self):
        try:
            return (self.policy_path.is_file() and not self.policy_path.is_symlink()
                    and self.policy_path.read_bytes() == self.policy_bytes
                    and load_config(self.policy_path) is not None)
        except (OSError, ValueError):
            return False

    def _source(self):
        try:
            source = self.store.load_nextcloud()
            if source is None or physical_folder(self.store.state, "system") != "98-System":
                return None
            return source
        except Exception:
            return None

    def _target_identity(self, source=None):
        """Stable destination identity (not credential bytes).

        The pinned Nextcloud oc:id chain distinguishes a replaced folder from
        one with the same human-readable path when the root was enrolled.
        """
        if source is None:
            source = self._source()
        if source is None or any(type(source.get(key)) is not str
                                 for key in _TARGET_KEYS):
            return None
        try:
            pin = current_pin(self.store.state)
            pin_value = (None if pin is None else {
                "path": pin.path,
                "chain": list(pin.chain) if pin.chain is not None else None,
                "instance_id": pin.instance_id,
            })
            content = json.dumps({
                "webdav_url": source["webdav_url"],
                "nextcloud_user": source["nextcloud_user"],
                "vault_folder": source["vault_folder"],
                "root_pin": pin_value,
            }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return hashlib.sha256(content).hexdigest()
        except (OSError, TypeError, ValueError):
            return None

    def _quarantined(self):
        return self.quarantine_path.exists() or self.quarantine_path.is_symlink()

    def _binding(self):
        value = read_private_json(self.target_binding_path, limit=2048)
        if (type(value) is not dict
            or set(value) != {"version", "target_sha256", "policy_sha256", "core_commit"}
            or value.get("version") != 1
            or type(value.get("target_sha256")) is not str
            or _DIGEST.fullmatch(value["target_sha256"]) is None
            or type(value.get("policy_sha256")) is not str
            or _DIGEST.fullmatch(value["policy_sha256"]) is None
            or type(value.get("core_commit")) is not str
            or _COMMIT.fullmatch(value["core_commit"]) is None):
            return None
        return value

    def _target_bound(self):
        record = self._binding()
        current = self._target_identity()
        return (not self._quarantined() and record is not None
                and current is not None
                and record["target_sha256"] == current
                and record["policy_sha256"] == self.policy_sha256)

    def _create_exclusive(self, path: Path, data: bytes):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _ensure_target_binding(self, target_sha256: str, baseline_commit: str):
        """Seal target BEFORE initializing a checkpoint.

        If process termination occurs after checkpoint creation, the bound
        destination is still durable. A retry must use identical target,
        policy and baseline; an unknown existing binding fails closed.
        """
        expected = {
            "version": 1, "target_sha256": target_sha256,
            "policy_sha256": self.policy_sha256, "core_commit": baseline_commit,
        }
        existing = self._binding()
        if self._quarantined() or self.target_binding_path.is_symlink():
            raise Invalid("core_promotion_target_reauthorization_required")
        if self.target_binding_path.exists():
            if existing != expected:
                raise Invalid("core_promotion_target_binding_conflict")
            return
        encoded = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        try:
            self._create_exclusive(self.target_binding_path, encoded)
        except FileExistsError:
            if self._binding() != expected:
                raise Invalid("core_promotion_target_binding_conflict") from None

    def quarantine_for_target_change(self, candidate: object):
        """Pre-save admission barrier to prevent A->B->A checkpoint reuse.

        Caller must hold the Core Promotion effect gate and all Job locks.
        Invoked before changing a saved Nextcloud target. Password-only
        rotation of the SAME target does not quarantine the checkpoint.
        """
        if type(candidate) is not dict:
            raise Invalid("invalid_promotion_target_candidate")
        current = self._source()
        if (current is not None
            and all(candidate.get(k) == current.get(k) for k in _TARGET_KEYS)):
            return False
        if (not self.target_binding_path.exists() and not self.target_binding_path.is_symlink()
            and not self.checkpoint_path.exists() and not self.checkpoint_path.is_symlink()):
            return False
        if self._quarantined():
            return True
        try:
            self._create_exclusive(self.quarantine_path, b"target_changed\n")
        except FileExistsError:
            pass
        return True

    def source_generation(self):
        if not self._policy_verified() or self._source() is None:
            return None
        try:
            return hashlib.sha256(
                self.store.path.read_bytes() + self.policy_sha256.encode("ascii")
            ).hexdigest()
        except OSError:
            return None

    def configured(self):
        if self.source_generation() is None or not self._target_bound():
            return False
        try:
            checkpoint = load_checkpoint(self.checkpoint_path)
            return (
                checkpoint.source_repository == CORE_REPOSITORY
                and checkpoint.policy_version == PROMOTION_POLICY_VERSION
                and checkpoint.policy_sha256 == self.policy_sha256
            )
        except Exception:
            return False

    def status(self):
        return {
            "configured": self.configured(),
            "policy_sha256": self.policy_sha256,
            "checkpoint_initialized": self.checkpoint_path.exists(),
            "target_binding_valid": self._target_bound(),
            "requires_reauthorization": self._quarantined() or (
                (self.checkpoint_path.exists() or self.target_binding_path.exists())
                and not self._target_bound()
            ),
            "uses_shared_nextcloud": True,
        }

    def bootstrap(self, payload):
        """Explicit UI request: seal target and initialize immutable checkpoint.

        It does NOT read back the whole Vault or establish that the requested
        generated baseline is reflected there. That independent prerequisite
        remains a Human-reviewed production gate.
        """
        if (type(payload) is not dict
            or set(payload) != {"core_commit", "policy_sha256"}
            or type(payload["core_commit"]) is not str
            or _COMMIT.fullmatch(payload["core_commit"]) is None
            or payload["policy_sha256"] != self.policy_sha256):
            raise Invalid("invalid_core_promotion_bootstrap")
        if self.checkpoint_path.exists():
            raise Invalid("checkpoint_already_initialized")
        if self._source() is None or self._quarantined():
            raise Invalid("nextcloud_or_mapping_not_configured")
        if self.policy_path.exists():
            if not self._policy_verified():
                raise Invalid("promotion_policy_mismatch")
        else:
            with self.policy_path.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(self.policy_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        verified = self.guard()
        target = self._target_identity()
        if target is None:
            raise Invalid("core_promotion_target_unavailable")

        def seal_after_source_validation(base, head, policy_sha):
            if (base != payload["core_commit"] or policy_sha != self.policy_sha256
                or self._target_identity() != target):
                raise Invalid("core_promotion_baseline_or_target_changed")
            self._ensure_target_binding(target, base)

        def do_bootstrap():
            # The existing bootstrap verifies Git ancestry, generated commit
            # subject and policy before invoking the target seal callback.
            # The seal becomes durable BEFORE the checkpoint is initialized.
            base, head, _ = bootstrap_production_checkpoint(
                state_root=self.root, config_path=self.policy_path,
                core_commit=payload["core_commit"],
                before_checkpoint=seal_after_source_validation,
            )
            return {"initialized": True, "baseline_commit": base, "head_commit": head}

        return run_verified(verified, do_bootstrap)

    def run(self):
        if not self.configured():
            raise Invalid("core_promotion_not_configured")
        before = self.source_generation()
        target = self._target_identity()
        source = self._source()
        if before is None or target is None or source is None:
            raise Invalid("source_unavailable")
        verified = self.guard()

        def execute():
            scratch = "/dev/shm" if Path("/dev/shm").is_dir() else self.root
            with tempfile.TemporaryDirectory(prefix="oa-promotion-", dir=scratch) as tmp:
                password_file = Path(tmp) / "password"
                with password_file.open("x", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(source["nextcloud_password"])
                if (self.source_generation() != before
                    or self._target_identity() != target
                    or not self._target_bound()):
                    raise Invalid("core_promotion_connection_changed")
                folder = "/".join(quote(part, safe="") for part in source["vault_folder"].split("/"))
                base_url = source["webdav_url"].rstrip("/") + "/" + folder + "/"
                result = run_promotion_cycle(
                    state_root=self.root, config_path=self.policy_path,
                    base_url=base_url, username=source["nextcloud_user"],
                    password_file=password_file,
                )
                return {
                    "status": result.result,
                    "base_commit": result.base_commit,
                    "head_commit": result.head_commit,
                    "plan_sha256": result.plan_sha256,
                    "receipt_sha256": result.receipt_sha256,
                }
        return run_verified(verified, execute)
