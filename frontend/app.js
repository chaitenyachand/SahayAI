// SahayAI operator console
// NOTE ON VOICE: this demo uses the browser's Web Speech API for convenience.
// Production build replaces this with an on-device ASR model (e.g. Whisper-tiny
// or a Conformer model) compiled via Qualcomm AI Hub for the Hexagon NPU, so
// voice input works with zero internet and zero audio leaving the device.

const API = "";
let sessionToken = localStorage.getItem("sahayai_token") || null;
let currentOperator = null;

let state = {
  selectedSchemeId: null,
  selectedSchemeName: null,
  consentId: null,
  selectedFile: null,
  lastOcr: null,
  lastFraud: null,
  lastCompleteness: null,
  lastSubmission: null,
};

// ---------- Auth helpers ----------
function authHeaders() {
  return sessionToken ? { "Authorization": `Bearer ${sessionToken}` } : {};
}

async function apiFetch(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: { ...(options.headers || {}), ...authHeaders() },
  });
  if (res.status === 401) {
    signOut();
    throw new Error("Session expired");
  }
  return res;
}

function showApp() {
  document.getElementById("loginOverlay").classList.add("hidden");
  document.getElementById("appFrame").classList.remove("hidden");
}

function showLogin(message) {
  document.getElementById("appFrame").classList.add("hidden");
  document.getElementById("loginOverlay").classList.remove("hidden");
  if (message) {
    const err = document.getElementById("loginError");
    err.textContent = message;
    err.classList.remove("hidden");
  }
}

function signOut() {
  sessionToken = null;
  currentOperator = null;
  localStorage.removeItem("sahayai_token");
  showLogin();
}

async function tryResumeSession() {
  if (!sessionToken) { showLogin(); return; }
  try {
    const res = await apiFetch("/api/auth/me");
    if (!res.ok) throw new Error("invalid session");
    const data = await res.json();
    currentOperator = data.operator;
    onLoginSuccess();
  } catch {
    signOut();
  }
}

function onLoginSuccess() {
  showApp();
  document.getElementById("operatorName").textContent = currentOperator.name;
  document.getElementById("operatorRole").textContent = currentOperator.role;
  document.getElementById("operatorAvatar").textContent = currentOperator.name.charAt(0).toUpperCase();
  if (currentOperator.role === "supervisor") {
    document.getElementById("supervisorTokens").classList.remove("hidden");
  }
  checkHealth();
  loadAllSchemes();
  goToStep(0);
}

document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("loginName").value.trim();
  const pin = document.getElementById("loginPin").value.trim();
  const errEl = document.getElementById("loginError");
  errEl.classList.add("hidden");

  const form = new FormData();
  form.append("name", name);
  form.append("pin", pin);
  try {
    const res = await fetch(`${API}/api/auth/login`, { method: "POST", body: form });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      errEl.textContent = data.detail || "Sign-in failed";
      errEl.classList.remove("hidden");
      return;
    }
    const data = await res.json();
    sessionToken = data.token;
    currentOperator = data.operator;
    localStorage.setItem("sahayai_token", sessionToken);
    onLoginSuccess();
  } catch {
    errEl.textContent = "Could not reach the local server";
    errEl.classList.remove("hidden");
  }
});

document.getElementById("logoutBtn").addEventListener("click", signOut);

// ---------- Step navigation ----------
function goToStep(n) {
  document.querySelectorAll(".panel").forEach(p => p.classList.add("hidden"));
  document.getElementById(`panel-${n}`).classList.remove("hidden");
  document.querySelectorAll(".token[data-step]").forEach(s => {
    const step = Number(s.dataset.step);
    s.classList.toggle("active", step === n);
    s.classList.toggle("done", step < n && step >= 1 && step <= 5);
  });
  if (n === 0) loadOverview();
  if (n === 5) loadSubmissions();
  if (n === 6) loadOperators();
  if (n === 7) loadConflicts();
}

document.querySelectorAll(".token[data-step]").forEach(el => {
  el.addEventListener("click", () => {
    const step = Number(el.dataset.step);
    // Steps 1-5 are the linear citizen workflow; 0, 6, 7 are independent
    // views reachable any time. Guard: can't jump ahead of consent/scan in
    // the linear flow without the prerequisite state.
    if (step >= 3 && step <= 5 && !state.consentId) return;
    goToStep(step);
  });
});

// ---------- Health check ----------
async function checkHealth() {
  try {
    const res = await apiFetch("/api/health");
    await res.json();
    document.getElementById("connDot").className = "dot";
    document.getElementById("connLabel").textContent = "Local engine running";
  } catch {
    document.getElementById("connDot").className = "dot error";
    document.getElementById("connLabel").textContent = "Backend unreachable";
  }
}

// ---------- Step 1: scheme matching ----------
const SCHEME_ICONS = {
  widow_pension: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>`,
  scholarship_sc_st: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c0 1.66 2.69 3 6 3s6-1.34 6-3v-5"/></svg>`,
  ayushman_bharat: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>`,
  kisan_samman_nidhi: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4.5 8-11.8A8 8 0 0 0 4 10.2C4 17.5 12 22 12 22z"/><path d="M12 22V10"/></svg>`,
};

async function loadAllSchemes() {
  const res = await apiFetch("/api/schemes");
  const data = await res.json();
  const grid = document.getElementById("schemeGrid");
  grid.innerHTML = "";
  data.schemes.forEach(s => {
    const card = document.createElement("div");
    card.className = "scheme-card";
    card.dataset.id = s.scheme_id;
    card.innerHTML = `<span class="scheme-card-icon">${SCHEME_ICONS[s.scheme_id] || ""}</span><div class="name">${s.name}</div>`;
    card.addEventListener("click", () => selectScheme(s.scheme_id, s.name));
    grid.appendChild(card);
  });
}

function selectScheme(id, name) {
  state.selectedSchemeId = id;
  state.selectedSchemeName = name;
  state.consentId = null; // new scheme selection requires fresh consent
  document.querySelectorAll(".scheme-card").forEach(c => {
    c.classList.toggle("selected", c.dataset.id === id);
  });
  document.getElementById("consentSchemeLabel").textContent = name;
  document.getElementById("activeSchemeLabel").textContent = name;
  document.getElementById("consentCheckbox").checked = false;
  document.getElementById("consentBtn").disabled = true;
  loadConsentText();
  goToStep(2);
}

document.getElementById("matchBtn").addEventListener("click", async () => {
  const query = document.getElementById("queryInput").value.trim();
  if (!query) return;
  const form = new FormData();
  form.append("query", query);
  const res = await apiFetch("/api/schemes/match", { method: "POST", body: form });
  const data = await res.json();
  const box = document.getElementById("schemeResult");
  box.classList.remove("hidden");
  if (data.matched) {
    box.innerHTML = `Best match: <strong>${data.scheme.name}</strong> (confidence ${Math.round(data.confidence * 100)}%) — <a href="#" id="useMatchLink">use this scheme</a>`;
    document.getElementById("useMatchLink").addEventListener("click", (e) => {
      e.preventDefault();
      selectScheme(data.scheme_id, data.scheme.name);
    });
  } else {
    box.innerHTML = `No confident match — pick from the list below.`;
  }
});

// Voice input
const voiceBtn = document.getElementById("voiceBtn");
let recognizer = null;
if ("webkitSpeechRecognition" in window || "SpeechRecognition" in window) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  recognizer = new SR();
  recognizer.lang = "hi-IN";
  recognizer.interimResults = false;
  recognizer.onresult = (e) => {
    document.getElementById("queryInput").value = e.results[0][0].transcript;
    voiceBtn.classList.remove("listening");
    document.getElementById("matchBtn").click();
  };
  recognizer.onerror = () => voiceBtn.classList.remove("listening");
  recognizer.onend = () => voiceBtn.classList.remove("listening");
} else {
  voiceBtn.title = "Voice input not supported in this browser";
}
voiceBtn.addEventListener("click", () => {
  if (!recognizer) return;
  voiceBtn.classList.add("listening");
  recognizer.start();
});

// ---------- Step 2: consent ----------
async function loadConsentText() {
  // Fetch the canonical consent text without logging an event yet — the
  // event is only written once the operator actually confirms it.
  document.getElementById("consentText").textContent =
    "The citizen has been informed their document will be processed on this device only, " +
    "used solely to complete this application, and will not be shared except with the relevant " +
    "government scheme, per applicable data protection law.";
}

document.getElementById("consentCheckbox").addEventListener("change", (e) => {
  document.getElementById("consentBtn").disabled = !e.target.checked;
});

document.getElementById("consentBtn").addEventListener("click", async () => {
  const statusEl = document.getElementById("consentStatus");
  statusEl.classList.remove("hidden");
  statusEl.textContent = "Recording consent…";
  const form = new FormData();
  form.append("scheme_id", state.selectedSchemeId);
  try {
    const res = await apiFetch("/api/consent", { method: "POST", body: form });
    const data = await res.json();
    state.consentId = data.consent_id;
    statusEl.textContent = `Consent recorded at ${new Date(data.given_at).toLocaleTimeString()}.`;
    goToStep(3);
  } catch {
    statusEl.textContent = "Could not record consent — try again.";
  }
});

// ---------- Step 3: document scan ----------
const fileInput = document.getElementById("fileInput");
const dropzone = document.getElementById("dropzone");
const processBtn = document.getElementById("processBtn");

fileInput.addEventListener("change", () => {
  if (fileInput.files.length) handleFile(fileInput.files[0]);
});
["dragover"].forEach(evt => dropzone.addEventListener(evt, e => e.preventDefault()));
dropzone.addEventListener("drop", e => {
  e.preventDefault();
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});

function handleFile(file) {
  state.selectedFile = file;
  const reader = new FileReader();
  reader.onload = () => {
    document.getElementById("preview").src = reader.result;
    document.getElementById("previewWrap").classList.remove("hidden");
  };
  reader.readAsDataURL(file);
  processBtn.disabled = false;
}

processBtn.addEventListener("click", async () => {
  if (!state.selectedFile || !state.selectedSchemeId || !state.consentId) return;
  const statusEl = document.getElementById("processStatus");
  statusEl.classList.remove("hidden");
  statusEl.textContent = "Running on-device OCR + fraud check…";
  processBtn.disabled = true;

  const form = new FormData();
  form.append("file", state.selectedFile);
  form.append("scheme_id", state.selectedSchemeId);
  form.append("consent_id", state.consentId);

  try {
    const res = await apiFetch("/api/documents/process", { method: "POST", body: form });
    const data = await res.json();

    if (data.duplicate) {
      statusEl.textContent = `⚠ Duplicate document — already processed on ${new Date(data.first_seen_at).toLocaleString()}.`;
      processBtn.disabled = false;
      return;
    }

    state.lastOcr = data.ocr;
    state.lastFraud = data.fraud;
    state.lastCompleteness = data.completeness;
    state.lastSubmission = data.submission;

    statusEl.textContent = data.ocr.manual_fallback
      ? `Automated extraction failed — switch to manual entry on the next screen.`
      : `Done — ${data.ocr.field_count_found} fields found, OCR confidence ${Math.round(data.ocr.confidence * 100)}%.`;
    renderReview();
    goToStep(4);
  } catch (e) {
    statusEl.textContent = "Something went wrong processing this document.";
  } finally {
    processBtn.disabled = false;
  }
});

// ---------- Step 4: review ----------
function renderReview() {
  const fallbackBanner = document.getElementById("fallbackBanner");
  const manualWrap = document.getElementById("manualEntryWrap");
  const bothFallback = state.lastOcr.manual_fallback;

  if (bothFallback) {
    fallbackBanner.classList.remove("hidden");
    fallbackBanner.innerHTML = `⚠ On-device OCR model is unavailable (${state.lastOcr.fallback_reason || "unknown error"}). Falling back to manual entry — this submission is flagged for manual review.`;
    manualWrap.classList.remove("hidden");
  } else {
    fallbackBanner.classList.add("hidden");
    manualWrap.classList.add("hidden");
  }

  const table = document.getElementById("fieldTable");
  table.innerHTML = "";
  const fields = state.lastOcr.fields;
  if (Object.keys(fields).length === 0) {
    table.innerHTML = `<tr><td colspan="2">No fields could be extracted — please enter manually below.</td></tr>`;
  } else {
    for (const [k, v] of Object.entries(fields)) {
      const row = document.createElement("tr");
      row.innerHTML = `<td class="key">${k.replace(/_/g, " ")}</td><td class="value">${v}</td>`;
      table.appendChild(row);
    }
  }

  const c = state.lastCompleteness;
  document.getElementById("completenessBar").style.width = `${c.completeness_pct}%`;
  document.getElementById("completenessLabel").textContent = `${c.completeness_pct}% complete (${c.present_fields.length}/${c.required_fields.length} required fields found)`;
  const missing = document.getElementById("missingList");
  missing.innerHTML = "";
  c.missing_fields.forEach(f => {
    const li = document.createElement("li");
    li.textContent = `Missing: ${f.replace(/_/g, " ")}`;
    missing.appendChild(li);
  });

  const fraud = state.lastFraud;
  const stampArea = document.getElementById("stampArea");
  const stampCopy = {
    low: { title: "VERIFIED", sub: "LOW RISK" },
    medium: { title: "REVIEW", sub: "MEDIUM RISK" },
    high: { title: "FLAGGED", sub: "HIGH RISK" },
  }[fraud.risk_level];
  stampArea.innerHTML = `
    <div class="stamp ${fraud.risk_level}">
      <span class="stamp-title">${stampCopy.title}</span>
      <span class="stamp-sub">${stampCopy.sub}</span>
    </div>`;

  const flagsList = document.getElementById("fraudFlagsList");
  flagsList.innerHTML = "";
  if (fraud.flags.length === 0) {
    flagsList.innerHTML = `<li>No tamper/quality issues detected.</li>`;
  } else {
    fraud.flags.forEach(f => {
      const li = document.createElement("li");
      li.textContent = f.message;
      flagsList.appendChild(li);
    });
  }
  document.getElementById("fraudMetrics").textContent = fraud.manual_fallback
    ? "metrics unavailable — model fallback"
    : `blur=${fraud.metrics.blur_score} · compression_delta=${fraud.metrics.compression_delta} · edge_density=${fraud.metrics.edge_density}`;
}

document.getElementById("confirmBtn").addEventListener("click", () => {
  goToStep(5);
});

// ---------- Step 5: sync queue ----------
function fmtSyncBadge(s) {
  if (s.conflict_status === "conflict") return `<span class="badge conflict">conflict</span>`;
  if (s.sync_status === "synced") return `<span class="badge synced">synced</span>`;
  if (s.sync_status === "failed") return `<span class="badge failed">failed</span>`;
  if (s.last_sync_error) return `<span class="badge queued">retrying</span>`;
  return `<span class="badge queued">queued</span>`;
}

async function loadSubmissions() {
  const res = await apiFetch("/api/submissions");
  const data = await res.json();
  const body = document.getElementById("submissionsBody");
  body.innerHTML = "";
  data.submissions.forEach(s => {
    const row = document.createElement("tr");
    const canRetry = s.sync_status !== "synced" && s.status === "ready_to_submit";
    row.innerHTML = `
      <td>${s.scheme_id.replace(/_/g, " ")}</td>
      <td><span class="badge ${s.status}">${s.status.replace(/_/g, " ")}</span></td>
      <td>${fmtSyncBadge(s)}</td>
      <td class="mono">${s.sync_attempts || 0}</td>
      <td class="mono">${Math.round(s.confidence * 100)}%</td>
      <td class="mono">${new Date(s.created_at).toLocaleString()}</td>
      <td>${canRetry ? `<button class="retry-btn" data-id="${s.id}">Retry</button>` : ""}<button class="audit-btn" data-audit-id="${s.id}">Audit</button></td>
    `;
    body.appendChild(row);
  });
  body.querySelectorAll(".retry-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "…";
      await apiFetch(`/api/sync/retry/${btn.dataset.id}`, { method: "POST" });
      loadSubmissions();
    });
  });
  body.querySelectorAll(".audit-btn").forEach(btn => {
    btn.addEventListener("click", () => openAuditModal(btn.dataset.auditId));
  });
}

document.getElementById("syncBtn").addEventListener("click", async () => {
  const res = await apiFetch("/api/sync/run", { method: "POST" });
  const data = await res.json();
  const r = data.results;
  document.getElementById("syncResult").textContent =
    `Synced ${r.synced?.length || 0} · retrying ${r.retrying?.length || 0} · failed ${r.failed?.length || 0} · blocked (manual review) ${r.blocked?.length || 0} · conflicts ${r.conflict?.length || 0}`;
  loadSubmissions();
});

// ---------- Step 6 (supervisor): team management ----------
async function loadOperators() {
  const list = document.getElementById("opList");
  list.innerHTML = `<p class="empty-note">Loading…</p>`;
  try {
    const res = await apiFetch("/api/operators");
    if (!res.ok) throw new Error("forbidden");
    const data = await res.json();
    list.innerHTML = "";
    data.operators.forEach(op => {
      const row = document.createElement("div");
      row.className = "op-row";
      row.innerHTML = `
        <span class="op-avatar">${op.name.charAt(0).toUpperCase()}</span>
        <span class="op-name">${op.name}</span>
        <span class="op-role-badge ${op.role}">${op.role}</span>
        <span class="mono-note">${op.active ? "active" : "deactivated"}</span>
      `;
      list.appendChild(row);
    });
  } catch {
    list.innerHTML = `<p class="empty-note">Could not load the team list.</p>`;
  }
}

document.getElementById("addOpForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const statusEl = document.getElementById("addOpStatus");
  statusEl.classList.remove("hidden");
  statusEl.textContent = "Adding operator…";

  const form = new FormData();
  form.append("name", document.getElementById("newOpName").value.trim());
  form.append("pin", document.getElementById("newOpPin").value.trim());
  form.append("role", document.getElementById("newOpRole").value);

  try {
    const res = await apiFetch("/api/operators", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) {
      statusEl.textContent = data.detail || "Could not add operator.";
      return;
    }
    statusEl.textContent = `Added ${data.operator.name} as ${data.operator.role}.`;
    document.getElementById("addOpForm").reset();
    loadOperators();
  } catch {
    statusEl.textContent = "Could not reach the local server.";
  }
});

// ---------- Step 7 (supervisor): conflicts ----------
async function loadConflicts() {
  const list = document.getElementById("conflictList");
  list.innerHTML = `<p class="empty-note">Loading…</p>`;
  try {
    const res = await apiFetch("/api/submissions?all_operators=true");
    const data = await res.json();
    const conflicts = data.submissions.filter(s => s.conflict_status === "conflict");
    if (conflicts.length === 0) {
      list.innerHTML = `<p class="empty-note">No open conflicts — everything's clean.</p>`;
      return;
    }
    list.innerHTML = "";
    conflicts.forEach(s => {
      const card = document.createElement("div");
      card.className = "conflict-card";
      card.innerHTML = `
        <div class="conflict-card-head">
          <span class="conflict-scheme">${s.scheme_id.replace(/_/g, " ")}</span>
          <span class="mono-note">${new Date(s.created_at).toLocaleString()}</span>
        </div>
        <div class="conflict-meta">Submission ${s.id.slice(0, 8)}… flagged: another synced submission shares this citizen's Aadhaar number for the same scheme.</div>
        <div class="conflict-actions">
          <button class="btn-small-outline" data-resolve="${s.id}" data-action="force_sync">Force sync anyway</button>
          <button class="btn-small-outline btn-small-danger" data-resolve="${s.id}" data-action="discard">Discard as duplicate</button>
          <button class="audit-btn" data-audit-id="${s.id}">Audit</button>
        </div>
      `;
      list.appendChild(card);
    });
    list.querySelectorAll("[data-resolve]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const form = new FormData();
        form.append("resolution", btn.dataset.action);
        await apiFetch(`/api/sync/${btn.dataset.resolve}/resolve-conflict`, { method: "POST", body: form });
        loadConflicts();
      });
    });
    list.querySelectorAll(".audit-btn").forEach(btn => {
      btn.addEventListener("click", () => openAuditModal(btn.dataset.auditId));
    });
  } catch {
    list.innerHTML = `<p class="empty-note">Could not load conflicts.</p>`;
  }
}

// ---------- Audit trail modal ----------
async function openAuditModal(submissionId) {
  const overlay = document.getElementById("auditModalOverlay");
  const events = document.getElementById("auditEvents");
  document.getElementById("auditSubmissionId").textContent = submissionId;
  events.innerHTML = `<p class="empty-note">Loading…</p>`;
  overlay.classList.remove("hidden");
  try {
    const res = await apiFetch(`/api/submissions/${submissionId}/audit`);
    const data = await res.json();
    events.innerHTML = "";
    data.audit_trail.forEach(e => {
      const row = document.createElement("div");
      row.className = "audit-event";
      row.innerHTML = `
        <span class="audit-event-dot"></span>
        <div class="audit-event-body">
          <div class="audit-event-type">${e.event_type.replace(/_/g, " ")}</div>
          <div class="audit-event-time">${new Date(e.created_at).toLocaleString()}</div>
        </div>
      `;
      events.appendChild(row);
    });
    const chainEl = document.getElementById("auditChainStatus");
    chainEl.innerHTML = data.chain_valid
      ? `<span class="chain-valid">✓ Hash chain verified — no tampering detected across the full audit log.</span>`
      : `<span class="chain-invalid">⚠ Hash chain verification failed — historical records may have been altered.</span>`;
  } catch {
    events.innerHTML = `<p class="empty-note">Could not load audit trail.</p>`;
  }
}

document.getElementById("auditModalClose").addEventListener("click", () => {
  document.getElementById("auditModalOverlay").classList.add("hidden");
});
document.getElementById("auditModalOverlay").addEventListener("click", (e) => {
  if (e.target.id === "auditModalOverlay") e.currentTarget.classList.add("hidden");
});

// ---------- Step 0: overview dashboard ----------
const DONUT_COLORS = {
  synced: "#16A34A",
  queued: "#D97706",
  failed: "#DC2626",
  conflict: "#6D28D9",
};

async function loadOverview() {
  const res = await apiFetch("/api/submissions");
  const data = await res.json();
  const subs = data.submissions;

  // Stat tiles
  const total = subs.length;
  const syncedCount = subs.filter(s => s.sync_status === "synced").length;
  const avgConfidence = total ? Math.round((subs.reduce((sum, s) => sum + s.confidence, 0) / total) * 100) : 0;
  const syncRate = total ? Math.round((syncedCount / total) * 100) : 0;

  document.getElementById("overviewStats").innerHTML = `
    <div class="ov-stat"><span class="ov-stat-value">${total}</span><span class="ov-stat-label">Documents processed</span></div>
    <div class="ov-stat"><span class="ov-stat-value">${syncRate}%</span><span class="ov-stat-label">Sync success rate</span></div>
    <div class="ov-stat"><span class="ov-stat-value">${avgConfidence}%</span><span class="ov-stat-label">Avg. OCR confidence</span></div>
  `;

  // Donut: sync status breakdown (conflict takes priority over sync_status bucket)
  const buckets = { synced: 0, queued: 0, failed: 0, conflict: 0 };
  subs.forEach(s => {
    if (s.conflict_status === "conflict") buckets.conflict++;
    else if (buckets[s.sync_status] !== undefined) buckets[s.sync_status]++;
    else buckets.queued++;
  });

  const donutSvg = document.getElementById("donutSvg");
  const donutCenter = document.getElementById("donutCenter");
  const legend = document.getElementById("donutLegend");

  if (total === 0) {
    donutSvg.innerHTML = `<circle cx="60" cy="60" r="46" fill="none" stroke="#EEF0F6" stroke-width="16"/>`;
    donutCenter.innerHTML = `<span class="donut-center-num">0</span>`;
    legend.innerHTML = `<span class="empty-note" style="padding:0;">No submissions yet — process a document to see activity here.</span>`;
  } else {
    const r = 46, cx = 60, cy = 60;
    const circumference = 2 * Math.PI * r;
    let offset = 0;
    let segs = "";
    Object.entries(buckets).forEach(([key, count]) => {
      if (count === 0) return;
      const frac = count / total;
      const len = frac * circumference;
      segs += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${DONUT_COLORS[key]}" stroke-width="16"
        stroke-dasharray="${len} ${circumference - len}" stroke-dashoffset="${-offset}"
        transform="rotate(-90 ${cx} ${cy})" stroke-linecap="butt"/>`;
      offset += len;
    });
    donutSvg.innerHTML = segs;
    donutCenter.innerHTML = `<span class="donut-center-num">${total}</span><span class="donut-center-label">total</span>`;
    legend.innerHTML = Object.entries(buckets).filter(([, c]) => c > 0).map(([key, count]) => `
      <div class="donut-legend-row">
        <span class="donut-dot" style="background:${DONUT_COLORS[key]}"></span>
        <span class="donut-legend-label">${key}</span>
        <span class="donut-legend-count mono">${count}</span>
      </div>
    `).join("");
  }

  // Bar chart: submissions by scheme
  const bySchemeMap = {};
  subs.forEach(s => { bySchemeMap[s.scheme_id] = (bySchemeMap[s.scheme_id] || 0) + 1; });
  const bySchemeEntries = Object.entries(bySchemeMap).sort((a, b) => b[1] - a[1]);
  const barsEl = document.getElementById("overviewBars");

  if (bySchemeEntries.length === 0) {
    barsEl.innerHTML = `<span class="empty-note" style="padding:0;">No submissions yet.</span>`;
  } else {
    const maxCount = Math.max(...bySchemeEntries.map(([, c]) => c));
    barsEl.innerHTML = bySchemeEntries.map(([scheme, count]) => `
      <div class="ov-bar-row">
        <span class="ov-bar-label">${scheme.replace(/_/g, " ")}</span>
        <div class="ov-bar-track"><div class="ov-bar-fill" style="width:${Math.round((count / maxCount) * 100)}%"></div></div>
        <span class="ov-bar-count mono">${count}</span>
      </div>
    `).join("");
  }
}

// ---------- Boot ----------
tryResumeSession();
