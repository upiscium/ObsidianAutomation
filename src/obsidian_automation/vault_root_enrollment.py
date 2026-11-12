"""#303 V1/A-gate: bind the saved shared Nextcloud account (enrollment).

One read-only ``PROPFIND Depth:0`` of the account's DAV files root records:
- the instance ID (suffix of ``oc:id``), so a different Nextcloud behind the
  same URL is refused later;
- the account root's ``oc:id``, the trusted container for every walk;
- the authenticated user ID (``DAV:current-user-principal``), which must equal
  the user segment of the saved ``/remote.php/dav/files/<user>/`` URL.

The record is private (0600), holds no secret and is valid only for the saved
WebDAV URL + user it was made under. Like every provider read in #303 it is
refused before any I/O unless the proof register admits it. Nothing here is
wired into the running app; the picker/activation stay default OFF.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import stat
import time
from typing import Callable
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from .snapshot_setup import SetupStore, _atomic
from .vault_root_activation import _origin, _source_digest, current_pin
from .vault_root_dav_fixture import DAV, OC, _ACCOUNT, _Blocked, _canonical_href, _parse_xml
from .vault_root_dav_verifier import (
    CURRENT_PROVIDER_PROOF, NC, PER_CALL_TIMEOUT_S, DavResponse, DavTransport,
    DavTransportError, ProviderProof, TrustedDavAccount, read_root_identity,
)
from .vault_root_dav_adapter import MAX_PAGE_BYTES
from .vault_root_preview import _identifier
from .vault_root_selection import TrustedRootContext

ENROLLMENT_FILE = "vault_root_account.json"
ENROLL_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"'
    b' xmlns:nc="http://nextcloud.org/ns"><d:prop>'
    b"<d:resourcetype/><d:current-user-principal/><oc:fileid/><oc:id/>"
    b"<nc:mount-type/></d:prop></d:propfind>"
)
_INSTANCE = re.compile(r"[a-z0-9]{3,64}\Z", re.ASCII)
_PRINCIPAL = re.compile(r"/remote\.php/dav/principals/users/([^/]+)/\Z", re.ASCII)


@dataclass(frozen=True)
class AccountEnrollment:
    instance_id: str
    root_resource_id: str
    user_id: str
    source_sha256: str


def _account_user(prefix: str) -> str:
    return unquote(prefix.rstrip("/").rsplit("/", 1)[-1])


def _parse_enrollment(body: bytes, prefix: str) -> tuple[str, str, str]:
    rows = _parse_xml(body)
    if len(rows) != 1:
        raise _Blocked("account_unverified")
    row = rows[0]
    hrefs = row.findall(DAV + "href")
    if len(hrefs) != 1 or _canonical_href(hrefs[0].text) != _canonical_href(prefix):
        raise _Blocked("account_unverified")
    ok = [ps for ps in row.findall(DAV + "propstat")
          if (ps.findtext(DAV + "status") or "") == "HTTP/1.1 200 OK"]
    if len(ok) != 1 or len(ok[0].findall(DAV + "prop")) != 1:
        raise _Blocked("account_unverified")
    prop = ok[0].find(DAV + "prop")

    def one(tag: str) -> ET.Element:
        found = prop.findall(tag)
        if len(found) != 1:
            raise _Blocked("account_unverified")
        return found[0]
    types = [child.tag for child in one(DAV + "resourcetype")]
    if types != [DAV + "collection"] or (one(NC + "mount-type").text or "").strip():
        raise _Blocked("account_unverified")
    fileid, resource_id = one(OC + "fileid").text or "", one(OC + "id").text or ""
    padded = fileid.zfill(8)
    if (not fileid.isdecimal() or not resource_id.startswith(padded)
            or not _identifier(resource_id)):
        raise _Blocked("identity_unverified")
    instance = resource_id[len(padded):]
    if _INSTANCE.fullmatch(instance) is None:
        raise _Blocked("identity_unverified")
    principal = one(DAV + "current-user-principal").findall(DAV + "href")
    match = (_PRINCIPAL.fullmatch(principal[0].text or "")
             if len(principal) == 1 else None)
    if match is None or unquote(match.group(1)) != _account_user(prefix):
        raise _Blocked("account_mismatch")
    return instance, resource_id, unquote(match.group(1))


def enroll_account(
    store: SetupStore,
    transport: DavTransport,
    *,
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
    clock: Callable[[], float] = time.time,
) -> tuple[str, AccountEnrollment | None]:
    """Returns (``enrolled`` | denial reason, record). Refuses without I/O
    while the register does not admit provider reads."""
    if type(proof) is not ProviderProof or not proof.admits(restricted_coherence=True):
        return "provider_unproven", None
    saved = store.load_nextcloud()
    if saved is None:
        return "binding_unavailable", None
    origin, prefix = _origin(saved["webdav_url"]), urlsplit(saved["webdav_url"]).path
    if _ACCOUNT.fullmatch(prefix) is None:
        return "account_unverified", None
    try:
        response = transport.propfind(origin, prefix, "0", ENROLL_BODY,
                                      timeout=PER_CALL_TIMEOUT_S, max_bytes=MAX_PAGE_BYTES)
    except DavTransportError:
        return "provider_unavailable", None
    if (type(response) is not DavResponse or response.redirected is not False
            or response.status != 207 or type(response.body) is not bytes
            or len(response.body) > MAX_PAGE_BYTES):
        return "provider_unavailable", None
    try:
        instance, root_id, user = _parse_enrollment(response.body, prefix)
    except _Blocked as err:
        return err.code, None
    record = AccountEnrollment(instance, root_id, user, _source_digest(saved))
    try:
        _atomic(store.state / ENROLLMENT_FILE, json.dumps({
            "version": 1, "instance_id": instance, "root_resource_id": root_id,
            "user_id": user, "source_sha256": record.source_sha256,
            "enrolled_at": int(clock()),
        }, sort_keys=True))
    except OSError:
        return "enrollment_unrecorded", None
    return "enrolled", record


def load_enrollment(store: SetupStore) -> AccountEnrollment | None:
    """The record, only if it was made under the currently saved URL + user."""
    path = store.state / ENROLLMENT_FILE
    try:
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    saved = store.load_nextcloud()
    if (type(data) is not dict or data.get("version") != 1 or saved is None
            or data.get("source_sha256") != _source_digest(saved)):
        return None
    instance, root_id, user = (data.get("instance_id"), data.get("root_resource_id"),
                               data.get("user_id"))
    if (type(instance) is not str or _INSTANCE.fullmatch(instance) is None
            or not _identifier(root_id) or not root_id.endswith(instance)
            or type(user) is not str or user != _account_user(
                urlsplit(saved["webdav_url"]).path)):
        return None
    return AccountEnrollment(instance, root_id, user, data["source_sha256"])


def context_provider(
    store: SetupStore,
    transport_factory: Callable[[dict], DavTransport],
    *,
    jobs_busy: Callable[[], bool],
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
) -> Callable[[], TrustedRootContext | None]:
    """Server-side ``TrustedRootContext`` for the V1/C picker and V1/D.

    ``None`` (picker shows ``binding_unavailable``) unless the account is
    enrolled for the saved URL + user and the active root's identity is known.
    """
    def build() -> TrustedRootContext | None:
        saved = store.load_nextcloud()
        enrollment = load_enrollment(store)
        revision = store.nextcloud_revision()
        if saved is None or enrollment is None or revision is None:
            return None
        account = TrustedDavAccount(_origin(saved["webdav_url"]),
                                    urlsplit(saved["webdav_url"]).path,
                                    enrollment.instance_id)
        pin = current_pin(store.state)
        if pin is not None and pin.chain and pin.path == saved["vault_folder"]:
            active_id = pin.chain[-1]
        else:
            active_id = read_root_identity(account, transport_factory(saved),
                                           saved["vault_folder"], proof=proof)
        if active_id is None:
            return None
        busy = jobs_busy()
        return TrustedRootContext(
            account=account, active=saved["vault_folder"], active_resource_id=active_id,
            container_resource_id=enrollment.root_resource_id,
            source_revision=revision, jobs_in_flight=busy is not False,
        )
    return build
