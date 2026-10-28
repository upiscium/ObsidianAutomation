(function () {
  "use strict";
  const M = globalThis.ObsidianDesign;
  const $ = id => document.getElementById(id);
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const link = (text, href) => { const a = el("a", text); a.href = href; return a; };
  let state = M.scenario("catalog");
  let selectedFile = false;
  function fact(list, title, value) {
    const row = el("div", undefined, "fact");
    row.append(el("dt", title), el("dd", value)); list.append(row);
  }
  function outcomeBadge(service) { const [text, tone] = M.outcome(service); return el("span", text, "badge " + tone); }
  function integrationText(s) { return s.controlProjection ? "Synthetic read-only · " + M.INTEGRATION[s.integration] + " (fixture, not live)" : s.synthetic ? "Demo only · not connected" : M.INTEGRATION[s.integration]; }
  const controlTime = value => value === null ? "Unknown" : value + " Unix seconds UTC (synthetic)";
  function historyText(s) {
    const h = s.lastVerifiedOutcome;
    return h ? "Historical " + h.status + " · " + M.effectText(h.effect) + " · " + controlTime(h.completedAt) + " · not current health" : "Unknown · no verified history";
  }
  function serviceCard(s) {
    const article = el("article", undefined, "card");
    article.setAttribute("aria-label", s.id.toUpperCase() + " " + s.name);
    article.append(el("p", s.id.toUpperCase(), "eyebrow"), el("h3", s.name),
      el("p", s.description, "hint"), el("p", "● " + integrationText(s), "badge unknown"));
    const facts = el("dl");
    fact(facts, "Execution", s.execution);
    fact(facts, s.id === "b5" ? "Mode" : "Schedule", M.SCHEDULE[s.schedule]);
    article.append(facts, outcomeBadge(s));
    if (s.controlProjection) article.append(el("p", historyText(s), "historical-outcome"));
    const actions = el("div", undefined, "actions");
    actions.append(link("Settings / details", "#service/" + s.id));
    article.append(actions); return article;
  }
  function renderOverview() {
    const grid = $("service-grid"); grid.replaceChildren();
    for (const s of state.services) grid.append(serviceCard(s));
    const health = el("aside", undefined, "card health-summary");
    health.setAttribute("aria-label", "Global service health summary, not a backend");
    health.append(el("p", "GLOBAL SUMMARY · NOT A SIXTH SERVICE", "eyebrow"), el("h3", "Global service health"),
      el("p", "? Unknown · no live health evidence", "badge unknown"),
      el("p", "Five product workflows. These observations do not prove connectivity, credentials, scheduler health or Vault integrity."),
      el("p", state.phase === "stale" ? "Stale · previous observations must not authorize actions." : "Offline preview · no live observation timestamp.", "hint"));
    grid.append(health);
  }
  function mockButton(text, service, action) {
    const button = el("button", text); button.type = "button";
    button.disabled = state.phase !== "ready";
    button.addEventListener("click", () => {
      const result = M.simulate(service, action);
      state.services = state.services.map(s => s.id === service.id ? result.service : s);
      renderOverview(); renderDetail(service.id);
      $("command-message").textContent = result.message;
      // Re-render replaces controls; explicitly restore keyboard focus.
      const replacement = $("detail-content").querySelector('[data-action="' + action + '"]');
      if (replacement && !replacement.disabled) replacement.focus();
      else $("detail-title").focus();
    });
    button.dataset.action = action; return button;
  }
  function renderDetail(id) {
    const s = state.services.find(item => item.id === id);
    selectedFile = false;
    $("detail-title").textContent = s.name;
    $("detail-id").textContent = s.id.toUpperCase() + " / " + (s.synthetic ? "SYNTHETIC DEMONSTRATION" : "MOCK CATALOG OBSERVATION");
    const content = $("detail-content"); content.replaceChildren();
    content.append(el("p", s.description), el("p", integrationText(s), "badge unknown"));
    const facts = el("dl");
    fact(facts, "Configuration", s.configuration === "invalid" ? "Invalid configuration" : s.synthetic ? "Demo · " + M.CONFIGURATION[s.configuration] : M.CONFIGURATION[s.configuration]);
    fact(facts, "Execution", s.execution);
    if (s.id === "b5") fact(facts, "Mode", "Manual ZIP Import · no timer");
    else fact(facts, "Schedule", M.SCHEDULE[s.schedule]);
    if (s.controlProjection) {
      fact(facts, "Observation timestamp", controlTime(s.observedAt));
      fact(facts, "Configuration reason", s.configurationReason || "None");
      fact(facts, "Schedule reason", s.scheduleReason || "None");
      fact(facts, "Last attempt admitted", controlTime(s.lastAttemptAt));
      fact(facts, "Latest attempt completed", controlTime(s.lastAttempt?.completedAt ?? null));
    } else fact(facts, "Last run timestamp", s.lastRun ? s.lastRun + " (demo · UTC)" : "Unknown · not provided by catalog v1");
    const resultRow = el("div", undefined, "fact");
    const resultValue = el("dd"); resultValue.append(outcomeBadge(s));
    resultRow.append(el("dt", s.controlProjection ? "Latest attempt (not historical result)" : "Last verified result"), resultValue); facts.append(resultRow);
    if (s.controlProjection) fact(facts, "Historical verified outcome", historyText(s));
    if (s.id !== "b5") fact(facts, "Next run", s.controlProjection ? s.nextRun === null ? s.schedule === "off" ? "None · scheduling disabled" : "Unknown · not a promised start" : controlTime(s.nextRun) + " · eligibility estimate only" : s.nextRun ? s.nextRun + " (demo · UTC)" : s.schedule === "off" ? "None · scheduling disabled" : "Unknown · not provided by catalog v1");
    content.append(facts);
    const actions = el("div", undefined, "actions");
    const existing = M.controlsLink(s.id);
    if (s.controlProjection) {
      content.append(el("p", "Read-only projection · all controls unavailable. Observation and available actions are not admission authority. No command, staging or execution receipt is created.", "notice"));
    } else if (existing) {
      actions.append(link("Existing " + (s.id === "b2" ? "Snapshot" : "Daily") + " Controls ↗", existing));
      content.append(el("p", "Navigation only: leaves this preview for the existing dashboard. No operation is performed here. Its own availability and safety checks still apply.", "hint"));
    } else if (s.id === "b5") {
      const label = el("label", "Choose a dummy ZIP (not read or uploaded)", "import-label");
      const input = el("input"); input.type = "file"; input.accept = ".zip,application/zip";
      input.disabled = state.phase !== "ready"; input.id = "mock-zip";
      label.htmlFor = "mock-zip"; label.append(input); content.append(label);
      const button = mockButton("Simulate ZIP Import", s, "import"); button.disabled = true;
      input.addEventListener("change", () => {
        // Read the local filename only for its .zip suffix. Never display, upload,
        // retain it or inspect file contents.
        selectedFile = !!input.files && input.files.length === 1 && /\.zip$/i.test(input.files[0].name);
        button.disabled = !selectedFile || state.phase !== "ready";
        $("command-message").textContent = selectedFile ? "ZIP selected locally. Contents are not inspected; this is not ZIP validation." : "Choose one dummy .zip file. No file was uploaded.";
      });
      actions.append(button);
    } else {
      actions.append(mockButton(s.id === "b3" ? "Simulate Sync now" : "Simulate Run once", s, "run"),
        mockButton(s.schedule === "on" ? "Simulate Disable" : "Simulate Enable", s, "toggle"));
    }
    actions.append(link("Connections", "#connections")); content.append(actions);
    content.append(el("p", "Simulations are not admission or execution receipts. Real operations remain unavailable pending separately reviewed integration.", "hint"));
  }
  function currentId() {
    const match = /^#service\/(b[1-5])$/.exec(location.hash); return match ? match[1] : null;
  }
  function route(focus) {
    const id = currentId();
    const connections = location.hash === "#connections";
    $("overview-view").hidden = !!id || connections;
    $("detail-view").hidden = !id;
    $("connections-view").hidden = !connections;
    $("command-message").textContent = "";
    // Never preserve entered secrets across navigation.
    for (const input of $("connections-form").querySelectorAll('input[type="password"]')) input.value = "";
    if (id) renderDetail(id);
    setMenu(false);
    if (focus) $(location.hash === "#legend" ? "legend-title" : id ? "detail-title" : connections ? "connections-title" : "overview-title").focus();
  }
  function setMenu(open) {
    $("navigation").hidden = !open;
    $("menu-toggle").setAttribute("aria-expanded", String(open));
    $("menu-toggle").setAttribute("aria-label", open ? "Close navigation menu" : "Open navigation menu");
  }
  function reset() {
    state = M.scenario($("scenario").value);
    const messages = {ready: "Mock data · not a live status report.", loading: "Loading mock observation · all actions unavailable.", unavailable: "Observation unavailable · no prior success or connectivity is assumed. Try Reset mock or change the preview state.", stale: "Stale observation · refresh required; all simulated actions disabled."};
    $("app-status").textContent = messages[state.phase];
    renderOverview(); route(false);
  }
  function clearValidation(form) {
    for (const input of form.querySelectorAll("input")) { input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby"); }
  }
  $("menu-toggle").addEventListener("click", () => setMenu($("navigation").hidden));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !$("navigation").hidden) { setMenu(false); $("menu-toggle").focus(); }
  });
  $("scenario").addEventListener("change", reset);
  $("reset-demo").addEventListener("click", () => { $("connections-form").reset(); clearValidation($("connections-form")); $("connection-message").textContent = "Not validated · no connection attempted."; reset(); });
  $("connections-form").addEventListener("submit", event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const errors = M.validateFields(values);
    clearValidation(form);
    for (const input of form.querySelectorAll('input[type="password"]')) { input.value = ""; delete values[input.name]; }
    $("connection-message").textContent = errors.length ? "Validation failed. " + errors[0].message + " Nothing sent or saved; secret fields cleared." : "Local format check complete. Connectivity and credentials NOT verified. Nothing sent or saved; secret fields cleared.";
    for (const error of errors) {
      const input = form.elements.namedItem(error.field);
      input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", "connection-message");
    }
    if (errors.length) form.elements.namedItem(errors[0].field).focus();
  });
  globalThis.addEventListener("hashchange", () => route(true));
  globalThis.addEventListener("pagehide", () => { $("connections-form").reset(); });
  reset();
})();
