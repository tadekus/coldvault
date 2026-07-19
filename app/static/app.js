const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

async function api(path, opts = {}) {
  if (opts.body) {
    opts.method = opts.method || "POST";
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}

const chip = s => s ? `<span class="chip ${esc(s)}">${esc(s)}</span>` : "—";

/* ---------- tabs ---------- */
const loaders = { dashboard: loadDashboard, files: loadFiles, sessions: loadSessions, restores: loadRestores, logs: loadLogs };
let activeTab = "dashboard";

$$(".tab").forEach(b => b.onclick = () => {
  $$(".tab").forEach(x => x.classList.remove("active"));
  $$(".panel").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  $("#tab-" + b.dataset.tab).classList.add("active");
  activeTab = b.dataset.tab;
  loaders[activeTab]();
});

setInterval(() => {
  if (activeTab === "dashboard" || activeTab === "sessions") loaders[activeTab]();
  if (activeTab === "logs" && $("#logAuto").checked) loadLogs();
}, 5000);

/* ---------- dashboard ---------- */
async function loadDashboard() {
  try {
    const [st, stats] = await Promise.all([api("/api/status"), api("/api/stats")]);
    $("#bucketBadge").textContent = `s3://${st.bucket || "?"} · ${st.storage_class}`;

    const f = stats.files || {};
    const g = k => f[k] || { count: 0, bytes: 0 };
    $("#statCards").innerHTML = `
      <div class="card"><div class="num">${g("verified").count}</div><div class="lbl">verified objects</div></div>
      <div class="card"><div class="num">${fmtBytes(g("verified").bytes)}</div><div class="lbl">verified data</div></div>
      <div class="card"><div class="num">${g("remote").count}</div><div class="lbl">imported (remote)</div></div>
      <div class="card"><div class="num">${g("failed").count}</div><div class="lbl">failed uploads</div></div>
      <div class="card"><div class="num">${stats.sessions}</div><div class="lbl">sessions</div></div>
      <div class="card"><div class="num">${stats.active_restores}</div><div class="lbl">restores in progress</div></div>`;

    $("#watcherInfo").innerHTML = `
      <div><b>Watch dirs</b><code>${esc(st.watch_dirs.join(", "))}</code></div>
      <div><b>Canary file</b><code>${esc(st.canary)}</code></div>
      <div><b>Auto-upload</b>${st.auto_upload ? "enabled" : "disabled"}</div>
      <div><b>Upload queue</b>${st.queue_size} waiting${st.current_session ? `, session #${st.current_session} running` : ""}</div>` +
      ((st.uploading || []).length
        ? `<div><b>Uploading now</b><div>` +
          st.uploading.map(u => `<code>${esc(u.key)}</code> <span class="muted">(${fmtBytes(u.size)})</span>`).join("<br>") +
          `</div></div>`
        : "");

    const mounts = Object.entries(st.active_mounts || {});
    $("#activeMounts").innerHTML = mounts.length
      ? mounts.map(([m, i]) =>
          `<div><code>${esc(m)}</code> → label <b>${esc(i.label)}</b>` +
          (i.session_id ? ` (session #${i.session_id})` : "") + `</div>`).join("")
      : "none";

    $("#connInfo").innerHTML = `
      <div><b>Bucket</b><code>${esc(st.bucket) || "⚠ not set"}</code></div>
      <div><b>Region</b><code>${esc(st.region) || "—"}</code></div>
      <div><b>Prefix</b><code>${esc(st.prefix) || "(none)"}</code></div>`;
  } catch (e) {
    $("#statCards").innerHTML = `<div class="card"><div class="lbl">error: ${esc(e.message)}</div></div>`;
  }
}

$("#btnTest").onclick = async () => {
  $("#testResult").textContent = "testing…";
  try {
    const r = await api("/api/test", { method: "POST" });
    $("#testResult").textContent = `✔ OK — account ${r.account} (${r.arn})`;
  } catch (e) {
    $("#testResult").textContent = "✘ " + e.message;
  }
};

$("#btnSync").onclick = async () => {
  if (!confirm("List the whole bucket and import unknown objects into the index?")) return;
  $("#testResult").textContent = "syncing (may take a while on big buckets)…";
  try {
    const r = await api("/api/sync", { method: "POST" });
    $("#testResult").textContent = `✔ imported ${r.imported} of ${r.listed} listed objects`;
  } catch (e) {
    $("#testResult").textContent = "✘ " + e.message;
  }
};

$("#btnListBuckets").onclick = async () => {
  $("#testResult").textContent = "listing buckets…";
  try {
    const r = await api("/api/buckets");
    const sel = $("#bucketSelect");
    sel.innerHTML = r.buckets.map(b =>
      `<option value="${esc(b.name)}" ${b.name === r.current ? "selected" : ""}>${esc(b.name)}</option>`
    ).join("");
    sel.style.display = "";
    $("#btnUseBucket").style.display = "";
    $("#testResult").textContent = r.buckets.length
      ? `${r.buckets.length} bucket(s) — pick one and click "Use this bucket"`
      : "no buckets in this account";
  } catch (e) {
    $("#testResult").textContent = "✘ " + e.message;
  }
};

$("#btnUseBucket").onclick = async () => {
  const name = $("#bucketSelect").value;
  if (!name) return;
  $("#testResult").textContent = `checking access to ${name}…`;
  try {
    const r = await api("/api/bucket", { body: { name } });
    $("#testResult").textContent = `✔ now using s3://${r.bucket}`;
    loadDashboard();
  } catch (e) {
    $("#testResult").textContent = "✘ " + e.message;
  }
};

/* ---------- browse + manual upload ---------- */
async function browse(path) {
  try {
    const r = await api("/api/browse?path=" + encodeURIComponent(path || ""));
    $("#uploadPath").value = r.path;
    let html = "";
    if (r.parent) html += `<a data-p="${esc(r.parent)}">⬑ up</a>`;
    html += r.dirs.map(d => `<a data-p="${esc(r.path.replace(/\/$/, "") + "/" + d)}">📁 ${esc(d)}</a>`).join("");
    html += `<span class="muted" style="padding:3px 6px">${r.file_count} files here</span>`;
    $("#browseList").innerHTML = html;
    $$("#browseList a").forEach(a => a.onclick = () => browse(a.dataset.p));
  } catch (e) {
    $("#browseList").innerHTML = `<span class="muted">✘ ${esc(e.message)}</span>`;
  }
}
$("#btnBrowse").onclick = () => browse($("#uploadPath").value);

$("#btnUpload").onclick = async () => {
  const path = $("#uploadPath").value.trim();
  if (!path) return alert("Enter a path first");
  try {
    const r = await api("/api/upload", { body: { path, label: $("#uploadLabel").value.trim() } });
    alert(`Upload session #${r.session_id} queued`);
    loadDashboard();
  } catch (e) {
    alert("✘ " + e.message);
  }
};

/* ---------- index / search ---------- */
let page = 0;
const PAGE_SIZE = 100;
// selection entries are "bucket|key" (bucket names can never contain "|")
const selected = new Set();
const selId = (b, k) => `${b}|${k}`;
const selItem = s => {
  const i = s.indexOf("|");
  return { bucket: s.slice(0, i), key: s.slice(i + 1) };
};

function updateSelCount() {
  $("#selCount").textContent = `${selected.size} selected`;
}

function fillBucketFilter(buckets, active) {
  const sel = $("#bucketFilter");
  const cur = sel.value;
  const opts = [`<option value="*">all buckets</option>`]
    .concat([...new Set([active, ...buckets])].filter(Boolean).map(b =>
      `<option value="${esc(b)}">${esc(b)}${b === active ? " (active)" : ""}</option>`));
  const html = opts.join("");
  if (sel.dataset.html !== html) {
    sel.dataset.html = html;
    sel.innerHTML = html;
    sel.value = cur && [...sel.options].some(o => o.value === cur) ? cur : active || "*";
  }
}

async function loadFiles() {
  const q = new URLSearchParams({
    q: $("#search").value, status: $("#statusFilter").value,
    bucket: $("#bucketFilter").value || "",
    limit: PAGE_SIZE, offset: page * PAGE_SIZE,
  });
  const r = await api("/api/files?" + q);
  fillBucketFilter(r.buckets, r.active);
  $("#filesSummary").textContent = `${r.total.toLocaleString()} objects · ${fmtBytes(r.total_bytes)}`;
  $("#pageInfo").textContent = `page ${page + 1} / ${Math.max(1, Math.ceil(r.total / PAGE_SIZE))}`;
  $("#prevPage").disabled = page === 0;
  $("#nextPage").disabled = (page + 1) * PAGE_SIZE >= r.total;

  $("#filesTable tbody").innerHTML = r.items.map(f => {
    const rst = f.restore
      ? `${chip(f.restore.status)}${f.restore.expiry ? `<div class="mono muted" style="font-size:10px">until ${esc(f.restore.expiry)}</div>` : ""}`
      : "—";
    const id = selId(f.bucket, f.key);
    return `<tr>
      <td><input type="checkbox" class="sel" data-id="${esc(id)}" ${selected.has(id) ? "checked" : ""}></td>
      <td class="mono">${esc(f.bucket)}</td>
      <td class="key">${esc(f.key)}${f.error ? `<div class="muted" style="color:var(--err);font-size:11px">${esc(f.error)}</div>` : ""}</td>
      <td class="num">${fmtBytes(f.size)}</td>
      <td>${chip(f.status)}</td>
      <td>${rst}</td>
      <td class="mono">${esc(f.uploaded_at || "—")}</td>
      <td class="num" title="${f.upload_seconds ? `uploaded in ${f.upload_seconds}s` : ""}">${f.upload_seconds ? fmtBytes(f.size / f.upload_seconds) + "/s" : "—"}</td>
      <td class="mono" title="${esc(f.sha256 || "")}">${f.sha256 ? esc(f.sha256.slice(0, 12)) + "…" : "—"}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="9" class="muted" style="padding:20px">no matches</td></tr>`;

  $$("#filesTable .sel").forEach(cb => cb.onchange = () => {
    cb.checked ? selected.add(cb.dataset.id) : selected.delete(cb.dataset.id);
    updateSelCount();
  });
}

$("#btnSearch").onclick = () => { page = 0; loadFiles(); };
$("#search").addEventListener("keydown", e => { if (e.key === "Enter") { page = 0; loadFiles(); } });
$("#statusFilter").onchange = () => { page = 0; loadFiles(); };
$("#bucketFilter").onchange = () => { page = 0; loadFiles(); };
$("#prevPage").onclick = () => { page = Math.max(0, page - 1); loadFiles(); };
$("#nextPage").onclick = () => { page++; loadFiles(); };
$("#selAll").onchange = e => {
  $$("#filesTable .sel").forEach(cb => {
    cb.checked = e.target.checked;
    cb.checked ? selected.add(cb.dataset.id) : selected.delete(cb.dataset.id);
  });
  updateSelCount();
};

$("#btnRestore").onclick = async () => {
  if (!selected.size) return alert("Select at least one object first");
  const tier = $("#restoreTier").value, days = +$("#restoreDays").value;
  if (!confirm(`Request ${tier} restore of ${selected.size} object(s) for ${days} days?`)) return;
  try {
    const r = await api("/api/restore", { body: { items: [...selected].map(selItem), tier, days } });
    const failed = r.results.filter(x => !x.ok);
    alert(failed.length
      ? `Requested with ${failed.length} failure(s) — see Restores/Logs tab`
      : `✔ Restore requested for ${r.results.length} object(s)`);
    selected.clear();
    updateSelCount();
    loadFiles();
  } catch (e) {
    alert("✘ " + e.message);
  }
};

/* ---------- sessions ---------- */
async function loadSessions() {
  const rows = await api("/api/sessions");
  $("#sessionsTable tbody").innerHTML = rows.map(s => {
    const pct = s.total_bytes ? Math.round(100 * s.done_bytes / s.total_bytes) : (s.status === "done" ? 100 : 0);
    return `<tr>
      <td>${s.id}</td>
      <td class="key">${esc(s.source)}</td>
      <td class="mono">${esc(s.bucket || "—")}</td>
      <td>${esc(s.label)}</td>
      <td>${esc(s.trigger)}</td>
      <td>${chip(s.status)}</td>
      <td><div class="progress"><i style="width:${pct}%"></i></div>
          <span class="mono">${s.done_files}/${s.total_files} files · ${fmtBytes(s.done_bytes)} / ${fmtBytes(s.total_bytes)} (${pct}%)</span></td>
      <td class="num">${s.done_files}</td>
      <td class="num">${s.skipped_files}</td>
      <td class="num" ${s.failed_files ? 'style="color:var(--err)"' : ""}>${s.failed_files}</td>
      <td class="mono">${esc(s.started_at || "")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="11" class="muted" style="padding:20px">no sessions yet</td></tr>`;
}

/* ---------- restores ---------- */
async function loadRestores() {
  const rows = await api("/api/restores");
  $("#restoresTable tbody").innerHTML = rows.map(r => `<tr>
      <td class="mono">${esc(r.bucket || "—")}</td>
      <td class="key">${esc(r.key)}${r.error ? `<div class="muted" style="color:var(--err);font-size:11px">${esc(r.error)}</div>` : ""}</td>
      <td>${esc(r.tier)}</td>
      <td class="num">${r.days}</td>
      <td>${chip(r.status)}</td>
      <td class="mono">${esc(r.requested_at || "")}</td>
      <td class="mono">${esc(r.last_checked || "—")}</td>
      <td class="mono">${esc(r.expiry || "—")}</td>
    </tr>`).join("") || `<tr><td colspan="8" class="muted" style="padding:20px">no restore requests yet</td></tr>`;
}

$("#btnRefreshRestores").onclick = async () => {
  $("#restoreMsg").textContent = "checking S3…";
  try {
    const r = await api("/api/restores/refresh", { method: "POST" });
    $("#restoreMsg").textContent = `done — ${r.completed_now} newly completed`;
    loadRestores();
  } catch (e) {
    $("#restoreMsg").textContent = "✘ " + e.message;
  }
};

/* ---------- logs ---------- */
async function loadLogs() {
  const q = new URLSearchParams({
    level: $("#logLevel").value, category: $("#logCategory").value,
    q: $("#logSearch").value, limit: 300,
  });
  const rows = await api("/api/logs?" + q);
  $("#logsTable tbody").innerHTML = rows.map(e => `<tr>
      <td class="mono">${esc(e.ts)}</td>
      <td>${chip(e.level)}</td>
      <td class="muted">${esc(e.category)}</td>
      <td class="key">${esc(e.message)}${e.detail ? `<div class="muted" style="font-size:11px">${esc(e.detail)}</div>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan="4" class="muted" style="padding:20px">no log entries</td></tr>`;
}
$("#btnLogs").onclick = loadLogs;

/* ---------- init ---------- */
loadDashboard();
