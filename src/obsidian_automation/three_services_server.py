"""Private-network, three-service-only control plane.

No AI, Formatter, B3, generic systemd operator or arbitrary workflow
endpoint is mounted. All mutating calls require exact Origin plus a one-use
CSRF token. Job admission is not reported as a verified outcome.
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from .core_promotion_job import Invalid as PromotionInvalid
from .core_promotion_runtime import Invalid as PromotionSetupInvalid
from .github_daily_job import Invalid as DailyInvalid
from .github_daily_setup import DailySetupError
from .snapshot_job import Invalid as SnapshotInvalid
from .snapshot_setup import GITEA_FIELDS, validate, validate_nextcloud
from .connection_fields import FieldDrafts, FieldError
from .phase_schedule import StaleScheduleError, preview as phase_preview
from .vault_root_activation import reset_pin_after_connection_save


_REQUEST_BYTES = 8192
_HEADER_BYTES = 8192
_HEADER_SECONDS = 2.0
_JSON = "application/json; charset=utf-8"
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/three-services.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/three-services.css": ("app.css", "text/css; charset=utf-8"),
}


class Refused(ValueError):
    def __init__(self, code: str, http: int = 409):
        super().__init__(code)
        self.code, self.http = code, http


class CsrfTokens:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._issued: dict[str, float] = {}

    def issue(self):
        with self._lock:
            now = self.clock()
            self._issued = {key: value for key, value in self._issued.items() if value > now}
            if len(self._issued) >= 64:
                raise Refused("session_busy", 503)
            token = secrets.token_urlsafe(32)
            self._issued[token] = now + 300
            return token

    def consume(self, token: str):
        if type(token) is not str:
            return False
        with self._lock:
            expiry = self._issued.pop(token, 0)
            return expiry > self.clock()


class _BoundedHeaderReader:
    """A total monotonic deadline across request-line and ALL header lines.

    Socket idle timeouts alone are insufficient: a peer can trickle bytes
    forever while occupying every bounded handler slot.
    """
    def __init__(self, raw, connection, *, clock=time.monotonic):
        self.raw = raw
        self.connection = connection
        self.clock = clock
        self.deadline = clock() + _HEADER_SECONDS
        self.read_total = 0

    def readline(self, size=-1):
        limit = min(size if type(size) is int and size > 0 else _HEADER_BYTES,
                    _HEADER_BYTES)
        result = bytearray()
        while True:
            if len(result) >= limit or self.read_total >= _HEADER_BYTES:
                raise TimeoutError("request_header_bytes_exceeded")
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("request_header_deadline")
            self.connection.settimeout(remaining)
            try:
                part = self.raw.read1(1)
            except (TimeoutError, OSError):
                raise TimeoutError("request_header_deadline") from None
            if not part:
                return bytes(result)
            result.extend(part)
            self.read_total += len(part)
            if part == b"\n":
                return bytes(result)

    def read1(self, size=-1):
        # The JSON body has its own one-second monotonic deadline.
        return self.raw.read1(size)

    def __getattr__(self, name):
        return getattr(self.raw, name)


class ThreeServicesServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, handler):
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "ObsidianThree/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        # Begin the total header deadline before parsing the request line.
        self.rfile = _BoundedHeaderReader(self.rfile, self.connection)

    def log_message(self, *_):
        # No request path, query, URL or credential in HTTP logs.
        return

    def _send(self, code: int, data: object, mime=_JSON):
        if isinstance(data, bytes):
            body = data
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        for key, value in (
            ("Content-Type", mime),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Content-Security-Policy",
             "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
             "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ):
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self):
        address, port = self.server.server_address[:2]
        hosts = {f"{address}:{port}"}
        if address == "127.0.0.1":
            hosts.add(f"localhost:{port}")
        received = self.headers.get_all("Host", [])
        origin = self.headers.get_all("Origin", [])
        return (len(received) == 1 and received[0] in hosts
                and len(origin) <= 1
                and (not origin or origin[0] == "http://" + received[0])
                and not self.headers.get_all("X-Obsidian-Private-Ingress", []))

    def _write_origin_ok(self):
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        fetch_site = self.headers.get_all("Sec-Fetch-Site", [])
        return (self._host_ok() and len(hosts) == 1 and len(origins) == 1
                and origins[0] == "http://" + hosts[0]
                and len(fetch_site) <= 1 and
                (not fetch_site or fetch_site[0] in ("same-origin", "none")))

    def _body(self):
        length = self.headers.get_all("Content-Length", [])
        types = self.headers.get_all("Content-Type", [])
        if (len(length) != 1 or len(types) != 1
            or types[0] != "application/json"
            or not length[0].isascii() or not length[0].isdecimal()
            or len(length[0]) > 4
            or self.headers.get_all("Transfer-Encoding")
            or self.headers.get_all("Content-Encoding")):
            raise Refused("invalid_request", 400)
        count = int(length[0])
        if not 1 <= count <= _REQUEST_BYTES:
            raise Refused("invalid_request", 400)
        deadline = time.monotonic() + 1.0
        parts = []
        while count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refused("body_timeout", 400)
            self.connection.settimeout(remaining)
            try:
                part = self.rfile.read1(min(count, 256))
            except OSError:
                raise Refused("body_unavailable", 400) from None
            if not part:
                raise Refused("body_truncated", 400)
            parts.append(part)
            count -= len(part)

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise Refused("duplicate_json_key", 400)
                value[key] = item
            return value

        try:
            return json.loads(b"".join(parts), object_pairs_hook=unique)
        except (UnicodeError, ValueError):
            raise Refused("invalid_json", 400) from None

    def _job(self, name):
        return getattr(self.server, {
            "snapshot": "snapshot_job",
            "daily": "daily_job",
            "core-promotion": "core_promotion_job",
        }[name], None)

    def do_GET(self):
        if not self._host_ok():
            self._send(403, {"error": "host_rejected"})
            return
        # A normalized //alias must never bypass the fixed asset or API map.
        if self.requestline.split(" ")[1] != self.path:
            self._send(404, {"error": "not_found"})
            return
        try:
            if self.path in _ASSETS:
                filename, mime = _ASSETS[self.path]
                self._send(200, files("obsidian_automation").joinpath(
                    "three_services", filename
                ).read_bytes(), mime)
                return
            if self.path == "/api/v1/session":
                self._send(200, {"csrf": self.server.csrf.issue()})
                return
            if self.path in ("/api/v1/nextcloud/connections",
                             "/api/v1/snapshot/connections",
                             "/api/v1/daily/connections"):
                store = self.server.snapshot_setup
                if store is None:
                    raise Refused("service_unavailable", 503)
                drafts = FieldDrafts(store)
                if self.path.startswith("/api/v1/nextcloud/"):
                    data = {**store.nextcloud_status(),
                            **drafts.status("nextcloud")}
                elif self.path.startswith("/api/v1/snapshot/"):
                    data = {**store.status(), **drafts.status("gitea")}
                else:
                    setup = self.server.daily_setup
                    if setup is None:
                        raise Refused("service_unavailable", 503)
                    data = {**setup.status(), **drafts.status("github", setup)}
                self._send(200, data)
                return
            if self.path == "/api/v1/core-promotion/configuration":
                runtime = self.server.core_promotion_runtime
                self._send(200 if runtime else 503,
                           runtime.status() if runtime else {"error": "unavailable"})
                return
            if self.path in ("/api/v1/vault-root", "/api/v1/vault-bindings"):
                service = (self.server.vault_root_selection
                           if self.path == "/api/v1/vault-root"
                           else self.server.vault_bindings_editor)
                self._send(200 if service else 503,
                           service.status() if service else {"error": "unavailable"})
                return
            for name in ("snapshot", "core-promotion", "daily"):
                base = f"/api/v1/{name}"
                if self.path == base or self.path.startswith(base + "/runs/"):
                    job = self._job(name)
                    if job is None:
                        raise Refused("service_unavailable", 503)
                    data = (job.status() if self.path == base
                            else job.run(self.path[len(base + "/runs/"):]))
                    self._send(200 if data is not None else 404,
                               data if data is not None else {"error": "not_found"})
                    return
            self._send(404, {"error": "not_found"})
        except Refused as exc:
            self._send(exc.http, {"error": exc.code})
        except Exception:
            self._send(503, {"error": "read_unavailable"})

    def do_POST(self):
        allowed = {
            "/api/v1/snapshot/settings", "/api/v1/snapshot/runs",
            "/api/v1/snapshot/schedule-preview",
            "/api/v1/daily/settings", "/api/v1/daily/runs",
            "/api/v1/daily/schedule-preview",
            "/api/v1/core-promotion/settings", "/api/v1/core-promotion/runs",
            "/api/v1/core-promotion/schedule-preview",
            "/api/v1/core-promotion/bootstrap",
            "/api/v1/nextcloud/connections",
            "/api/v1/snapshot/connections", "/api/v1/daily/connections",
            "/api/v1/nextcloud/connections/field",
            "/api/v1/snapshot/connections/field",
            "/api/v1/daily/connections/field",
            "/api/v1/nextcloud/connections/verify",
            "/api/v1/snapshot/connections/verify",
            "/api/v1/daily/connections/verify",
            "/api/v1/vault-root/candidates", "/api/v1/vault-root/previews",
            "/api/v1/vault-root/enrollment", "/api/v1/vault-root/activations",
            "/api/v1/vault-root/rollbacks",
            "/api/v1/vault-bindings/previews", "/api/v1/vault-bindings/saves",
            "/api/v1/vault-bindings/resets", "/api/v1/vault-bindings/rollbacks",
        }
        if self.path not in allowed:
            self._send(405, {"error": "method_not_allowed"})
            return
        if not self._write_origin_ok():
            self._send(403, {"error": "origin_rejected"})
            return
        tokens = self.headers.get_all("X-Obsidian-CSRF", [])
        if len(tokens) != 1 or not self.server.csrf.consume(tokens[0]):
            self._send(403, {"error": "csrf_rejected"})
            return
        try:
            payload = self._body()
            for name in ("snapshot", "core-promotion", "daily"):
                base = f"/api/v1/{name}"
                if self.path in (base + "/settings", base + "/runs",
                                 base + "/schedule-preview"):
                    job = self._job(name)
                    if job is None:
                        raise Refused("service_unavailable", 503)
                    if self.path.endswith("/schedule-preview"):
                        if (type(payload) is not dict
                            or set(payload) != {
                                "timezone", "anchor_local_time", "interval_seconds"}):
                            raise Refused("invalid_schedule", 400)
                        try:
                            result = phase_preview(
                                payload["timezone"], payload["anchor_local_time"],
                                payload["interval_seconds"], time.time(),
                                minimum=3600 if name == "daily" else 60)
                        except ValueError:
                            raise Refused("invalid_schedule", 400) from None
                        self._send(200, result)
                    elif self.path.endswith("/settings"):
                        # Old API remains usable for legacy interval records.
                        # Once anchored, browser writes MUST carry CAS revision.
                        current = job.settings()
                        if (current.get("schedule", {}).get("mode") == "anchored"
                            and (type(payload) is not dict
                                 or "expected_revision" not in payload)):
                            raise Refused("schedule_revision_required", 409)
                        self._send(200, job.update(payload))
                    else:
                        if type(payload) is not dict or set(payload) != {"request_id"}:
                            raise Refused("invalid_request", 400)
                        decision, receipt = job.submit(payload["request_id"])
                        self._send(202 if decision in ("accepted", "replayed") else 409,
                                   {"outcome": decision,
                                    "run_id": receipt["run_id"] if receipt else None})
                    return
            if self.path == "/api/v1/core-promotion/bootstrap":
                job = self.server.core_promotion_job
                runtime = self.server.core_promotion_runtime
                if job is None or runtime is None:
                    raise Refused("service_unavailable", 503)
                if not job._effect_gate.acquire(blocking=False):
                    raise Refused("promotion_busy")
                try:
                    with job._lock:
                        if job.settings()["enabled"] or self._has_running(job):
                            raise Refused("disable_promotion_before_bootstrap")
                        self._send(200, runtime.bootstrap(payload))
                finally:
                    job._effect_gate.release()
                return
            if self.path.startswith("/api/v1/vault-root/"):
                routes = {
                    "/api/v1/vault-root/candidates": "candidates",
                    "/api/v1/vault-root/previews": "preview",
                    "/api/v1/vault-root/enrollment": "enroll",
                    "/api/v1/vault-root/activations": "activate",
                    "/api/v1/vault-root/rollbacks": "rollback",
                }
                service = self.server.vault_root_selection
                if service is None:
                    raise Refused("service_unavailable", 503)
                code, data = getattr(service, routes[self.path])(payload)
                self._send(code, data)
                return
            if self.path.startswith("/api/v1/vault-bindings/"):
                routes = {
                    "/api/v1/vault-bindings/previews": "preview",
                    "/api/v1/vault-bindings/saves": "save",
                    "/api/v1/vault-bindings/resets": "reset",
                    "/api/v1/vault-bindings/rollbacks": "rollback",
                }
                service = self.server.vault_bindings_editor
                if service is None:
                    raise Refused("service_unavailable", 503)
                code, data = getattr(service, routes[self.path])(payload)
                self._send(code, data)
                return
            if self.path in ("/api/v1/nextcloud/connections",
                             "/api/v1/snapshot/connections",
                             "/api/v1/nextcloud/connections/field",
                             "/api/v1/snapshot/connections/field",
                             "/api/v1/nextcloud/connections/verify",
                             "/api/v1/snapshot/connections/verify"):
                self._save_nextcloud_or_snapshot(payload)
                return
            if self.path in ("/api/v1/daily/connections",
                             "/api/v1/daily/connections/field",
                             "/api/v1/daily/connections/verify"):
                setup, job = self.server.daily_setup, self.server.daily_job
                if setup is None or job is None:
                    raise Refused("service_unavailable", 503)
                with job._lock:
                    if self._has_running(job):
                        raise Refused("daily_busy")
                    drafts = FieldDrafts(self.server.snapshot_setup)
                    is_field = self.path.endswith(("/field","/verify"))
                    if is_field:
                        if self.path.endswith("/verify"):
                            if payload != {}:
                                raise Refused("invalid_request", 400)
                            action, candidate = drafts.complete("github", setup)
                        else:
                            action, candidate = drafts.prepare("github", payload, setup)
                        if action != "commit":
                            self._send(200, {**setup.status(),
                                             **drafts.status("github", setup),
                                             "field_result": action})
                            return
                        payload = candidate
                    if not setup.save(payload):
                        raise Refused("github_probe_failed", 422)
                    job.source_invalidated()
                    try:
                        drafts.committed("github")
                    except FieldError:
                        # Credential activation already committed. A cleanup
                        # failure is indeterminate, never an invalid request.
                        raise Refused("operation_unavailable", 503) from None
                self._send(200, {**setup.status(),
                                 **drafts.status("github", setup)})
                return
            raise Refused("method_not_allowed", 405)
        except Refused as exc:
            self._send(exc.http, {"error": exc.code})
        except StaleScheduleError:
            self._send(409, {"error": "stale_settings"})
        except (SnapshotInvalid, DailyInvalid, DailySetupError, FieldError,
                PromotionInvalid, PromotionSetupInvalid, ValueError, TypeError):
            self._send(400, {"error": "invalid_request"})
        except Exception:
            # Never echo exception strings: these can contain private
            # WebDAV paths, tokens or provider response snippets.
            self._send(503, {"error": "operation_unavailable"})

    @staticmethod
    def _has_running(job):
        return job.db.execute(
            "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone() is not None

    @contextmanager
    def _connection_fence(self):
        promotion = self.server.core_promotion_job
        if promotion is not None and not promotion._effect_gate.acquire(blocking=False):
            raise Refused("promotion_busy")
        try:
            with ExitStack() as stack:
                for job in (self.server.snapshot_job,
                            self.server.daily_job, promotion):
                    if job is not None:
                        stack.enter_context(job._lock)
                        if self._has_running(job):
                            raise Refused("job_busy")
                yield
        finally:
            if promotion is not None:
                promotion._effect_gate.release()

    @staticmethod
    def _mapping_blocks_change(store, payload):
        from .vault_bindings_editor import mapping_present
        if not mapping_present(store.state):
            return False
        current = store.load_nextcloud()
        return current is None or any(
            key in payload and payload[key] != current.get(key)
            for key in ("webdav_url", "nextcloud_user", "vault_folder")
        )

    def _save_nextcloud_or_snapshot(self, payload):
        store, job = self.server.snapshot_setup, self.server.snapshot_job
        daily, daily_setup = self.server.daily_job, self.server.daily_setup
        promotion = self.server.core_promotion_job
        if store is None or job is None:
            raise Refused("service_unavailable", 503)
        with self._connection_fence():
            before_daily = daily_setup.source_fingerprint() if daily_setup else None
            promotion_runtime = self.server.core_promotion_runtime
            drafts = FieldDrafts(store)
            is_field = self.path.endswith(("/field","/verify"))
            group = ("gitea" if self.path.startswith("/api/v1/snapshot/")
                     else "nextcloud")
            if is_field:
                if self.path.endswith("/verify"):
                    if payload != {}:
                        raise Refused("invalid_request", 400)
                    action, candidate = drafts.complete(group)
                else:
                    action, candidate = drafts.prepare(group, payload)
                if action != "commit":
                    status = (store.status() if group == "gitea"
                              else store.nextcloud_status())
                    self._send(200, {**status, **drafts.status(group),
                                     "field_result": action})
                    return
                payload = candidate
            if group == "gitea":
                if type(payload) is dict and set(payload) == GITEA_FIELDS:
                    previous = store.load_nextcloud()
                    if previous is None:
                        raise Refused("nextcloud_missing")
                    payload = {**previous, **payload}
                payload = validate(payload)
                if self._mapping_blocks_change(store, payload):
                    raise Refused("mapping_present")
                # Probe a valid candidate BEFORE sealing a changed target.
                # A simple connectivity failure must not quarantine a
                # perfectly good checkpoint on the unchanged Vault.
                if not store.probe(payload):
                    raise Refused("connection_probe_failed", 422)
                if promotion_runtime is not None:
                    promotion_runtime.quarantine_for_target_change(payload)
                # Commit only after a successful probe and durable quarantine.
                if not store.save(payload, probed=True):
                    raise Refused("connection_probe_failed", 422)
                reset_pin_after_connection_save(store)
                job.connections_saved(store.generation())
                response = store.status()
            else:
                payload = validate_nextcloud(payload)
                if self._mapping_blocks_change(store, payload):
                    raise Refused("mapping_present")
                # Nextcloud is shared. A full B2 record cannot be updated by
                # save_nextcloud(); preserve the existing private Gitea values.
                previous = store.load()
                if previous is not None:
                    combined = validate({**previous, **payload})
                    if not store.probe(combined):
                        raise Refused("connection_probe_failed", 422)
                elif not store.nextcloud_probe(payload):
                    raise Refused("connection_probe_failed", 422)
                # After validated remote admission, invalidate an old target
                # BEFORE the new credentials are durably committed.
                if promotion_runtime is not None:
                    promotion_runtime.quarantine_for_target_change(payload)
                if previous is not None:
                    if not store.save(combined, probed=True):
                        raise Refused("connection_probe_failed", 422)
                    job.connections_saved(store.generation())
                elif not store.save_nextcloud(payload, probed=True):
                    raise Refused("connection_probe_failed", 422)
                reset_pin_after_connection_save(store)
                response = store.nextcloud_status()
            if daily is not None and daily_setup is not None:
                if before_daily != daily_setup.source_fingerprint():
                    daily.source_invalidated()
            if promotion is not None:
                promotion.source_invalidated()
            try:
                drafts.committed(group)
            except FieldError:
                # The provider is already saved: never report a plain 400
                # that might encourage blind repetition of a target switch.
                raise Refused("operation_unavailable", 503) from None
        self._send(200, {**response, **drafts.status(group),
                         "field_result": "verified"})

    do_HEAD = lambda self: self._send(405, {"error": "method_not_allowed"})
    do_PUT = lambda self: self._send(405, {"error": "method_not_allowed"})
    do_DELETE = lambda self: self._send(405, {"error": "method_not_allowed"})


def make_server(*, port=8370, bind_address="127.0.0.1",
                snapshot_setup=None, minimal_mode=True):
    if minimal_mode is not True:
        raise ValueError("only_three_services_supported")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid_port")
    ip = ipaddress.ip_address(bind_address)
    # is_private includes 0.0.0.0 on current CPython. Do not trust it for
    # the operator-only HTTP perimeter. RFC1918 or loopback ONLY.
    rfc1918 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if (ip.version != 4 or ip.is_unspecified or ip.is_multicast
        or not (ip.is_loopback or any(ip in ipaddress.ip_network(net)
                                     for net in rfc1918))):
        raise ValueError("private_bind_required")
    server = ThreeServicesServer((bind_address, port), Handler)
    server.csrf = CsrfTokens()
    server.snapshot_setup = snapshot_setup
    server.snapshot_job = None
    server.daily_job = None
    server.daily_setup = None
    server.core_promotion_job = None
    server.core_promotion_runtime = None
    server.vault_root_selection = None
    server.vault_bindings_editor = None
    return server
