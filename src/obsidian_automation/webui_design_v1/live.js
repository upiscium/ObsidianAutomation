/* Fixed same-origin GET only. The standalone offline demo never loads this file. */
(function () {
  "use strict";
  const M = globalThis.ObsidianDesign;
  const reader = M.controlReader("same-origin-http");
  const status = document.getElementById("live-status");
  const summary = document.getElementById("live-summary");
  const grid = document.getElementById("live-services");
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function time(value) {
    if (value === null || value === undefined) return "Unknown";
    const date = new Date(value * 1000);
    return Number.isFinite(date.getTime()) ? date.toISOString() + " (UTC)" : "Unknown · outside display range";
  }
  function fact(list, label, value) {
    const row = node("div", undefined, "fact");
    row.append(node("dt", label), node("dd", value)); list.append(row);
  }
  function render() {
    const state = reader.snapshot();
    const ready = state.phase === "ready";
    grid.setAttribute("aria-busy", String(state.phase === "loading"));
    status.textContent = ready ? "LIVE · Read-only HTTP observation at " + time(state.observedAt) :
      state.phase === "loading" ? "Loading observation · previous results cleared." :
      "Observation unavailable · no current result. Use Refresh observation to retry.";
    summary.textContent = ready ? "5 workflow observations · " +
      state.services.filter(s => s.integration === "connected" && s.adapterMode !== "mock").length +
      " connected adapters. Connection is not proof of product success." : "No current observation. No actions are available here.";
    grid.replaceChildren();
    for (const s of state.services) {
      const card = node("article", undefined, "card");
      card.append(node("h2", s.id.toUpperCase() + " · " + s.name), node("p", s.description));
      if (!ready) {
        card.append(node("p", "Unavailable · current evidence unknown", "badge unknown"));
        grid.append(card); continue;
      }
      card.append(node("p", s.adapterMode === "mock" ? "Mock adapter observation · not production evidence" :
        "HTTP-derived read-only observation", "hint"));
      const facts = node("dl");
      fact(facts, "Integration", M.INTEGRATION[s.integration]);
      fact(facts, "Configuration", s.configuration === "invalid" ? "Invalid configuration" : M.CONFIGURATION[s.configuration]);
      fact(facts, "Schedule", M.SCHEDULE[s.schedule]);
      fact(facts, "Execution", s.execution);
      const [outcome, style] = M.outcome(s);
      card.append(facts, node("p", outcome, "badge " + style));
      const details = node("details");
      details.append(node("summary", "Observation details"));
      const detailFacts = node("dl");
      fact(detailFacts, "Source observed at", time(s.observedAt));
      fact(detailFacts, "Adapter mode", s.adapterMode === "mock" ? "Mock · synthetic source" : "Read-only source observation");
      fact(detailFacts, "Configuration reason", s.configurationReason || "None");
      fact(detailFacts, "Schedule reason", s.scheduleReason || "None");
      if (s.id !== "b5") {
        fact(detailFacts, "Interval", s.intervalSeconds === null ? "Unknown" : s.intervalSeconds + " seconds");
        fact(detailFacts, "Next due", time(s.nextRun));
      }
      fact(detailFacts, "Latest attempt created", time(s.lastAttemptAt));
      fact(detailFacts, "Latest attempt completed", time(s.lastAttempt?.completedAt));
      const history = s.lastVerifiedOutcome;
      fact(detailFacts, "Historical verified outcome (not current health)", history ?
        history.status + " · " + M.effectText(history.effect) + " · " + time(history.completedAt) : "Unknown");
      details.append(detailFacts);
      card.append(details, node("p", "Read-only · no workflow controls or admission available.", "hint"));
      grid.append(card);
    }
  }
  async function refresh() {
    const token = reader.begin(); render();
    const controller = new AbortController();
    let timer;
    try {
      // Deadline includes response decoding. Race still settles unavailable if
      // an implementation ignores abort, and the generation guard rejects any
      // older request's late success or rejection.
      const deadline = new Promise((_, reject) => {
        timer = setTimeout(() => { controller.abort(); reject(new Error("Observation timeout")); }, 10000);
      });
      const observation = (async () => {
        const response = await fetch("/api/v1/workflow-control", {
          method: "GET", mode: "same-origin", credentials: "omit", cache: "no-store",
          redirect: "error", headers: {Accept: "application/json"}, signal: controller.signal
        });
        if (response.status !== 200) throw new Error("Observation unavailable");
        return response.json();
      })();
      const payload = await Promise.race([observation, deadline]);
      if (reader.settle(token, payload)) render();
    } catch {
      if (reader.settle(token, null)) render();
    } finally {
      clearTimeout(timer);
    }
  }
  document.getElementById("refresh-observation").addEventListener("click", refresh);
  refresh();
})();
