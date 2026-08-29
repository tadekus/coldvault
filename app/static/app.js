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
const loaders = { dashboard: loadDashboard, files: loadFiles, sessions: loadSessions,
                  restores: loadRestores, downloads: loadDownloads, notify: loadNotify,
                  logs: loadLogs };
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
  if (activeTab === "dashboard" || activeTab === "sessions" || activeTab === "downloads")
    loaders[activeTab]();
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

$("#btnAudit").onclick = async () => {
  if (!confirm("Reconcile the index against the actual bucket contents?\n" +
    "Lists every object (paginated) and flags anything missing or mismatched.")) return;
  $("#auditResult").innerHTML = "auditing bucket (may take a while on big buckets)…";
  try {
    const r = await api("/api/audit", { method: "POST" });
    const problems = r.missing_count + r.size_mismatch_count + r.class_drift_count;
    let msg = problems
      ? `<span style="color:var(--err)">⚠ ${problems} issue(s)</span> — `
      : `<span style="color:var(--ok)">✔ all good</span> — `;
    msg += `${r.ok} verified in bucket, ${r.in_bucket} objects total`;
    if (r.imported) msg += `, ${r.imported} newly imported`;
    if (r.missing_count) msg += `<br><span style="color:var(--err)">${r.missing_count} MISSING from bucket</span>` +
      (r.missing.length ? `: <span class="mono" style="font-size:11px">${r.missing.slice(0,10).map(esc).join(", ")}${r.missing_count>10?" …":""}</span>` : "");
    if (r.size_mismatch_count) msg += `<br><span style="color:var(--warn)">${r.size_mismatch_count} size mismatch</span>`;
    if (r.class_drift_count) msg += `<br><span style="color:var(--warn)">${r.class_drift_count} in unexpected storage class</span>`;
    msg += `<br><span class="muted">Flagged files are badged in the Index tab. See the Logs (category: audit).</span>`;
    $("#auditResult").innerHTML = msg;
    if (activeTab === "files") loadFiles();
  } catch (e) {
    $("#auditResult").textContent = "✘ " + e.message;
  }
};

async function loadNotify() {
  try {
    renderNotify(await api("/api/status"));
  } catch (e) {
    $("#notifyInfo").innerHTML = `<div class="muted">error: ${esc(e.message)}</div>`;
  }
}

function renderNotify(st) {
  const ready = st.notify_ready;
  $("#notifyInfo").innerHTML = `
    <div><b>Auto after canary</b>${st.notify_enabled ? "enabled" : "disabled (set COLDVAULT_NOTIFY=true)"}</div>
    <div><b>Recipients</b><code>${st.email_to && st.email_to.length ? esc(st.email_to.join(", ")) : "(none set)"}</code></div>
    <div><b>Resend config</b>${ready ? "✔ ready" : "⚠ incomplete — see below"}</div>`;
  const dis = !ready;
  $("#btnEmailReport").disabled = dis;
  $("#btnEmailTest").disabled = dis;
}

$("#btnEmailTest").onclick = async () => {
  $("#notifyResult").textContent = "sending test email…";
  try {
    const r = await api("/api/notify/test", { method: "POST" });
    $("#notifyResult").textContent = "✔ test email sent" + (r.id ? ` (id ${r.id})` : "");
  } catch (e) {
    $("#notifyResult").textContent = "✘ " + e.message;
  }
};

$("#btnEmailReport").onclick = async () => {
  $("#notifyResult").textContent = "running audit and emailing report…";
  try {
    const r = await api("/api/notify/report", { method: "POST" });
    $("#notifyResult").textContent = "✔ audit report emailed" + (r.id ? ` (id ${r.id})` : "");
  } catch (e) {
    $("#notifyResult").textContent = "✘ " + e.message;
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
async function loadBrowseRoots() {
  try {
    const r = await api("/api/browse/roots");
    if (!r.roots.length) {
      $("#browseRoots").innerHTML = `<span class="muted">no roots configured — add mounts and COLDVAULT_BROWSE_ROOTS</span>`;
      return;
    }
    $("#browseRoots").innerHTML = r.roots.map(root => {
      if (!root.exists)
        return `<span class="muted" title="mounted in compose but not present in container">${esc(root.path)} (not mounted)</span>`;
      if (!root.readable)
        return `<span class="muted" title="present but not readable — check permissions">${esc(root.path)} (not readable)</span>`;
      const tag = root.is_watch ? " 👁" : "";
      return `<a data-p="${esc(root.path)}" title="${root.is_watch ? "watch dir (canary)" : "browse root"}">🗄 ${esc(root.path)}${tag}</a>`;
    }).join("");
    $$("#browseRoots a").forEach(a => a.onclick = () => browse(a.dataset.p));
  } catch (e) {
    $("#browseRoots").innerHTML = `<span class="muted">✘ ${esc(e.message)}</span>`;
  }
}

// Manual-upload selection: absolute path -> {type: "dir"|"file", name}
const uploadSel = new Map();
const joinPath = (dir, name) => dir.replace(/\/$/, "") + "/" + name;

function updateUploadSel() {
  $("#uploadSelCount").textContent = `${uploadSel.size} selected`;
  $("#uploadSelHint").textContent = uploadSel.size
    ? "Start upload sends only the selected items"
    : "Nothing selected → Start upload sends the whole current folder";
}

async function browse(path) {
  try {
    const r = await api("/api/browse?path=" + encodeURIComponent(path || ""));
    $("#uploadPath").value = r.path;
    $("#uploadSelBar").style.display = "";
    const rows = [];
    if (r.parent)
      rows.push(`<div class="browse-item"><span style="width:16px"></span><a class="navdir" data-p="${esc(r.parent)}">⬑ up</a></div>`);
    r.dirs.forEach(d => {
      const abs = joinPath(r.path, d);
      rows.push(`<div class="browse-item">
        <input type="checkbox" class="upsel" data-p="${esc(abs)}" data-type="dir" data-name="${esc(d)}" ${uploadSel.has(abs) ? "checked" : ""}>
        <a class="navdir" data-p="${esc(abs)}">📁 ${esc(d)}</a></div>`);
    });
    r.files.forEach(f => {
      const abs = joinPath(r.path, f.name);
      rows.push(`<div class="browse-item">
        <input type="checkbox" class="upsel" data-p="${esc(abs)}" data-type="file" data-name="${esc(f.name)}" ${uploadSel.has(abs) ? "checked" : ""}>
        <span class="fname">📄 ${esc(f.name)} <span class="muted">${fmtBytes(f.size)}</span></span></div>`);
    });
    const summary = `${r.dirs.length} folder(s), ${r.file_count} file(s) · ${fmtBytes(r.total_bytes)}`;
    rows.push(`<div class="muted" style="padding:4px 2px">${summary}${r.files_truncated ? " · file list truncated" : ""}</div>`);
    $("#browseList").innerHTML = rows.join("");

    $$("#browseList a.navdir").forEach(a => a.onclick = () => browse(a.dataset.p));
    $$("#browseList .upsel").forEach(cb => cb.onchange = () => {
      cb.checked
        ? uploadSel.set(cb.dataset.p, { type: cb.dataset.type, name: cb.dataset.name })
        : uploadSel.delete(cb.dataset.p);
      updateUploadSel();
    });
    updateUploadSel();
  } catch (e) {
    $("#browseList").innerHTML = `<span class="muted">✘ ${esc(e.message)}</span>`;
  }
}
$("#btnBrowse").onclick = () => browse($("#uploadPath").value);

$("#btnSelAllHere").onclick = () => {
  $$("#browseList .upsel").forEach(cb => {
    cb.checked = true;
    uploadSel.set(cb.dataset.p, { type: cb.dataset.type, name: cb.dataset.name });
  });
  updateUploadSel();
};
$("#btnClearSel").onclick = () => {
  uploadSel.clear();
  $$("#browseList .upsel").forEach(cb => cb.checked = false);
  updateUploadSel();
};

$("#btnUpload").onclick = async () => {
  const label = $("#uploadLabel").value.trim();
  let body;
  if (uploadSel.size) {
    const items = [...uploadSel.keys()];
    const dirs = [...uploadSel.values()].filter(v => v.type === "dir").length;
    if (!confirm(`Upload ${items.length} selected item(s) (${dirs} folder(s), ${items.length - dirs} file(s))?`)) return;
    body = { items, label };
  } else {
    const path = $("#uploadPath").value.trim();
    if (!path) return alert("Pick a location, or select files/folders to upload");
    if (!confirm(`Upload the entire folder ${path}?`)) return;
    body = { path, label };
  }
  try {
    const r = await api("/api/upload", { body });
    alert(`Upload session #${r.session_id} queued`);
    uploadSel.clear();
    updateUploadSel();
    browse($("#uploadPath").value);
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
    bucket: $("#bucketFilter").value || "", sort: $("#sortBy").value,
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
      <td>${chip(f.status)}${f.audit_state && f.audit_state !== "ok" ? " " + chip(f.audit_state) : ""}</td>
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
$("#sortBy").onchange = () => { page = 0; loadFiles(); };
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

/* ---------- edit list (XML/AAF) matching ---------- */
$("#btnEditList").onclick = async () => {
  const f = $("#editFile").files[0];
  if (!f) return alert("Choose an XML, FCPXML or AAF file first");
  $("#editSummary").textContent = "parsing…";
  const fd = new FormData();
  fd.append("file", f);
  fd.append("bucket", $("#bucketFilter").value || "");
  try {
    const resp = await fetch("/api/editlist", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    let added = 0;
    data.matched.forEach(m => m.files.forEach(x => {
      if (!selected.has(selId(x.bucket, x.key))) added++;
      selected.add(selId(x.bucket, x.key));
    }));
    updateSelCount();
    $("#editSummary").textContent =
      `${data.format}: ${data.total_refs} media refs — ${data.matched.length} matched ` +
      `(${added} objects added to selection), ${data.unmatched.length} not found`;
    const res = $("#editResults");
    if (data.unmatched.length) {
      res.style.display = "";
      res.innerHTML = `<b style="color:var(--warn)">Not found in index (${data.unmatched.length}):</b> ` +
        data.unmatched.map(u => `<code title="${esc(u.source)}">${esc(u.ref)}</code>`).join(", ");
    } else {
      res.style.display = "none";
    }
    loadFiles();
  } catch (e) {
    $("#editSummary").textContent = "✘ " + e.message;
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
          <span class="mono">${s.done_files} up${s.skipped_files ? ` · ${s.skipped_files} skip` : ""}${s.failed_files ? ` · ${s.failed_files} fail` : ""} / ${s.total_files} files · ${fmtBytes(s.done_bytes)} / ${fmtBytes(s.total_bytes)} (${pct}%)</span></td>
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

/* ---------- downloads ---------- */
const dlSelected = new Map();   // "bucket|key" -> {bucket, key, size, sha256}
let restoredCache = [];

function updateDlCount() {
  $("#dlSelCount").textContent = `${dlSelected.size} selected`;
}

async function loadDownloads() {
  const [r, s] = await Promise.all([
    api("/api/restored"),
    api("/api/download/sessions"),
  ]);
  restoredCache = r.items;
  if (!$("#destPath").value) $("#destPath").value = r.download_dir;

  $("#dlSummary").textContent =
    `${r.items.length} restored object(s) available` +
    (s.queue_size ? ` · ${s.queue_size} session(s) queued` : "") +
    (s.current_session ? ` · session #${s.current_session} running` : "");

  $("#restoredTable tbody").innerHTML = r.items.map(i => {
    const id = selId(i.bucket, i.key);
    const local = i.local_state === "present"
      ? `<span class="chip verified">on disk</span> <span class="mono muted" style="font-size:10px">${esc(i.downloaded_to)}</span>`
      : i.local_state === "deleted"
      ? `<span class="chip failed">deleted</span>${i.prev_path ? ` <span class="mono muted" style="font-size:10px">was ${esc(i.prev_path)}</span>` : ""}`
      : `<span class="muted">not downloaded</span>`;
    const expiryCell = i.expired
      ? `<span class="chip failed">expired</span>`
      : `<span class="mono">${esc(i.expiry || "—")}</span>`;
    return `<tr${i.expired ? ' style="opacity:.55"' : ""}>
      <td><input type="checkbox" class="dlsel" data-id="${esc(id)}" ${dlSelected.has(id) ? "checked" : ""}${i.expired ? " disabled title='restore expired — re-request in the Index tab'" : ""}></td>
      <td class="mono">${esc(i.bucket)}</td>
      <td class="key">${esc(i.key)}</td>
      <td class="num">${fmtBytes(i.size)}</td>
      <td>${esc(i.tier || "—")}</td>
      <td>${expiryCell}</td>
      <td>${local}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="muted" style="padding:20px">
      no completed restores yet — request restores in the Index tab, they appear here once S3 reports them ready</td></tr>`;

  $$("#restoredTable .dlsel").forEach(cb => cb.onchange = () => {
    const item = restoredCache.find(x => selId(x.bucket, x.key) === cb.dataset.id);
    cb.checked ? dlSelected.set(cb.dataset.id, item) : dlSelected.delete(cb.dataset.id);
    updateDlCount();
  });

  $("#dlSessionsTable tbody").innerHTML = s.sessions.map(d => {
    const pct = d.total_bytes ? Math.round(100 * d.done_bytes / d.total_bytes)
                              : (d.status === "done" ? 100 : 0);
    return `<tr>
      <td>${d.id}</td>
      <td class="key">${esc(d.dest)}</td>
      <td>${chip(d.status)}</td>
      <td><div class="progress"><i style="width:${pct}%"></i></div>
          <span class="mono">${d.done_files}/${d.total_files} files · ${fmtBytes(d.done_bytes)} / ${fmtBytes(d.total_bytes)} (${pct}%)</span></td>
      <td class="num">${d.done_files}</td>
      <td class="num">${d.skipped_files}</td>
      <td class="num" ${d.failed_files ? 'style="color:var(--err)"' : ""}>${d.failed_files}</td>
      <td class="mono">${esc(d.started_at || "")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="muted" style="padding:20px">no download sessions yet</td></tr>`;

  $("#dlFilesTable tbody").innerHTML = s.files.map(f => `<tr>
      <td class="key">${esc(f.key)}${f.error ? `<div class="muted" style="color:var(--err);font-size:11px">${esc(f.error)}</div>` : ""}</td>
      <td class="mono">${esc(f.local_path || "—")}</td>
      <td class="num">${fmtBytes(f.size)}</td>
      <td>${chip(f.status)}</td>
      <td class="num">${f.download_seconds && f.size ? fmtBytes(f.size / f.download_seconds) + "/s" : "—"}</td>
      <td class="mono">${esc(f.finished_at || "—")}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="muted" style="padding:20px">nothing downloaded yet</td></tr>`;
}

$("#btnDlRefresh").onclick = loadDownloads;

$("#btnDlVerify").onclick = async () => {
  $("#dlSummary").textContent = "checking local files…";
  try {
    const r = await api("/api/download/verify", { method: "POST" });
    $("#dlSummary").textContent = `checked ${r.checked} downloaded file(s) — ${r.deleted} now missing`;
    loadDownloads();
  } catch (e) {
    $("#dlSummary").textContent = "✘ " + e.message;
  }
};

$("#dlSelAllBox").onchange = e => {
  $$("#restoredTable .dlsel:not([disabled])").forEach(cb => {
    cb.checked = e.target.checked;
    const item = restoredCache.find(x => selId(x.bucket, x.key) === cb.dataset.id);
    cb.checked ? dlSelected.set(cb.dataset.id, item) : dlSelected.delete(cb.dataset.id);
  });
  updateDlCount();
};

// select every restored object that hasn't expired
$("#btnDlSelAll").onclick = () => {
  restoredCache.filter(i => !i.expired)
    .forEach(i => dlSelected.set(selId(i.bucket, i.key), i));
  updateDlCount();
  loadDownloads();
};

// select only objects not already on disk (and not expired) — skips redundant re-downloads
$("#btnDlSelNew").onclick = () => {
  restoredCache.filter(i => !i.expired && i.local_state !== "present")
    .forEach(i => dlSelected.set(selId(i.bucket, i.key), i));
  updateDlCount();
  loadDownloads();
};

async function browseDest(path) {
  try {
    const r = await api("/api/download/browse?path=" + encodeURIComponent(path || ""));
    $("#destPath").value = r.path;
    let html = "";
    if (r.parent) html += `<a data-p="${esc(r.parent)}">⬑ up</a>`;
    html += r.dirs.map(d => `<a data-p="${esc(r.path.replace(/\/$/, "") + "/" + d)}">📁 ${esc(d)}</a>`).join("");
    $("#destBrowse").innerHTML = html || `<span class="muted">(no subfolders)</span>`;
    $$("#destBrowse a").forEach(a => a.onclick = () => browseDest(a.dataset.p));
  } catch (e) {
    $("#destBrowse").innerHTML = `<span class="muted">✘ ${esc(e.message)}</span>`;
  }
}
$("#btnDestBrowse").onclick = () => browseDest($("#destPath").value);

$("#btnDownload").onclick = async () => {
  if (!dlSelected.size) return alert("Select at least one restored object");
  const base = $("#destPath").value.trim();
  const sub = $("#destSub").value.trim().replace(/^\/+|\/+$/g, "");
  const dest = sub ? base.replace(/\/$/, "") + "/" + sub : base;
  const items = [...dlSelected.values()];
  const totalBytes = items.reduce((a, x) => a + (x.size || 0), 0);
  if (!confirm(`Download ${items.length} object(s) (${fmtBytes(totalBytes)}) to ${dest}?`)) return;
  try {
    let r = await api("/api/download", { body: { dest, items } });

    if (r.needs_confirmation) {
      if (r.fits_count === 0) {
        alert(`Not enough space at ${dest}.\n` +
              `Free: ${fmtBytes(r.free)} · needed: ${fmtBytes(r.required)}.\n` +
              `Not even the smallest selected file fits — download cancelled.`);
        return;
      }
      const ok = confirm(
        `Not enough space at ${dest}.\n\n` +
        `Needed: ${fmtBytes(r.required)}      Free: ${fmtBytes(r.free)}\n` +
        `Only ${r.fits_count} of ${r.total_count} file(s) (${fmtBytes(r.fits_bytes)}) will fit.\n\n` +
        `OK  = download the ${r.fits_count} file(s) that fit\n` +
        `Cancel = cancel the whole download`);
      if (!ok) return;
      r = await api("/api/download", { body: { dest, items, on_insufficient: "fit" } });
    }

    if (r.session_id) {
      let msg = `Download session #${r.session_id} queued`;
      if (r.partial) msg += `\n${r.downloaded_count} file(s) that fit; ${r.skipped_count} skipped for space.`;
      alert(msg);
      dlSelected.clear();
      updateDlCount();
      loadDownloads();
    }
  } catch (e) {
    alert("✘ " + e.message);
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
loadBrowseRoots();
