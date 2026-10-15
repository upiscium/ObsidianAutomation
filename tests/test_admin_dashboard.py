from __future__ import annotations

import http.client
import json
import re
import threading
from contextlib import contextmanager
from html.parser import HTMLParser

import pytest

from obsidian_automation.admin_dashboard import Handler, JS, make_server


class DashboardHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.ids: set[str] = set()
        self.navs: list[dict[str, object]] = []
        self.sections: dict[str, dict[str, object]] = {}
        self._nav_stack: list[dict[str, object]] = []
        self._section_stack: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)

        if tag == "nav":
            nav: dict[str, object] = {"label": attributes.get("aria-label"), "links": []}
            self.navs.append(nav)
            self._nav_stack.append(nav)
        if tag == "section":
            section: dict[str, object] = {"tags": set(), "headings": [], "text": []}
            if element_id:
                self.sections[element_id] = section
            self._section_stack.append(section)

        for section in self._section_stack:
            section["tags"].add(tag)
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                section["headings"].append(tag)
        if tag == "a":
            for nav in self._nav_stack:
                nav["links"].append(attributes.get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav" and self._nav_stack:
            self._nav_stack.pop()
        if tag == "section" and self._section_stack:
            self._section_stack.pop()

    def handle_data(self, data: str) -> None:
        for section in self._section_stack:
            section["text"].append(data)


@contextmanager
def serving(observer, ai_observer=None):
    if ai_observer is None:
        ai_observer = lambda: {
            "freshness": "unavailable", "health": "unknown",
            "generated_at": None, "age_seconds": None,
            "review_count": None, "operational_failures": None,
            "deterministic_rejections": None, "backpressure": None,
            "review_reminder_due": None,
        }
    server = make_server(port=0, observer=observer, ai_observer=ai_observer)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def response(server, method="GET", path="/api/v1/status", *, host=None, origin=None):
    assert server.server_address[0] == "127.0.0.1"
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
    if origin is not None:
        headers["Origin"] = origin
    try:
        connection.request(method, path, headers=headers)
        reply = connection.getresponse()
        body = reply.read()
        return reply.status, dict(reply.getheaders()), body
    finally:
        connection.close()


def fixture_observer():
    return [
        {
            "id": "ai-vault-pull", "label": "Vault Mirror",
            "timer": "obsidian-ai-vault-pull.timer",
            "service": "obsidian-ai-vault-pull.service",
            "schedule": "scheduled", "latest_execution": "succeeded",
            "health": "observed_success",
        }
    ]


def _section_text(section: dict[str, object]) -> str:
    return " ".join(section["text"]).casefold()


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase.casefold() in text for phrase in phrases)


def _mobile_media_block(css: str) -> str:
    match = re.search(r"@media\s*\(\s*max-width\s*:\s*620px\s*\)\s*\{", css, re.IGNORECASE)
    assert match is not None, "mobile styles must target a maximum width of 620px"
    opening_brace = css.find("{", match.start())
    depth = 0
    for index in range(opening_brace, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[opening_brace + 1:index]
    raise AssertionError("mobile media query is not closed")


def test_is_bound_to_loopback_and_json_observation_only() -> None:
    with serving(fixture_observer) as server:
        code, headers, body = response(server)
        assert code == 200
        value = json.loads(body)
        assert value["version"] == 1
        assert value["authority"] == "observation_only"
        assert value["actions"] == {"start": False, "stop": False, "trigger": False}
        assert len(value["services"]) == 1
        assert value["services"][0]["health"] == "observed_success"
        assert value["ai_pipeline"]["freshness"] == "unavailable"
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in headers


def test_browsable_document_and_external_assets() -> None:
    with serving(fixture_observer) as server:
        assert response(server, path="/")[0] == 200
        assert b"ObsidianAutomation" in response(server, path="/")[2]
        css = response(server, path="/static/style.css")
        js = response(server, path="/static/app.js")
        assert css[0] == 200 and css[1]["Content-Type"].startswith("text/css")
        assert js[0] == 200 and js[1]["Content-Type"].startswith("text/javascript")
        assert b"innerHTML" not in js[2]  # use textContent for untrusted data
        assert "textContent" in JS
        assert response(server, path="/../../../etc/passwd")[0] == 404


def test_phase_a_shell_navigation_sections_and_existing_status_hooks() -> None:
    with serving(fixture_observer) as server:
        status, _, body = response(server, path="/")

    assert status == 200
    parser = DashboardHTMLParser()
    parser.feed(body.decode("utf-8"))
    expected_sections = ["overview", "services-section", "connections", "setup-wizard", "vaults"]

    navigation = [nav for nav in parser.navs if nav["label"] == "ページ内ナビゲーション"]
    assert len(navigation) == 1
    assert navigation[0]["links"] == [f"#{section_id}" for section_id in expected_sections]
    for section_id in expected_sections:
        assert section_id in parser.sections
        assert parser.sections[section_id]["headings"], f"#{section_id} needs a section heading"

    assert {"updated", "count", "pipeline", "pipeline-summary", "pipeline-details",
            "services", "error", "refresh"}.issubset(parser.ids)
    assert any(tag == "button" and attrs.get("id") == "refresh" for tag, attrs in parser.elements)


def test_phase_a_placeholders_and_setup_checklist_are_explicitly_read_only() -> None:
    with serving(fixture_observer) as server:
        status, _, body = response(server, path="/")

    assert status == 200
    parser = DashboardHTMLParser()
    parser.feed(body.decode("utf-8"))
    connections = _section_text(parser.sections["connections"])
    setup = _section_text(parser.sections["setup-wizard"])
    vaults = _section_text(parser.sections["vaults"])
    connection_and_setup_text = f"{connections} {setup}"
    unavailable = (
        "未接続", "未設定", "未提供", "未対応", "未実装", "未構築", "対象外", "利用できません",
        "利用できない", "利用不可", "使用できません", "使用できない", "使用不可", "ありません", "なし",
        "not connected", "not configured", "not provided", "not implemented", "not available",
        "unavailable", "not usable", "cannot be used", "no connection", "not wired", "no backend",
    )

    assert _contains_any(connections, ("接続", "connection"))
    assert _contains_any(connections, unavailable)
    assert _contains_any(setup, ("設定", "構成", "config", "configuration"))
    assert _contains_any(setup, unavailable)
    assert _contains_any(connection_and_setup_text, ("バックエンド", "backend"))
    assert _contains_any(connection_and_setup_text, unavailable)
    assert _contains_any(vaults, ("vault", "保管庫"))
    assert _contains_any(vaults, (
        "利用できません", "利用できない", "利用不可", "使えません", "使えない", "使用できません",
        "使用不可", "not usable", "cannot be used", "unavailable", "not available",
    ))

    checklist_tags = parser.sections["setup-wizard"]["tags"]
    assert "li" in checklist_tags and ("ul" in checklist_tags or "ol" in checklist_tags)
    assert _contains_any(_section_text(parser.sections["setup-wizard"]), (
        "読み取り専用", "read-only", "確認のみ", "変更できません",
    ))


def test_phase_a_mobile_rules_and_browser_are_read_only() -> None:
    with serving(fixture_observer) as server:
        responses = {
            path: response(server, path=path)
            for path in ("/", "/static/style.css", "/static/app.js", "/api/v1/status")
        }

    assert all(result[0] == 200 for result in responses.values())
    css = responses["/static/style.css"][2].decode("utf-8")
    mobile_rules = _mobile_media_block(css)
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", mobile_rules)
    nav_rules = [declarations for selector, declarations in rules if re.search(r"\bnav\b", selector)]
    card_rules = [declarations for selector, declarations in rules if re.search(r"cards", selector)]
    assert any(re.search(r"(?:flex-wrap|overflow(?:-[xy])?|flex-direction|gap)\s*:", rule) for rule in nav_rules)
    assert any(re.search(r"(?:grid-template-columns|grid-template|grid-auto-flow|flex-direction)\s*:", rule)
               for rule in card_rules)

    html = responses["/"][2].decode("utf-8")
    js = responses["/static/app.js"][2].decode("utf-8")
    api_body = responses["/api/v1/status"][2].decode("utf-8")
    api_value = json.loads(api_body)
    parser = DashboardHTMLParser()
    parser.feed(html)
    served_content = html + css + js + api_body
    assert not re.search(r"\b(?:localStorage|sessionStorage)\b", served_content, re.IGNORECASE)
    assert not {"form", "input", "select", "textarea"}.intersection(tag for tag, _ in parser.elements)
    assert not re.search(
        r"createElement\(\s*['\"](?:form|input|select|textarea)['\"]\s*\)", js, re.IGNORECASE
    )
    assert not re.search(r"<\s*(?:form|input|select|textarea)\b", js, re.IGNORECASE)
    assert [attrs.get("id") for tag, attrs in parser.elements if tag == "button"] == ["refresh"]
    assert "innerHTML" not in js
    assert not re.search(r"\bmethod\s*:\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]", js, re.IGNORECASE)
    fetch_paths = re.findall(r"fetch\(\s*['\"]([^'\"]+)['\"]", js)
    assert fetch_paths == ["/api/v1/status"]
    assert api_value["authority"] == "observation_only"
    assert api_value["actions"] == {"start": False, "stop": False, "trigger": False}

    def field_names(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                yield str(key).casefold()
                yield from field_names(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from field_names(nested)

    forbidden_fields = {"password", "token", "secret", "credential", "credentials", "authorization"}
    assert forbidden_fields.isdisjoint(field_names(api_value))

    csp_required = (
        "default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'",
        "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'",
    )
    for result in responses.values():
        csp = result[1]["Content-Security-Policy"]
        assert all(directive in csp for directive in csp_required)


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_no_state_changing_routes(verb: str) -> None:
    with serving(fixture_observer) as server:
        code, _, body = response(server, method=verb)
        assert code == 405
        assert body == b"Method not allowed"


def test_placeholder_and_save_routes_do_not_exist() -> None:
    unknown_read_paths = (
        "/api/v1/connections", "/api/v1/config", "/api/v1/setup", "/api/v1/vaults", "/api/v1/save",
    )
    write_methods = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS")
    with serving(fixture_observer) as server:
        for path in unknown_read_paths:
            assert response(server, path=path)[0] == 404
            for method in write_methods:
                status, _, body = response(server, method=method, path=path)
                assert status == 405
                assert body == b"Method not allowed"


def test_host_validation_and_origin_validation() -> None:
    with serving(fixture_observer) as server:
        port = server.server_address[1]
        assert response(server, host="attacker.example")[0] == 403
        assert response(server, host="127.0.0.1.attacker.invalid")[0] == 403
        assert response(server, origin="https://attacker.example")[0] == 403
        assert response(server, origin=f"http://127.0.0.1:{port}")[0] == 200
        assert response(server, host=f"localhost:{port}")[0] == 200


def test_duplicate_host_header_is_rejected() -> None:
    with serving(fixture_observer) as server:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.putrequest("GET", "/", skip_host=True)
            conn.putheader("Host", f"127.0.0.1:{port}")
            conn.putheader("Host", "attacker.invalid")
            conn.endheaders()
            r = conn.getresponse()
            assert r.status == 403
            r.read()
        finally:
            conn.close()


def test_observer_failure_is_redacted_and_not_a_200() -> None:
    def fail():
        raise RuntimeError("UNEXPECTED_DIAGNOSTIC_CANARY")
    with serving(fail) as server:
        status, headers, body = response(server)
        assert status == 503
        assert json.loads(body) == {"error": "status_unavailable"}
        assert b"UNEXPECTED_DIAGNOSTIC_CANARY" not in body


def test_ai_pipeline_projection_is_sanitized_and_not_a_health_override() -> None:
    def ai_projection():
        return {
            "freshness": "fresh", "health": "critical",
            "generated_at": "2026-10-11T11:00:00Z", "age_seconds": 1,
            "review_count": 2, "operational_failures": 3,
            "pending_retry_failures": 1,
            "incidents": [
                {"code": "blocked_generations", "severity": "critical", "affected": 3}
            ],
            "deterministic_rejections": 4,
            "backpressure": True, "review_reminder_due": False,
        }
    with serving(fixture_observer, ai_observer=ai_projection) as server:
        status, _, raw = response(server)
        data = json.loads(raw)
        assert status == 200
        assert data["ai_pipeline"]["health"] == "critical"
        assert data["ai_pipeline"]["review_count"] == 2
        assert data["ai_pipeline"]["operational_failures"] == 3
        assert data["services"][0]["health"] == "observed_success"
        assert "Authorization" not in raw.decode("utf-8")


def test_ai_projection_loader_failure_is_redacted() -> None:
    def ai_failure():
        raise ValueError("password=secret-token")
    with serving(fixture_observer, ai_observer=ai_failure) as server:
        status, _, raw = response(server)
        assert status == 503
        assert json.loads(raw) == {"error": "status_unavailable"}
        assert b"secret-token" not in raw


def test_head_no_response_body() -> None:
    with serving(fixture_observer) as server:
        code, headers, body = response(server, method="HEAD")
        assert code == 200
        assert body == b""
        assert int(headers["Content-Length"]) > 0


def test_failed_refresh_drops_previous_healthy_observations() -> None:
    # A request error must not leave the old "healthy" cards as current.
    error_branch = JS.split("} catch {", 1)[1].split("} finally {", 1)[0]
    assert '"services").replaceChildren()' in error_branch
    assert '"pipeline-details").replaceChildren()' in error_branch
    assert '"pipeline-summary").textContent' in error_branch
    assert '"updated").textContent = "観測失敗"' in error_branch


def test_invalid_port_rejected() -> None:
    with pytest.raises(ValueError):
        make_server(port=-1)
    with pytest.raises(ValueError):
        make_server(port=65536)

def test_status_javascript_renders_distinct_incident_and_retry_metrics() -> None:
    from obsidian_automation.admin_dashboard import JS
    assert 'metric("要対応エラー", value(p.operational_failures))' in JS
    assert 'metric("失敗・再試行待ち（予算未確認）", value(p.pending_retry_failures))' in JS
    assert 'retryable_failure_pending' in JS
    assert 'metric("Validation棄却（正常）", value(p.deterministic_rejections))' in JS
    assert 'retry_exhausted_generations' in JS
    assert 'blocked_generations' in JS
    assert 'innerHTML' not in JS
