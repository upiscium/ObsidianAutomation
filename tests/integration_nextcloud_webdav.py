"""Opt-in real Nextcloud E2E against a disposable *synthetic* container (#601).

Set OA601_NC_PORT and OA601_NC_USER; OA601_NC_PASSWORD supplies the synthetic
admin password. The Nextcloud container is plain HTTP on localhost only, so the
transport's connection factory is redirected for the test; URL admission stays
https-only in production code. Creates only a throwaway folder it removes.
"""
from __future__ import annotations

import http.client
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from obsidian_automation import core_promotion_transport as transport
from obsidian_automation.webdav_connection import PROPFIND_BODY, _collection_multistatus

sys.path.insert(0, str(Path(__file__).parent))
from test_core_promotion_transport import _base_remote, _fixture

PORT = int(os.environ["OA601_NC_PORT"])
USER = os.environ["OA601_NC_USER"]
PASSWORD = os.environ["OA601_NC_PASSWORD"]
FOLDER = "oa601-e2e"
BASE = f"https://localhost:{PORT}/remote.php/dav/files/{USER}/{FOLDER}"


def plain_connect(url, *, timeout):
    assert url.hostname == "localhost" and url.port == PORT
    return http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)


def req(method, rel, body=None, headers=None):
    url = BASE + ("/" + rel if rel else "")
    return transport._real_http_request(
        method=method, target_url=url, headers=headers or {}, body=body,
        username=USER, password=PASSWORD, timeout=20, response_limit=1 << 20)


def put_seed(rel, data):
    parts = rel.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        r = req("MKCOL", quote("/".join(parts[:i]), safe="/") + "/")
        assert r.status in (201, 405), (rel, r.status)
    r = req("PUT", quote(rel, safe="/"), data)
    assert r.status in (201, 204), (rel, r.status)


def main():
    transport._connection = plain_connect
    assert req("DELETE", "").status in (204, 404)
    assert req("MKCOL", "").status == 201
    kw = dict(base_url=BASE, username=USER, password=PASSWORD, timeout=20)
    try:
        # 1. Real PROPFIND hrefs are accepted by the strict collection check.
        r = req("PROPFIND", "", PROPFIND_BODY,
                {"Depth": "0", "Content-Type": "application/xml"})
        assert r.status == 207 and _collection_multistatus(r.body, BASE + "/"), r
        print("NC root PROPFIND href strict match: PASS")

        # 2. Full promotion run: update + nested create (no pre-existing parents) + delete.
        with tempfile.TemporaryDirectory(prefix="oa601-nc-") as tmp:
            case = Path(tmp)
            repo, config, plan, checkpoint, receipts = _fixture(case)
            for name, data in _base_remote(plan, repo).items():
                put_seed(name, data)
            observed = transport.preflight_remote(plan, **kw)
            print("PRE-FLIGHT", [(p.change.action, p.remote.status_code, p.remote.etag,
                                  p.disposition) for p in observed])
            assert all(p.disposition == "apply" for p in observed)
            sha, receipt_path, receipt, advanced = transport.execute_promotion(
                plan=plan, core_repository=repo, config_path=config,
                checkpoint_path=checkpoint, receipt_directory=receipts, **kw)
            assert advanced.last_observed_core_commit == plan.head_commit
            assert {o.result for o in receipt.outcomes} == {"applied"}
            assert req("GET", "98-System/new/path/feature.css").body == b".feature{}\n"
            assert req("GET", "98-System/01-script/task.js").body == b"version=2\n"
            assert req("GET", "98-System/obsolete.md").status == 404
            print("NC full promotion (nested CREATE/UPDATE/DELETE, receipt, checkpoint): PASS")

        # 3. CAS conflicts on real Nextcloud.
        for hdr in ({"If-Match": '"stale-etag"'}, {"If-None-Match": "*"}):
            r = req("PUT", "98-System/01-script/task.js", b"must-not-overwrite", hdr)
            assert r.status in (412, 409), r.status
        assert req("GET", "98-System/01-script/task.js").body == b"version=2\n"
        print("NC If-Match / If-None-Match conflicts protect content: PASS")

        # 4. Review findings 1 & 3: awkward real-world names.
        for path in ("Weekly (2025)/it's @ 100, ok!/100% done.md",
                     "日本語/メモ 1.md", "a+b&c=d/x;y.md"):
            created = transport.ensure_parent_collections(path=path, transport=None, **kw)
            assert created, path
            assert transport.ensure_parent_collections(path=path, transport=None, **kw) == ()
            target = transport.build_target_url(BASE, path)
            r = transport._real_http_request(
                method="PUT", target_url=target,
                headers={"If-None-Match": "*", "Content-Type": "application/octet-stream"},
                body=b"x", username=USER, password=PASSWORD, timeout=20, response_limit=65536)
            assert r.status in (201, 204), (path, r.status)
            print("NC awkward path OK:", path)
        # A bad ancestor (a file) is found read-only in preflight, before any mutation.
        put_seed("blocker", b"file")
        try:
            transport.ensure_parent_collections(path="blocker/child/x.md", transport=None,
                                                create_missing=False, **kw)
        except transport.PromotionTransportConflict:
            print("NC file-as-ancestor refused read-only: PASS")
        else:
            raise AssertionError("file ancestor accepted")
    finally:
        req("DELETE", "")
    print("Synthetic Nextcloud folder cleaned")


if __name__ == "__main__":
    main()
