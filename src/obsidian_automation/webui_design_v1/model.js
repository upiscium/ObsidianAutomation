/* Offline presentation only: legacy catalog compatibility + strict #480 read DTO. */
(function (root) {
  "use strict";
  const SERVICES = Object.freeze([
    {id: "b1", name: "AI review & derived notes", description: "Generate reviewable notes from an existing Vault. AI Provider required."},
    {id: "b2", name: "Vault Snapshot → Gitea", description: "Back up Vault snapshots and history to private Gitea."},
    {id: "b3", name: "Project data → Vault", description: "Reflect project updates through Project / Mirror / Writer / Sync."},
    {id: "b4", name: "GitHub progress → Daily Note", description: "Deterministic progress without an LLM. Preview is not publication."},
    {id: "b5", name: "ChatGPT Export import", description: "Manual ZIP intake. No periodic schedule is required."}
  ].map(Object.freeze));
  const INTEGRATION = Object.freeze({connected: "Connected adapter", pending: "Pending integration", not_attached: "Not attached", unavailable: "Unavailable"});
  const CONFIGURATION = Object.freeze({configured: "Configured", missing: "Missing configuration", not_observed: "Unknown"});
  const SCHEDULE = Object.freeze({on: "Enabled", off: "Disabled", not_observed: "Unknown", not_applicable: "Manual only"});
  const RESULTS = Object.freeze({
    saved: ["✓ Succeeded · snapshot saved", "success"],
    no_changes: ["✓ Succeeded · no changes", "success"],
    published: ["✓ Succeeded · publication verified", "success"],
    preview_ready: ["Preview ready · Vault outcome unknown", "unknown"],
    failed: ["! Failed", "failure"], publish_failed: ["! Publication failed", "failure"],
    unconfirmed: ["? Unknown · unconfirmed", "unknown"], unknown: ["? Unknown", "unknown"],
    queued: ["? Unknown · queued, no verified outcome", "unknown"],
    running: ["? Unknown · running, no verified outcome", "unknown"]
  });
  const owns = (object, key) => typeof key === "string" && Object.hasOwn(object, key);
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
  function blank(service) {
    return {...service, integration: "unavailable", configuration: "not_observed",
      schedule: service.id === "b5" ? "not_applicable" : "not_observed",
      execution: "Unknown", result: "unknown", lastRun: null, nextRun: null, synthetic: false};
  }
  /** Strict compatibility boundary for existing GET /api/v1/workflows v1.
   * Labels/notes/extra fields are never copied: this API has no timestamp or
   * command authority. A future contract needs an explicitly reviewed adapter.
   */
  function fromCatalog(payload) {
    if (!record(payload) || payload.version !== 1 || payload.authority !== "observation_only" ||
        !Array.isArray(payload.workflows) || payload.workflows.length !== 5) throw new Error("Unsupported catalog");
    const byId = new Map();
    for (const item of payload.workflows) {
      if (!record(item) || !SERVICES.some(s => s.id === item.id) || byId.has(item.id) ||
          !owns(INTEGRATION, item.integration) || !owns(CONFIGURATION, item.configuration) ||
          !owns(SCHEDULE, item.schedule) || typeof item.control_available !== "boolean" ||
          !(item.last_result === null || owns(RESULTS, item.last_result)) ||
          (item.id === "b5" ? item.schedule !== "not_applicable" : item.schedule === "not_applicable")) {
        throw new Error("Invalid catalog");
      }
      byId.set(item.id, item);
    }
    return SERVICES.map(service => {
      const item = byId.get(service.id);
      // An unavailable adapter cannot lend credibility to stale success fields.
      if (item.integration === "unavailable") return blank(service);
      return {...blank(service), integration: item.integration, configuration: item.configuration,
        schedule: item.schedule, result: item.last_result || "unknown",
        execution: item.last_result === "queued" ? "Queued" : item.last_result === "running" ? "Running" : "Unknown"};
    });
  }
  function mockCatalog() {
    return {version: 1, authority: "observation_only", workflows: SERVICES.map(s => ({
      id: s.id, integration: s.id === "b2" ? "not_attached" : "pending",
      configuration: "not_observed", schedule: s.id === "b5" ? "not_applicable" : "not_observed",
      last_result: null, control_available: false
    }))};
  }
  const CONTROL_VERSION = "webui-workflow-control/v1";
  const ACTIONS = Object.freeze({b1: ["generate", "set_schedule"], b2: ["run_once", "set_schedule"],
    b3: ["sync_project", "set_schedule"], b4: ["run_once", "set_schedule"], b5: ["stage_archive", "import_inventory"]});
  const REASONS = Object.freeze(["pending_integration", "not_attached", "observation_unavailable", "read_only", "mock_only",
    "configuration_missing", "configuration_invalid", "busy", "scheduler_decision_required", "input_required",
    "reconciliation_required", "due_unavailable", "manual_only", "unsupported_action", "stale_revision", "authority_rejected"]);
  const EFFECTS = Object.freeze({b1: ["review_artifact"], b2: ["snapshot_ref"], b3: ["project_vault"],
    b4: ["daily_preview", "daily_vault"], b5: ["private_inventory"]});
  const valid = condition => { if (!condition) throw new Error("Invalid workflow observation"); };
  // JSON-shaped, exact own data fields only; neither extensions nor accessors are read.
  function fields(value, keys) {
    valid(record(value));
    const proto = Object.getPrototypeOf(value);
    valid(proto === null || Object.getPrototypeOf(proto) === null);
    valid(Reflect.ownKeys(value).length === keys.length);
    for (const key of keys) {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      valid(!!descriptor && owns(descriptor, "value") && descriptor.enumerable);
    }
  }
  const choice = (value, values) => valid(values.includes(value));
  const timestamp = value => valid(typeof value === "number" && Number.isFinite(value) && value >= 0);
  const handle = value => valid(typeof value === "string" && /^[a-f0-9]{32}$/.test(value));
  const reason = value => { if (value !== null) choice(value, REASONS); };
  function past(value, observedAt) { if (value !== null) { timestamp(value); valid(value <= observedAt); } }
  function effect(value, id) {
    fields(value, ["kind", "state", "changed"]);
    choice(value.kind, ["none", ...EFFECTS[id]]);
    choice(value.state, ["none", "verified", "unknown"]);
    if (value.kind === "none") valid(value.state === "none" && value.changed === null);
    else {
      valid(value.state !== "none");
      valid(value.state === "verified" ? value.changed === null || typeof value.changed === "boolean" : value.changed === null);
    }
  }
  function validateWorkflow(w, id, observedAt) {
    fields(w, ["schemaVersion", "id", "label", "adapterMode", "revision", "observedAt", "integration", "configuration",
      "schedule", "execution", "lastAttempt", "lastVerifiedOutcome", "lastAttemptAt", "availableActions", "actionReasons"]);
    valid(w.schemaVersion === CONTROL_VERSION && w.id === id);
    valid(typeof w.label === "string" && w.label.length >= 1 && w.label.length <= 160);
    choice(w.adapterMode, ["connected", "read_only", "mock"]); handle(w.revision);
    timestamp(w.observedAt); valid(w.observedAt <= observedAt);
    choice(w.integration, Object.keys(INTEGRATION));
    const c = w.configuration;
    fields(c, ["state", "reason"]); choice(c.state, ["configured", "missing", "invalid", "not_observed"]); reason(c.reason);
    valid(c.state === "configured" ? c.reason === null : c.reason !== null);
    const s = w.schedule;
    fields(s, ["state", "intervalSeconds", "nextDueAt", "reason"]);
    choice(s.state, ["enabled", "disabled", "not_applicable", "unknown"]); reason(s.reason);
    if (id === "b5") valid(s.state === "not_applicable" && s.intervalSeconds === null && s.nextDueAt === null && s.reason === "manual_only");
    else {
      valid(s.state !== "not_applicable");
      if (id === "b1" || id === "b3") valid(s.state === "unknown"); // No scheduler decision is made here.
      if (s.state === "unknown") valid(s.intervalSeconds === null && s.nextDueAt === null && s.reason !== null);
      else {
        if (s.intervalSeconds !== null) valid(Number.isSafeInteger(s.intervalSeconds) &&
          s.intervalSeconds >= (id === "b2" ? 60 : 3600) && s.intervalSeconds <= 86400);
        if (s.state === "enabled") {
          valid(c.state === "configured" && s.intervalSeconds !== null);
          if (s.nextDueAt === null) valid(s.reason === "due_unavailable");
          else { timestamp(s.nextDueAt); valid(s.reason === null); }
        } else valid(s.nextDueAt === null);
      }
    }
    const e = w.execution;
    fields(e, ["state", "requestId", "startedAt"]); choice(e.state, ["idle", "queued", "running", "unknown"]);
    past(e.startedAt, w.observedAt);
    if (e.state === "queued" || e.state === "running") { handle(e.requestId); if (e.state === "queued") valid(e.startedAt === null); }
    else valid(e.requestId === null && e.startedAt === null);
    past(w.lastAttemptAt, w.observedAt);
    const a = w.lastAttempt;
    if (a !== null) {
      fields(a, ["requestId", "state", "outcome", "completedAt", "effect"]); handle(a.requestId);
      choice(a.state, ["queued", "running", "completed"]); effect(a.effect, id); past(a.completedAt, w.observedAt);
      valid(w.lastAttemptAt !== null);
      if (a.state === "completed") {
        choice(a.outcome, ["success", "failure", "unknown"]);
        valid(a.completedAt !== null && w.lastAttemptAt <= a.completedAt);
        if (a.outcome === "success") valid(a.effect.state === "verified");
        if (a.outcome === "unknown") valid(a.effect.state === "unknown");
      } else valid(a.outcome === null && a.completedAt === null && a.effect.state === "unknown");
    } else valid(w.lastAttemptAt === null);
    if (["queued", "running"].includes(e.state)) {
      valid(a !== null && a.state === e.state && a.requestId === e.requestId);
      if (e.startedAt !== null) valid(e.startedAt >= w.lastAttemptAt);
    }
    if (e.state === "idle") valid(a === null || a.state === "completed");
    const h = w.lastVerifiedOutcome;
    if (h !== null) {
      fields(h, ["status", "completedAt", "effect"]); choice(h.status, ["success", "failure"]);
      timestamp(h.completedAt); past(h.completedAt, w.observedAt); effect(h.effect, id);
      choice(h.effect.state, ["verified", "none"]);
      if (h.status === "success") valid(h.effect.state === "verified");
      if (a !== null && a.completedAt !== null) valid(h.completedAt <= a.completedAt);
      // History can be this same latest verified attempt (the normative B2 fixture),
      // or a prior one. A later timestamp cannot lend another attempt's evidence.
      if (a !== null && h.completedAt > w.lastAttemptAt) {
        valid(a.state === "completed" && h.completedAt === a.completedAt && h.status === a.outcome &&
          h.effect.kind === a.effect.kind && h.effect.state === a.effect.state && h.effect.changed === a.effect.changed);
      }
    }
    valid(Array.isArray(w.availableActions) && new Set(w.availableActions).size === w.availableActions.length);
    for (const action of w.availableActions) choice(action, ACTIONS[id]);
    fields(w.actionReasons, ACTIONS[id].filter(action => !w.availableActions.includes(action)));
    for (const value of Object.values(w.actionReasons)) choice(value, REASONS);
    if (w.adapterMode !== "connected" || w.integration !== "connected") valid(w.availableActions.length === 0);
    if (c.state !== "configured" || e.state !== "idle" || a?.outcome === "unknown") valid(w.availableActions.length === 0);
    if (id === "b1" || id === "b3") valid(!w.availableActions.includes("set_schedule"));
    if (id === "b5") valid(!w.availableActions.includes("stage_archive") && w.actionReasons.stage_archive === "pending_integration");
  }
  const copyEffect = e => ({kind: e.kind, state: e.state, changed: e.changed});
  /** Strict, read-only v1 projection. Never a command/admission/receipt parser.
   * Labels, revisions, handles and action guidance are validated, never forwarded.
   * Accepts parsed DTO objects only, not raw JSON or runtime ports.
   */
  function fromControlCatalog(payload) {
    fields(payload, ["schemaVersion", "authority", "observedAt", "workflows"]);
    valid(payload.schemaVersion === CONTROL_VERSION && payload.authority === "observation_only"); timestamp(payload.observedAt);
    valid(Array.isArray(payload.workflows) && payload.workflows.length === 5);
    return SERVICES.map((service, index) => {
      const w = payload.workflows[index]; validateWorkflow(w, service.id, payload.observedAt);
      // Unavailable evidence cannot make stale success look current.
      const unavailable = w.integration === "unavailable";
      return {...blank(service), controlProjection: true, readOnly: true, synthetic: true,
        observedAt: w.observedAt, adapterMode: w.adapterMode, integration: w.integration,
        configuration: unavailable ? "not_observed" : w.configuration.state,
        configurationReason: unavailable ? "observation_unavailable" : w.configuration.reason,
        schedule: unavailable && service.id !== "b5" ? "not_observed" :
          ({enabled: "on", disabled: "off", unknown: "not_observed", not_applicable: "not_applicable"})[w.schedule.state],
        scheduleReason: service.id === "b5" ? "manual_only" : unavailable ? "observation_unavailable" : w.schedule.reason,
        intervalSeconds: unavailable ? null : w.schedule.intervalSeconds,
        nextRun: unavailable ? null : w.schedule.nextDueAt,
        execution: unavailable ? "Unknown" : ({idle: "Idle", queued: "Queued", running: "Running", unknown: "Unknown"})[w.execution.state],
        lastAttemptAt: unavailable ? null : w.lastAttemptAt,
        lastAttempt: unavailable || w.lastAttempt === null ? null : {state: w.lastAttempt.state, outcome: w.lastAttempt.outcome,
          completedAt: w.lastAttempt.completedAt, effect: copyEffect(w.lastAttempt.effect)},
        lastVerifiedOutcome: unavailable || w.lastVerifiedOutcome === null ? null : {status: w.lastVerifiedOutcome.status,
          completedAt: w.lastVerifiedOutcome.completedAt, effect: copyEffect(w.lastVerifiedOutcome.effect)},
        availableActions: []};
    });
  }
  function mockControlCatalog() {
    const observedAt = 1793059200;
    const workflows = SERVICES.map((s, i) => ({schemaVersion: CONTROL_VERSION, id: s.id, label: s.name, adapterMode: "mock",
      revision: String(i + 1).repeat(32), observedAt, integration: "pending",
      configuration: {state: "not_observed", reason: "pending_integration"},
      schedule: {state: s.id === "b5" ? "not_applicable" : "unknown", intervalSeconds: null, nextDueAt: null,
        reason: s.id === "b5" ? "manual_only" : i === 0 || i === 2 ? "scheduler_decision_required" : "pending_integration"},
      execution: {state: "unknown", requestId: null, startedAt: null}, lastAttempt: null, lastVerifiedOutcome: null, lastAttemptAt: null,
      availableActions: [], actionReasons: Object.fromEntries(ACTIONS[s.id].map(action => [action,
        action === "set_schedule" && (i === 0 || i === 2) ? "scheduler_decision_required" : "pending_integration"]))}));
    const b2 = workflows[1], b4 = workflows[3];
    for (const w of [b2, b4]) {
      w.integration = "connected"; w.configuration = {state: "configured", reason: null};
      w.actionReasons = {run_once: "mock_only", set_schedule: "mock_only"};
    }
    b2.schedule = {state: "disabled", intervalSeconds: 300, nextDueAt: null, reason: null};
    b2.execution = {state: "idle", requestId: null, startedAt: null}; b2.lastAttemptAt = observedAt - 200;
    b2.lastAttempt = {requestId: "a".repeat(32), state: "completed", outcome: "success", completedAt: observedAt - 100,
      effect: {kind: "snapshot_ref", state: "verified", changed: false}};
    b2.lastVerifiedOutcome = {status: "success", completedAt: observedAt - 100, effect: copyEffect(b2.lastAttempt.effect)};
    workflows[2].integration = "unavailable";
    workflows[2].configuration.reason = "observation_unavailable"; workflows[2].schedule.reason = "observation_unavailable";
    workflows[2].actionReasons = {sync_project: "observation_unavailable", set_schedule: "observation_unavailable"};
    b4.schedule = {state: "enabled", intervalSeconds: 86400, nextDueAt: observedAt + 86400, reason: null};
    b4.execution = {state: "running", requestId: "b".repeat(32), startedAt: null}; b4.lastAttemptAt = observedAt - 10;
    b4.lastAttempt = {requestId: "b".repeat(32), state: "running", outcome: null, completedAt: null,
      effect: {kind: "daily_vault", state: "unknown", changed: null}};
    b4.lastVerifiedOutcome = {status: "success", completedAt: observedAt - 86400,
      effect: {kind: "daily_preview", state: "verified", changed: null}};
    return {schemaVersion: CONTROL_VERSION, authority: "observation_only", observedAt, workflows};
  }
  // In-memory request-generation guard; no transport, clocks, storage or runtime dependencies.
  function controlReader() {
    let generation = 0;
    let state = {phase: "unavailable", services: SERVICES.map(blank)};
    return Object.freeze({
      begin() { ++generation; state = {phase: "loading", services: SERVICES.map(blank)}; return generation; },
      settle(token, payload) {
        if (token !== generation || state.phase !== "loading") return false;
        try { state = {phase: "ready", services: fromControlCatalog(payload)}; }
        catch { state = {phase: "unavailable", services: SERVICES.map(blank)}; }
        return true;
      },
      snapshot() { return JSON.parse(JSON.stringify(state)); }
    });
  }
  function scenario(name) {
    if (name === "control-v1") {
      const reader = controlReader(); reader.settle(reader.begin(), mockControlCatalog()); return reader.snapshot();
    }
    if (name === "loading" || name === "unavailable" || name === "stale") {
      return {phase: name, services: SERVICES.map(blank)};
    }
    const services = fromCatalog(mockCatalog());
    if (name === "demonstration") {
      const results = ["running", "saved", "failed", "preview_ready", "unknown"];
      services.forEach((s, index) => Object.assign(s, {
        // These are private mock view fields, never a fabricated wire DTO.
        synthetic: true, integration: "pending", configuration: "configured",
        schedule: s.id === "b5" ? "not_applicable" : index === 2 ? "off" : "on",
        execution: index === 0 ? "Running" : "Idle", result: results[index],
        lastRun: index === 4 ? null : "2026-10-27T09:00:00Z",
        nextRun: s.id === "b5" || index === 2 ? null : "2026-10-27T10:00:00Z"
      }));
    }
    return {phase: "ready", services};
  }
  function outcome(service) {
    if (service.controlProjection) {
      const a = service.lastAttempt;
      if (!a) return ["? Latest attempt unknown", "unknown"];
      if (a.outcome === "failure") return ["! Latest attempt failed · " + effectText(a.effect), "failure"];
      if (a.outcome === "success") return ["✓ Latest attempt succeeded · " + effectText(a.effect), "success"];
      return ["? Latest attempt " + a.state + " · effect unknown", "unknown"];
    }
    return owns(RESULTS, service.result) ? RESULTS[service.result] : RESULTS.unknown;
  }
  function effectText(e) {
    const names = {none: "no product effect", review_artifact: "Review artifact (not canonical apply)", snapshot_ref: "Snapshot ref",
      project_vault: "Project Vault", daily_preview: "Daily preview (not Vault publication)", daily_vault: "Daily Vault", private_inventory: "Private inventory (not generation or Vault)"};
    return names[e.kind] + " · " + e.state + (e.changed === false ? " · no change" : e.changed === true ? " · changed" : " · change not established");
  }
  function controlsLink(id) {
    return id === "b2" ? "/#snapshot-controls" : id === "b4" ? "/#github-daily" : null;
  }
  function simulate(service, action) {
    if (service.controlProjection || service.readOnly) return {service, message: "Read-only observation · no command sent, no admission or effect implied."};
    if (!["b1", "b3", "b5"].includes(service.id) || !["run", "toggle", "import"].includes(action) ||
        (service.id === "b5" ? action !== "import" : action === "import")) {
      return {service, message: "Unavailable in this mock. No command was sent."};
    }
    // A click only changes a local illustration; never fabricate a success receipt.
    const updated = {...service, synthetic: true};
    if (action === "toggle") updated.schedule = service.schedule === "on" ? "off" : "on";
    else { updated.execution = "Queued"; updated.result = "unknown"; }
    return {service: updated, message: "Simulation only · no command sent, no effect verified, nothing saved."};
  }
  function validateFields(values) {
    const errors = [];
    const safeURL = value => {
      try { const u = new URL(value); return u.protocol === "https:" && !!u.hostname && !u.username && !u.password && !u.hash && !u.search; }
      catch { return false; }
    };
    for (const name of ["webdav_url", "gitea_repo", "ai_endpoint"]) {
      if (values[name] && !safeURL(values[name])) errors.push({field: name, message: "Use an HTTPS URL without embedded credentials, query or fragment."});
    }
    const needs = (group, required) => {
      if (group.some(name => values[name])) for (const name of required) {
        if (!values[name] || !values[name].trim()) errors.push({field: name, message: "Complete this connection group before validation."});
      }
    };
    needs(["nextcloud_account", "webdav_url", "nextcloud_password"], ["nextcloud_account", "webdav_url"]);
    needs(["gitea_account", "gitea_repo", "gitea_branch", "gitea_token"], ["gitea_account", "gitea_repo", "gitea_branch"]);
    if (values.gitea_branch && (!/^[a-zA-Z0-9][a-zA-Z0-9_./-]*$/.test(values.gitea_branch) ||
        /\.\.|\/\/|\/$|\.lock$/.test(values.gitea_branch))) {
      errors.push({field: "gitea_branch", message: "Use a simple branch name; backend validation is still required."});
    }
    for (const name of ["vault_root", "vault_folder"]) {
      if (values[name] && (/^(\/|[A-Za-z]:)|\\/.test(values[name]) || values[name].split("/").some(p => p === ".." || p === "."))) {
        errors.push({field: name, message: "Use a relative Vault mapping without traversal."});
      }
    }
    return errors;
  }
  root.ObsidianDesign = Object.freeze({SERVICES, INTEGRATION, CONFIGURATION, SCHEDULE,
    fromCatalog, mockCatalog, fromControlCatalog, mockControlCatalog, controlReader, effectText,
    scenario, outcome, controlsLink, simulate, validateFields});
})(globalThis);
