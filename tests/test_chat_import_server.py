"""Chat-history import panel API: opt-in, count-only, bounded upload. Synthetic only."""
import http.client
import io
import json
import threading
import zipfile

import pytest

from obsidian_automation.chat_import_service import ChatImportService
from obsidian_automation.three_services_server import make_server
from test_three_services_server import request

ORIGIN = "http://127.0.0.1:{port}"


def zip_bytes(payload, name="conversations.json"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, json.dumps(payload))
    return buffer.getvalue()


def claude(uuid="c1"):
    return {"uuid": uuid, "name": "t", "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z", "account": {"uuid": "a"},
            "chat_messages": [
                {"uuid": uuid + "m1", "sender": "human", "text": "private question"},
                {"uuid": uuid + "m2", "sender": "assistant",
                 "content": [{"type": "text", "text": "private answer"}]}]}


def chatgpt(cid="g1"):
    return {"id": cid, "title": "t", "current_node": "n2", "mapping": {
        "n1": {"id": "n1", "parent": None, "children": ["n2"], "message": {
            "id": "m1", "author": {"role": "user"}, "create_time": 1.0,
            "content": {"content_type": "text", "parts": ["private gpt question"]}}},
        "n2": {"id": "n2", "parent": "n1", "children": [], "message": {
            "id": "m2", "author": {"role": "assistant"}, "create_time": 2.0,
            "content": {"content_type": "text", "parts": ["private gpt answer"]}}}}}


class Server:
    def __init__(self, tmp_path, enabled=True, max_bytes=1 << 20):
        self.server = make_server(port=0, bind_address="127.0.0.1")
        if enabled:
            self.server.chat_import = ChatImportService(tmp_path, max_bytes=max_bytes)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def token(self):
        code, body = request(self.port, "GET", "/api/v1/session")
        return json.loads(body)["csrf"]

    def post_zip(self, data, *, csrf=True, origin=True, content_type="application/zip",
                 length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": content_type}
        if origin:
            headers["Origin"] = ORIGIN.format(port=self.port)
        if csrf:
            headers["X-Obsidian-CSRF"] = self.token()
        if length is not None:
            headers["Content-Length"] = str(length)
        conn.request("POST", "/api/v1/chat-import/upload", body=data, headers=headers)
        result = conn.getresponse()
        code, body = result.status, result.read()
        conn.close()
        return code, json.loads(body)

    def post_json(self, path, payload):
        code, body = request(self.port, "POST", path, payload=payload,
                             csrf=self.token(), origin=ORIGIN.format(port=self.port))
        return code, json.loads(body)


@pytest.fixture
def srv(tmp_path):
    server = Server(tmp_path)
    yield server
    server.close()


def test_disabled_by_default_and_page_never_names_providers(tmp_path):
    server = Server(tmp_path, enabled=False)
    try:
        assert request(server.port, "GET", "/api/v1/chat-import")[0] == 503
        code, page = request(server.port, "GET", "/")
        assert code == 200 and b"ChatGPT" not in page and b"Claude" not in page
        assert server.post_zip(zip_bytes([claude()]))[0] == 503
        assert server.post_json("/api/v1/chat-import/inventory",
                                {"provider": "claude", "commit": False})[0] == 503
        assert not (tmp_path / "chat-import").exists()
    finally:
        server.close()


def test_upload_requires_origin_csrf_exact_type_and_valid_length(srv):
    data = zip_bytes([claude()])
    assert srv.post_zip(data, origin=False)[0] == 403
    assert srv.post_zip(data, csrf=False)[0] == 403
    assert srv.post_zip(data, content_type="application/json")[0] == 400
    assert srv.post_zip(data, content_type="application/zip; x=1")[0] == 400
    assert srv.post_zip(b"")[1]["error"] == "invalid_request"


def test_claude_upload_inventory_roundtrip_is_count_only(srv, tmp_path):
    code, body = srv.post_zip(zip_bytes([claude("a"), claude("b")]))
    assert code == 200, body
    assert body["provider"] == "claude"
    assert body["counts"] == {"conversations": 2, "messages": 4, "empty_slots": 0}
    assert body["vault_write"] is False
    code, preview = srv.post_json("/api/v1/chat-import/inventory",
                                  {"provider": "claude", "commit": False})
    assert code == 200 and preview["status"] == "preview"
    assert preview["runs"][0]["added"] == 2
    code, commit = srv.post_json("/api/v1/chat-import/inventory",
                                 {"provider": "claude", "commit": True})
    assert code == 200 and commit["status"] == "committed_inventory_only"
    assert commit["generation_jobs_queued"] == 0
    code, status = request(srv.port, "GET", "/api/v1/chat-import")
    status = json.loads(status)
    assert code == 200 and status["ready_archives"] == 1
    assert status["scopes"]["private-claude"]["known_conversations"] == 2
    for text in (json.dumps(body), json.dumps(preview), json.dumps(commit),
                 json.dumps(status)):
        assert "private question" not in text and "private answer" not in text
    # temporary upload file is gone; only the ready archive remains
    assert not list((tmp_path / "chat-import" / "incoming").iterdir())
    assert len(list((tmp_path / "chat-import" / "ready").iterdir())) == 1


def test_chatgpt_upload_detected_and_scopes_are_provider_bound(srv):
    code, body = srv.post_zip(zip_bytes([chatgpt()]))
    assert code == 200 and body["provider"] == "chatgpt", body
    code, preview = srv.post_json("/api/v1/chat-import/inventory",
                                  {"provider": "chatgpt", "commit": True})
    assert code == 200 and preview["runs"][0]["added"] == 1
    # The same ZIP read as Claude is rejected and recorded, never half-imported.
    code, wrong = srv.post_json("/api/v1/chat-import/inventory",
                                {"provider": "claude", "commit": False})
    assert code == 200 and wrong["runs"][0]["status"] == "normalization_rejected"


def test_garbage_and_oversize_and_bad_inventory_requests_are_refused(srv, tmp_path):
    assert srv.post_zip(b"this is not a zip")[1]["error"] == "not_a_valid_export"
    assert srv.post_zip(zip_bytes([{"unrelated": 1}]))[1]["error"] == "unsupported_export_format"
    big = Server(tmp_path / "small", max_bytes=100)
    try:
        code, body = big.post_zip(zip_bytes([claude()]))
        assert code == 413 and body["error"] == "upload_too_large"
    finally:
        big.close()
    for payload in ({"provider": "x", "commit": False},
                    {"provider": "claude", "commit": "yes"},
                    {"provider": "claude"},
                    {"provider": "claude", "commit": False, "extra": 1}):
        assert srv.post_json("/api/v1/chat-import/inventory", payload)[0] == 400


def test_truncated_upload_is_refused_and_cleaned(srv, tmp_path):
    data = zip_bytes([claude()])
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.putrequest("POST", "/api/v1/chat-import/upload")
    conn.putheader("Content-Type", "application/zip")
    conn.putheader("Content-Length", str(len(data) + 50))
    conn.putheader("Origin", ORIGIN.format(port=srv.port))
    conn.putheader("X-Obsidian-CSRF", srv.token())
    conn.endheaders()
    conn.send(data)
    conn.sock.shutdown(1)  # half-close: server sees EOF before Content-Length bytes
    result = conn.getresponse()
    assert result.status == 400 and json.loads(result.read())["error"] == "upload_truncated"
    conn.close()
    incoming = tmp_path / "chat-import" / "incoming"
    assert not list(incoming.iterdir())
