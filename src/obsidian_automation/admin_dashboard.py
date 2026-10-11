"""Loopback-only, read-only ObsidianAutomation status dashboard (experimental).

No login because the listener is loopback only; not a public administration server.
No mutating method or arbitrary service/unit name is accepted.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Sequence

from .admin_status import aggregate_status

HTML = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ObsidianAutomation | Status</title>
  <link rel="stylesheet" href="/static/style.css">
  <script defer src="/static/app.js"></script>
</head>
<body>
  <main class="frame">
    <header>
      <div><p class="eyebrow">OBSIDIAN AUTOMATION</p>
        <h1>サービスの稼働状況</h1>
        <p class="desc">読み取り専用の管理Dashboard（試作版）</p></div>
      <button id="refresh" type="button">状態を更新</button>
    </header>
    <section aria-label="システムの説明" class="notice">
      <strong>Observation only</strong>
      <span>この画面からサービスの起動・停止やVaultへの書き込みはできません。</span>
    </section>
    <div class="summary">
      <span id="updated" aria-live="polite">状態を取得しています…</span>
      <span id="count">—</span>
    </div>
    <section id="services" class="cards" aria-label="サービス一覧" aria-live="polite"></section>
    <p id="error" class="error" role="status"></p>
    <footer>状態はsystemdの直近の観測結果です。成功履歴やVaultの完全性を証明するものではありません。</footer>
  </main>
</body>
</html>
"""

CSS = """
:root { color-scheme: light dark; font: 15px/1.6 system-ui, -apple-system, sans-serif;
  --bg:#f7f8fb; --surface:#ffffff; --ink:#182536; --muted:#5c6979;
  --border:#dce2e9; --accent:#315cb5; --focus:#3169db }
@media (prefers-color-scheme: dark) { :root { --bg:#111820; --surface:#1a2632; --ink:#f1f5fa;
  --muted:#b2becd; --border:#344455; --accent:#8dacff; --focus:#8dacff } }
* { box-sizing: border-box }
body { background:var(--bg); color:var(--ink); margin:0 }
.frame { max-width:1080px; padding:40px 22px; margin:auto }
header { display:flex; justify-content:space-between; align-items:center; gap:24px }
h1 { font-size:clamp(1.5rem, 4vw, 2.15rem); letter-spacing:-.04em; margin:.1em 0 .2em }
.eyebrow { color:var(--accent); font-weight:750; letter-spacing:.12em; font-size:.73rem; margin:0 }
.desc,.summary,footer { color:var(--muted) }
.desc { margin:.15em 0 }
button { background:var(--accent); color:var(--bg); border:0; padding:9px 16px; border-radius:9px;
  font:inherit; font-weight:700; cursor:pointer; white-space:nowrap }
button:focus-visible { outline:3px solid var(--focus); outline-offset:3px }
button:disabled { opacity:.55; cursor:wait }
.notice { background:var(--surface); border:1px solid var(--border); border-left:4px solid var(--accent);
  border-radius:10px; padding:13px 16px; display:flex; gap:16px; margin:26px 0 22px; flex-wrap:wrap }
.summary { display:flex; justify-content:space-between; gap:12px; margin:0 0 15px }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:16px }
.card { background:var(--surface); border:1px solid var(--border); border-radius:14px;
  padding:18px 19px; min-height:168px }
.card h2 { margin:0; font-size:1.08rem }
.card .small { font-size:.81rem; color:var(--muted); margin-top:8px; overflow-wrap:anywhere }
.headline { display:flex; justify-content:space-between; align-items:center; gap:8px }
.state { padding:3px 9px; border:1px solid var(--border); border-radius:999px;
  color:var(--muted); white-space:nowrap; font-size:.73rem; font-weight:700 }
.state[data-health="healthy"] { color:#2e9164 }
.state[data-health="failed"] { color:#cf5b51 }
.state[data-health="paused"] { color:#a77d2d }
.rowline { display:flex; justify-content:space-between; gap:12px; font-size:.88rem; margin-top:12px }
.rowline .value { color:var(--muted); text-align:right }
.error { color:#cb4545; min-height:1.5em }
footer { margin-top:32px; font-size:.78rem }
@media (max-width:620px) { .frame { padding:26px 14px } header { align-items:start; flex-direction:column }
  button { width:100% } .notice { margin-top:18px } }
"""

JS = """
"use strict";
const labels = {
  healthy:"正常", failed:"異常", paused:"一時停止", unknown:"未確認",
  unverified:"未検証", scheduled:"待機中", triggering:"起動処理中",
  not_installed:"未導入", blocked:"ブロック", stopped:"停止",
  no_next_run:"次回未設定", running:"実行中", succeeded:"直近成功",
  not_observed:"実行記録なし"
};
const $ = (id) => document.getElementById(id);
const stateText = (value) => labels[value] || "不明";
function line(name, value) {
  const row = document.createElement("div");
  row.className = "rowline";
  const label = document.createElement("span");
  label.textContent = name;
  const content = document.createElement("span");
  content.className = "value";
  content.textContent = stateText(value);
  row.append(label, content);
  return row;
}
function card(service) {
  const box = document.createElement("article");
  box.className = "card";
  const top = document.createElement("div");
  top.className = "headline";
  const title = document.createElement("h2");
  title.textContent = service.label;
  const status = document.createElement("span");
  status.className = "state";
  status.dataset.health = service.health;
  status.textContent = stateText(service.health);
  top.append(title, status);
  const name = document.createElement("div");
  name.className = "small";
  name.textContent = service.id;
  box.append(top, name, line("定期実行", service.schedule), line("直近の実行", service.latest_execution));
  return box;
}
async function refresh() {
  $("refresh").disabled = true;
  $("error").textContent = "";
  try {
    const response = await fetch("/api/v1/status", {cache:"no-store", credentials:"same-origin"});
    if (!response.ok) throw Error("server");
    const data = await response.json();
    if (data.version !== 1 || !Array.isArray(data.services)) throw Error("schema");
    $("services").replaceChildren(...data.services.map(card));
    $("updated").textContent = "観測日時: " + data.generated_at;
    $("count").textContent = data.services.length + " services";
  } catch {
    $("error").textContent = "状態を取得できませんでした。サーバー側の設定を確認してください。";
  } finally {
    $("refresh").disabled = false;
  }
}
$("refresh").addEventListener("click", refresh);
refresh();
"""

Observer = Callable[[], list[dict[str, object]]]


class Handler(BaseHTTPRequestHandler):
    server_version = "ObsidianStatus/0.1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        # Never log a browser URL, user input, exception, or systemd data.
        return

    def _reply(self, status: int, body: bytes = b"", mime: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _origin_allowed(self) -> bool:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        received_hosts = self.headers.get_all("Host", [])
        received_origins = self.headers.get_all("Origin", [])
        if len(received_hosts) != 1 or len(received_origins) > 1:
            return False
        origin = received_origins[0] if received_origins else None
        return (received_hosts[0] in hosts and (
            origin is None or origin in {f"http://{host}" for host in hosts}
        ))

    def do_GET(self) -> None:
        if not self._origin_allowed():
            self._reply(403, b"Forbidden")
            return
        if self.path == "/":
            self._reply(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/static/style.css":
            self._reply(200, CSS.encode("utf-8"), "text/css; charset=utf-8")
        elif self.path == "/static/app.js":
            self._reply(200, JS.encode("utf-8"), "text/javascript; charset=utf-8")
        elif self.path == "/api/v1/status":
            try:
                services = self.server.observer()
            except Exception:
                self._reply(503, b'{"error":"status_unavailable"}', "application/json")
                return
            data = json.dumps({
                "version": 1, "authority": "observation_only",
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "services": services,
                "actions": {"start": False, "stop": False, "trigger": False},
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self._reply(200, data, "application/json; charset=utf-8")
        else:
            self._reply(404, b"Not found")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        self._reply(405, b"Method not allowed")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST


def make_server(*, port: int = 8370, observer: Observer = aggregate_status) -> ThreadingHTTPServer:
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.observer = observer
    server.daemon_threads = True
    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimental read-only localhost status dashboard")
    parser.add_argument("--port", type=int, default=8370)
    args = parser.parse_args(argv)
    with make_server(port=args.port) as server:
        server.serve_forever(poll_interval=0.25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
