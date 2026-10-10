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
  // Decimal (SI) units — 1 TB = 1000^4 bytes — matching DIT/offload tools,
  // drive capacities and Finder, so our totals agree with the DIT report.
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}

const chip = s => s ? `<span class="chip ${esc(s)}">${esc(s)}</span>` : "—";

// Manifest (offload checksum) badge — only shown when it says something useful.
function manifestBadge(state) {
  if (state === "ok") return ` <span class="chip verified" title="matches offload checksum manifest">csv✓</span>`;
  if (state === "mismatch") return ` <span class="chip failed" title="hash does NOT match the offload manifest">csv✗</span>`;
  if (state === "algo_unsupported") return ` <span class="chip WARNING" title="manifest hash algorithm unsupported">csv?</span>`;
  if (state === "listed") return ` <span class="chip remote" title="in the offload manifest (name+size match); bytes not yet re-hashed — verified on download or by deep verify">csv·</span>`;
  return "";  // not_in_manifest / none -> no badge
}

/* ---------- tabs ---------- */
const loaders = { dashboard: loadDashboard, files: loadFiles, sessions: loadSessions,
                  restores: loadRestores, downloads: loadDownloads, notify: loadNotify,
                  tree: loadTree, retention: loadRetention, logs: loadLogs };
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
    $("#bucketBadge").dataset.bucket = st.bucket || "";

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

$("#btnClearIndex").onclick = async () => {
  const which = prompt(
    "Clear the LOCAL INDEX (metadata only — this does NOT delete anything from S3).\n\n" +
    "Type a bucket name to wipe just that bucket's records,\n" +
    "or type  ALL  to wipe the whole database:", $("#bucketBadge").dataset.bucket || "");
  if (which === null) return;
  const val = which.trim();
  if (!val) return;
  const everything = val.toUpperCase() === "ALL";
  const expected = everything ? "ALL" : val;
  if (!confirm(`Really remove local index records for ${everything ? "ALL buckets" : "bucket '" + val + "'"}?\n` +
               `This can't be undone (but your S3 data is untouched).`)) return;
  $("#clearResult").textContent = "clearing…";
  try {
    const r = await api("/api/index/clear", { body: { bucket: everything ? "*" : val, confirm: expected } });
    $("#clearResult").textContent = `✔ cleared ${r.cleared}: ${r.total} record(s) removed`;
    loadDashboard();
  } catch (e) {
    $("#clearResult").textContent = "✘ " + e.message;
  }
};

$("#btnAudit").onclick = async () => {
  if (!confirm("Reconcile the index against the actual bucket contents?\n" +
    "Lists every object (paginated) and flags anything missing or mismatched.")) return;
  $("#auditResult").innerHTML = "auditing bucket (may take a while on big buckets)…";
  try {
    const r = await api("/api/audit", { method: "POST" });
    const problems = r.missing_count + r.size_mismatch_count + r.class_drift_count + (r.manifest_mismatch_count || 0);
    let msg = problems
      ? `<span style="color:var(--err)">⚠ ${problems} issue(s)</span> — `
      : `<span style="color:var(--ok)">✔ all good</span> — `;
    msg += `${r.ok} verified in bucket, ${r.in_bucket} objects total`;
    if (r.imported) msg += `, ${r.imported} newly imported`;
    if (r.missing_count) msg += `<br><span style="color:var(--err)">${r.missing_count} MISSING from bucket</span>` +
      (r.missing.length ? `: <span class="mono" style="font-size:11px">${r.missing.slice(0,10).map(esc).join(", ")}${r.missing_count>10?" …":""}</span>` : "");
    if (r.size_mismatch_count) msg += `<br><span style="color:var(--warn)">${r.size_mismatch_count} size mismatch</span>`;
    if (r.class_drift_count) msg += `<br><span style="color:var(--warn)">${r.class_drift_count} in unexpected storage class</span>`;
    if (r.manifest_mismatch_count) msg += `<br><span style="color:var(--err)">${r.manifest_mismatch_count} offload-manifest mismatch</span>` +
      (r.manifest_mismatch && r.manifest_mismatch.length ? `: <span class="mono" style="font-size:11px">${r.manifest_mismatch.slice(0,10).map(esc).join(", ")}</span>` : "");
    if (r.manifest_ok) msg += `<br><span class="muted">${r.manifest_ok} file(s) verified against offload manifests</span>`;
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
    manifest: $("#manifestFilter").value,
    limit: PAGE_SIZE, offset: page * PAGE_SIZE,
  });
  const r = await api("/api/files?" + q);
  fillBucketFilter(r.buckets, r.active);
  $("#filesSummary").textContent = `${r.total.toLocaleString()} objects · ${fmtBytes(r.total_bytes)}`;
  $("#filesSummary").title = `${(r.total_bytes || 0).toLocaleString()} bytes exactly`;
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
      <td class="num" title="${(f.size || 0).toLocaleString()} bytes">${fmtBytes(f.size)}</td>
      <td>${chip(f.status)}${f.audit_state && f.audit_state !== "ok" ? " " + chip(f.audit_state) : ""}${manifestBadge(f.manifest_state)}</td>
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
$("#manifestFilter").onchange = () => { page = 0; loadFiles(); };
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

/* ---------- verify against offload manifest ---------- */
function renderManifestReport(r) {
  const bad = r.size_mismatch + r.mismatch;
  let h = bad
    ? `<span style="color:var(--err)">⚠ ${bad} problem(s)</span> — `
    : (r.missing ? `<span style="color:var(--warn)">⚠ ${r.missing} not archived</span> — `
                 : `<span style="color:var(--ok)">✔ all manifest files are archived</span> — `);
  h += `${r.total} file(s) in ${r.sources.length} manifest(s): ` +
       `<b>${r.verified}</b> byte-verified, <b>${r.present}</b> present (name+size ok, hash attached), ` +
       `${r.size_mismatch} size mismatch, ${r.mismatch} hash mismatch, ${r.missing} not archived`;
  if (r.ambiguous) h += `, ${r.ambiguous} ambiguous name(s) — narrow with the scope field`;
  const list = (title, arr, fmt) => arr && arr.length
    ? `<div style="margin-top:6px"><b>${title}</b><div class="mono" style="font-size:11px">${arr.slice(0, 30).map(fmt).join("<br>")}${arr.length > 30 ? "<br>…" : ""}</div></div>` : "";
  h += list("Not archived:", r.missing_list, x => esc(x));
  h += list("Size mismatch:", r.size_mismatch_list, x => `${esc(x.key)} (manifest ${x.manifest}, archived ${x.archived})`);
  h += list("Hash mismatch:", r.mismatch_list, x => esc(x.key || x.name));
  if (!r.xxhash) h += `<div style="color:var(--err);margin-top:6px">⚠ xxhash is not installed on the server — XXH64 checks can't run. Rebuild the image.</div>`;
  h += `<div style="margin-top:6px">Present files now carry the manifest hash: restoring + downloading them re-verifies the bytes${r.job_id ? `; deep verify #${r.job_id} is running below` : ""}.</div>`;
  $("#mfResult").innerHTML = h;
  if (r.job_id) pollManifestJobs();
  if (activeTab === "files") loadFiles();
}

async function submitManifest(fd) {
  fd.append("bucket", $("#bucketFilter").value || "");
  fd.append("scope", $("#mfScope").value.trim());
  if ($("#mfDeep").checked) fd.append("deep", "1");
  $("#mfResult").textContent = "verifying…";
  try {
    const resp = await fetch("/api/manifest/verify", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    renderManifestReport(data);
  } catch (e) {
    $("#mfResult").textContent = "✘ " + e.message;
  }
}

function sendManifestFiles(files) {
  const list = [...files].filter(f => /\.(csv|mhl)$/i.test(f.name));
  if (!list.length) { $("#mfResult").textContent = "✘ drop .csv or .mhl manifest files"; return; }
  const fd = new FormData();
  list.forEach(f => fd.append("file", f));
  submitManifest(fd);
}

(() => {
  const dz = $("#mfDrop");
  ["dragenter", "dragover"].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); dz.classList.add("over");
  }));
  ["dragleave", "drop"].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); dz.classList.remove("over");
  }));
  dz.addEventListener("drop", e => sendManifestFiles(e.dataTransfer.files));
  $("#mfPick").onclick = e => { e.preventDefault(); $("#mfFiles").click(); };
  $("#mfFiles").onchange = e => { sendManifestFiles(e.target.files); e.target.value = ""; };
  $("#btnMfScan").onclick = () => {
    const folder = $("#mfFolder").value.trim();
    if (!folder) return alert("Enter a server folder, e.g. /media/SSD15");
    const fd = new FormData();
    fd.append("folder", folder);
    submitManifest(fd);
  };
})();

let mfPollTimer = null;
async function pollManifestJobs() {
  clearTimeout(mfPollTimer);
  try {
    const jobs = await api("/api/manifest/jobs");
    $("#mfJobs").innerHTML = jobs.slice(0, 3).map(j => {
      const pct = j.total ? Math.round(100 * j.done / j.total) : 100;
      return `<div>Deep verify #${j.id} — ${esc(j.status)} ${j.done}/${j.total} (${pct}%) · ` +
        `<span style="color:var(--ok)">${j.ok} ok</span>, ` +
        `<span style="color:var(--err)">${j.mismatch} mismatch</span>, ` +
        `${j.changed} local changed, ${j.no_local} source unavailable` +
        (j.unsupported ? `, ${j.unsupported} unsupported` : "") + `</div>`;
    }).join("");
    if (jobs.some(j => j.status === "running")) mfPollTimer = setTimeout(pollManifestJobs, 3000);
    else if (activeTab === "files") loadFiles();
  } catch (e) { /* ignore transient poll errors */ }
}

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
      <td>${chip(f.status)}${manifestBadge(f.manifest_state)}</td>
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

/* ---------- tree view ---------- */
const trPrefixes = new Set();   // ticked folders (act on whole subtree)
const trKeys = new Set();       // ticked individual files
const trOpen = new Set();       // expanded folders — survives re-renders

function trBucket() {
  return ($("#bucketFilter") && $("#bucketFilter").value && $("#bucketFilter").value !== "*")
    ? $("#bucketFilter").value : ($("#bucketBadge").dataset.bucket || "");
}

function trUpdateSel() {
  const n = trPrefixes.size + trKeys.size;
  $("#trSel").textContent = n
    ? `${trPrefixes.size} folder(s) + ${trKeys.size} file(s) selected`
    : "nothing selected";
  $("#btnTrRestore").disabled = !n;
  $("#btnTrDelete").disabled = !n || trCanDelete === false;
}

// a folder is covered if it or any ancestor prefix is ticked
function trCovered(prefix) {
  for (const p of trPrefixes) if (prefix === p || prefix.startsWith(p)) return true;
  return false;
}

// Reflect the selection on the checkboxes already in the DOM, so ticking a
// folder doesn't need a re-render (which used to collapse the whole tree).
function trSyncChecks() {
  $("#treeRoot").querySelectorAll(".trfolder").forEach(cb => {
    const p = cb.dataset.prefix, cov = trCovered(p);
    cb.checked = cov;
    cb.disabled = cov && !trPrefixes.has(p);   // covered by an ancestor
  });
  $("#treeRoot").querySelectorAll(".trfile").forEach(cb => {
    const cov = trCovered(cb.dataset.key);
    cb.checked = cov || trKeys.has(cb.dataset.key);
    cb.disabled = cov;
  });
}

async function trExpand(el, prefix) {
  const kids = el.querySelector(`[data-kids="${CSS.escape(prefix)}"]`);
  const tw = el.querySelector(`.twisty[data-toggle="${CSS.escape(prefix)}"]`);
  if (!kids) return;
  kids.hidden = false;
  if (tw) tw.textContent = "▾";
  if (!kids.dataset.loaded) { kids.dataset.loaded = "1"; await trRenderInto(kids, prefix); }
}

async function trRenderInto(el, prefix) {
  el.innerHTML = `<div class="muted" style="padding:2px 0">loading…</div>`;
  let d;
  try {
    d = await api(`/api/tree?bucket=${encodeURIComponent(trBucket())}&prefix=${encodeURIComponent(prefix)}`);
  } catch (e) { el.innerHTML = `<div class="muted">✘ ${esc(e.message)}</div>`; return; }
  if (!d.folders.length && !d.files.length) {
    el.innerHTML = `<div class="muted" style="padding:2px 0">(empty)</div>`;
    return;
  }
  el.innerHTML =
    d.folders.map(f => `
      <div class="node" data-prefix="${esc(f.prefix)}">
        <span class="twisty" data-toggle="${esc(f.prefix)}">▸</span>
        <input type="checkbox" class="trfolder" data-prefix="${esc(f.prefix)}"
               ${trCovered(f.prefix) ? "checked" : ""} ${trCovered(f.prefix) && !trPrefixes.has(f.prefix) ? "disabled" : ""}>
        <span class="fname" data-toggle="${esc(f.prefix)}">📁 ${esc(f.name.replace(/\/$/, ""))}</span>
        <span class="meta">${f.count.toLocaleString()} file(s) · ${fmtBytes(f.bytes)}</span>
      </div>
      <div class="kids" data-kids="${esc(f.prefix)}" hidden></div>`).join("") +
    d.files.map(f => `
      <div class="node">
        <span class="twisty"></span>
        <input type="checkbox" class="trfile" data-key="${esc(f.key)}"
               ${trKeys.has(f.key) || trCovered(f.key) ? "checked" : ""} ${trCovered(f.key) ? "disabled" : ""}>
        <span class="leaf">📄 ${esc(f.name)}</span>
        <span class="meta" title="${(f.size || 0).toLocaleString()} bytes">${fmtBytes(f.size)}</span>
        ${chip(f.status)}${manifestBadge(f.manifest_state)}
        ${f.expires_at ? `<span class="chip WARNING" title="scheduled for deletion">expires ${esc(f.expires_at.slice(0, 10))}</span>` : ""}
      </div>`).join("");

  el.querySelectorAll("[data-toggle]").forEach(t => t.onclick = async () => {
    const p = t.dataset.toggle;
    const kids = el.querySelector(`[data-kids="${CSS.escape(p)}"]`);
    const tw = el.querySelector(`.twisty[data-toggle="${CSS.escape(p)}"]`);
    if (!kids) return;
    if (kids.hidden) {
      trOpen.add(p);
      await trExpand(el, p);
    } else {
      // keep descendants in trOpen so re-expanding restores the deeper state
      trOpen.delete(p);
      kids.hidden = true; if (tw) tw.textContent = "▸";
    }
  });
  el.querySelectorAll(".trfolder").forEach(cb => cb.onchange = () => {
    cb.checked ? trPrefixes.add(cb.dataset.prefix) : trPrefixes.delete(cb.dataset.prefix);
    trUpdateSel();
    trSyncChecks();   // mark descendants as covered, in place — no re-render
  });
  el.querySelectorAll(".trfile").forEach(cb => cb.onchange = () => {
    cb.checked ? trKeys.add(cb.dataset.key) : trKeys.delete(cb.dataset.key);
    trUpdateSel();
  });

  // restore whatever the user had open at this level
  for (const f of d.folders) if (trOpen.has(f.prefix)) await trExpand(el, f.prefix);
}

let trCanDelete = null;
async function loadTree() {
  if (trCanDelete === null) {
    try { trCanDelete = (await api(`/api/retention/permission?bucket=${encodeURIComponent(trBucket())}`)).can_delete; }
    catch (e) { trCanDelete = null; }
  }
  trUpdateSel();
  const y = window.scrollY;
  await trRenderInto($("#treeRoot"), "");
  window.scrollTo({ top: y });   // a refresh shouldn't move you off your place
}

$("#btnTrClear").onclick = () => {
  trPrefixes.clear(); trKeys.clear(); trUpdateSel(); trSyncChecks();
};

$("#btnTrRestore").onclick = async () => {
  const bucket = trBucket();
  if (!confirm(`Request a ${$("#trTier").value} restore for ${trPrefixes.size} folder(s) and ${trKeys.size} file(s)?`)) return;
  $("#trResult").textContent = "requesting restores…";
  try {
    const r = await api("/api/restore", { body: {
      bucket, prefixes: [...trPrefixes], tier: $("#trTier").value, days: +$("#trDays").value,
      items: [...trKeys].map(k => ({ bucket, key: k })),
    }});
    const failed = r.results.filter(x => !x.ok).length;
    $("#trResult").innerHTML = failed
      ? `<span style="color:var(--err)">requested ${r.results.length}, ${failed} failed — see Restores/Logs</span>`
      : `<span style="color:var(--ok)">✔ restore requested for ${r.results.length} object(s)</span>`;
  } catch (e) { $("#trResult").textContent = "✘ " + e.message; }
};

async function trExpiry(date) {
  const bucket = trBucket();
  if (!trPrefixes.size && !trKeys.size) return alert("Tick a folder or some files first");
  $("#trResult").textContent = date ? "scheduling expiry…" : "clearing expiry…";
  try {
    const r = await api("/api/retention/expiry", { body: {
      bucket, prefixes: [...trPrefixes], keys: [...trKeys], date,
    }});
    $("#trResult").innerHTML = `<span style="color:var(--ok)">✔ expiry ${
      date ? "set to " + esc(date) : "cleared"} for ${r.updated.toLocaleString()} object(s)</span>`
      + ` <span class="muted">· nothing is deleted automatically — see Retention → Expiry schedule</span>`;
    loadTree();
  } catch (e) { $("#trResult").textContent = "✘ " + e.message; }
}

$("#btnTrExpiry").onclick = () => {
  const d = $("#trExpDate").value;
  if (!d) return alert("Pick a date first");
  trExpiry(d);
};
$("#btnTrExpiryClear").onclick = () => trExpiry(null);

// Fetched rather than navigated to, so a missing optional dependency (or any
// other error) shows up here instead of replacing the page with JSON.
async function trExport(fmt) {
  const q = new URLSearchParams({ bucket: trBucket(), format: fmt });
  $("#trResult").innerHTML = `<span class="muted">building the ${fmt.toUpperCase()} export…</span>`;
  try {
    const res = await fetch("/api/tree/export?" + q.toString());
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = (await res.json()).error || msg; } catch (_) { /* not JSON */ }
      throw new Error(msg);
    }
    const m = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = m ? m[1] : `coldvault-tree.${fmt}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    $("#trResult").innerHTML =
      `<span style="color:var(--ok)">✔ ${esc(a.download)} · ${fmtBytes(blob.size)}</span>`;
  } catch (e) { $("#trResult").textContent = "✘ " + e.message; }
}
$("#btnTrXlsx").onclick = () => trExport("xlsx");
$("#btnTrPdf").onclick = () => trExport("pdf");
$("#btnTrCsv").onclick = () => trExport("csv");

$("#btnTrDelete").onclick = async () => {
  const bucket = trBucket();
  $("#trResult").textContent = "checking what that covers…";
  let p;
  try {
    p = await api("/api/retention/preview", { body: { bucket, prefixes: [...trPrefixes], keys: [...trKeys] } });
  } catch (e) { $("#trResult").textContent = "✘ " + e.message; return; }
  let warn = `PERMANENTLY DELETE ${p.count} object(s) (${fmtBytes(p.bytes)}) from ${bucket}?`;
  if (p.early_count) warn += `\n\n${p.early_count} are inside the ${p.min_days}-day minimum storage duration — you'll still be billed for up to ${p.max_early_days} more day(s).`;
  if (p.versioning === "Enabled") warn += `\n\nBucket versioning is ENABLED: this adds delete markers; versions keep costing until purged.`;
  warn += `\n\nThis cannot be undone.`;
  if (!confirm(warn)) { $("#trResult").textContent = ""; return; }
  const typed = prompt("Type the bucket name to confirm:", "");
  if (typed === null) return;
  $("#trResult").textContent = "deleting…";
  try {
    const r = await api("/api/retention/delete", { body: {
      bucket, prefixes: [...trPrefixes], keys: [...trKeys],
      confirm: typed.trim(), reason: "tree selection",
    }});
    $("#trResult").innerHTML = `<span style="color:var(--ok)">✔ deleted ${r.deleted} object(s) · ${fmtBytes(r.bytes)}</span>`
      + (r.failed ? ` <span style="color:var(--err)">· ${r.failed} failed</span>` : "")
      + (r.early ? ` <span class="muted">· ${r.early} within the minimum duration (still billed)</span>` : "");
    trPrefixes.clear(); trKeys.clear();
    loadTree();
  } catch (e) { $("#trResult").textContent = "✘ " + e.message; }
};

/* ---------- retention / deletion ---------- */
let rtLast = null;   // last preview, so delete acts on exactly what was shown

function rtBucket() { return $("#bucketFilter").value && $("#bucketFilter").value !== "*"
  ? $("#bucketFilter").value : ($("#bucketBadge").dataset.bucket || ""); }

function rtBody(extra) {
  return Object.assign({ bucket: rtBucket(), prefix: $("#rtPrefix").value.trim() }, extra || {});
}

$("#btnRtPreview").onclick = async () => {
  $("#rtPreview").textContent = "checking…";
  try {
    const r = await api("/api/retention/preview", { body: rtBody() });
    rtLast = r;
    let h = `<b>${r.count.toLocaleString()}</b> object(s) · <b>${fmtBytes(r.bytes)}</b> under ` +
            `<code>${esc(r.prefix || "(whole bucket)")}</code> in <code>${esc(r.bucket)}</code>`;
    if (r.early_count) h += `<br><span style="color:var(--warn)">⚠ ${r.early_count} object(s) ` +
      `(${fmtBytes(r.early_bytes)}) are younger than the ${r.min_days}-day minimum storage ` +
      `duration — deleting them now still bills up to ${r.max_early_days} more day(s).</span>`;
    if (r.versioning === "Enabled") h += `<br><span style="color:var(--warn)">⚠ bucket versioning is ` +
      `ENABLED — deleting adds a delete marker; old versions keep costing until purged.</span>`;
    if (r.sample.length) h += `<div class="mono" style="font-size:11px;margin-top:6px">` +
      r.sample.slice(0, 15).map(s => esc(s.key)).join("<br>") +
      (r.count > 15 ? `<br>… and ${(r.count - 15).toLocaleString()} more` : "") + `</div>`;
    $("#rtPreview").innerHTML = h;
  } catch (e) { rtLast = null; $("#rtPreview").textContent = "✘ " + e.message; }
};

async function rtSetExpiry(date) {
  if (!$("#rtPrefix").value.trim()) return alert("Enter a prefix first (then Preview to check it)");
  $("#rtExpiryResult").textContent = "applying…";
  try {
    const r = await api("/api/retention/expiry", { body: rtBody({ date }) });
    $("#rtExpiryResult").textContent = `✔ ${r.updated} object(s) — expiry ${r.expires_at || "cleared"}`;
    loadRetention();
  } catch (e) { $("#rtExpiryResult").textContent = "✘ " + e.message; }
}
$("#btnRtSetExpiry").onclick = () => {
  const d = $("#rtDate").value;
  if (!d) return alert("Pick a date");
  rtSetExpiry(d);
};
$("#btnRtClearExpiry").onclick = () => rtSetExpiry(null);

async function rtDelete({ due }) {
  const bucket = rtBucket();
  const what = due
    ? `every object currently DUE for expiry in ${bucket}`
    : (rtLast ? `${rtLast.count} object(s) (${fmtBytes(rtLast.bytes)}) under "${rtLast.prefix || "(whole bucket)"}"` : null);
  if (!due && !rtLast) return alert("Preview first — delete acts on exactly what the preview showed");
  if (!due && rtLast.count === 0) return alert("The preview matched nothing");
  const early = !due && rtLast.early_count
    ? `\n\n${rtLast.early_count} of them are inside the ${rtLast.min_days}-day minimum storage duration, so you will still be billed for up to ${rtLast.max_early_days} more day(s).` : "";
  if (!confirm(`PERMANENTLY DELETE ${what} from S3?${early}\n\nThis cannot be undone.`)) return;
  const typed = prompt(`This is irreversible.\n\nType the bucket name to confirm:`, "");
  if (typed === null) return;
  const el = due ? $("#rtDueResult") : $("#rtDeleteResult");
  el.textContent = "deleting…";
  try {
    const body = due
      ? { bucket, due: true, confirm: typed.trim(), reason: $("#rtReason").value.trim() }
      : rtBody({ confirm: typed.trim(), reason: $("#rtReason").value.trim() });
    const r = await api("/api/retention/delete", { body });
    let msg = `✔ deleted ${r.deleted} object(s) · ${fmtBytes(r.bytes)} freed`;
    if (r.early) msg += ` · ${r.early} within the minimum duration (still billed)`;
    if (r.failed) msg += ` · ✘ ${r.failed} failed`;
    if (r.versioning === "Enabled") msg += " · versioning on: delete markers added, versions remain";
    el.textContent = msg;
    rtLast = null;
    loadRetention();
    if (activeTab === "files") loadFiles();
  } catch (e) { el.textContent = "✘ " + e.message; }
}
$("#btnRtDelete").onclick = () => rtDelete({ due: false });
$("#btnRtDeleteDue").onclick = () => rtDelete({ due: true });
$("#btnRtDueRefresh").onclick = () => loadRetention();
$("#rtDueScope").onchange = () => loadRetention();

// Unschedule everything in the current list in one call — clearing a folder's
// worth of objects a row at a time is a lot of clicking (and a lot of log lines).
$("#btnRtUnsetAll").onclick = async () => {
  const keys = [...$("#rtDueTable").querySelectorAll(".rtUnset")].map(b => b.dataset.key);
  if (!keys.length) return;
  if (!confirm(`Clear the expiry date on ${keys.length} object(s)?\n\n`
             + `This only unschedules them — no data is touched.`)) return;
  $("#rtDueResult").textContent = "clearing…";
  try {
    const r = await api("/api/retention/expiry", {
      body: { bucket: rtBucket(), keys, date: null } });
    $("#rtDueResult").textContent = `✔ expiry cleared for ${r.updated.toLocaleString()} object(s)`;
    loadRetention(true);
  } catch (e) { $("#rtDueResult").textContent = "✘ " + e.message; }
};

async function loadRetentionPermission(recheck) {
  const el = $("#rtPerm");
  el.style.display = "";
  el.innerHTML = `<div class="muted">${recheck ? "re-probing" : "checking"} delete permission…</div>`;
  try {
    const r = await api(`/api/retention/permission?${recheck ? "recheck=1&" : ""}bucket=`
                        + encodeURIComponent(rtBucket()));
    trCanDelete = r.can_delete;
    const dis = r.can_delete !== true;
    $("#btnRtDelete").disabled = dis;
    $("#btnRtDeleteDue").disabled = dis;
    const foot = `<div class="muted" style="margin-top:6px">
        ${r.checked_at ? `Probed ${esc(r.checked_at)} and remembered — ColdVault does not re-probe
           on its own.` : ""}
        <button id="btnRtRecheck" class="secondary" style="margin-left:6px">Re-check</button></div>`;
    if (r.can_delete === true) {
      el.innerHTML = `<div class="banner warn">⚠ This IAM user <b>CAN permanently delete</b> from
        <code>${esc(r.bucket)}</code>. Deletions here are irreversible.</div>
        <div class="muted" style="margin-top:6px">To make ColdVault strictly append-only, remove
        <code>s3:DeleteObject</code> from the IAM policy — every other feature keeps working.</div>${foot}`;
    } else if (r.can_delete === false) {
      el.innerHTML = `<div class="banner ok">🔒 Append-only: this IAM user <b>cannot delete</b> from
        <code>${esc(r.bucket)}</code>, so nothing here can remove your archive.</div>
        <div class="muted" style="margin-top:6px">Scheduling expiry still works (it only flags what's due).
        To enable deletion, grant <code>s3:DeleteObject</code> on <code>arn:aws:s3:::${esc(r.bucket)}/*</code>.</div>${foot}`;
    } else {
      el.innerHTML = `<div class="banner warn">Delete permission unknown: ${esc(r.detail || "not probed")}</div>${foot}`;
    }
    $("#btnRtRecheck").onclick = () => loadRetentionPermission(true);
  } catch (e) {
    el.innerHTML = `<div class="muted">permission check failed: ${esc(e.message)}</div>`;
  }
}

async function loadRetention(skipPerm) {
  if (!skipPerm) loadRetentionPermission();
  try {
    const scope = $("#rtDueScope").value;
    const [due, del] = await Promise.all([
      api(`/api/retention/due?scope=${scope}&bucket=` + encodeURIComponent(rtBucket())),
      api("/api/deletions?bucket=" + encodeURIComponent(rtBucket())),
    ]);
    $("#rtDueCount").textContent =
      `— ${due.due} due now (${fmtBytes(due.due_bytes)}) of ${due.scheduled} scheduled (${fmtBytes(due.scheduled_bytes)})`
      + (due.next_expiry && !due.due ? ` · earliest ${due.next_expiry.slice(0, 10)}` : "");
    $("#btnRtDeleteDue").disabled = due.due === 0 || trCanDelete !== true;
    $("#rtDueTable tbody").innerHTML = due.items.map(i => `<tr${i.due ? "" : ' style="opacity:.8"'}>
        <td class="key">${esc(i.key)}</td>
        <td class="num" title="${(i.size || 0).toLocaleString()} bytes">${fmtBytes(i.size)}</td>
        <td class="mono">${esc(i.uploaded_at || "—")}</td>
        <td class="mono">${esc(i.expires_at || "—")}</td>
        <td class="num">${i.due ? `<span style="color:var(--warn)">due</span>`
                                : `${(i.days_left ?? 0).toLocaleString()}d`}</td>
        <td class="num">${i.early_days ? `<span style="color:var(--warn)">${i.early_days}d billed</span>` : "—"}</td>
        <td><button class="secondary rtUnset" data-key="${esc(i.key)}"
                    title="remove this object's expiry date">clear</button></td>
      </tr>`).join("") || `<tr><td colspan="7" class="muted" style="padding:20px">${
        due.scheduled
          ? (scope === "due"
              ? `nothing is due yet — ${due.scheduled.toLocaleString()} object(s) are scheduled${
                  due.next_expiry ? `, the earliest on ${esc(due.next_expiry.slice(0, 10))}` : ""
                }. Switch to <b>all scheduled</b> to see them.`
              : "nothing scheduled")
          : "nothing scheduled — set an expiry above, or tick a folder in the Tree tab"
      }</td></tr>`;
    if (due.truncated) {
      $("#rtDueTable tbody").insertAdjacentHTML("beforeend",
        `<tr><td colspan="7" class="muted">showing the first 500 of ${due.scheduled.toLocaleString()}</td></tr>`);
    }
    $("#rtDueTable").querySelectorAll(".rtUnset").forEach(b => b.onclick = async () => {
      b.disabled = true;
      try {
        await api("/api/retention/expiry", { body: {
          bucket: rtBucket(), keys: [b.dataset.key], date: null } });
        loadRetention(true);   // just the table — no need to re-check permission
      } catch (e) { b.disabled = false; alert("✘ " + e.message); }
    });

    const t = del.totals;
    $("#rtDelTotals").textContent = `— ${t.count} object(s), ${fmtBytes(t.bytes)} removed${t.early ? `, ${t.early} early` : ""}`;
    $("#rtDeletionsTable tbody").innerHTML = del.items.map(d => `<tr>
        <td class="mono">${esc(d.deleted_at || "")}</td>
        <td class="mono">${esc(d.bucket || "")}</td>
        <td class="key">${esc(d.key)}</td>
        <td class="num" title="${(d.size || 0).toLocaleString()} bytes">${fmtBytes(d.size)}</td>
        <td class="mono">${esc(d.uploaded_at || "—")}</td>
        <td>${chip(d.mode)}</td>
        <td>${esc(d.reason || "—")}</td>
        <td class="num">${d.early_days ? `<span style="color:var(--warn)">${d.early_days}d</span>` : "—"}</td>
      </tr>`).join("") || `<tr><td colspan="8" class="muted" style="padding:20px">nothing has been deleted</td></tr>`;
  } catch (e) {
    $("#rtDueCount").textContent = "— error: " + e.message;
  }
}

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
