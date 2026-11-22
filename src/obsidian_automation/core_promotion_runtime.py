"""Fixed-purpose Core Promotion adapter for the minimum three-service runtime.

Only an explicitly initialized, exact-policy checkpoint can enable effects.
The existing conditional managed Promotion transport, not the WebUI, performs
and verifies writes. The HTTP surface never receives raw credentials.
"""
from __future__ import annotations

import hashlib
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


_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class Invalid(ValueError):
    pass


class CorePromotionRuntime:
    """All paths are fixed under the single app's private state directory."""

    def __init__(self, app_state: Path, setup_store, guard):
        self.root = app_state / "core-promotion"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.policy_path = self.root / "public-export.toml"
        self.checkpoint_path = self.root / "checkpoint.json"
        self.store = setup_store
        self.guard = guard
        self.policy_bytes = files("obsidian_automation").joinpath(
            "three_services", "public-export.toml"
        ).read_bytes()
        self.policy_sha256 = hashlib.sha256(self.policy_bytes).hexdigest()

    def _policy_verified(self):
        try:
            # No HTTP-provided policies; arbitrary local drift disables runs.
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

    def source_generation(self):
        if not self._policy_verified() or self._source() is None:
            return None
        try:
            # Private-only fingerprint; no raw credential, source URL or path
            # can be returned to the browser.
            return hashlib.sha256(
                self.store.path.read_bytes() + self.policy_sha256.encode("ascii")
            ).hexdigest()
        except OSError:
            return None

    def configured(self):
        if self.source_generation() is None:
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
            "uses_shared_nextcloud": True,
        }

    def bootstrap(self, payload):
        """Explicit UI action: choose an existing generated Core commit.

        This makes only private checkpoint/policy files. It never promotes
        or mutates the Live Vault; ambiguous/old source is rejected by the
        existing bootstrap verification.
        """
        if (type(payload) is not dict
            or set(payload) != {"core_commit", "policy_sha256"}
            or type(payload["core_commit"]) is not str
            or _COMMIT.fullmatch(payload["core_commit"]) is None
            or payload["policy_sha256"] != self.policy_sha256):
            raise Invalid("invalid_core_promotion_bootstrap")
        if self.checkpoint_path.exists():
            raise Invalid("checkpoint_already_initialized")
        if self._source() is None:
            raise Invalid("nextcloud_or_mapping_not_configured")
        if self.policy_path.exists():
            if not self._policy_verified():
                raise Invalid("promotion_policy_mismatch")
        else:
            # Exclusive creation, never replace policy or existing files.
            with self.policy_path.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(self.policy_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        verified = self.guard()
        def do_bootstrap():
            base, head, _ = bootstrap_production_checkpoint(
                state_root=self.root, config_path=self.policy_path,
                core_commit=payload["core_commit"],
            )
            return {"initialized": True, "baseline_commit": base, "head_commit": head}
        return run_verified(verified, do_bootstrap)

    def run(self):
        if not self.configured():
            raise Invalid("core_promotion_not_configured")
        before = self.source_generation()
        source = self._source()
        if before is None or source is None:
            raise Invalid("source_unavailable")
        verified = self.guard()

        def execute():
            # No extra persistent credential file. The shared SetupStore
            # already owns a verified private copy; ephemeral plaintext is
            # needed only by the existing audited Promotion API.
            # /dev/shm is preferred to avoid crash-left disk secrets.
            scratch = "/dev/shm" if Path("/dev/shm").is_dir() else self.root
            with tempfile.TemporaryDirectory(prefix="oa-promotion-", dir=scratch) as tmp:
                password_file = Path(tmp) / "password"
                with password_file.open("x", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(source["nextcloud_password"])
                if self.source_generation() != before:
                    raise Invalid("core_promotion_connection_changed")
                folder = "/".join(quote(part, safe="") for part in source["vault_folder"].split("/"))
                base_url = source["webdav_url"].rstrip("/") + "/" + folder + "/"
                result = run_promotion_cycle(
                    state_root=self.root, config_path=self.policy_path,
                    base_url=base_url, username=source["nextcloud_user"],
                    password_file=password_file,
                )
                # The existing code checks checkpoint/plan and verifies
                # readback before declaring an effect 'promoted'.
                return {
                    "status": result.result,
                    "base_commit": result.base_commit,
                    "head_commit": result.head_commit,
                    "plan_sha256": result.plan_sha256,
                    "receipt_sha256": result.receipt_sha256,
                }
        return run_verified(verified, execute)
