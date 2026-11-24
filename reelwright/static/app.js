/* Reelwright UI — vanilla JS, no build step. */
"use strict";

// ---------- utilities ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = $("#view");

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body ? "POST" : "GET"), headers: {} };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${r.status})`);
  return data;
}

function toast(msg, kind = "info", ms = 4200) {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), ms);
}
const fail = (e) => toast(e.message || String(e), "err", 7000);

const fmt = (t, dp = 1) => {
  if (!isFinite(t)) return "0:00";
  t = Math.max(0, t);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = (t % 60).toFixed(dp).padStart(dp ? 3 + dp : 2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
};
const parseTime = (s) => String(s).trim().split(":").reverse().reduce((acc, p, i) => acc + parseFloat(p || 0) * 60 ** i, 0);
const when = (iso) => iso ? new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—";
const toLocalInput = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 16);
const tomorrowAt9 = () => { const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); return toLocalInput(d); };
const fromLocalInput = (v) => (v ? new Date(v).toISOString() : null);
const pill = (map, key) => { const [cls, label] = map[key] || ["", key]; return `<span class="pill ${cls}">${esc(label)}</span>`; };
const bar = (p) => `<div class="bar"><div style="width:${Math.round((p || 0) * 100)}%"></div></div>`;
const num = (n) => (n == null ? "—" : Number(n) >= 1e6 ? (n / 1e6).toFixed(1) + "M" : Number(n) >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n));

const PROJECT_STATUS = { importing: ["run", "Importing"], queued: ["info", "Queued"], analyzing: ["run", "Analyzing"], ready: ["ok", "Ready"], failed: ["err", "Failed"] };
const UPLOAD_STATUS = { waiting_render: ["info", "Rendering first"], scheduled: ["warn", "Waiting to post"], queued: ["info", "Queued"], uploading: ["run", "Uploading"], done: ["ok", "Posted"], failed: ["err", "Failed"], canceled: ["", "Canceled"], needs_auth: ["warn", "Needs connection"] };
const JOB_STATUS = { queued: ["info", "Queued"], running: ["run", "Running"], done: ["ok", "Done"], failed: ["err", "Failed"], canceled: ["", "Canceled"] };
const JOB_LABEL = { analyze: "Analyze video", import_url: "Import video", find: "AI clip search", export: "Render clip", upload: "Publish", scan_source: "Scan channel", virality: "Virality analysis", train: "Train style profile", research: "Live platform research", results: "Fetch post results", ingest_file: "Import from OBS" };
const PLATFORM = { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram" };
const platTag = (p) => `<span class="plat ${p}">${PLATFORM[p] || esc(p)}</span>`;
const SIZES = { "9:16": [1080, 1920], "1:1": [1080, 1080], "4:5": [1080, 1350], "16:9": [1920, 1080] };
const CAPTION_POS = { low: [0.24, 0.08], middle: [0.42, 0.40], high: [0.68, 0.72] };
const CATEGORIES = { 1: "Film & Animation", 2: "Autos & Vehicles", 10: "Music", 15: "Pets & Animals", 17: "Sports", 19: "Travel & Events", 20: "Gaming", 22: "People & Blogs", 23: "Comedy", 24: "Entertainment", 25: "News & Politics", 26: "Howto & Style", 27: "Education", 28: "Science & Technology" };

const S = { settings: null, conn: null, timers: [], nav: 0 };

function modal(html, onMount, wide = false) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-bg"><div class="modal" ${wide ? 'style="width:min(900px,100%)"' : ""}>${html}</div></div>`;
  const close = () => (root.innerHTML = "");
  $(".modal-bg", root).addEventListener("mousedown", (e) => e.target.classList.contains("modal-bg") && close());
  root.querySelectorAll("[data-close]").forEach((b) => (b.onclick = close));
  onMount && onMount($(".modal", root), close);
  return close;
}
const confirmBox = (title, body, label = "Delete") => new Promise((res) => {
  modal(`<h2>${esc(title)}</h2><p class="muted">${esc(body)}</p><div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="cf-ok">${esc(label)}</button></div>`,
    (m, close) => { $("#cf-ok", m).onclick = () => { close(); res(true); }; $$("[data-close]", m).forEach((b) => b.addEventListener("click", () => res(false))); });
});

// ---------- router & polling ----------
const routes = { library: Library, editor: Editor, sources: Sources, obs: Obs, lab: Lab, publish: Publish, jobs: Jobs, settings: Settings };
let current = null;

async function route() {
  const [name, arg] = (location.hash.replace(/^#\//, "") || "library").split("/");
  const nav = ++S.nav;
  current?.destroy?.();
  view.onclick = view.onchange = null;
  S.timers.forEach(clearInterval);
  S.timers = [];
  $("#modal-root").innerHTML = "";
  $$(".side nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  if (name === "editor" && !arg) return (location.hash = localStorage.lastProject ? `#/editor/${localStorage.lastProject}` : "#/library");
  const View = routes[name] || Library;
  current = new View(arg);
  try { await current.mount(); } catch (e) { fail(e); }
  if (nav !== S.nav) return; // the user navigated away while this page was loading
  if (current.refresh) S.timers.push(setInterval(() => current.refresh().catch(() => {}), current.every || 1500));
}
window.addEventListener("hashchange", route);

async function sideStatus() {
  try {
    const [settings, conn, jobs] = await Promise.all([api("/api/settings"), api("/api/connections"), api("/api/jobs")]);
    S.settings = settings; S.conn = conn;
    const running = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
    const b = $("#jobs-badge"); b.hidden = !running; b.textContent = running;
    const row = (ok, label, text, href) => `<div class="row"><span class="pill ${ok ? "ok" : "warn"}">${label}</span><span>${ok ? esc(text) : `<a href="${href}">connect</a>`}</span></div>`;
    $("#side-status").innerHTML = row(settings.anthropic_key_set, "Claude AI", "connected", "#/settings")
      + row(conn.youtube.connected, "YouTube", conn.youtube.channel?.title || "connected", "#/publish")
      + row(conn.tiktok.connected, "TikTok", conn.tiktok.name || "connected", "#/publish")
      + row(conn.instagram.connected, "Instagram", conn.instagram.name || "connected", "#/publish");
  } catch { /* server restarting */ }
}

// ---------- autopilot form (shared) ----------
const AP_DEFAULT = { enabled: false, top_n: 3, first_publish: "", interval_hours: 24, privacy: "public", aspect: "9:16", captions: true, jump_cuts: false, mode: "publish_at", platforms: ["youtube"] };
const apLoad = () => { try { return { ...AP_DEFAULT, ...JSON.parse(localStorage.autopilot || "{}") }; } catch { return { ...AP_DEFAULT }; } };
function apForm(ap, withToggle = true) {
  return `
  ${withToggle ? `<div class="row"><h2 class="grow" style="margin:0">⚡ Autopilot</h2><label class="switch"><input type="checkbox" id="ap-enabled" ${ap.enabled ? "checked" : ""}></label></div>
  <p class="muted" style="margin:6px 0 14px;font-size:12.5px">When the scan finishes, the top clips are rendered and scheduled to your channels automatically.</p>` : ""}
  <div class="ap-body stack">
    <div class="plats">${Object.entries(PLATFORM).map(([k, v]) => `<label><input type="checkbox" class="ap-plat" value="${k}" ${ap.platforms.includes(k) ? "checked" : ""}> ${v}</label>`).join("")}</div>
    <div class="grid2">
      <label class="field">Clips to publish<input type="number" id="ap-top" min="1" max="30" value="${ap.top_n}"></label>
      <label class="field">Every (hours)<input type="number" id="ap-int" min="1" max="720" step="1" value="${ap.interval_hours}"></label>
    </div>
    <label class="field">First one goes live<input type="datetime-local" id="ap-first" value="${ap.first_publish || tomorrowAt9()}"></label>
    <div class="grid2">
      <label class="field">Format<select id="ap-aspect">${["9:16", "1:1", "4:5", "16:9", "source"].map((a) => `<option ${a === ap.aspect ? "selected" : ""} value="${a}">${a === "9:16" ? "9:16 vertical" : a === "source" ? "Original" : a}</option>`).join("")}</select></label>
      <label class="field">Visibility<select id="ap-priv">${["public", "unlisted", "private"].map((p) => `<option ${p === ap.privacy ? "selected" : ""}>${p}</option>`).join("")}</select></label>
    </div>
    <label class="field">Method<select id="ap-mode">
      <option value="publish_at" ${ap.mode === "publish_at" ? "selected" : ""}>Upload now; YouTube publishes on schedule (TikTok/IG post at the time)</option>
      <option value="upload_at" ${ap.mode === "upload_at" ? "selected" : ""}>Hold locally; post everywhere at each scheduled time</option></select></label>
    <label class="switch"><input type="checkbox" id="ap-cap" ${ap.captions ? "checked" : ""}> Burn in animated captions</label>
    <label class="switch"><input type="checkbox" id="ap-jc" ${ap.jump_cuts ? "checked" : ""}> AI jump cuts (remove silences)</label>
  </div>`;
}
function apRead(root) {
  const ap = {
    enabled: $("#ap-enabled", root)?.checked ?? true, top_n: +$("#ap-top", root).value || 3, interval_hours: +$("#ap-int", root).value || 24,
    first_publish: $("#ap-first", root).value, aspect: $("#ap-aspect", root).value, privacy: $("#ap-priv", root).value,
    mode: $("#ap-mode", root).value, captions: $("#ap-cap", root).checked, jump_cuts: $("#ap-jc", root).checked,
    platforms: $$(".ap-plat", root).filter((c) => c.checked).map((c) => c.value),
  };
  // only the Library card's own switch changes whether Autopilot is on for new imports
  localStorage.autopilot = JSON.stringify({ ...ap, enabled: $("#ap-enabled", root) ? ap.enabled : apLoad().enabled });
  return { ...ap, first_publish: fromLocalInput(ap.first_publish) };
}

// ---------- virality report view (shared) ----------
function reportHTML(r) {
  return `<div class="row" style="gap:18px;align-items:flex-start;flex-wrap:wrap">
      <div class="ring" style="--p:${+r.score || 0}"><b>${+r.score || 0}</b></div>
      <div class="grow" style="min-width:240px"><h2 style="margin-bottom:4px">${esc(r.title)}</h2><p style="margin:0 0 6px">${esc(r.verdict)}</p>
        <div class="muted" style="font-size:12.5px">Best fit: <b>${esc(r.best_platform)}</b></div></div></div>
    <div class="two" style="margin-top:16px"><div>${(r.dimensions || []).map((d) => `<div class="dim-row"><span>${esc(d.name)}</span>${bar(d.score / 100)}<span class="mono">${+d.score}</span></div><div class="muted" style="font-size:11.5px;margin:-2px 0 8px">${esc(d.note)}</div>`).join("")}</div>
      <div class="stack"><div><h3>Strengths</h3><ul class="list">${r.strengths.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
        <div><h3>Risks</h3><ul class="list">${r.risks.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div></div>
    <div class="sec"><h3>Improve it</h3><ul class="list">${r.improvements.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
    <div class="sec"><h3>Title ideas</h3><div class="chips">${r.title_ideas.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div></div>
    <div class="sec"><h3>What the top similar videos do</h3><p style="margin:0;font-size:13px;color:#c4c7d6">${esc(r.comparable_patterns)}</p></div>
    ${r.hot_moments?.length ? `<div class="sec"><h3>Strongest moments</h3>${r.hot_moments.map((m, i) => `<div class="row" style="font-size:13px;margin-bottom:6px"><span class="mono">${fmt(m.start)}–${fmt(m.end)}</span><span class="grow">${esc(m.why)}</span>
      ${r.project_id ? `<button class="btn xs" data-mk="${i}">＋ Clip</button>` : ""}</div>`).join("")}</div>` : ""}
    <div class="sec"><h3>Similar videos on YouTube — “${esc(r.query)}”</h3><table><thead><tr><th>Views</th><th>Length</th><th>Title</th></tr></thead><tbody>
      ${(r.similar || []).map((v) => `<tr><td class="mono">${num(v.views)}</td><td class="mono">${v.duration ? fmt(v.duration, 0) : "—"}</td><td>${/^https:\/\//.test(v.url || "") ? `<a href="${esc(v.url)}" target="_blank">${esc(v.title)}</a>` : esc(v.title)} <span class="dim">· ${esc(v.channel || "")}</span></td></tr>`).join("")}</tbody></table></div>`;
}
async function openReport(id) {
  const r = await api(`/api/reports/${id}`);
  modal(`${reportHTML(r)}<div class="foot"><button class="btn" data-close>Close</button></div>`, (m) => {
    m.onclick = async (e) => {
      const b = e.target.closest("[data-mk]"); if (!b) return;
      const h = r.hot_moments[+b.dataset.mk];
      try { await api(`/api/projects/${r.project_id}/clips`, { body: { start: h.start, end: h.end, title: h.why.slice(0, 60) } }); toast("Clip added — open it in the editor.", "ok"); b.disabled = true; } catch (err) { fail(err); }
    };
  }, true);
}

// ======================================================================
// Library
// ======================================================================
function Library() {
  this.mount = async () => {
    const ap = apLoad();
    view.innerHTML = `<div class="page">
      <h1>Library</h1><p class="sub">Drop in long-form video or a stream VOD — Reelwright transcribes it, finds the best moments, and gets them ready to post.</p>
      <div class="hero">
        <div class="drop" id="drop">
          <div class="big">Drop a video here</div>
          <div class="muted">MP4, MOV, MKV, WebM… any length. Processing happens on this computer.</div>
          <button class="btn primary" id="browse">Choose file</button><input type="file" id="file" accept="video/*,.mkv" hidden multiple>
          <div class="or">or import a link — YouTube, Twitch, Kick VODs &amp; clips, 1,000+ sites</div>
          <form id="urlform"><input type="url" id="url" placeholder="https://www.twitch.tv/videos/…" required><button class="btn">Import</button></form>
          <a href="#/sources" class="muted" style="font-size:12.5px">Connect a Twitch / Kick / YouTube channel to auto-import every new stream →</a>
          <div id="upl" class="stack" style="width:100%;max-width:460px"></div>
        </div>
        <div class="card autopilot ${ap.enabled ? "" : "off"}" id="ap">${apForm(ap)}</div>
      </div>
      <div class="row" style="margin-bottom:12px"><h2 class="grow" style="margin:0">Projects</h2></div>
      <div id="projects" class="grid"></div></div>`;
    const drop = $("#drop"), file = $("#file");
    $("#browse").onclick = () => file.click();
    file.onchange = () => [...file.files].forEach(upload);
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => [...e.dataTransfer.files].forEach(upload));
    $("#ap-enabled").onchange = (e) => { $("#ap").classList.toggle("off", !e.target.checked); apRead($("#ap")); };
    $("#ap").addEventListener("change", () => apRead($("#ap")));
    $("#urlform").onsubmit = async (e) => {
      e.preventDefault();
      try {
        const p = await api("/api/projects/url", { body: { url: $("#url").value, autopilot: apRead($("#ap")) } });
        $("#url").value = ""; toast("Import started — analysis begins when the download finishes.", "ok"); this.refresh();
        localStorage.lastProject = p.id;
      } catch (err) { fail(err); }
    };
    await this.refresh();
  };

  const upload = (f) => {
    const row = document.createElement("div");
    row.innerHTML = `<div class="row" style="font-size:12px"><span class="grow" style="text-align:left">${esc(f.name)}</span><span class="pct">0%</span></div>${bar(0)}`;
    $("#upl").append(row);
    const fd = new FormData();
    fd.append("file", f);
    fd.append("autopilot", JSON.stringify(apRead($("#ap"))));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/projects");
    xhr.upload.onprogress = (e) => { const p = e.loaded / e.total; $(".pct", row).textContent = `${Math.round(p * 100)}%`; $(".bar div", row).style.width = `${p * 100}%`; };
    xhr.onload = () => {
      row.remove();
      if (xhr.status >= 400) return fail(new Error(JSON.parse(xhr.responseText || "{}").detail || "Upload failed"));
      toast(`${f.name} added — analyzing now.`, "ok");
      localStorage.lastProject = JSON.parse(xhr.responseText).id;
      this.refresh();
    };
    xhr.onerror = () => { row.remove(); fail(new Error("Upload failed")); };
    xhr.send(fd);
  };

  this.refresh = async () => {
    const list = await api("/api/projects");
    const el = $("#projects");
    if (!el) return;
    if (!list.length) { el.innerHTML = `<div class="empty card" style="grid-column:1/-1"><b>No projects yet</b>Add a video above to get started.</div>`; return; }
    el.innerHTML = list.map((p) => `
      <div class="proj" data-id="${p.id}">
        <div class="thumb" style="background-image:url('/files/${p.id}/poster.jpg')">${p.meta ? `<span class="dur">${fmt(p.meta.duration, 0)}</span>` : ""}</div>
        <button class="btn xs danger del" data-del="${p.id}">Delete</button>
        <div class="body">
          <div class="name" title="${esc(p.name)}">${esc(p.name)}</div>
          <div class="row wrap">${pill(PROJECT_STATUS, p.status)}<span class="muted grow" style="font-size:12px">${p.status === "ready" ? `${p.clip_count} clips` : esc(p.stage || "")}</span>
            ${p.viral ? `<span class="vbadge" title="Virality score">✦ ${+p.viral.score}</span>` : ""}${p.origin ? `<span class="chip">${esc(p.origin.platform)}</span>` : ""}
            ${p.autopilot?.enabled ? '<span class="pill run" title="Autopilot on">Autopilot</span>' : ""}</div>
          ${p.status !== "ready" && p.status !== "failed" ? bar(p.progress) : ""}
          ${p.error ? `<div style="color:var(--err);font-size:12px">${esc(p.error).slice(0, 180)}</div>` : ""}
        </div></div>`).join("");
    el.onclick = async (e) => {
      const del = e.target.closest("[data-del]");
      if (del) {
        e.stopPropagation();
        if (await confirmBox("Delete project?", "This removes the source video, clips and renders from this computer. Posts already published stay online.")) {
          await api(`/api/projects/${del.dataset.del}`, { method: "DELETE" }).catch(fail);
          if (localStorage.lastProject === del.dataset.del) delete localStorage.lastProject;
          this.refresh();
        }
        return;
      }
      const card = e.target.closest(".proj");
      if (card) location.hash = `#/editor/${card.dataset.id}`;
    };
  };
}

// ======================================================================
// Editor
// ======================================================================
function Editor(pid) {
  const E = { pid, proj: null, clips: [], segs: [], words: [], signals: {}, sel: null, tab: "clips", zoom: 1, inMark: null, outMark: null, loop: false, pending: {}, sig: "", picked: new Set() };
  let video, canvas, ctx, raf, keyHandler, listeners, ro;
  const dur = () => E.proj?.meta?.duration || 0;
  const selClip = () => E.clips.find((c) => c.id === E.sel);

  this.mount = async () => {
    E.proj = await api(`/api/projects/${pid}`);
    localStorage.lastProject = pid;
    $("#nav-editor").hidden = false;
    E.clips = E.proj.clips;
    if (E.proj.status === "ready" || E.proj.has_transcript) await loadAnalysis();
    if (E.dead) return;
    E.sel = E.clips[0]?.id || null;
    layout();
    keyHandler = onKey;
    document.addEventListener("keydown", keyHandler);
  };
  this.destroy = () => { E.dead = true; listeners?.abort(); ro?.disconnect(); cancelAnimationFrame(raf); document.removeEventListener("keydown", keyHandler); video?.pause(); };

  async function loadAnalysis() {
    const [tr, sig] = await Promise.all([
      fetch(`/files/${pid}/transcript.json`).then((r) => (r.ok ? r.json() : { segments: [] })),
      fetch(`/files/${pid}/signals.json`).then((r) => (r.ok ? r.json() : {})),
    ]);
    E.segs = tr.segments || [];
    E.words = E.segs.flatMap((s) => s.words || []);
    E.signals = sig;
  }

  function layout() {
    const p = E.proj, ready = p.status === "ready";
    view.innerHTML = `<div class="editor">
      <div class="ed-top">
        <a class="btn ghost sm" href="#/library">← Library</a>
        <div class="title" title="${esc(p.name)}">${esc(p.name)}</div>
        <span id="ed-status">${pill(PROJECT_STATUS, p.status)}</span>
        ${p.viral ? `<button class="btn xs" data-act="report" data-id="${p.viral.report_id}"><span class="vbadge">✦ ${+p.viral.score}</span> report</button>` : ""}
        <div class="grow"></div>
        <button class="btn sm" data-act="viral-video" ${ready ? "" : "disabled"} title="Score this whole video for virality against similar videos">✦ Virality</button>
        <button class="btn sm" data-act="autopilot" ${ready ? "" : "disabled"}>⚡ Autopilot</button>
        <button class="btn sm" data-act="reanalyze">↻ Re-analyze</button>
        <a class="btn sm" href="#/publish">Publish queue</a>
      </div>
      <div class="stage">
        <div class="player">
          ${p.source ? `<div class="vwrap"><video id="vid" src="/files/${pid}/${esc(p.source)}" preload="auto" playsinline></video>
            <div class="cambox" id="cam" hidden></div>
            <div class="cropbox" id="crop"><div class="title-preview" id="tprev" hidden></div><div class="sticker-preview" id="sprev" hidden></div><div class="cap-preview" id="cprev"></div></div></div>` : ""}
          <div class="overlay-msg" id="ovl" ${ready ? "hidden" : ""}></div>
        </div>
        <div class="transport">
          <button class="btn sm" data-act="play" title="Space">▶</button>
          <button class="btn sm" data-act="playclip" title="Play the selected clip on loop">⟲ Play clip</button>
          <span class="tc" id="tc">0:00.0 / ${fmt(dur())}</span>
          <div class="grow"></div>
          <span class="muted" style="font-size:12px">Mark</span>
          <button class="btn xs" data-act="in" title="I">In <span class="kbd">I</span></button>
          <button class="btn xs" data-act="out" title="O">Out <span class="kbd">O</span></button>
          <button class="btn sm primary" data-act="addclip" title="Create a clip from the in/out marks">＋ Clip from marks</button>
        </div>
        <div class="timeline">
          <div class="tl-scroll" id="tls"><div class="tl-inner" id="tli"><div class="tl-sprite" id="spr"></div><canvas id="cv"></canvas></div></div>
          <div class="tl-foot"><span>Zoom</span><input type="range" id="zoom" min="1" max="40" step="0.5" value="${E.zoom}" style="width:min(160px,30vw)">
            <span class="grow"></span><span>Drag clip edges to trim · click to seek · <span class="kbd">←</span><span class="kbd">→</span> nudge · <span class="kbd">[</span><span class="kbd">]</span> jump to clip</span></div>
        </div>
      </div>
      <div class="panel">
        <div class="tabs">${["clips", "transcript", "edit"].map((t) => `<button data-tab="${t}" class="${E.tab === t ? "on" : ""}">${{ clips: "AI Clips", transcript: "Transcript", edit: "Edit" }[t]}</button>`).join("")}</div>
        <div class="tab-body" id="tab"></div>
      </div></div>`;
    video = $("#vid");
    canvas = $("#cv");
    ctx = canvas.getContext("2d");
    listeners?.abort();
    listeners = new AbortController();
    ro?.disconnect();
    if (video) {
      video.addEventListener("loadedmetadata", () => { sizeTimeline(); applyPreview(); });
      video.addEventListener("play", () => { $("[data-act=play]").textContent = "❚❚"; tick(); });
      video.addEventListener("pause", () => { $("[data-act=play]").textContent = "▶"; onTime(); });
      video.addEventListener("seeked", onTime);
      ro = new ResizeObserver(() => { applyPreview(); sizeTimeline(); });
      ro.observe($(".player")); ro.observe($("#tls"));
    }
    if ($("#spr") && p.sprite_count) $("#spr").style.backgroundImage = `url('/files/${pid}/sprite.jpg')`;
    $("#zoom").oninput = (e) => { E.zoom = +e.target.value; sizeTimeline(); };
    bindTimeline();
    view.onclick = onClick;
    $(".tabs").onclick = (e) => { const t = e.target.closest("[data-tab]"); if (t) { E.tab = t.dataset.tab; markTab(); renderTab(); } };
    updateOverlay();
    renderTab();
    sizeTimeline();
  }

  function updateOverlay() {
    const o = $("#ovl"), p = E.proj;
    if (!o) return;
    o.hidden = p.status === "ready";
    if (p.status === "failed") o.innerHTML = `<b style="color:var(--err)">Analysis failed</b><div class="muted" style="max-width:520px">${esc(p.error)}</div><button class="btn primary" data-act="reanalyze">Try again</button>`;
    else o.innerHTML = `<div class="spin" style="width:28px;height:28px;color:var(--accent)"></div><b>${esc(p.stage || "Working…")}</b><div style="width:min(320px,70vw)">${bar(p.progress)}</div>
      <div class="muted" style="font-size:12px">Transcribing, detecting scenes and energy, and asking AI for the best moments.</div>`;
    $("#ed-status").innerHTML = pill(PROJECT_STATUS, p.status);
  }

  // ----- refresh loop -----
  this.refresh = async () => {
    const p = await api(`/api/projects/${pid}`);
    const wasReady = E.proj.status === "ready";
    E.proj = p;
    E.clips = p.clips.map((c) => (E.pending[c.id] ? { ...c, ...E.pending[c.id].local } : c));
    if (!wasReady && p.status === "ready") { await loadAnalysis(); E.sel = E.sel || E.clips[0]?.id; layout(); toast(`Analysis complete — ${E.clips.length} clips found.`, "ok"); return; }
    updateOverlay();
    const sig = JSON.stringify(E.clips.map((c) => [c.id, c.title, c.start, c.end, c.score, c.export?.status, c.export?.rev, c.rev, c.posts, c.viral, c.rating]));
    if (sig !== E.sig) { E.sig = sig; if (E.tab === "clips") renderTab(); draw(); }
    if (E.tab === "edit") renderActions();
  };

  // ----- preview: crop frame, facecam, CSS colour, caption/title/sticker -----
  function applyPreview() {
    if (!video || !video.videoWidth) return;
    const c = selClip(), box = $("#crop"), cam = $("#cam");
    const e = c?.edit;
    video.style.filter = e ? `brightness(${1 + e.brightness}) contrast(${e.contrast}) saturate(${e.saturation})` : "";
    video.playbackRate = e ? +e.speed : 1;
    const dw = video.clientWidth, dh = video.clientHeight, sw = video.videoWidth, sh = video.videoHeight, k = dw / sw;
    cam.hidden = true;
    const [OW, OH] = e && SIZES[e.aspect] ? SIZES[e.aspect] : [sw, sh];
    let panelH = OH; // output pixels covered by the crop box
    if (!e || c.parts || (e.aspect === "source" && (e.zoom || 1) <= 1)) {
      Object.assign(box.style, { left: "0px", top: "0px", width: dw + "px", height: dh + "px", boxShadow: "none", borderColor: "transparent" });
      box.className = "cropbox"; box.dataset.scale = dh / OH;
    } else if (e.aspect !== "source" && e.fit === "blur" && e.layout !== "split") {
      Object.assign(box.style, { left: "0px", top: "0px", width: dw + "px", height: dh + "px", boxShadow: "", borderColor: "" });
      box.className = "cropbox blur"; box.dataset.scale = dw / OW;
    } else {
      const split = e.layout === "split" && e.aspect !== "source";
      if (split) panelH = OH - Math.round(OH * e.split);
      const ratio = OW / panelH, z = Math.max(e.zoom || 1, 1);
      let w, h;
      if (sw / sh > ratio) { h = sh / z; w = h * ratio; } else { w = sw / z; h = w / ratio; }
      const x = Math.min(Math.max(sw * e.crop_x - w / 2, 0), sw - w), y = Math.min(Math.max(sh * e.crop_y - h / 2, 0), sh - h);
      Object.assign(box.style, { left: x * k + "px", top: y * k + "px", width: w * k + "px", height: h * k + "px", boxShadow: "", borderColor: "" });
      box.className = "cropbox"; box.dataset.scale = (h * k) / panelH;
      if (split) { cam.hidden = false; Object.assign(cam.style, { left: e.cam_x * dw + "px", top: e.cam_y * dh + "px", width: e.cam_w * dw + "px", height: e.cam_h * dh + "px" }); }
    }
    const scale = +box.dataset.scale, vertical = OH > OW;
    const fs = Math.min(OW, OH) * (vertical ? 0.078 : 0.06) * scale;
    let mv = CAPTION_POS[e?.caption_pos || "low"][vertical ? 0 : 1] * OH;
    if (e?.layout === "split" && e.caption_pos === "middle") mv = (1 - e.split - 0.07) * OH;
    const cp = $("#cprev");
    cp.style.fontSize = fs * (e?.caption_style === "clean" ? 0.9 : 1) + "px";
    cp.style.bottom = Math.max(mv * scale, 4) + "px";
    cp.style.fontFamily = e?.caption_style === "clean" ? "Arial, Inter, sans-serif" : "";
    cp.style.background = e?.caption_style === "boxed" ? "#0009" : "";
    const tp = $("#tprev"); tp.hidden = !e?.title_text; tp.textContent = e?.title_text || ""; tp.style.fontSize = fs * 0.8 + "px";
    const sp = $("#sprev"); sp.hidden = !e?.sticker_text; sp.textContent = e?.sticker_text || "";
    (cam.hidden ? box : cam).append(sp); // in split layout the top (facecam) panel is the top of the output frame
    if (e?.sticker_text) {
      sp.style.fontSize = fs * 0.55 + "px"; sp.style.background = S.settings?.caption_color || "#FFD400";
      const pos = e.sticker_pos || "top-right";
      Object.assign(sp.style, { top: pos.startsWith("top") ? "5%" : "", bottom: pos.startsWith("bottom") ? "5%" : "",
        left: pos.endsWith("left") ? "4%" : pos.endsWith("center") ? "50%" : "", right: pos.endsWith("right") ? "4%" : "",
        transform: pos.endsWith("center") ? "translateX(-50%)" : "" });
    }
    updateCaption();
  }

  function captionChunks(c) {
    const vertical = SIZES[c.edit.aspect] && SIZES[c.edit.aspect][1] > SIZES[c.edit.aspect][0];
    const max = vertical ? 3 : 6, out = [];
    let cur = [];
    for (const w of E.words.filter((w) => w.s >= c.start - 0.05 && w.e <= c.end + 0.05 && w.w.trim())) {
      if (cur.length && (cur.length >= max || w.s - cur.at(-1).e > 0.6 || /[.?!]$/.test(cur.at(-1).w.trim()))) { out.push(cur); cur = []; }
      cur.push(w);
    }
    if (cur.length) out.push(cur);
    return out;
  }
  function updateCaption() {
    const c = selClip(), el = $("#cprev");
    if (!el) return;
    if (!c || c.parts || !c.edit.captions || !video) return (el.innerHTML = "");
    const t = video.currentTime;
    if (t < c.start || t > c.end) return (el.innerHTML = "");
    const key = c.id + ":" + c.rev;
    if (E._chunks?.key !== key) E._chunks = { key, v: captionChunks(c) };
    const chunk = E._chunks.v.find((ch) => t >= ch[0].s && t <= ch.at(-1).e + 0.15);
    const upper = S.settings?.caption_uppercase !== false, color = S.settings?.caption_color || "#FFD400";
    el.innerHTML = chunk ? chunk.map((w, i) => { const txt = esc(upper ? w.w.trim().toUpperCase() : w.w.trim()); const active = t >= w.s && (i === chunk.length - 1 || t < chunk[i + 1].s); return active ? `<em style="color:${esc(color)}">${txt}</em>` : txt; }).join(" ") : "";
  }

  // ----- playback -----
  function tick() { onTime(); if (!video.paused) raf = requestAnimationFrame(tick); }
  function onTime() {
    if (!video) return;
    const c = selClip(), t = video.currentTime;
    if (E.loop && c && !c.parts && !video.paused && t >= c.end) video.currentTime = c.start;
    $("#tc").textContent = `${fmt(t)} / ${fmt(dur())}`;
    updateCaption();
    draw();
    if (E.tab === "transcript") highlightWord(t);
  }
  const seek = (t) => { if (video) video.currentTime = Math.min(Math.max(t, 0), dur()); };

  // ----- timeline -----
  const RULER = 16, SPR_Y = 18, SPR_H = 44, WAVE_Y = 66, WAVE_H = 40, CLIP_Y = 110, CLIP_H = 14;
  let pps = 1;
  function sizeTimeline() {
    const scroll = $("#tls");
    if (!scroll || !dur()) return;
    const base = scroll.clientWidth / dur();
    pps = Math.min(base * E.zoom, 30000 / dur());
    const w = Math.max(scroll.clientWidth, Math.round(dur() * pps));
    $("#tli").style.width = w + "px";
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr; canvas.height = 128 * dpr; canvas.style.width = w + "px"; canvas.style.height = "128px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }
  function draw() {
    if (!ctx || !dur()) return;
    const w = canvas.width / (window.devicePixelRatio || 1);
    ctx.clearRect(0, 0, w, 128);
    const step = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800].find((s) => s * pps > 70) || 3600;
    ctx.fillStyle = "#5d6178"; ctx.font = "10px JetBrains Mono, monospace";
    for (let t = 0; t <= dur(); t += step) { const x = t * pps; ctx.fillRect(x, 10, 1, 6); ctx.fillText(fmt(t, 0), x + 3, 9); }
    ctx.fillStyle = "#6aa8ff88";
    (E.signals.scenes || []).forEach((t) => ctx.fillRect(t * pps, SPR_Y, 1, SPR_H));
    const wf = E.signals.waveform || [];
    if (wf.length) {
      ctx.fillStyle = "#3b3f56";
      const per = 0.1 * pps, mid = WAVE_Y + WAVE_H / 2, stride = Math.max(1, Math.floor(1 / per));
      for (let i = 0; i < wf.length; i += stride) {
        let m = 0; for (let j = i; j < i + stride && j < wf.length; j++) m = Math.max(m, wf[j]);
        const h = Math.max(1, m * WAVE_H);
        ctx.fillRect(i * per, mid - h / 2, Math.max(1, per * stride - (per > 3 ? 1 : 0)), h);
      }
    }
    ctx.fillStyle = "#0b0c10aa";
    (E.signals.silences || []).forEach(([a, b]) => ctx.fillRect(a * pps, WAVE_Y, (b - a) * pps, WAVE_H));
    for (const c of E.clips) {
      if (c.parts) continue;
      const x = c.start * pps, cw = (c.end - c.start) * pps, sel = c.id === E.sel;
      ctx.fillStyle = sel ? "#ff6a3d" : c.source === "manual" ? "#6aa8ff66" : "#ffc24755";
      ctx.beginPath(); ctx.roundRect(x, CLIP_Y, Math.max(cw, 2), CLIP_H, 4); ctx.fill();
      if (cw > 50) { ctx.fillStyle = sel ? "#1b0b04" : "#eceef6cc"; ctx.font = "600 10px Inter, sans-serif"; ctx.fillText(c.title.slice(0, Math.floor(cw / 6)), x + 5, CLIP_Y + 10.5); }
    }
    const c = selClip();
    if (c && !c.parts) {
      const x = c.start * pps, cw = (c.end - c.start) * pps;
      ctx.fillStyle = "#ff6a3d14"; ctx.fillRect(x, RULER, cw, 128 - RULER);
      ctx.fillStyle = "#ff6a3d"; ctx.fillRect(x - 1.5, RULER, 3, 128 - RULER); ctx.fillRect(x + cw - 1.5, RULER, 3, 128 - RULER);
      ctx.fillRect(x - 5, RULER + 30, 10, 18); ctx.fillRect(x + cw - 5, RULER + 30, 10, 18);
    }
    ctx.fillStyle = "#37d39a";
    if (E.inMark != null) ctx.fillRect(E.inMark * pps, RULER, 2, 128 - RULER);
    if (E.outMark != null) ctx.fillRect(E.outMark * pps - 2, RULER, 2, 128 - RULER);
    if (E.inMark != null && E.outMark != null) { ctx.fillStyle = "#37d39a1f"; ctx.fillRect(E.inMark * pps, RULER, (E.outMark - E.inMark) * pps, 128 - RULER); }
    if (video) { const x = video.currentTime * pps; ctx.fillStyle = "#fff"; ctx.fillRect(x - 1, 0, 2, 128); ctx.beginPath(); ctx.moveTo(x - 6, 0); ctx.lineTo(x + 6, 0); ctx.lineTo(x, 8); ctx.fill(); }
  }
  function bindTimeline() {
    const opts = { signal: listeners.signal };
    const inner = $("#tli");
    let drag = null;
    const tAt = (ev) => (ev.clientX - inner.getBoundingClientRect().left) / pps;
    inner.addEventListener("mousedown", (ev) => {
      const t = tAt(ev), y = ev.offsetY, c = selClip();
      if (c && !c.parts && Math.abs(t - c.start) * pps < 8) drag = { edge: "start" };
      else if (c && !c.parts && Math.abs(t - c.end) * pps < 8) drag = { edge: "end" };
      else if (y >= CLIP_Y - 2) {
        const hit = E.clips.find((k) => !k.parts && t >= k.start && t <= k.end);
        if (hit) { select(hit.id); return; }
        drag = { edge: "seek" }; seek(t);
      } else { drag = { edge: "seek" }; seek(t); }
    });
    window.addEventListener("mousemove", (ev) => {
      if (!drag) return;
      const t = Math.min(Math.max(tAt(ev), 0), dur()), c = selClip();
      if (drag.edge === "seek") return seek(t);
      if (drag.edge === "start") c.start = Math.min(t, c.end - 0.5);
      else c.end = Math.max(t, c.start + 0.5);
      seek(drag.edge === "start" ? c.start : c.end);
      draw();
    }, opts);
    window.addEventListener("mouseup", () => {
      if (drag && drag.edge !== "seek") { const c = selClip(); patchClip(c.id, { start: +c.start.toFixed(2), end: +c.end.toFixed(2) }); if (E.tab === "edit") renderTab(); }
      drag = null;
    }, opts);
    inner.addEventListener("mousemove", (ev) => {
      const t = tAt(ev), c = selClip();
      inner.style.cursor = c && !c.parts && (Math.abs(t - c.start) * pps < 8 || Math.abs(t - c.end) * pps < 8) ? "ew-resize" : ev.offsetY >= CLIP_Y - 2 ? "pointer" : "text";
    });
  }

  // ----- selection & editing -----
  function select(id, { play = false } = {}) {
    E.sel = id;
    const c = selClip();
    if (c && !c.parts) seek(c.start);
    applyPreview(); draw();
    if (E.tab === "edit" || E.tab === "clips") renderTab();
    if (play && video) { E.loop = true; video.play(); }
  }

  function patchClip(id, body, delay = 0) {
    const c = E.clips.find((k) => k.id === id);
    if (!c) return;
    if (body.edit) c.edit = { ...c.edit, ...body.edit };
    for (const k of ["start", "end", "title", "rating"]) if (k in body) c[k] = body[k];
    const p = (E.pending[id] ||= { body: {}, local: {} });
    p.body = { ...p.body, ...body, ...(body.edit ? { edit: { ...(p.body.edit || {}), ...body.edit } } : {}) };
    p.local = { edit: c.edit, start: c.start, end: c.end, title: c.title, rating: c.rating };
    clearTimeout(p.timer);
    applyPreview(); draw();
    p.timer = setTimeout(async () => {
      const send = p.body; delete E.pending[id];
      try { const fresh = await api(`/api/clips/${id}`, { method: "PATCH", body: send }); Object.assign(E.clips.find((k) => k.id === id) || {}, fresh); if (E.tab === "edit") renderActions(); }
      catch (e) { fail(e); }
    }, delay);
  }
  const replaceClip = (fresh) => { const c = E.clips.find((k) => k.id === fresh.id); if (c) Object.assign(c, fresh); applyPreview(); draw(); };

  async function onClick(ev) {
    const a = ev.target.closest("[data-act]");
    if (!a) return;
    const act = a.dataset.act, c = selClip();
    const busy = (label) => { a.disabled = true; a.innerHTML = `<span class="spin"></span>${label ? " " + label : ""}`; };
    try {
      switch (act) {
        case "play": if (video) { E.loop = false; video.paused ? video.play() : video.pause(); } break;
        case "playclip": if (c && video && !c.parts) { E.loop = true; seek(c.start); video.play(); } break;
        case "in": E.inMark = video.currentTime; draw(); break;
        case "out": E.outMark = video.currentTime; draw(); break;
        case "addclip": {
          const [s, e] = [E.inMark, E.outMark];
          if (s == null || e == null || Math.abs(e - s) < 0.5) return toast("Set an In and Out mark first (keys I and O), at least 0.5 s apart.");
          const nc = await api(`/api/projects/${pid}/clips`, { body: { start: Math.min(s, e), end: Math.max(s, e), title: "New clip" } });
          E.clips.push(nc); E.inMark = E.outMark = null; E.tab = "edit"; markTab(); select(nc.id); break;
        }
        case "reanalyze":
          if (await confirmBox("Re-analyze this video?", "Transcription and AI clip finding run again. AI-found clips are replaced; clips you made yourself are kept.", "Re-analyze")) {
            await api(`/api/projects/${pid}/analyze`, { method: "POST" }); E.proj.status = "queued"; E.proj.stage = "Queued"; updateOverlay();
          }
          break;
        case "autopilot": openAutopilot(); break;
        case "viral-video": await api(`/api/projects/${pid}/virality`, { body: {} }); toast("Analyzing virality against similar videos — the report appears in the Virality Lab and here when done.", "ok", 6000); break;
        case "viral-clip": await api(`/api/clips/${c.id}/virality`, { body: {} }); toast("Scoring this clip… the score appears on the clip when done.", "ok"); break;
        case "report": openReport(a.dataset.id).catch(fail); break;
        case "find": {
          const q = $("#q").value.trim();
          busy();
          await api(`/api/projects/${pid}/find`, { body: { query: q } });
          toast(q ? `Searching for “${q}”… new clips appear here.` : "Finding more clips…");
          setTimeout(() => { a.disabled = false; a.textContent = "Find"; }, 2500);
          break;
        }
        case "select": if (ev.target.closest(".pick, .rate, [data-act=edit], [data-act=del], a")) return; select(a.dataset.id); break;
        case "edit": E.tab = "edit"; markTab(); select(a.dataset.id); break;
        case "rate": { const k = E.clips.find((x) => x.id === a.dataset.id); patchClip(k.id, { rating: +a.dataset.v === k.rating ? 0 : +a.dataset.v }); renderTab(); break; }
        case "del":
          if (await confirmBox("Delete clip?", `“${a.dataset.title}” will be removed.`)) {
            await api(`/api/clips/${a.dataset.id}`, { method: "DELETE" });
            E.clips = E.clips.filter((k) => k.id !== a.dataset.id); E.picked.delete(a.dataset.id);
            if (E.sel === a.dataset.id) E.sel = E.clips[0]?.id || null;
            renderTab(); draw(); applyPreview();
          }
          break;
        case "montage": {
          const ids = E.clips.filter((k) => E.picked.has(k.id)).sort((x, y) => x.start - y.start).map((k) => k.id);
          const m = await api(`/api/projects/${pid}/montage`, { body: { clip_ids: ids, title: `Best of ${E.proj.name}`.slice(0, 90) } });
          E.clips.push(m); E.picked.clear(); E.tab = "edit"; markTab(); select(m.id); toast("Montage created — render or publish it like any clip.", "ok"); break;
        }
        case "clipsel": {
          const r = E.tsel; if (!r) return;
          const nc = await api(`/api/projects/${pid}/clips`, { body: { start: r.s, end: r.e, title: r.text.slice(0, 60) } });
          E.clips.push(nc); E.tab = "edit"; markTab(); select(nc.id); break;
        }
        case "set": { const { k, v } = a.dataset; patchClip(c.id, { edit: { [k]: v === "" || isNaN(+v) ? v : +v } }, 300); renderTab(); break; }
        case "preset": patchClip(c.id, { edit: PRESETS[a.dataset.v] }, 300); renderTab(); break;
        case "hook": patchClip(c.id, { edit: { title_text: c.hook.slice(0, 60) } }, 200); renderTab(); break;
        case "nudge": { const [k, d] = a.dataset.v.split(","); const v = Math.min(Math.max(c[k] + +d, 0), dur()); patchClip(c.id, { [k]: +v.toFixed(2) }, 500); seek(v); renderTab(); break; }
        case "toplay": { const k = a.dataset.v; patchClip(c.id, { [k]: +video.currentTime.toFixed(2) }, 300); renderTab(); break; }
        case "reframe": busy("Finding faces…"); try { replaceClip(await api(`/api/clips/${c.id}/reframe`, { method: "POST" })); toast("Framed on the speaker.", "ok"); } finally { renderTab(); } break;
        case "facecam": busy("Detecting facecam…"); try { replaceClip(await api(`/api/clips/${c.id}/facecam`, { method: "POST" })); toast("Facecam found — split-screen layout applied.", "ok"); } finally { renderTab(); } break;
        case "music": $("#musicf").click(); break;
        case "nomusic": patchClip(c.id, { edit: { music: "" } }); renderTab(); break;
        case "ask": {
          const txt = $("#askq").value.trim(); if (!txt) return;
          busy();
          try { const r = await api(`/api/clips/${c.id}/assist`, { body: { instruction: txt } }); replaceClip(r.clip); toast(r.explanation || "Done.", "ok"); }
          finally { renderTab(); }
          break;
        }
        case "render": await api(`/api/clips/${c.id}/export`, { method: "POST" }); c.export = { status: "rendering", progress: 0, rev: c.rev }; renderActions(); break;
        case "publish": openPublish(c); break;
      }
    } catch (e) { fail(e); }
  }
  const markTab = () => $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === E.tab));

  function onKey(e) {
    if (e.target.closest("input, textarea, select") || !video || $("#modal-root").innerHTML) return;
    const c = selClip();
    if (e.code === "Space") { e.preventDefault(); E.loop = false; video.paused ? video.play() : video.pause(); }
    else if (e.key === "i" || e.key === "I") { E.inMark = video.currentTime; draw(); }
    else if (e.key === "o" || e.key === "O") { E.outMark = video.currentTime; draw(); }
    else if (e.key === "ArrowLeft") seek(video.currentTime - (e.shiftKey ? 1 : 1 / 30));
    else if (e.key === "ArrowRight") seek(video.currentTime + (e.shiftKey ? 1 : 1 / 30));
    else if (e.key === "[" && c) seek(c.start);
    else if (e.key === "]" && c) seek(c.end);
  }

  // ----- right panel -----
  function renderTab() {
    const el = $("#tab");
    if (!el) return;
    el.oninput = el.onchange = el.onkeydown = el.onmouseup = el.onclick = null;
    if (E.tab === "clips") return renderClips(el);
    if (E.tab === "transcript") return renderTranscript(el);
    return renderInspector(el);
  }

  function renderClips(el) {
    const ready = E.proj.status === "ready";
    const sorted = [...E.clips].sort((a, b) => (a.source === "manual") - (b.source === "manual") || b.score - a.score);
    const srcLabel = { search: "search", manual: "manual", ai: "AI", auto: "auto", montage: "montage" };
    el.innerHTML = `
      <div class="search"><input type="text" id="q" placeholder="Find a moment: “when chat goes crazy”" ${ready ? "" : "disabled"}><button class="btn primary" data-act="find" ${ready ? "" : "disabled"}>Find</button></div>
      ${!S.settings?.anthropic_key_set ? `<div class="card" style="padding:10px 12px;margin-bottom:12px;font-size:12.5px">Clips below come from the offline heuristic. <a href="#/settings">Add a Claude API key</a> for AI-ranked clips, semantic search, the edit assistant, virality scoring and auto-written titles.</div>` : ""}
      <div class="row" style="margin-bottom:10px;font-size:12px" ${E.picked.size ? "" : "hidden"} id="pickbar"><span class="grow">${E.picked.size} selected</span><button class="btn sm primary" data-act="montage" ${E.picked.size > 1 ? "" : "disabled"}>▤ Make montage</button></div>
      ${sorted.length ? sorted.map((c) => `
        <div class="clip ${c.id === E.sel ? "sel" : ""}" data-act="select" data-id="${c.id}">
          <div class="row"><input type="checkbox" class="pick" data-id="${c.id}" title="Select for a montage" ${E.picked.has(c.id) ? "checked" : ""} ${c.parts ? "disabled" : ""}>
            <div class="t grow">${esc(c.title)}</div>${c.viral ? `<span class="vbadge" title="Virality score">✦${+c.viral.score}</span>` : ""}${c.source !== "manual" && !c.parts ? `<span class="score" title="AI pick score">${+c.score}</span>` : ""}</div>
          ${c.reason ? `<div class="why">${esc(c.reason)}</div>` : ""}
          <div class="meta wrap"><span class="mono">${c.parts ? `${c.parts.length} clips` : `${fmt(c.start)} → ${fmt(c.end)}`}</span><span>${(c.end - c.start).toFixed(1)}s</span>
            <span class="src-tag">${srcLabel[c.source] || esc(c.source)}</span><span class="grow"></span>
            ${Object.entries(c.posts || {}).map(([p, r]) => (/^https:\/\//.test(r.url || "") ? `<a href="${esc(r.url)}" target="_blank" class="plat ${p}">${PLATFORM[p]}</a>` : platTag(p))).join("")}
            ${c.export?.status === "done" && c.export.rev === c.rev ? '<span title="Rendered" style="color:var(--ok)">●</span>' : ""}
            ${c.fit != null ? `<span class="chip" title="How well this matches what you've liked and posted before (learned from your feedback). AI score ${+c.ai_score}." style="font-size:11px">♥ ${+c.fit}% your taste</span>` : ""}
            <span class="rate"><button data-act="rate" data-id="${c.id}" data-v="1" class="${c.rating === 1 ? "on" : ""}" title="Good pick (trains your style profile)">👍</button><button data-act="rate" data-id="${c.id}" data-v="-1" class="${c.rating === -1 ? "on" : ""}" title="Bad pick">👎</button></span>
            <button class="btn xs" data-act="edit" data-id="${c.id}">Edit</button>
            <button class="btn xs ghost danger" data-act="del" data-id="${c.id}" data-title="${esc(c.title)}">✕</button></div>
        </div>`).join("") : `<div class="empty"><b>${ready ? "No clips yet" : "Clips appear after analysis"}</b>${ready ? "Search for a moment above, or mark In/Out on the timeline." : ""}</div>`}`;
    el.onkeydown = (e) => { if (e.key === "Enter" && e.target.id === "q") $("[data-act=find]").click(); };
    el.onchange = (e) => {
      if (!e.target.classList.contains("pick")) return;
      e.target.checked ? E.picked.add(e.target.dataset.id) : E.picked.delete(e.target.dataset.id);
      const pb = $("#pickbar"); pb.hidden = !E.picked.size; $("span", pb).textContent = `${E.picked.size} selected`; $("button", pb).disabled = E.picked.size < 2;
    };
  }

  function renderTranscript(el) {
    if (!E.segs.length) { el.innerHTML = `<div class="empty"><b>No transcript</b>${E.proj.status === "ready" ? "No speech was detected in this video." : "It appears here once transcription finishes."}</div>`; return; }
    const c = selClip();
    el.innerHTML = `<div class="row" style="margin-bottom:10px"><span class="muted grow" style="font-size:12px">Select words to make a clip · click a word to jump</span>
        <button class="btn sm primary" data-act="clipsel" id="clipsel" disabled>＋ Clip selection</button></div>
      <div class="transcript">${E.segs.map((s) => `<p><span class="ts" data-t="${s.start}">${fmt(s.start, 0)}</span>${(s.words?.length ? s.words : [{ s: s.start, e: s.end, w: s.text }]).map((w) =>
        `<span class="w ${c && !c.parts && w.s >= c.start && w.e <= c.end ? "in" : ""}" data-s="${w.s}" data-e="${w.e}">${esc(w.w)}</span>`).join("")}</p>`).join("")}</div>`;
    el.onclick = (e) => { const w = e.target.closest("[data-s],[data-t]"); if (w && !getSelection().toString()) seek(+(w.dataset.s ?? w.dataset.t)); };
    el.onmouseup = () => {
      const sel = getSelection();
      const spans = $$(".w", el).filter((s) => sel.containsNode(s, true));
      E.tsel = spans.length > 1 ? { s: +spans[0].dataset.s, e: +spans.at(-1).dataset.e, text: spans.map((s) => s.textContent).join("").trim() } : null;
      $("#clipsel").disabled = !E.tsel;
      if (E.tsel) $("#clipsel").textContent = `＋ Clip selection (${(E.tsel.e - E.tsel.s).toFixed(1)}s)`;
    };
  }
  let lastWord = null;
  function highlightWord(t) {
    const el = $("#tab"); if (!el) return;
    const w = $$(".w", el).find((s) => t >= +s.dataset.s && t <= +s.dataset.e);
    if (w === lastWord) return;
    lastWord?.classList.remove("now"); w?.classList.add("now"); lastWord = w;
    if (w && !video.paused) w.scrollIntoView({ block: "nearest" });
  }

  const PRESETS = {
    natural: { brightness: 0, contrast: 1, saturation: 1, gamma: 1 },
    vivid: { brightness: 0.02, contrast: 1.12, saturation: 1.35, gamma: 1 },
    cinematic: { brightness: -0.03, contrast: 1.2, saturation: 0.85, gamma: 0.95 },
    bright: { brightness: 0.08, contrast: 1.05, saturation: 1.1, gamma: 1.1 },
    mono: { brightness: 0, contrast: 1.15, saturation: 0, gamma: 1 },
  };
  const slider = (key, label, min, max, step, v, unit = "") => `<label class="slider"><span>${label}</span><input type="range" data-k="${key}" min="${min}" max="${max}" step="${step}" value="${v}"><output>${(+v).toFixed(step < 0.1 ? 2 : 1)}${unit}</output></label>`;
  const seg = (k, cur, opts) => `<div class="seg">${opts.map(([v, l]) => `<button class="${String(cur) === String(v) ? "on" : ""}" data-act="set" data-k="${k}" data-v="${v}">${l}</button>`).join("")}</div>`;

  function renderInspector(el) {
    const c = selClip();
    if (!c) { el.innerHTML = `<div class="empty"><b>No clip selected</b>Pick a clip from AI Clips, select transcript words, or mark In/Out on the timeline.</div>`; return; }
    const e = c.edit;
    if (c.parts) {
      el.innerHTML = `<div class="sec"><input type="text" id="ctitle" value="${esc(c.title)}" style="font-weight:600">
        <div class="muted" style="font-size:12.5px">Montage of ${c.parts.length} clips (≈${(c.end - c.start).toFixed(1)}s). Each part keeps its own edits; parts are rendered and joined in order.</div>
        <ol class="list">${c.parts.map((id) => { const p = E.clips.find((k) => k.id === id); return `<li>${p ? esc(p.title) : "<span class='dim'>deleted clip</span>"}</li>`; }).join("")}</ol></div>
        <div class="actions" id="actions"></div>`;
      el.onchange = (ev) => { if (ev.target.id === "ctitle") patchClip(c.id, { title: ev.target.value }); };
      return renderActions();
    }
    const reframed = e.aspect !== "source";
    el.innerHTML = `
      <div class="sec">
        <input type="text" id="ctitle" value="${esc(c.title)}" style="font-weight:600">
        ${c.hook ? `<div class="row" style="font-size:12px"><span class="muted grow">Hook: “${esc(c.hook)}”</span><button class="btn xs" data-act="hook" title="Use the AI hook as an on-screen title">Use as title</button></div>` : ""}
        <div class="row"><button class="btn xs" data-act="viral-clip">✦ Score virality</button>${c.viral ? `<button class="btn xs" data-act="report" data-id="${c.viral.report_id}"><span class="vbadge">✦ ${+c.viral.score}</span> view report</button>` : ""}</div>
      </div>
      <div class="sec"><h3>Timing</h3>
        <div class="time-in">
          ${["start", "end"].map((k) => `<label class="field">${k === "start" ? "Start" : "End"}<input type="text" class="mono" data-time="${k}" value="${fmt(c[k], 2)}">
            <div class="nudge"><button class="btn xs" data-act="nudge" data-v="${k},-1">-1s</button><button class="btn xs" data-act="nudge" data-v="${k},-0.1">-.1</button><button class="btn xs" data-act="nudge" data-v="${k},0.1">+.1</button><button class="btn xs" data-act="nudge" data-v="${k},1">+1s</button><button class="btn xs" data-act="toplay" data-v="${k}" title="Set to playhead">⇤▶</button></div></label>`).join("")}
        </div>
        <label class="switch"><input type="checkbox" data-k="remove_silence" ${e.remove_silence ? "checked" : ""}> AI jump cuts — remove silences &amp; dead air</label>
        <label class="switch"><input type="checkbox" data-k="remove_fillers" ${e.remove_fillers ? "checked" : ""}> Cut filler words (“um”, “uh”)</label>
        <div class="muted" style="font-size:12px">Length ${(c.end - c.start).toFixed(1)}s · output ≈ ${((c.end - c.start) / e.speed).toFixed(1)}s${e.remove_silence ? " before jump cuts" : ""}</div>
      </div>
      <div class="sec"><h3>Frame &amp; layout</h3>
        ${seg("aspect", e.aspect, [["9:16", "9:16"], ["1:1", "1:1"], ["4:5", "4:5"], ["16:9", "16:9"], ["source", "Original"]])}
        ${reframed ? `${seg("layout", e.layout, [["single", "Single view"], ["split", "Split · facecam + gameplay"]])}
          ${e.layout === "split" ? `<div class="row wrap"><button class="btn sm" data-act="facecam">◎ Detect facecam</button><span class="muted" style="font-size:12px">Blue = facecam (top panel); orange = main view (bottom).</span></div>
            ${slider("cam_x", "Cam ↔", 0, 1, 0.01, e.cam_x)}${slider("cam_y", "Cam ↕", 0, 1, 0.01, e.cam_y)}${slider("cam_w", "Cam width", 0.05, 1, 0.01, e.cam_w)}${slider("cam_h", "Cam height", 0.05, 1, 0.01, e.cam_h)}${slider("split", "Cam share", 0.2, 0.6, 0.01, e.split)}`
          : `<div class="row wrap">${seg("fit", e.fit, [["crop", "Crop to fill"], ["blur", "Fit + blur"]])}<button class="btn sm" data-act="reframe" ${e.fit === "blur" ? "disabled" : ""}>◎ Auto-reframe on face</button></div>`}` : ""}
        ${!reframed || e.fit !== "blur" || e.layout === "split" ? slider("crop_x", "Framing ↔", 0, 1, 0.01, e.crop_x) + slider("crop_y", "Framing ↕", 0, 1, 0.01, e.crop_y) + slider("zoom", "Zoom", 1, 2.5, 0.05, e.zoom, "×") : ""}
      </div>
      <div class="sec"><h3>Captions &amp; text</h3>
        <label class="switch"><input type="checkbox" data-k="captions" ${e.captions ? "checked" : ""}> Animated captions (word-by-word highlight)</label>
        ${seg("caption_style", e.caption_style, [["bold", "Bold"], ["clean", "Clean"], ["boxed", "Boxed"], ["neon", "Neon"]])}
        ${seg("caption_pos", e.caption_pos, [["low", "Low"], ["middle", e.layout === "split" ? "At seam" : "Middle"], ["high", "High"]])}
        <label class="field">Title card (top of frame)<input type="text" data-k="title_text" value="${esc(e.title_text)}" placeholder="Optional headline overlay"></label>
        <div class="two"><label class="field">Sticker / handle<input type="text" data-k="sticker_text" value="${esc(e.sticker_text)}" placeholder="@yourchannel"></label>
          <label class="field">Sticker spot<select data-k="sticker_pos">${["top-left", "top-center", "top-right", "bottom-left", "bottom-right"].map((p) => `<option ${p === e.sticker_pos ? "selected" : ""}>${p}</option>`).join("")}</select></label></div>
      </div>
      <div class="sec"><h3>Look</h3>
        <div class="presets">${Object.keys(PRESETS).map((p) => `<button class="btn xs" data-act="preset" data-v="${p}">${p[0].toUpperCase() + p.slice(1)}</button>`).join("")}</div>
        ${slider("brightness", "Brightness", -0.3, 0.3, 0.01, e.brightness)}${slider("contrast", "Contrast", 0.6, 1.6, 0.01, e.contrast)}
        ${slider("saturation", "Saturation", 0, 2, 0.01, e.saturation)}${slider("gamma", "Gamma", 0.6, 1.6, 0.01, e.gamma)}
        <label class="switch"><input type="checkbox" data-k="sharpen" ${e.sharpen ? "checked" : ""}> Sharpen</label>
      </div>
      <div class="sec"><h3>Audio</h3>
        ${slider("volume", "Voice volume", 0, 3, 0.05, e.volume, "×")}
        <label class="switch"><input type="checkbox" data-k="loudnorm" ${e.loudnorm ? "checked" : ""}> Normalize loudness (−14 LUFS)</label>
        <label class="switch"><input type="checkbox" data-k="denoise" ${e.denoise ? "checked" : ""}> Reduce background noise</label>
        <div class="row wrap"><button class="btn sm" data-act="music">♫ ${e.music ? "Replace music" : "Add background music"}</button>${e.music ? `<span class="muted" style="font-size:12px">${esc(e.music.split("/").pop())}</span><button class="btn xs ghost" data-act="nomusic">Remove</button>` : ""}
          <input type="file" id="musicf" accept="audio/*" hidden></div>
        ${e.music ? slider("music_volume", "Music level", 0, 1, 0.01, e.music_volume) : ""}
      </div>
      <div class="sec"><h3>Motion</h3>
        ${slider("speed", "Speed", 0.5, 2, 0.05, e.speed, "×")}${slider("fade_in", "Fade in", 0, 3, 0.1, e.fade_in, "s")}${slider("fade_out", "Fade out", 0, 3, 0.1, e.fade_out, "s")}
      </div>
      <div class="sec"><h3>Ask AI</h3>
        <div class="ask"><textarea id="askq" placeholder="“Make it vertical and punchier, cut the first sentence, warmer colors”"></textarea>
          <div class="row"><span class="hint grow">Claude edits timing, framing, look, audio and text for you. <span class="kbd">Ctrl</span>+<span class="kbd">Enter</span></span><button class="btn sm primary" data-act="ask">Apply</button></div></div>
      </div>
      <div class="actions" id="actions"></div>`;
    el.oninput = (ev) => {
      const t = ev.target;
      if (t.type === "range") { t.nextElementSibling.textContent = (+t.value).toFixed(+t.step < 0.1 ? 2 : 1) + (t.nextElementSibling.textContent.match(/[×s]$/)?.[0] || ""); patchClip(c.id, { edit: { [t.dataset.k]: +t.value } }, 450); }
      else if (["title_text", "sticker_text"].includes(t.dataset.k)) patchClip(c.id, { edit: { [t.dataset.k]: t.value } }, 600);
    };
    el.onchange = async (ev) => {
      const t = ev.target;
      if (t.type === "checkbox" && t.dataset.k) { patchClip(c.id, { edit: { [t.dataset.k]: t.checked } }, 200); if (t.dataset.k === "remove_silence") renderTab(); }
      if (t.tagName === "SELECT" && t.dataset.k) patchClip(c.id, { edit: { [t.dataset.k]: t.value } }, 200);
      if (t.id === "ctitle") patchClip(c.id, { title: t.value });
      if (t.id === "musicf" && t.files[0]) {
        const fd = new FormData(); fd.append("file", t.files[0]);
        try { replaceClip(await api(`/api/clips/${c.id}/music`, { body: fd })); toast("Music added.", "ok"); renderTab(); } catch (err) { fail(err); }
      }
      if (t.dataset.time) {
        const v = parseTime(t.value);
        if (!isFinite(v)) return toast("Use a time like 1:23.4");
        patchClip(c.id, { [t.dataset.time]: +v.toFixed(2) }); seek(v); renderTab();
      }
    };
    $("#askq").onkeydown = (ev) => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) $("[data-act=ask]").click(); };
    renderActions();
  }

  function renderActions() {
    const el = $("#actions"), c = selClip();
    if (!el || !c) return;
    const ex = c.export || {}, current = ex.status === "done" && ex.rev === c.rev;
    el.innerHTML = ex.status === "rendering"
      ? `<div class="grow stack" style="gap:6px"><div class="row" style="font-size:12px"><span class="spin"></span> Rendering… ${Math.round((ex.progress || 0) * 100)}%</div>${bar(ex.progress)}</div>`
      : `<button class="btn" data-act="render">${current ? "↻ Re-render" : "⬇ Render"}</button>
         ${current ? `<a class="btn" href="/files/${pid}/${ex.file}" download="${esc(c.title.replace(/[\\/:*?"<>|]/g, "_"))}.mp4">Download</a>` : ""}
         ${current && ex.cover ? `<a href="/files/${pid}/${ex.cover}?r=${c.rev}" target="_blank" title="Auto-picked cover (YouTube thumbnail / TikTok cover) at ${(+ex.cover_t).toFixed(1)}s"><img src="/files/${pid}/${ex.cover}?r=${c.rev}" alt="Cover" style="height:36px;border-radius:5px;border:1px solid var(--line-2);vertical-align:middle"></a>` : ""}
         ${ex.status === "failed" ? `<span style="color:var(--err);font-size:12px" title="${esc(ex.error)}">Render failed</span>` : ""}
         <div class="grow"></div><button class="btn primary" data-act="publish">⇪ Publish</button>`;
  }

  function openAutopilot() {
    const ap = { ...apLoad(), ...(E.proj.autopilot || {}) };
    ap.platforms = ap.platforms || ["youtube"];
    if (ap.first_publish && /Z|\+00:00$/.test(ap.first_publish)) ap.first_publish = toLocalInput(new Date(ap.first_publish));
    modal(`<h2>⚡ Autopilot this video</h2><p class="muted">Render the top clips and schedule them to your channels in one go.</p>${apForm(ap, false)}
      <div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="ap-go">Schedule clips</button></div>`, (m, close) => {
      $("#ap-go", m).onclick = async () => {
        const body = apRead(m);
        if (!body.platforms.length) return toast("Pick at least one platform.");
        try { await api(`/api/projects/${pid}/autopilot/run`, { body }); close(); toast("Clips are rendering and will be scheduled — follow along on the Publish page.", "ok", 6000); }
        catch (e) { fail(e); }
      };
    });
  }

  function openPublish(c) {
    const vertical = ["9:16", "4:5"].includes(c.edit.aspect), conn = S.conn || {};
    const tags = (c.hashtags || []).map((h) => h.replace(/^#/, ""));
    modal(`<h2>Publish</h2>
      <div class="stack">
        <div class="plats">${Object.entries(PLATFORM).map(([k, v]) => `<label title="${conn[k]?.connected ? "Connected" : "Not connected yet — it posts once you connect"}"><input type="checkbox" class="pp" value="${k}" ${k === "youtube" || conn[k]?.connected ? "checked" : ""}> ${v} ${conn[k]?.connected ? '<span style="color:var(--ok)">●</span>' : '<span class="dim">○</span>'}</label>`).join("")}</div>
        <div class="row"><label class="field grow">Title (YouTube)<input type="text" id="pt" maxlength="100" value="${esc(c.title)}"></label>
          <button class="btn" id="pai" style="align-self:flex-end" ${S.settings?.anthropic_key_set ? "" : "disabled title='Add a Claude key in Settings'"}>✨ Write with AI</button></div>
        <label class="field">Description (YouTube)<textarea id="pd" rows="4">${esc(c.hook || "")}${vertical ? "\n\n#Shorts" : ""}</textarea></label>
        <label class="field">Caption (TikTok &amp; Instagram)<textarea id="pc" rows="2">${esc(c.title)} ${esc(tags.slice(0, 5).map((t) => "#" + t.replace(/\s+/g, "")).join(" "))}</textarea></label>
        <label class="field">Tags (comma separated)<input type="text" id="ptags" value="${esc(tags.join(", "))}"></label>
        <div class="row wrap"><label class="field grow">Category (YouTube)<select id="pcat">${Object.entries(CATEGORIES).map(([k, v]) => `<option value="${k}" ${k === "22" ? "selected" : ""}>${v}</option>`).join("")}</select></label>
          <label class="field grow">Visibility<select id="ppriv">${["public", "unlisted", "private"].map((p) => `<option ${p === (S.settings?.default_privacy || "private") ? "selected" : ""}>${p}</option>`).join("")}</select></label></div>
        <div><h3 style="margin-bottom:8px">When</h3><div class="seg" id="pwhen">
          <button class="on" data-v="now">Post now</button><button data-v="publish_at">Schedule</button><button data-v="upload_at">Hold &amp; post later</button></div></div>
        <label class="field" id="ptime-l" hidden>Date &amp; time<input type="datetime-local" id="ptime" value="${tomorrowAt9()}"></label>
        <p class="muted" id="pwhen-help" style="font-size:12px;margin:0"></p>
        <label class="switch"><input type="checkbox" id="pkids"> Made for kids (YouTube)</label>
      </div>
      <div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="pgo">Queue post</button></div>`, (m, close) => {
      let mode = "now";
      const help = { now: "Posts as soon as the render is ready.", publish_at: "YouTube: uploads now as private and goes public at this time. TikTok/Instagram: posted at this time (Reelwright must be running).", upload_at: "Everything stays on this computer and is posted at this time (Reelwright must be running)." };
      const setMode = (v) => { mode = v; $$("#pwhen button", m).forEach((b) => b.classList.toggle("on", b.dataset.v === v)); $("#ptime-l", m).hidden = v === "now"; $("#pwhen-help", m).textContent = help[v]; };
      setMode("now");
      $("#pwhen", m).onclick = (e) => e.target.dataset.v && setMode(e.target.dataset.v);
      $("#pai", m).onclick = async (e) => {
        const b = e.currentTarget; b.disabled = true; b.innerHTML = `<span class="spin"></span> Writing…`;
        try {
          const md = await api(`/api/clips/${c.id}/metadata`, { method: "POST" });
          $("#pt", m).value = md.title; $("#pd", m).value = md.description; $("#ptags", m).value = md.tags.join(", "); $("#pc", m).value = md.social_caption || md.title;
          if (CATEGORIES[md.category_id]) $("#pcat", m).value = md.category_id;
        } catch (err) { fail(err); }
        b.disabled = false; b.textContent = "✨ Write with AI";
      };
      $("#pgo", m).onclick = async () => {
        const t = fromLocalInput($("#ptime", m).value), plats = $$(".pp", m).filter((x) => x.checked).map((x) => x.value);
        if (!plats.length) return toast("Pick at least one platform.");
        if (mode !== "now" && (!t || new Date(t) < new Date())) return toast("Pick a time in the future.");
        const body = {
          platforms: plats, title: $("#pt", m).value.trim(), description: $("#pd", m).value, caption: $("#pc", m).value.trim(),
          tags: $("#ptags", m).value.split(",").map((s) => s.trim()).filter(Boolean),
          category_id: $("#pcat", m).value, privacy: $("#ppriv", m).value, made_for_kids: $("#pkids", m).checked,
          publish_at: mode === "publish_at" ? t : null, upload_at: mode === "upload_at" ? t : null,
        };
        if (!body.title) return toast("Add a title.");
        try { await api(`/api/clips/${c.id}/upload`, { body }); close(); toast(mode === "now" ? `Queued for ${plats.map((p) => PLATFORM[p]).join(", ")} — posts when the render finishes.` : `Scheduled for ${when(t)}.`, "ok"); renderActions(); }
        catch (err) { fail(err); }
      };
    });
  }
}

// ======================================================================
// Channels (streaming sources)
// ======================================================================
function Sources() {
  const SRC = { twitch: "Twitch", kick: "Kick", youtube: "YouTube (live streams)", youtube_videos: "YouTube (videos)", url: "Other (any channel/playlist link)" };
  const open = new Set();
  this.every = 3000;
  this.mount = async () => {
    view.innerHTML = `<div class="page"><h1>Channels</h1><p class="sub">Connect the streaming channels you clip from. Reelwright lists their VODs, imports the ones you pick, and can watch for new streams and process them automatically.</p>
      <div class="card" style="margin-bottom:20px"><h2>Add a channel</h2>
        <form id="sf" class="stack">
          <div class="row wrap"><label class="field" style="min-width:200px">Platform<select id="sp">${Object.entries(SRC).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
            <label class="field grow" style="min-width:220px">Channel name or link<input type="text" id="sh" placeholder="e.g. shroud" required></label></div>
          <div class="row wrap"><label class="switch"><input type="checkbox" id="sa"> Auto-import new streams</label>
            <label class="field" style="flex-direction:row;align-items:center;gap:8px">every <input type="number" id="si" value="2" min="0.5" step="0.5" style="width:70px"> hours</label>
            <label class="field" style="flex-direction:row;align-items:center;gap:8px">max <input type="number" id="sm" value="2" min="1" max="10" style="width:60px"> per check</label>
            <label class="switch"><input type="checkbox" id="sap"> Run Autopilot on them (uses your Library Autopilot settings)</label></div>
          <div><button class="btn primary">Add channel</button></div>
        </form></div>
      <div id="srcs" class="stack"></div></div>`;
    $("#sf").onsubmit = async (e) => {
      e.preventDefault();
      const ap = apLoad();
      try {
        await api("/api/sources", { body: { platform: $("#sp").value, handle: $("#sh").value, auto_import: $("#sa").checked, interval_hours: +$("#si").value,
          max_per_scan: +$("#sm").value, autopilot: $("#sap").checked ? { ...ap, enabled: true, first_publish: fromLocalInput(ap.first_publish || tomorrowAt9()) } : { enabled: false } } });
        $("#sh").value = ""; toast("Channel added — scanning its VODs…", "ok"); sig = ""; this.refresh();
      } catch (err) { fail(err); }
    };
    $("#srcs").onclick = onClick;
    $("#srcs").onchange = async (e) => {
      const t = e.target.closest("[data-auto]");
      if (t) { await api(`/api/sources/${t.dataset.auto}`, { method: "PATCH", body: { auto_import: t.checked } }).catch(fail); sig = ""; this.refresh(); }
    };
    await this.refresh();
  };
  let sig = "";
  this.refresh = async () => {
    const list = await api("/api/sources");
    const s = JSON.stringify(list) + [...open];
    if (s === sig) return; sig = s;
    $("#srcs").innerHTML = list.length ? list.map((src) => {
      const imported = new Set(src.imported || []);
      const isOpen = open.has(src.id) !== (list.length === 1); // a lone channel starts open
      return `<div class="card"><div class="row wrap"><div class="grow"><b style="font-size:15px">${esc(src.handle)}</b> <span class="chip">${SRC[src.platform] || esc(src.platform)}</span>
          <div class="muted" style="font-size:12px;margin-top:4px">${src.last_scan ? `Last checked ${when(src.last_scan)}` : "Scanning…"} · ${(src.vods || []).length} VODs listed · ${imported.size} imported</div>
          ${src.error ? `<div style="color:var(--err);font-size:12px;margin-top:4px">${esc(src.error)}</div>` : ""}</div>
        <label class="switch"><input type="checkbox" data-auto="${src.id}" ${src.auto_import ? "checked" : ""}> Auto-import every ${+src.interval_hours}h${src.autopilot?.enabled ? " + Autopilot" : ""}</label>
        <button class="btn sm" data-scan="${src.id}">↻ Check now</button><button class="btn sm" data-toggle="${src.id}">${isOpen ? "Hide VODs" : "Show VODs"}</button><button class="btn sm ghost danger" data-rm="${src.id}">Remove</button></div>
        ${isOpen ? `<div class="vods">${(src.vods || []).map((v, i) => `<div class="vod"><div class="th" style="background-image:url('${/^https:\/\//.test(v.thumb || "") ? esc(v.thumb) : ""}')">${v.duration ? `<span class="dur">${fmt(v.duration, 0)}</span>` : ""}</div>
          <div class="b"><div class="t" title="${esc(v.title)}">${esc(v.title)}</div><div class="dim" style="font-size:11.5px">${v.views != null ? num(v.views) + " views" : ""}</div>
          ${imported.has(v.id) ? '<span class="pill ok">Imported</span>' : `<button class="btn xs primary" data-imp="${src.id}" data-i="${i}">Import &amp; analyze</button>`}</div></div>`).join("") || '<div class="muted">No VODs found yet.</div>'}</div>` : ""}</div>`;
    }).join("") : `<div class="empty card"><b>No channels yet</b>Add a Twitch, Kick or YouTube channel above.</div>`;
    S.srcList = list;
  };
  const onClick = async (e) => {
    const t = e.target.closest("[data-scan],[data-rm],[data-imp],[data-toggle]");
    if (!t) return;
    try {
      if (t.dataset.scan) { await api(`/api/sources/${t.dataset.scan}/scan`, { method: "POST" }); toast("Checking for VODs…"); }
      if (t.dataset.toggle) open.has(t.dataset.toggle) ? open.delete(t.dataset.toggle) : open.add(t.dataset.toggle);
      if (t.dataset.rm && (await confirmBox("Remove channel?", "Projects already imported stay in your Library."))) await api(`/api/sources/${t.dataset.rm}`, { method: "DELETE" });
      if (t.dataset.imp) {
        const src = S.srcList.find((x) => x.id === t.dataset.imp), vod = src.vods[+t.dataset.i];
        await api(`/api/sources/${src.id}/import`, { body: { vod_ids: [vod.id] } }); toast("Importing — it appears in your Library and analyzes automatically.", "ok");
      }
      sig = ""; this.refresh();
    } catch (err) { fail(err); }
  };
}

// ======================================================================
// OBS link: receive clips from OBS on this PC or other PCs on the network
// ======================================================================
function Obs() {
  this.every = 2500;
  const OBS_STATUS = { connected: ["ok", "Connected"], connecting: ["run", "Connecting"], offline: ["err", "Offline"], off: ["", "Paused"] };
  this.mount = async () => {
    const st = await api("/api/obs");
    view.innerHTML = `<div class="page"><h1>OBS link</h1><p class="sub">Clips you save in OBS (replay buffer “clip that”, or finished recordings) land in your Library and are analyzed automatically — from OBS on this PC or any PC on your network. Optional: nothing here runs unless you turn it on.</p>
      <div class="cols"><div class="stack">
        <div class="card stack"><h2>Connect OBS directly</h2>
          <p class="muted" style="margin:0;font-size:12.5px">In OBS: <b>Tools → WebSocket Server Settings</b> → enable it, then <b>Show Connect Info</b> for the port and password. Works for OBS on this PC, or on another PC whose recording folder this PC can open (a shared drive). Also lets you trigger OBS from here.</p>
          <form id="of" class="stack">
            <div class="row wrap"><label class="field grow">Name<input type="text" id="on" placeholder="Gaming PC" required></label>
              <label class="field grow">OBS PC address<input type="text" id="oh" value="127.0.0.1" placeholder="192.168.1.20" required></label>
              <label class="field" style="width:100px">Port<input type="number" id="op" value="4455" min="1" max="65535"></label></div>
            <label class="field">WebSocket password<input type="password" id="opw" autocomplete="off" placeholder="from Show Connect Info"></label>
            <label class="field">Folder mapping for another PC <span class="dim" style="font-size:11.5px">(path on the OBS PC =&gt; the same folder as this PC sees it, one per line)</span>
              <textarea id="omap" rows="2" placeholder="D:/Videos/OBS => //GAMING-PC/OBS"></textarea></label>
            <div class="row wrap"><label class="switch"><input type="checkbox" id="orp" checked> Import replay-buffer saves</label>
              <label class="switch"><input type="checkbox" id="orc"> Import finished recordings (large)</label>
              <label class="switch"><input type="checkbox" id="oap"> Run Autopilot on them</label></div>
            <div><button class="btn primary">Add OBS</button></div></form></div>
        <div class="card stack"><h2>Receive clips from other PCs</h2>
          <p class="muted" style="margin:0;font-size:12.5px">For OBS on another PC without a shared folder: run the small <b>Reelwright Relay</b> there and it sends each saved clip here over your network. Only clip uploads with the pairing token are accepted; the rest of Reelwright stays private to this PC.</p>
          <div class="row wrap"><label class="switch"><input type="checkbox" id="lon" ${st.enabled ? "checked" : ""}> Receive clips from other PCs</label>
            <label class="field" style="flex-direction:row;align-items:center;gap:8px">port <input type="number" id="lport" value="${+st.port}" min="1024" max="65535" style="width:90px"></label>
            <label class="switch" title="For safety, clips from other PCs are always scheduled as Private: you approve before anything goes public"><input type="checkbox" id="lap" ${st.autopilot?.enabled ? "checked" : ""}> Run Autopilot on received clips (posts stay private)</label></div>
          <div id="lanstat"></div></div>
      </div>
      <div class="stack"><div id="conns" class="stack"></div></div></div></div>`;
    $("#of").onsubmit = async (e) => {
      e.preventDefault();
      const ap = apLoad();
      try {
        await api("/api/obs", { body: { name: $("#on").value, host: $("#oh").value, port: +$("#op").value, password: $("#opw").value, path_map: $("#omap").value,
          import_replays: $("#orp").checked, import_recordings: $("#orc").checked,
          autopilot: $("#oap").checked ? { ...ap, enabled: true, first_publish: fromLocalInput(ap.first_publish || tomorrowAt9()) } : { enabled: false } } });
        e.target.reset(); $("#oh").value = "127.0.0.1"; $("#op").value = 4455; toast("OBS added — connecting…", "ok"); sig = ""; this.refresh();
      } catch (err) { fail(err); }
    };
    const saveLan = async () => {
      const ap = apLoad();
      try {
        await api("/api/lan", { method: "PUT", body: { enabled: $("#lon").checked, port: +$("#lport").value,
          autopilot: $("#lap").checked ? { ...ap, enabled: true, first_publish: fromLocalInput(ap.first_publish || tomorrowAt9()) } : { enabled: false } } });
        sig = ""; this.refresh();
      } catch (err) { fail(err); }
    };
    $("#lon").onchange = saveLan; $("#lport").onchange = saveLan; $("#lap").onchange = saveLan;
    view.onclick = onClick;
    await this.refresh();
  };
  let sig = "";
  this.refresh = async () => {
    const st = await api("/api/obs");
    const s = JSON.stringify(st);
    if (s === sig) return; sig = s;
    const url = st.ips.length ? `http://${st.ips[0]}:${st.port}` : `http://THIS-PC-IP:${st.port}`;
    const cmd = (extra) => `python reelwright_relay.py --token=${st.token} --server ${url}${extra}`;
    $("#lanstat").innerHTML = !st.enabled ? `<div class="dim" style="font-size:12.5px">Off.</div>` : `
      ${st.error ? `<div style="color:var(--err);font-size:12.5px">${esc(st.error)}</div>` : `<div class="row"><span class="pill ${st.running ? "ok" : "warn"}">${st.running ? "Listening" : "Starting"}</span><span class="dim" style="font-size:12px">${esc(st.hostname)} · ${st.ips.map(esc).join(", ") || "no network address found"} · port ${+st.port} (TCP + UDP for auto-discovery)</span></div>`}
      <label class="field">Pairing token<div class="row"><input type="text" readonly value="${esc(st.token)}" class="mono grow" id="ltok"><button class="btn sm" data-copy="#ltok">Copy</button><button class="btn sm" data-newtok="1" title="Old relays stop working">New token</button></div></label>
      <h3 style="margin:6px 0 0">On the OBS PC</h3>
      <ol class="list" style="font-size:12.5px;margin:0">
        <li><a href="/api/obs/relay.py" download="reelwright_relay.py">Download reelwright_relay.py</a> and copy it to that PC (needs <a href="https://www.python.org/downloads/" target="_blank">Python 3.10+</a>; for OBS mode also run <span class="mono">pip install websockets</span>).</li>
        <li>In OBS: Tools → WebSocket Server Settings → enable. Then run one of:</li></ol>
      ${[["Send replay-buffer saves", cmd(" --obs-password OBS_PASSWORD")], ["Also send full recordings", cmd(" --obs-password OBS_PASSWORD --recordings")], ["No OBS websocket? Watch the recording folder", cmd(' --watch "D:/Videos/OBS"')]].map(([label, c], i) =>
        `<div class="field"><span class="dim" style="font-size:11.5px">${label}</span><div class="row"><input type="text" readonly class="mono grow" id="rc${i}" value="${esc(c)}"><button class="btn sm" data-copy="#rc${i}">Copy</button></div></div>`).join("")}
      <p class="dim" style="font-size:11.5px;margin:0">Windows may ask to allow Reelwright through the firewall — allow it on <b>private</b> networks. The relay finds this PC by itself if --server is left out.</p>`;
    $("#conns").innerHTML = st.connections.length ? st.connections.map((c) => `<div class="card"><div class="row wrap">
        <div class="grow"><b style="font-size:15px">${esc(c.name)}</b> ${pill(OBS_STATUS, c.enabled ? c.status || "connecting" : "off")}
          <div class="dim mono" style="font-size:11.5px;margin-top:3px">${esc(c.host)}:${+c.port}${c.has_password ? " · password saved" : ""}${(c.path_map || []).length ? ` · ${c.path_map.length} folder mapping(s)` : ""}</div></div>
        <label class="switch"><input type="checkbox" data-oen="${c.id}" ${c.enabled ? "checked" : ""}> Active</label>
        <button class="btn sm ghost danger" data-orm="${c.id}">Remove</button></div>
      ${c.error ? `<div style="color:var(--err);font-size:12px;margin-top:6px">${esc(c.error)}</div>` : ""}
      <div class="row wrap" style="margin-top:10px">
        <button class="btn sm primary" data-oact="save_replay" data-id="${c.id}" title="Save the last seconds of OBS's replay buffer as a clip">✂ Clip that</button>
        <button class="btn sm" data-oact="start_replay" data-id="${c.id}">Start replay buffer</button><button class="btn sm" data-oact="stop_replay" data-id="${c.id}">Stop replay buffer</button>
        <button class="btn sm" data-oact="start_record" data-id="${c.id}">● Record</button><button class="btn sm" data-oact="stop_record" data-id="${c.id}">■ Stop</button>
        <button class="btn sm" data-otest="${c.id}">Test</button></div>
      <div class="dim" style="font-size:12px;margin-top:8px">${c.imported || 0} clips imported${c.last_at ? ` · last ${when(c.last_at)}: ${esc((c.last_file || "").split(/[\\/]/).pop())}` : ""} · ${c.import_replays !== false ? "replays" : ""}${c.import_recordings ? " + recordings" : ""}${c.autopilot?.enabled ? " · Autopilot" : ""}</div>
      <div id="otest-${c.id}" style="font-size:12.5px;margin-top:6px"></div></div>`).join("")
      : `<div class="empty card"><b>No OBS connected yet</b>Add OBS on the left, or turn on “Receive clips from other PCs” and run the relay there.</div>`;
  };
  const onClick = async (e) => {
    const t = e.target.closest("[data-copy],[data-newtok],[data-oen],[data-orm],[data-oact],[data-otest]");
    if (!t) return;
    try {
      if (t.dataset.copy) { await navigator.clipboard.writeText($(t.dataset.copy).value); return toast("Copied.", "ok"); }
      if (t.dataset.newtok && (await confirmBox("New pairing token?", "Relays using the old token stop sending until you update them.", "Replace"))) await api("/api/lan/token", { method: "POST" });
      if (t.dataset.oen) await api(`/api/obs/${t.dataset.oen}`, { method: "PATCH", body: { enabled: t.checked } });
      if (t.dataset.orm && (await confirmBox("Remove this OBS?", "Clips already imported stay in your Library."))) await api(`/api/obs/${t.dataset.orm}`, { method: "DELETE" });
      if (t.dataset.oact || t.dataset.otest) {
        const id = t.dataset.id || t.dataset.otest, out = $(`#otest-${id}`);
        t.disabled = true;
        try {
          const r = t.dataset.oact ? await api(`/api/obs/${id}/action`, { body: { action: t.dataset.oact } }) : await api(`/api/obs/${id}/test`, { method: "POST" });
          out.innerHTML = `<span class="pill ok">OBS ${esc(r.obs_version)}</span> replay buffer ${r.replay_buffer == null ? "not enabled in OBS" : r.replay_buffer ? "running" : "stopped"} · recording ${r.recording ? "on" : "off"}${r.record_dir ? ` · saves to <span class="mono">${esc(r.record_dir)}</span>` : ""}`;
          if (t.dataset.oact === "save_replay") toast("Clip saved in OBS — it'll appear in your Library shortly.", "ok");
        } finally { t.disabled = false; }
        return;
      }
      sig = ""; this.refresh();
    } catch (err) { fail(err); }
  };
}

// ======================================================================
// Virality Lab: reports + custom style training
// ======================================================================
function Lab() {
  this.every = 3000;
  this.mount = async () => {
    view.innerHTML = `<div class="page"><h1>Virality Lab</h1><p class="sub">Score videos against what's performing right now, and train Reelwright on your own style so every pick, edit and title fits your channel.</p>
      <div class="cols"><div class="stack">
        <div class="card"><h2>Analyze a video</h2><div class="stack">
          <label class="field">Project<select id="lp"></select></label>
          <label class="field">Compare against (optional search)<input type="text" id="lq" placeholder="Auto: AI picks a query from the transcript"></label>
          <button class="btn primary" id="lgo">✦ Analyze virality</button></div></div>
        <div class="card"><h2>New style profile</h2><p class="muted" style="font-size:12.5px;margin-top:-6px">Custom training: learns from reference videos/channels you admire, clips you rate 👍/👎, and how your published posts performed.</p>
          <form id="pf" class="stack">
            <label class="field">Name<input type="text" id="pn" placeholder="e.g. My gaming channel" required></label>
            <label class="field">Reference videos or channels (one link per line)<textarea id="pr" rows="3" placeholder="https://www.youtube.com/@yourfavcreator/shorts"></textarea></label>
            <label class="field">Notes about your audience &amp; style<textarea id="pnotes" rows="2" placeholder="Fast-paced, hype moments, clean captions…"></textarea></label>
            <button class="btn">Create profile</button></form></div>
      </div>
      <div class="stack"><div class="card" id="learned"></div><div id="profiles" class="stack"></div><div class="card" style="overflow-x:auto"><h2>Reports</h2><div id="reports"></div></div></div></div></div>`;
    loadLearned();
    const projects = await api("/api/projects");
    $("#lp").innerHTML = projects.filter((p) => p.status === "ready").map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("") || "<option value='' disabled selected>No analyzed projects yet</option>";
    $("#lgo").onclick = async () => {
      if (!$("#lp").value) return toast("Analyze a video in the Library first.");
      try { await api(`/api/projects/${$("#lp").value}/virality`, { body: { query: $("#lq").value } }); toast("Analysis started — the report appears below in a minute.", "ok"); sig = ""; this.refresh(); }
      catch (e) { fail(e); }
    };
    view.onchange = async (e) => {
      const f = e.target.closest("[data-limport]");
      if (!f?.files?.[0]) return;
      const fd = new FormData();
      fd.append("file", f.files[0]);
      try { const r = await api("/api/learning/import", { body: fd }); toast(`Imported ${r.added} lessons (${r.skipped} already known or invalid).`, "ok"); loadLearned(); }
      catch (err) { fail(err); }
      f.value = "";
    };
    $("#pf").onsubmit = async (e) => {
      e.preventDefault();
      try {
        await api("/api/profiles", { body: { name: $("#pn").value, notes: $("#pnotes").value, references: $("#pr").value.split(/\n+/) } });
        e.target.reset(); toast("Profile created — press Train to learn from it.", "ok"); sig = ""; this.refresh();
      } catch (err) { fail(err); }
    };
    view.onclick = onClick;
    await this.refresh();
  };
  let sig = "";
  this.refresh = async () => {
    const [profiles, reports, jobs] = await Promise.all([api("/api/profiles"), api("/api/reports"), api("/api/jobs")]);
    const training = new Set(jobs.filter((j) => ["train", "research"].includes(j.kind) && ["queued", "running"].includes(j.status)).map((j) => j.profile_id));
    const trainMsg = Object.fromEntries(jobs.filter((j) => training.has(j.profile_id) && j.status === "running").map((j) => [j.profile_id, j.message]));
    const analyzing = jobs.filter((j) => j.kind === "virality" && ["queued", "running"].includes(j.status)).length;
    const failedViral = jobs.find((j) => j.kind === "virality" && j.status === "failed" && j.finished > Date.now() / 1000 - 600);
    const s = JSON.stringify([profiles, reports.map((r) => r.id), [...training], trainMsg, analyzing, failedViral?.id]);
    if (s === sig) return; sig = s;
    $("#profiles").innerHTML = profiles.map((p) => {
      const t = p.trained;
      return `<div class="card"><div class="row wrap"><h2 class="grow" style="margin:0">${esc(p.name)} ${p.active ? '<span class="pill ok">Active</span>' : ""}</h2>
        <button class="btn sm primary" data-train="${p.id}" ${training.has(p.id) ? "disabled" : ""}>${training.has(p.id) ? '<span class="spin"></span> Training…' : t ? "↻ Retrain" : "Train"}</button>
        ${!p.active ? `<button class="btn sm" data-activate="${p.id}">Make active</button>` : ""}
        ${t ? `<button class="btn sm" data-theme="${p.id}" title="Use the learned caption colour and format as your defaults">Apply theme</button>` : ""}
        <button class="btn sm" data-interview="${p.id}" title="The AI asks what it still needs to know about you">💬 Interview</button>
        <button class="btn sm" data-research="${p.id}" ${training.has(p.id) ? "disabled" : ""} title="Search the open web for the latest on each platform">🌐 Refresh live intel</button>
        <button class="btn sm ghost danger" data-delp="${p.id}">✕</button></div>
        ${training.has(p.id) ? `<div class="row" style="font-size:12.5px;margin-top:8px"><span class="spin"></span> ${esc(trainMsg[p.id] || "Queued…")}</div>` : ""}
        <div class="row wrap" style="margin-top:10px;font-size:12.5px"><span class="muted">Train for:</span>${Object.entries(PLATFORM).map(([k, v]) => `<label class="switch"><input type="checkbox" data-pplat="${p.id}" value="${k}" ${(p.platforms || Object.keys(PLATFORM)).includes(k) ? "checked" : ""}> ${v}</label>`).join("")}
          <label class="switch" title="Re-research every 7 days"><input type="checkbox" data-autoref="${p.id}" ${p.auto_refresh !== false ? "checked" : ""}> Weekly auto-refresh</label></div>
        ${Object.keys(p.answers || {}).length ? `<div class="dim" style="font-size:11.5px;margin-top:6px">${Object.keys(p.answers).length} interview answers on file</div>` : ""}
        ${p.error ? `<div style="color:var(--err);font-size:12px;margin-top:6px">${esc(p.error)}</div>` : ""}
        ${t ? `<p style="font-size:13px;margin:10px 0">${esc(t.summary)}</p>
          <div class="two"><div><h3>Rules Reelwright now follows</h3><div style="white-space:pre-wrap;font-size:12.5px;color:#c4c7d6">${esc(t.guidance)}</div></div>
          <div class="stack"><div><h3>Hooks that work</h3><div class="chips">${t.hook_patterns.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div></div>
            <div><h3>Winning topics</h3><div class="chips">${t.winning_topics.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div></div>
            <div><h3>Avoid</h3><div class="chips">${t.avoid.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div></div>
            <div class="muted" style="font-size:12.5px">Ideal length <b>${t.ideal_length_s.map(Number).join("–")}s</b> · Title formula: <b>${esc(t.title_formula)}</b></div>
            <div class="muted" style="font-size:12.5px">Theme: <span class="swatch" style="background:${/^#[0-9a-f]{6}$/i.test(t.theme.caption_color) ? t.theme.caption_color : "#FFD400"}"></span> ${esc(t.theme.caption_style)} captions, ${esc(t.theme.caption_pos)}, ${esc(t.theme.aspect)}, ${esc(t.theme.layout)} layout</div></div></div>
          <div class="dim" style="font-size:11.5px;margin-top:8px">Trained ${when(p.trained_at)} on ${+p.sample_counts?.references || 0} reference videos, ${+p.sample_counts?.rated || 0} rated clips, ${+p.sample_counts?.published || 0} published posts.</div>`
        : `<p class="muted" style="font-size:12.5px">${(p.references || []).length} reference links. Answer the interview and rate clips 👍/👎 in the editor to teach it what you like, then Train.</p>`}
        ${Object.entries(p.playbooks || {}).map(([pl, b]) => `<details class="sec" style="padding-bottom:4px"><summary style="cursor:pointer;font-weight:600">${platTag(pl)} live playbook <span class="dim" style="font-weight:400;font-size:12px">· researched ${when(b.at)}</span></summary>
          <p style="font-size:13px">${esc(b.summary)}</p>
          <div class="two"><div><h3>Algorithm rewards</h3><ul class="list">${b.algorithm_signals.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
            <h3 style="margin-top:10px">Trending now</h3><ul class="list">${b.current_trends.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
          <div><h3>Hooks</h3><ul class="list">${b.hook_rules.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
            <h3 style="margin-top:10px">Captions</h3><ul class="list">${b.caption_rules.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div>
          <div class="muted" style="font-size:12.5px;margin-top:8px">Length <b>${b.ideal_length_s.map(Number).join("–")}s</b> · Hashtags: ${esc(b.hashtag_strategy)} · Best times: ${esc(b.best_posting_times)}<br>Specs: ${esc(b.format_specs)} · Avoid: ${b.avoid.map(esc).join("; ")}</div>
          ${b.sources?.length ? `<div style="font-size:11.5px;margin-top:8px" class="dim">Sources: ${b.sources.slice(0, 8).map((x) => (/^https?:\/\//.test(x.url) ? `<a href="${esc(x.url)}" target="_blank">${esc((x.title || x.url).slice(0, 50))}</a>` : "")).join(" · ")}</div>` : ""}</details>`).join("")}</div>`;
    }).join("");
    $("#reports").innerHTML = (analyzing ? `<div class="row" style="margin-bottom:10px;font-size:13px"><span class="spin"></span> ${analyzing} analysis running…</div>` : "")
      + (failedViral ? `<div style="color:var(--err);font-size:12.5px;margin-bottom:10px">Last analysis failed: ${esc(failedViral.error)}</div>` : "")
      + (reports.length ? `<table><thead><tr><th>Score</th><th>Video / clip</th><th>Best fit</th><th>When</th></tr></thead><tbody>${reports.map((r) => `
      <tr style="cursor:pointer" data-rep="${r.id}"><td><span class="vbadge" style="font-size:14px">✦ ${+r.score}</span></td><td><b>${esc(r.title)}</b><div class="dim" style="font-size:12px">${esc(r.verdict)}</div></td><td style="font-size:12px">${esc((r.best_platform || "").slice(0, 40))}</td><td class="dim" style="font-size:12px">${when(r.at)}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty" style="padding:24px"><b>No reports yet</b>Analyze a project, or press “Score virality” on any clip.</div>`);
  };
  const onClick = async (e) => {
    const t = e.target.closest("[data-train],[data-activate],[data-theme],[data-delp],[data-rep],[data-interview],[data-research],[data-pplat],[data-autoref],[data-lrefresh],[data-lreset]");
    if (!t) return;
    try {
      if (t.dataset.lrefresh) { await api("/api/learning/results", { method: "POST" }); toast("Fetching your posts' latest results…", "ok"); return setTimeout(loadLearned, 8000); }
      if (t.dataset.lreset) {
        if (await confirmBox("Reset everything learned?", "Deletes all lessons (ratings stay on clips that still exist). Style profiles are kept.", "Reset")) { await api("/api/learning", { method: "DELETE" }); loadLearned(); }
        return;
      }
      if (t.dataset.rep) return openReport(t.dataset.rep);
      if (t.dataset.interview) return interview(t.dataset.interview, t);
      if (t.dataset.research) { await api(`/api/profiles/${t.dataset.research}/research`, { method: "POST" }); toast("Researching the latest on each platform from the open web…", "ok"); }
      if (t.dataset.pplat) {
        const plats = $$(`[data-pplat="${t.dataset.pplat}"]`).filter((x) => x.checked).map((x) => x.value);
        await api(`/api/profiles/${t.dataset.pplat}`, { method: "PATCH", body: { platforms: plats } });
      }
      if (t.dataset.autoref) await api(`/api/profiles/${t.dataset.autoref}`, { method: "PATCH", body: { auto_refresh: t.checked } });
      if (t.dataset.train) { await api(`/api/profiles/${t.dataset.train}/train`, { method: "POST" }); toast("Training started — this reads your references and takes a minute.", "ok"); }
      if (t.dataset.activate) await api(`/api/profiles/${t.dataset.activate}`, { method: "PATCH", body: { active: true } });
      if (t.dataset.theme) { S.settings = await api(`/api/profiles/${t.dataset.theme}/apply-theme`, { method: "POST" }); toast("Theme applied to your defaults.", "ok"); }
      if (t.dataset.delp && (await confirmBox("Delete profile?", "Its learned rules stop being used."))) await api(`/api/profiles/${t.dataset.delp}`, { method: "DELETE" });
      sig = ""; this.refresh();
    } catch (err) { fail(err); }
  };
  const FEAT = { length: "length", ai: "AI score", wps: "speech pace", loud: "loudness", spikes: "energy spikes", cuts: "scene cuts", chat: "chat activity", pos: "later in video", question: "question hook", lead: "slow start" };
  const loadLearned = async () => {
    const box = $("#learned");
    let L;
    try { L = await api("/api/learning"); } catch (e) { box.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
    const m = L.model, hs = Object.entries(L.house_style || {}), tb = L.trim_bias;
    const weights = m ? Object.entries(m.weights).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 6) : [];
    const maxW = Math.max(0.01, ...weights.map(([, v]) => Math.abs(v)));
    box.innerHTML = `<div class="row wrap"><h2 class="grow" style="margin:0">What Reelwright has learned from you</h2>
        <button class="btn sm" data-lrefresh="1" title="Pull the latest views of your posts and re-learn">↻ Update from post results</button>
        <a class="btn sm" href="/api/learning/export" download="reelwright-learning.json" title="Back up, or move what it learned to another PC">Export</a>
        <label class="btn sm" title="Merge lessons exported from another PC">Import<input type="file" accept=".json,application/json" data-limport="1" hidden></label>
        <button class="btn sm ghost danger" data-lreset="1">Reset</button></div>
      <p class="muted" style="font-size:12.5px;margin:6px 0 10px">Every 👍/👎, every clip you post and every AI pick you delete is a lesson. It applies on the very next scan — no retraining needed — and works without an API key.</p>
      <div class="row wrap" style="gap:18px;font-size:13px"><span><b>${+L.lessons}</b> lessons</span><span style="color:var(--ok)">${+L.liked} liked/posted</span><span style="color:var(--err)">${+L.disliked} rejected</span><span class="dim">${+L.with_views} with real view counts${L.results_at ? ` · updated ${when(L.results_at)}` : ""}</span></div>
      <div class="two" style="margin-top:12px"><div>
        <h3>Personal ranking model</h3>
        ${!m ? `<p class="dim" style="font-size:12.5px">Needs about 12 lessons with both likes and rejections before it can learn your taste.</p>`
          : `<p style="font-size:12.5px;margin:0 0 8px">${m.useful ? `<span class="pill ok">Active</span> re-ranks every scan` : `<span class="pill warn">Learning</span> not yet better than guessing — keeps watching`} · ${Math.round(m.cv_acc * 100)}% accurate on clips it hadn't seen (baseline ${Math.round(m.baseline * 100)}%) · ${+m.n} samples</p>
            ${weights.map(([k, v]) => `<div class="row" style="font-size:12px;gap:8px"><span style="width:110px">${esc(FEAT[k] || k)}</span><div style="flex:1;height:6px;background:var(--panel-2);border-radius:3px;position:relative"><div style="position:absolute;${v >= 0 ? "left:50%" : "right:50%"};width:${(Math.abs(v) / maxW) * 50}%;height:100%;border-radius:3px;background:${v >= 0 ? "var(--ok)" : "var(--err)"}"></div></div><span class="dim" style="width:52px;text-align:right">${v >= 0 ? "likes" : "avoids"}</span></div>`).join("")}`}
      </div><div class="stack">
        <div><h3>Your editing habits</h3>${hs.length ? `<div class="chips">${hs.map(([k, v]) => `<span class="chip">${esc(k.replace(/_/g, " "))}: ${esc(String(v))}</span>`).join("")}</div><div class="dim" style="font-size:11.5px;margin-top:4px">New clips start with these.</div>` : `<div class="dim" style="font-size:12.5px">Learned once 3+ liked/posted clips agree on a setting.</div>`}</div>
        <div><h3>How you re-trim AI picks</h3>${tb ? `<div style="font-size:12.5px">Start ${tb.start >= 0 ? "later" : "earlier"} by <b>${Math.abs(tb.start)}s</b>, end ${tb.end >= 0 ? "later" : "earlier"} by <b>${Math.abs(tb.end)}s</b> (from ${+tb.n} clips) — applied to new picks automatically.</div>` : `<div class="dim" style="font-size:12.5px">Learned after you adjust 5+ AI picks you keep.</div>`}</div>
        ${L.hints ? `<div><h3>Told to the AI</h3><div style="font-size:12.5px">${esc(L.hints)}</div></div>` : ""}
      </div></div>`;
  };
  const interview = async (pid, btn) => {
    btn.disabled = true; btn.innerHTML = `<span class="spin"></span> Thinking of questions…`;
    let qs;
    try { qs = await api(`/api/profiles/${pid}/questions`, { method: "POST" }); }
    catch (err) { fail(err); return; }
    finally { btn.disabled = false; btn.textContent = "💬 Interview"; }
    modal(`<h2>💬 A few questions</h2><p class="muted">Your answers train the style profile and focus the live research for each platform.</p>
      <div class="stack">${qs.map((q, i) => `<label class="field"><span style="color:var(--text);font-size:13px;font-weight:600">${esc(q.question)}</span>
        <span class="dim" style="font-size:11.5px">${esc(q.why)}</span><textarea rows="2" data-q="${i}" placeholder="${esc(q.placeholder)}"></textarea></label>`).join("")}</div>
      <div class="foot"><button class="btn" data-close>Later</button><button class="btn" id="iv-save">Save</button><button class="btn primary" id="iv-train">Save &amp; train</button></div>`, (m, close) => {
      const save = async (train) => {
        const answers = Object.fromEntries(qs.map((q, i) => [q.id, $(`[data-q="${i}"]`, m).value.trim()]).filter(([, v]) => v));
        try {
          await api(`/api/profiles/${pid}`, { method: "PATCH", body: { answers } });
          if (train) await api(`/api/profiles/${pid}/train`, { method: "POST" });
          close(); toast(train ? "Saved — training and researching each platform now." : "Answers saved.", "ok"); sig = ""; this.refresh();
        } catch (err) { fail(err); }
      };
      $("#iv-save", m).onclick = () => save(false);
      $("#iv-train", m).onclick = () => save(true);
    }, true);
  };
}

// ======================================================================
// Publish
// ======================================================================
function Publish() {
  const self = this;
  this.mount = async () => {
    view.innerHTML = `<div class="page"><h1>Publish</h1><p class="sub">Your connected channels, the posting queue, and everything scheduled to go live.</p>
      <div class="conn-grid" id="conns"></div>
      <div class="cols"><div class="card"><h2>Upcoming</h2><div id="upcoming"></div></div>
      <div class="card" style="overflow-x:auto"><h2>Queue &amp; history</h2><div id="queue"></div></div></div></div>`;
    view.onclick = onClick;
    await this.refresh();
  };
  let connSig = "";
  this.refresh = async () => {
    const [conn, ups] = await Promise.all([api("/api/connections"), api("/api/uploads")]);
    S.conn = conn;
    const sig = JSON.stringify(conn);
    if (sig !== connSig) { connSig = sig; renderConns(conn); }
    const at = (u) => u.publish_at || u.upload_at;
    const upcoming = ups.filter((u) => !["failed", "canceled"].includes(u.status) && at(u) && new Date(at(u)) > new Date()).sort((a, b) => new Date(at(a)) - new Date(at(b)));
    const days = {};
    upcoming.forEach((u) => { const d = new Date(at(u)).toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" }); (days[d] ||= []).push(u); });
    $("#upcoming").innerHTML = upcoming.length ? Object.entries(days).map(([d, us]) => `<div class="day"><h3>${d}</h3>${us.map((u) => `
      <div class="slot"><span class="when">${new Date(at(u)).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>${platTag(u.platform)}
      <span class="grow" style="font-size:13px">${esc(u.title)}</span><span class="dim" style="font-size:11px">${u.publish_at ? "goes live" : "posts"}</span></div>`).join("")}</div>`).join("")
      : `<div class="empty" style="padding:24px"><b>Nothing scheduled</b>Schedule from a clip's Publish button, or turn on Autopilot.</div>`;
    $("#queue").innerHTML = ups.length ? `<table><thead><tr><th>Post</th><th>Status</th><th>Schedule</th><th></th></tr></thead><tbody>${ups.map((u) => `
      <tr><td>${platTag(u.platform)} <b>${esc(u.title)}</b><div class="dim mono">${u.clip ? `${fmt(u.clip.start)} → ${fmt(u.clip.end)}` : "clip deleted"} · ${esc(u.privacy)}</div></td>
      <td style="min-width:150px">${pill(UPLOAD_STATUS, u.status)}${u.status === "uploading" ? `<div style="margin-top:6px">${bar(u.progress)}</div>` : ""}${u.error ? `<div style="color:var(--err);font-size:11.5px;margin-top:4px">${esc(u.error).slice(0, 180)}</div>` : ""}</td>
      <td style="font-size:12.5px">${u.publish_at ? `Live ${when(u.publish_at)}` : u.upload_at ? `Posts ${when(u.upload_at)}` : "Immediately"}</td>
      <td style="text-align:right;white-space:nowrap">${/^https:\/\//.test(u.url || "") ? `<a class="btn xs" href="${esc(u.url)}" target="_blank">Open ↗</a> ` : ""}
        ${["failed", "needs_auth", "canceled"].includes(u.status) ? `<button class="btn xs" data-retry="${u.id}">Retry</button> ` : ""}
        ${u.status !== "uploading" ? `<button class="btn xs ghost" data-cancel="${u.id}" title="${u.status === "done" ? "Remove from list" : "Cancel"}">✕</button>` : ""}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty"><b>Nothing posted yet</b>Open a clip in the editor and press Publish.</div>`;
  };
  function renderConns(c) {
    const yt = c.youtube, tt = c.tiktok, ig = c.instagram;
    const safeImg = (u) => (/^https:\/\//.test(u || "") ? `<img src="${esc(u)}" alt="">` : "");
    const head = (p, ok, name, avatar, extra = "") => `<div class="row" style="margin-bottom:10px">${platTag(p)}<span class="grow"></span>${ok ? `<span class="pill ok">Connected</span>` : `<span class="pill warn">Not connected</span>`}</div>
      ${ok ? `<div class="chan">${safeImg(avatar)}<div class="grow"><b>${esc(name || "Connected")}</b><div class="muted" style="font-size:12px">${esc(extra)}</div></div><button class="btn sm ghost" data-disc="${p}">Disconnect</button></div>` : ""}`;
    const ytBody = yt.connected ? "" : !yt.has_secret ? `<ol class="steps"><li>In <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank">Google Cloud</a>, enable <b>YouTube Data API v3</b>.</li>
        <li>Configure the OAuth consent screen and add yourself as a test user.</li><li>Create an OAuth client of type <b>Desktop app</b>, download the JSON, and upload it:</li></ol>
        <div style="margin-top:10px"><input type="file" id="secret" accept=".json" hidden><button class="btn primary sm" data-yt="pick">Upload client JSON</button></div>`
      : `<div class="row wrap"><button class="btn primary sm" data-yt="connect" ${yt.connecting ? "disabled" : ""}>${yt.connecting ? '<span class="spin"></span> Waiting for Google…' : "Sign in with Google"}</button>
        <button class="btn ghost sm" data-yt="pick">Replace client JSON</button><input type="file" id="secret" accept=".json" hidden></div>`;
    const ttBody = tt.connected ? "" : `<p class="muted" style="font-size:12.5px;margin:0 0 8px">Create an app at <a href="https://developers.tiktok.com/apps" target="_blank">TikTok for Developers</a> with Login Kit + Content Posting API (scopes <code>user.info.basic</code>, <code>video.publish</code>). Add this redirect URI:</p>
        <div class="mono" style="background:#0d0e14;padding:6px 8px;border-radius:6px;margin-bottom:8px;word-break:break-all">${esc(tt.redirect_uri)}</div>
        <div class="two"><input type="text" id="ttk" placeholder="Client key"><input type="password" id="tts" placeholder="Client secret"></div>
        <div class="row" style="margin-top:8px"><button class="btn sm" data-tt="save">Save app</button><button class="btn primary sm" data-tt="connect" ${tt.has_app ? "" : "disabled"}>Sign in with TikTok</button></div>
        <p class="dim" style="font-size:11.5px;margin:8px 0 0">Until TikTok audits your app, posts are private (only you can see them).</p>`;
    const igBody = ig.connected ? "" : `<p class="muted" style="font-size:12.5px;margin:0 0 8px">Needs an Instagram <b>Business or Creator</b> account linked to a Facebook Page. In your <a href="https://developers.facebook.com/apps" target="_blank">Meta app</a> (Facebook Login for Business), generate a token with <code>instagram_basic</code> + <code>instagram_content_publish</code>, and copy your Instagram account ID.</p>
        <input type="password" id="igt" placeholder="Access token" style="margin-bottom:8px"><input type="text" id="igu" placeholder="Instagram account ID (numbers)">
        <div style="margin-top:8px"><button class="btn primary sm" data-ig="save">Connect Instagram</button></div>`;
    $("#conns").innerHTML = `<div class="card">${head("youtube", yt.connected, yt.channel?.title, yt.channel?.thumb, yt.channel ? `${num(yt.channel.subscribers)} subscribers` : "")}${ytBody}${yt.error ? `<p style="color:var(--err);font-size:12px">${esc(yt.error)}</p>` : ""}</div>
      <div class="card">${head("tiktok", tt.connected, tt.name, tt.avatar)}${ttBody}</div>
      <div class="card">${head("instagram", ig.connected, ig.name ? "@" + ig.name : "", ig.avatar, ig.followers != null ? `${num(ig.followers)} followers` : "")}${igBody}</div>`;
    const f = $("#secret");
    if (f) f.onchange = async () => { const fd = new FormData(); fd.append("file", f.files[0]); try { await api("/api/youtube/secret", { body: fd }); toast("Client saved — now sign in.", "ok"); connSig = ""; self.refresh(); } catch (e) { fail(e); } };
  }
  const onClick = async (e) => {
    const t = e.target.closest("[data-yt],[data-tt],[data-ig],[data-disc],[data-retry],[data-cancel]");
    if (!t) return;
    try {
      if (t.dataset.yt === "pick") return $("#secret").click();
      if (t.dataset.yt === "connect") { await api("/api/youtube/connect", { method: "POST" }); toast("Finish signing in in the browser window that opened."); }
      if (t.dataset.tt === "save") { await api("/api/connections/tiktok/app", { method: "PUT", body: { client_key: $("#ttk").value, client_secret: $("#tts").value } }); toast("TikTok app saved.", "ok"); }
      if (t.dataset.tt === "connect") { const { url } = await api("/api/connections/tiktok/connect", { method: "POST" }); window.open(url, "_blank"); toast("Approve Reelwright in the TikTok tab, then come back here."); }
      if (t.dataset.ig === "save") { await api("/api/connections/instagram", { method: "PUT", body: { access_token: $("#igt").value, ig_user_id: $("#igu").value } }); toast("Instagram connected.", "ok"); }
      if (t.dataset.disc && (await confirmBox(`Disconnect ${PLATFORM[t.dataset.disc]}?`, "Scheduled posts to it will wait until you reconnect.", "Disconnect"))) await api(`/api/connections/${t.dataset.disc}/disconnect`, { method: "POST" });
      if (t.dataset.retry) await api(`/api/uploads/${t.dataset.retry}/retry`, { method: "POST" });
      if (t.dataset.cancel) await api(`/api/uploads/${t.dataset.cancel}`, { method: "DELETE" });
      connSig = ""; self.refresh(); sideStatus();
    } catch (err) { fail(err); }
  };
}

// ======================================================================
// Activity (jobs)
// ======================================================================
function Jobs() {
  this.mount = async () => {
    view.innerHTML = `<div class="page"><h1>Activity</h1><p class="sub">Everything Reelwright is doing in the background.</p>
      <div class="card" id="usage" style="margin-bottom:16px"></div>
      <div class="card" id="jobs" style="overflow-x:auto"></div>
      <details class="card" id="logbox" style="margin-top:16px"><summary style="cursor:pointer;font-weight:600">Event log <span class="dim" style="font-weight:400;font-size:12px">· errors, OBS and relay events, imports — for troubleshooting</span></summary>
        <pre id="log" class="mono" style="font-size:11.5px;max-height:420px;overflow:auto;white-space:pre-wrap;margin:10px 0 0"></pre></details></div>`;
    $("#logbox").ontoggle = async () => {
      if (!$("#logbox").open) return;
      const { lines } = await api("/api/logs?n=400").catch(() => ({ lines: [] }));
      $("#log").textContent = lines.length ? lines.join("\n") : "Nothing logged yet.";
      $("#log").scrollTop = $("#log").scrollHeight;
    };
    loadUsage();
    await this.refresh();
  };
  const loadUsage = async () => {
    const u = await api("/api/usage?days=30").catch(() => null);
    if (!u) return;
    const money = (v) => (v < 0.01 && v > 0 ? "<$0.01" : `$${v.toFixed(2)}`);
    $("#usage").innerHTML = `<div class="row wrap"><h2 class="grow" style="margin:0">Claude usage · last 30 days</h2><span class="dim" style="font-size:12px">estimated from per-token prices; web searches during live research are billed separately and not included</span></div>
      ${u.calls ? `<div class="row wrap" style="gap:22px;margin:8px 0;font-size:13px"><span><b>${+u.calls}</b> calls</span><span><b>${money(u.cost_usd)}</b> est.${u.unpriced_calls ? ` <span class="dim">(+${+u.unpriced_calls} unpriced)</span>` : ""}</span>
          <span title="Share of input tokens served from the prompt cache (transcripts re-read at ~5% of the price)"><b>${u.cache_hit_ratio == null ? "—" : Math.round(u.cache_hit_ratio * 100) + "%"}</b> cache hits</span>
          <span class="dim">${num(u.input_tokens)} in · ${num(u.output_tokens)} out</span></div>
        <table><thead><tr><th>Feature</th><th>Calls</th><th>Input</th><th>Cached</th><th>Output</th><th>Est. cost</th></tr></thead><tbody>${Object.entries(u.by_kind).map(([k, v]) =>
          `<tr><td>${esc(k)}</td><td>${+v.calls}</td><td>${num(v.input)}</td><td>${num(v.cache_read)}</td><td>${num(v.output)}</td><td>${money(v.cost)}${v.unpriced ? " +?" : ""}</td></tr>`).join("")}</tbody></table>`
        : `<div class="dim" style="font-size:12.5px;margin-top:6px">No Claude calls yet.</div>`}`;
  };
  this.refresh = async () => {
    const [jobs, projects] = await Promise.all([api("/api/jobs"), api("/api/projects")]);
    const names = Object.fromEntries(projects.map((p) => [p.id, p.name]));
    $("#jobs").innerHTML = jobs.length ? `<table><thead><tr><th>Task</th><th>Project</th><th>Status</th><th style="width:30%">Progress</th><th>Started</th></tr></thead><tbody>${jobs.map((j) => `
      <tr><td><b>${JOB_LABEL[j.kind] || esc(j.kind)}</b>${j.platform ? " " + platTag(j.platform) : ""}${j.query ? `<div class="dim" style="font-size:12px">“${esc(j.query)}”</div>` : ""}</td>
      <td>${j.project_id ? `<a href="#/editor/${j.project_id}">${esc(names[j.project_id] || "—")}</a>` : "—"}</td>
      <td>${pill(JOB_STATUS, j.status)}</td>
      <td>${j.status === "running" ? bar(j.progress) : ""}<div style="font-size:12px;${j.error ? "color:var(--err)" : "color:var(--muted)"}">${esc(j.error || j.message || "")}</div></td>
      <td class="dim" style="font-size:12px">${when(new Date((j.started || j.created) * 1000).toISOString())}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty"><b>All quiet</b>Jobs show up here when you import, analyze, render or post.</div>`;
  };
}

// ======================================================================
// Settings
// ======================================================================
function Settings() {
  this.mount = async () => {
    const s = (S.settings = await api("/api/settings"));
    const opt = (list, v) => list.map(([k, l]) => `<option value="${k}" ${String(k) === String(v) ? "selected" : ""}>${l}</option>`).join("");
    view.innerHTML = `<div class="page" style="max-width:860px"><h1>Settings</h1><p class="sub">Everything runs on this computer. Keys and tokens never leave it except to call the service they belong to.</p>
      <form id="sf" class="stack">
        <div class="card stack"><h2>Claude AI</h2>
          <p class="muted" style="margin:0;font-size:13px">Powers clip ranking, moment search, the Ask-AI editor, virality analysis, style training and titles/captions. Get a key at <a href="https://console.anthropic.com" target="_blank">console.anthropic.com</a>.</p>
          <label class="field">API key ${s.anthropic_key_set ? `<span class="pill ok">saved ${esc(s.anthropic_key_hint)}</span>` : ""}<input type="password" id="key" placeholder="${s.anthropic_key_set ? "Leave blank to keep the current key" : "sk-ant-…"}" autocomplete="off"></label>
          <label class="field">Model<select id="model">${opt([["claude-opus-5-5", "Claude Opus 5.5 — best judgement (default)"], ["claude-sonnet-5-5", "Claude Sonnet 5.5 — faster, cheaper"], ["claude-haiku-5-5", "Claude Haiku 5.5 — fastest, cheapest"], ["claude-fable-5-2", "Claude Fable 5.2 — most capable, premium"]], s.model)}</select></label>
          ${s.anthropic_key_set ? `<div><button type="button" class="btn sm danger" id="rmkey">Remove key</button></div>` : ""}
        </div>
        <div class="card stack"><h2>Transcription</h2>
          <div class="row wrap"><label class="field grow">Whisper model<select id="wm">${opt([["large-v3-turbo", "large-v3-turbo — best speed/accuracy (default)"], ["large-v3", "large-v3 — most accurate"], ["medium", "medium"], ["small", "small — fast, lighter"], ["base", "base — fastest"]], s.whisper_model)}</select></label>
          <label class="field grow">Run on<select id="wd">${opt([["auto", "Auto (GPU if available)"], ["cuda", "NVIDIA GPU"], ["cpu", "CPU"]], s.whisper_device)}</select></label></div>
        </div>
        <div class="card stack"><h2>Clip finding</h2>
          <div class="row wrap"><label class="field grow">Clips per video<input type="number" id="cc" min="1" max="40" value="${+s.clip_count}"></label>
          <label class="field grow">Min length (s)<input type="number" id="cmin" min="3" max="600" value="${+s.clip_min}"></label>
          <label class="field grow">Max length (s)<input type="number" id="cmax" min="5" max="900" value="${+s.clip_max}"></label></div>
          <p class="muted" style="margin:0;font-size:12px">Shorts, TikTok and Reels allow up to 3 minutes; 20–60 s performs best for most channels.</p>
        </div>
        <div class="card stack"><h2>Defaults</h2>
          <div class="row wrap"><label class="field grow">Clip format<select id="da">${opt([["9:16", "9:16 vertical (Shorts / TikTok / Reels)"], ["1:1", "1:1 square"], ["4:5", "4:5 portrait"], ["16:9", "16:9 landscape"], ["source", "Original"]], s.default_aspect)}</select></label>
          <label class="field grow">Post visibility<select id="dp">${opt([["private", "Private"], ["unlisted", "Unlisted"], ["public", "Public"]], s.default_privacy)}</select></label></div>
          <div class="row wrap"><label class="switch grow"><input type="checkbox" id="cu" ${s.caption_uppercase ? "checked" : ""}> UPPERCASE captions</label>
          <label class="field" style="flex-direction:row;align-items:center;gap:10px">Caption &amp; sticker colour <input type="color" id="ccol" value="${esc(s.caption_color)}"></label></div>
        </div>
        <div><button class="btn primary">Save settings</button></div>
      </form>
      <div class="card stack" style="margin-top:20px"><div class="row wrap"><h2 class="grow" style="margin:0">Diagnostics</h2>
        <button class="btn sm" id="doc">Run checks</button><button class="btn sm" id="docd" title="Also verifies the API key with Anthropic">Deep check</button></div>
        <div id="docout" class="dim" style="font-size:12.5px">Checks FFmpeg, GPU encoding and transcription, Claude, disk space, the video downloader, the OBS link, publishing and learning.</div></div></div>`;
    const runDoctor = async (deep) => {
      $("#docout").innerHTML = `<span class="spin"></span> Checking…`;
      try {
        const res = await api(`/api/doctor${deep ? "?deep=true" : ""}`);
        const P = { ok: ["ok", "OK"], warn: ["warn", "Check"], fail: ["err", "Problem"] };
        $("#docout").innerHTML = `<table style="width:100%"><tbody>${res.map((r) => `<tr><td style="width:96px">${pill(P, r.status)}</td><td style="width:150px"><b>${esc(r.name)}</b></td><td>${esc(r.detail)}${r.fix ? `<div class="dim" style="font-size:11.5px">→ ${esc(r.fix)}</div>` : ""}</td></tr>`).join("")}</tbody></table>`;
      } catch (e) { $("#docout").textContent = e.message; }
    };
    $("#doc").onclick = () => runDoctor(false);
    $("#docd").onclick = () => runDoctor(true);
    $("#rmkey")?.addEventListener("click", async () => { await api("/api/settings", { method: "PUT", body: { anthropic_api_key: "" } }).catch(fail); toast("Key removed."); sideStatus(); route(); });
    $("#sf").onsubmit = async (e) => {
      e.preventDefault();
      const body = {
        model: $("#model").value, whisper_model: $("#wm").value, whisper_device: $("#wd").value, clip_count: +$("#cc").value, clip_min: +$("#cmin").value,
        clip_max: +$("#cmax").value, default_aspect: $("#da").value, default_privacy: $("#dp").value, caption_uppercase: $("#cu").checked, caption_color: $("#ccol").value,
      };
      if (body.clip_min >= body.clip_max) return toast("Min length must be below max length.", "err");
      if ($("#key").value.trim()) body.anthropic_api_key = $("#key").value.trim();
      try { S.settings = await api("/api/settings", { method: "PUT", body }); toast("Settings saved.", "ok"); sideStatus(); route(); } catch (err) { fail(err); }
    };
  };
}

// ---------- boot ----------
sideStatus().then(route);
setInterval(sideStatus, 5000);
if (localStorage.lastProject) $("#nav-editor").hidden = false;
