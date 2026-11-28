/* Failure banner. A service that failed several runs in a row is shown at the top
   of the page, so a stopped backup is noticed without opening each panel. Counts
   and times only; the data comes from GET /api/v1/health. */
(() => {
  "use strict";
  const mount = document.querySelector("main");
  if (!mount) return;
  const LABEL = {
    snapshot: "Snapshot（バックアップ）", daily: "GitHub Daily",
    core_promotion: "Core Promotion", knowledge: "知識提案", project_status: "Projectの状態更新"
  };
  const REFRESH_MS = 60000;

  function el(tag, props) { return Object.assign(document.createElement(tag), props || {}); }
  const when = (seconds) => (typeof seconds === "number")
    ? new Date(seconds * 1000).toLocaleString("ja-JP") : "記録なし";

  const banner = el("section", {id: "health-alert", className: "health-alert", hidden: true});
  banner.setAttribute("role", "alert");
  if (typeof mount.insertBefore === "function" && mount.firstChild) {
    mount.insertBefore(banner, mount.firstChild);
  } else {
    mount.appendChild(banner);
  }

  function render(data) {
    banner.textContent = "";
    const alerting = (data.services || []).filter((item) => item && item.alert === true);
    if (!alerting.length) { banner.hidden = true; return; }
    banner.appendChild(el("p", {className: "health-title",
      textContent: "連続で失敗しているサービスがあります"}));
    for (const item of alerting) {
      const name = LABEL[item.service] || item.service;
      banner.appendChild(el("p", {textContent:
        name + " が " + item.consecutive_failures + " 回連続で失敗しています。" +
        "失敗の開始: " + when(item.failing_since) + " / 最後の成功: " + when(item.last_success)}));
    }
    banner.appendChild(el("p", {className: "muted",
      textContent: "各パネルの「直近の実行」で、状態を確認してください。"}));
    banner.hidden = false;
  }

  async function refresh() {
    try {
      const response = await fetch("/api/v1/health", {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      render(await response.json());
    } catch (e) { /* the banner is advisory; a failed read leaves it as it was */ }
  }
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
