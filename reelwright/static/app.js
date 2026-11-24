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
  if (!r.ok) throw Object.assign(new Error(typeof data.detail === "string" ? data.detail : `Request failed (${r.status})`), { status: r.status });
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
const UPLOAD_STATUS = { held: ["warn", "Held (Autopilot paused)"], waiting_render: ["info", "Rendering first"], scheduled: ["warn", "Waiting to post"], queued: ["info", "Queued"], uploading: ["run", "Uploading"], done: ["ok", "Posted"], failed: ["err", "Failed"], canceled: ["", "Canceled"], needs_auth: ["warn", "Needs connection"] };
const JOB_STATUS = { queued: ["info", "Queued"], running: ["run", "Running"], done: ["ok", "Done"], failed: ["err", "Failed"], canceled: ["", "Canceled"] };
const JOB_LABEL = { analyze: "Analyze video", import_url: "Import video", find: "AI clip search", export: "Render clip", upload: "Publish", scan_source: "Scan channel", virality: "Virality analysis", train: "Train style profile", research: "Live platform research", results: "Fetch post results", ingest_file: "Import from OBS", track: "Track subject" };
const PLATFORM = { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram" };
const platTag = (p) => `<span class="plat ${p}">${PLATFORM[p] || esc(p)}</span>`;
const SIZES = { "9:16": [1080, 1920], "1:1": [1080, 1080], "4:5": [1080, 1350], "16:9": [1920, 1080] };
const CAPTION_POS = { low: [0.24, 0.08], middle: [0.42, 0.40], high: [0.68, 0.72] };
const CATEGORIES = { 1: "Film & Animation", 2: "Autos & Vehicles", 10: "Music", 15: "Pets & Animals", 17: "Sports", 19: "Travel & Events", 20: "Gaming", 22: "People & Blogs", 23: "Comedy", 24: "Entertainment", 25: "News & Politics", 26: "Howto & Style", 27: "Education", 28: "Science & Technology" };

// Find-bar length presets: "target/max" seconds (clips aim for the target, never exceed the max); "" = Settings defaults
const FIND_LENGTHS = [["", "Length: Settings"], ["15/20", "≈15 s · max 20"], ["30/45", "≈30 s · max 45"], ["45/60", "≈45 s · max 60"], ["60/90", "≈60 s · max 90"], ["90/180", "≈90 s · max 3 min"]];
const lsGet = (k) => { try { return localStorage[k]; } catch { return undefined; } };
const lsSet = (k, v) => { try { localStorage[k] = v; } catch { /* private mode: just don't remember */ } };

const S = { settings: null, conn: null, timers: [], nav: 0, ui: { app: 1, sidebar: 1, text: 1, preview: 1, timeline: 1, panels: 1 } };

// Pointer position in an element's own CSS pixels (correct even when an ancestor is scaled with CSS zoom).
const localXY = (ev, el) => { const r = el.getBoundingClientRect(), k = r.width / (el.offsetWidth || r.width || 1) || 1; return [(ev.clientX - r.left) / k, (ev.clientY - r.top) / k]; };

// ---------- interface scale (saved in settings.ui_scale; applied as CSS variables) ----------
const UI_KEYS = { app: "Whole app", sidebar: "Sidebar", text: "Text size", preview: "Video preview", timeline: "Timelines", panels: "Panel spacing" };
// Base size adapts to the window (small laptop → slightly smaller, 1440p/4K-class CSS widths → larger); the saved per-part scale multiplies on top.
const uiAuto = () => Math.min(Math.max(innerWidth / 1600, 0.9), 1.4);
function applyUi(ui = S.ui) {
  S.ui = Object.fromEntries(Object.keys(UI_KEYS).map((k) => [k, Math.min(Math.max(+ui?.[k] || 1, 0.6), 2)]));
  const auto = uiAuto(), root = document.documentElement.style, sb = S.ui.sidebar * auto;
  // the sidebar folds to icons when the window is narrow or the full sidebar would take more than a fifth of it
  const icons = innerWidth <= 1180 || 228 * sb > innerWidth * 0.2;
  document.body.classList.toggle("side-icons", icons);
  // ponytail: the applied zoom keeps >= ~1000x600 CSS px for the main view so layouts never run out of room; the saved value is untouched
  const room = Math.max(0.6, Math.min((innerWidth - (innerWidth <= 720 ? 0 : (icons ? 64 : 228) * sb)) / 1000, innerHeight / 600));
  const eff = { ...S.ui, app: Math.min(S.ui.app * auto, Math.max(room, Math.min(S.ui.app * auto, 1))), sidebar: sb };
  for (const [k, v] of Object.entries(eff)) root.setProperty(`--ui-${k}`, +v.toFixed(3));
}
applyUi();
let uiResize = 0;
window.addEventListener("resize", () => { clearTimeout(uiResize); uiResize = setTimeout(() => applyUi(), 150); });
function saveUi() {
  clearTimeout(S.uiTimer);
  S.uiTimer = setTimeout(() => api("/api/settings", { method: "PUT", body: { ui_scale: S.ui } }).then((s) => (S.settings = s)).catch(fail), 500);
}
document.addEventListener("keydown", (e) => { // Ctrl+= / Ctrl+- / Ctrl+0: scale the whole app (saved like the Settings sliders)
  if (!(e.ctrlKey || e.metaKey) || e.altKey || !["=", "+", "-", "_", "0"].includes(e.key)) return;
  e.preventDefault();
  const v = e.key === "0" ? 1 : Math.round((S.ui.app + (e.key === "-" || e.key === "_" ? -0.05 : 0.05)) * 100) / 100;
  const css = () => getComputedStyle(document.documentElement).getPropertyValue("--ui-app"), before = css();
  applyUi({ ...S.ui, app: v });
  if (["=", "+"].includes(e.key) && css() === before) { // capped by the window: don't save a size that isn't shown
    applyUi({ ...S.ui, app: Math.round((v - 0.05) * 100) / 100 }); return toast("That's the largest size that fits this window.", "info", 1500); }
  saveUi();
  $$("[data-ui=app]").forEach((r) => { r.value = Math.round(S.ui.app * 100); r.nextElementSibling.textContent = r.value + "%"; });
  toast(`Interface ${Math.round(S.ui.app * 100)}%`, "info", 1200);
});

// ---------- text styles: font picker + style controls (captions, title, sticker, text overlays) ----------
// Sizes are output pixels (1080-wide frame); k = on-screen px per output px.
const FONT_FALLBACK = ["Arial", "Arial Black", "Bahnschrift", "Calibri", "Cambria", "Candara", "Comic Sans MS", "Consolas", "Constantia", "Corbel", "Courier New",
  "Franklin Gothic Medium", "Gabriola", "Georgia", "Impact", "Lucida Console", "Palatino Linotype", "Segoe UI", "Segoe UI Black", "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana"];
// Font flavours by name (only fonts the backend reports, so the export can render every one).
const FONT_CATS = [["all", "All"], ["featured", "Featured"], ["bold", "Bold / Impact"], ["sans", "Clean sans"], ["serif", "Elegant serif"], ["script", "Handwritten"], ["display", "Retro / Display"], ["mono", "Mono / Tech"], ["other", "Other"]];
const FONT_FEATURED = ["Impact", "Anton", "Bebas Neue", "Montserrat", "Poppins", "Oswald", "Bahnschrift", "Segoe UI", "Arial", "Franklin Gothic", "Franklin Gothic Medium", "Verdana", "Trebuchet MS", "Futura", "Gill Sans MT", "Century Gothic", "Rockwell", "Georgia", "Comic Sans MS", "Segoe Print", "Ink Free", "Lucida Console", "Haettenschweiler", "Cooper Black", "Showcard Gothic"];
const FLAVOR = [
  ["mono", /mono|consol|courier|code|terminal|lucida console|fixedsys|ocr|typewriter|cascadia/i],
  ["script", /script|brush|hand|marker|comic|\bpen\b|\bink\b|chalk|signature|kristen|segoe print|gabriola|mistral|vivaldi|freestyle|pacifico|lobster|cursive|calligra|edwardian|french|pristina|viner|bradley|harlow|jokerman|curlz|papyrus|tempus|gigi|kunstler|rage|palace/i],
  ["display", /retro|display|stencil|neon|pixel|8.?bit|arcade|western|broadway|cooper|chiller|showcard|algerian|bauhaus|harrington|ravie|snap|magneto|old english|blackadder|engravers|castellar|copperplate|goudy stout|poplar|playbill|wide latin|elephant|niagara|matura|onyx|bernard|jazz|outline|shadow|deco|wild|ravie|forte|hobo/i],
  ["bold", /impact|anton|bebas|black|heavy|condensed|compressed|ultra|extra ?bold|poster|haettenschweiler|oswald|league gothic|fat|\bbold\b|thick/i],
  ["serif", /(?<!sans )serif|times|georgia|garamond|cambria|constantia|palatino|book antiqua|bodoni|didot|baskerville|caslon|playfair|merriweather|roman|schoolbook|rockwell|perpetua|sylfaen|bookman|centaur|footlight|high tower|californian|lora|crimson|elephant|modern no|calisto/i],
  ["sans", /sans|arial|segoe|calibri|verdana|tahoma|helvetica|inter\b|roboto|lato|montserrat|poppins|candara|corbel|trebuchet|bahnschrift|franklin|gill|futura|century gothic|nunito|source|ubuntu|noto|dejavu|aptos|selawik|myriad|avenir|raleway|quicksand|tw cen|gothic/i],
];
const fontFlavor = (fam) => FLAVOR.find(([, re]) => re.test(fam))?.[0] || "other";
const featuredFonts = (pool) => FONT_FEATURED.map((n) => pool.find((f) => (f.lc || f.family.toLowerCase()) === n.toLowerCase())).filter(Boolean);
const TS_KEYS = ["font", "size", "weight", "color", "outline", "outline_color", "shadow", "spacing"];
const famCss = (f) => `"${String(f || "").replace(/["\\\n\r;{}<>]/g, "")}"`;
const hexOr = (v, d) => (/^#[0-9a-f]{6}$/i.test(v || "") ? v : d);
const loadedFaces = new Set();
let fontsP = null;
function loadFonts(force = false) {
  if (force) fontsP = null;
  return (fontsP ||= api("/api/fonts").catch(() => FONT_FALLBACK.map((family) => ({ family, source: "system" }))).then((list) => {
    const seen = new Map();
    for (const f of Array.isArray(list) ? list : []) {
      const key = String(f?.family || "").toLowerCase();
      if (!key) continue;
      if (seen.has(key)) { const g = seen.get(key); if (f.style && !g.styles.includes(f.style)) g.styles.push(f.style); if (f.source === "uploaded") { g.source = "uploaded"; g.url = f.url; } }
      else seen.set(key, { family: f.family, source: f.source, url: f.url, lc: key, flavor: fontFlavor(f.family), styles: f.style ? [f.style] : [] }); // Pillow names "Arial Black" family Arial, style Black: one row per family
      if (f.source === "uploaded" && /^\/assets\/[\w./-]+$/.test(f.url || "") && !loadedFaces.has(f.url)) { // uploaded fonts: load by URL so the picker and preview can draw them
        loadedFaces.add(f.url);
        const face = new FontFace(f.family, `url("${f.url}")`);
        document.fonts.add(face); face.load().catch(() => {});
      }
    }
    return [...seen.values()].sort((a, b) => a.family.localeCompare(b.family));
  }));
}
function tsDefaults(kind, e, OW, OH) { // mirrors media.build_ass defaults
  const fs = Math.round(Math.min(OW, OH) * (OH > OW ? 0.078 : 0.06)), clean = e?.caption_style === "clean";
  const base = { font: "Arial Black", size: fs, weight: "bold", color: "#FFFFFF", outline: Math.max(Math.floor(fs / 12), 3), outline_color: "#000000", shadow: 2, spacing: 0 };
  if (kind === "caption") return { ...base, font: clean ? "Arial" : "Arial Black", size: Math.round(fs * (clean ? 0.9 : 1)), outline: clean ? 2 : base.outline, shadow: clean ? 2 : 3 };
  if (kind === "title") return { ...base, size: Math.round(fs * 0.8), outline: 0, shadow: 0 };
  if (kind === "sticker") return { ...base, size: Math.round(fs * 0.55), color: "#111111", outline: 0, shadow: 0 };
  return base;
}
const tsOf = (get, def) => Object.fromEntries(TS_KEYS.map((k) => [k, get(k) ?? def[k]]));
function styleText(el, st, k) {
  if (!el) return;
  Object.assign(el.style, {
    fontFamily: `${famCss(st.font)}, Inter, sans-serif`, fontSize: (+st.size || 40) * k + "px", fontWeight: st.weight === "normal" ? "400" : "800",
    color: hexOr(st.color, "#FFFFFF"), letterSpacing: (+st.spacing || 0) * k + "px", paintOrder: "stroke fill",
    webkitTextStroke: +st.outline > 0 ? `${st.outline * 2 * k}px ${hexOr(st.outline_color, "#000000")}` : "0",
    textShadow: +st.shadow > 0 ? `${st.shadow * k}px ${st.shadow * k}px 0 #000a` : "none",
  });
}
// One editor for a text style. get(k)/set(k, v) read and write the suffix keys (font, size…); v = null resets to the default.
function textStyleEditor(box, { get, set, def, sample = "Sample text" }) {
  if (!box) return;
  const cur = () => tsOf(get, def);
  const rng = (k, l, min, max) => `<label class="slider"><span>${l}</span><input type="range" data-ts="${k}" min="${min}" max="${max}" step="1" value="${+cur()[k]}"><output>${+cur()[k]}</output></label>`;
  const paint = () => styleText($(".ts-sample span", box), cur(), 0.4);
  const draw = () => {
    const s = cur();
    box.innerHTML = `<div class="ts-sample"><span>${esc(sample)}</span></div>
      <div class="fp"><button type="button" class="fp-btn" data-fp="open" title="Choose a font" style="font-family:${esc(famCss(s.font))}, Inter">${esc(s.font)} ▾</button>
        <div class="fp-pop" hidden><div class="row"><input type="search" class="fp-q grow" placeholder="Search fonts" aria-label="Search fonts">
            <label class="btn xs" title="Adds the font to your media library">⬆ Upload .ttf / .otf<input type="file" class="fp-up" accept=".ttf,.otf" hidden></label></div>
          <div class="fp-cats" role="tablist">${FONT_CATS.map(([k, l]) => `<button type="button" role="tab" data-fcat="${k}" class="${(box._cat || "all") === k ? "on" : ""}">${l}</button>`).join("")}</div>
          <div class="fp-list" role="listbox" aria-label="Fonts" tabindex="-1"><span class="spin"></span></div></div></div>
      ${rng("size", "Size", 24, 200)}
      <div class="row wrap"><div class="seg">${[["normal", "Normal"], ["bold", "Bold"]].map(([v, l]) => `<button type="button" class="${s.weight === v ? "on" : ""}" data-fw="${v}">${l}</button>`).join("")}</div>
        <label class="field" style="flex-direction:row;align-items:center;gap:6px">Colour <input type="color" data-ts="color" value="${esc(hexOr(s.color, "#FFFFFF"))}"></label>
        <label class="field" style="flex-direction:row;align-items:center;gap:6px">Outline <input type="color" data-ts="outline_color" value="${esc(hexOr(s.outline_color, "#000000"))}"></label></div>
      ${rng("outline", "Outline", 0, 20)}${rng("shadow", "Shadow", 0, 20)}${rng("spacing", "Spacing", -5, 20)}
      <div><button type="button" class="btn xs ghost" data-fp="reset">↺ Reset style</button></div>`;
    paint();
  };
  // Gallery: sections by flavour, each card draws the sample in its own font and the current colour/outline.
  // ponytail: sections show 24 cards and grow by 48 on demand ("Show more"); a big font folder (thousands) never renders at once.
  const fill = async () => {
    const list = await loadFonts(), q = ($(".fp-q", box)?.value || "").trim().toLowerCase(), el = $(".fp-list", box);
    if (!el) return;
    const s = cur(), cat = box._cat || "all", shown = (box._shown ||= {});
    const look = `color:${hexOr(s.color, "#FFFFFF")};-webkit-text-stroke:${Math.min(+s.outline || 0, 6) * 0.35}px ${hexOr(s.outline_color, "#000000")};paint-order:stroke fill;font-weight:${s.weight === "normal" ? 400 : 800}`;
    const card = (f) => `<button type="button" role="option" class="fp-card" aria-selected="${f.family === s.font}" data-fam="${esc(f.family)}" title="${esc(f.family)}${f.styles?.length > 1 ? " — " + esc(f.styles.join(", ")) : ""}">
        <span class="fp-s" style="font-family:${esc(famCss(f.family))}, Inter;${look}">THIS IS THE CLIP</span>
        <span class="fp-n">${esc(f.family)}${f.source === "uploaded" ? ' <b class="fp-tag">yours</b>' : ""}</span></button>`;
    const pool = list.filter((f) => !q || f.lc.includes(q));
    const groups = FONT_CATS.filter(([k]) => k !== "all" && (cat === "all" || cat === k)).map(([k, l]) => [k, l, k === "featured" ? featuredFonts(pool) : pool.filter((f) => f.flavor === k)]).filter(([, , g]) => g.length);
    el.innerHTML = groups.map(([k, l, g]) => { const n = shown[k] || (cat === "all" ? 12 : 24);
      return `<div class="fp-sec" data-k="${k}"><h4>${l} <span class="dim">${g.length}</span></h4><div class="fp-grid">${g.slice(0, n).map(card).join("")}</div>
        ${g.length > n ? `<button type="button" class="btn xs ghost fp-more" data-fmore="${k}">Show ${Math.min(48, g.length - n)} more</button>` : ""}</div>`; }).join("")
      || `<div class="dim" style="padding:10px">No installed fonts match “${esc(q)}”.</div>`;
  };
  box.oninput = (e) => {
    e.stopPropagation();
    const t = e.target;
    if (t.classList.contains("fp-q")) { box._shown = {}; clearTimeout(box._qT); box._qT = setTimeout(fill, 120); return; }
    if (!t.dataset.ts) return;
    if (t.type === "range") t.nextElementSibling.textContent = t.value;
    set(t.dataset.ts, t.type === "range" ? +t.value : t.value); paint();
  };
  box.onchange = async (e) => {
    e.stopPropagation();
    const t = e.target;
    if (!t.classList.contains("fp-up") || !t.files[0]) return;
    const f = t.files[0], fd = new FormData();
    fd.append("file", f); fd.append("name", f.name.replace(/\.[^.]+$/, "")); fd.append("tags", "font");
    try {
      const a = await api("/api/assets", { body: fd }), list = await loadFonts(true);
      const fam = list.find((x) => x.url === `/assets/${a.file}`)?.family || a.family;
      if (fam) set("font", fam);
      draw(); toast(`Font added${fam ? ` — ${fam}` : ""}.`, "ok");
    } catch (err) { fail(err); }
  };
  box.onkeydown = (e) => { // gallery keys: arrows move between cards, Escape closes, Enter/Space pick (native buttons)
    const pop = $(".fp-pop", box);
    if (e.key === "Escape" && pop && !pop.hidden) { e.stopPropagation(); pop.hidden = true; return $(".fp-btn", box).focus(); }
    if (!e.target.classList.contains("fp-card") && !(e.target.classList.contains("fp-q") && e.key === "ArrowDown")) return;
    const cards = $$(".fp-card", box), i = cards.indexOf(e.target), cols = 2;
    const j = { ArrowRight: i + 1, ArrowLeft: i - 1, ArrowDown: i < 0 ? 0 : i + cols, ArrowUp: i - cols, Home: 0, End: cards.length - 1 }[e.key];
    if (j == null) return;
    e.preventDefault(); e.stopPropagation();
    cards[Math.min(Math.max(j, 0), cards.length - 1)]?.focus();
  };
  box.onclick = (e) => {
    e.stopPropagation();
    const t = e.target.closest("[data-fp],[data-fam],[data-fw],[data-fcat],[data-fmore]");
    if (!t) return;
    if (t.dataset.fp === "open") { const p = $(".fp-pop", box); p.hidden = !p.hidden; if (!p.hidden) { fill(); $(".fp-q", box).focus(); } return; }
    if (t.dataset.fcat) { box._cat = t.dataset.fcat; box._shown = {}; $$("[data-fcat]", box).forEach((b) => b.classList.toggle("on", b === t)); return fill(); }
    if (t.dataset.fmore) { const k = t.dataset.fmore; box._shown = { ...box._shown, [k]: (box._shown?.[k] || (box._cat && box._cat !== "all" ? 24 : 12)) + 48 };
      const sec = () => $$(".fp-sec", box).find((x) => x.querySelector(`[data-fmore="${k}"]`) || x.dataset.k === k), n = $$(".fp-card", sec()).length;
      return fill().then(() => $$(".fp-card", sec())[n]?.focus()); }
    if (t.dataset.fp === "reset") TS_KEYS.forEach((k) => set(k, null));
    if (t.dataset.fam) set("font", t.dataset.fam);
    if (t.dataset.fw) set("weight", t.dataset.fw);
    draw();
  };
  draw();
}
document.addEventListener("mousedown", (e) => $$(".fp-pop:not([hidden])").forEach((p) => { if (!p.parentElement.contains(e.target)) p.hidden = true; }));
// Caption chunks as the renderer groups them: up to 3 words (vertical) or 6, broken at pauses and sentence ends.
function chunkWords(words, vertical) {
  const max = vertical ? 3 : 6, out = [];
  let cur = [];
  for (const w of words.filter((x) => String(x.w).trim())) {
    if (cur.length && (cur.length >= max || w.s - cur.at(-1).e > 0.6 || /[.?!]$/.test(String(cur.at(-1).w).trim()))) { out.push(cur); cur = []; }
    cur.push(w);
  }
  if (cur.length) out.push(cur);
  return out;
}

// Coalesced scrubbing: at most one seek per animation frame; fastSeek (where the browser has it) while dragging.
function scrubber(getVideo) {
  let want = null, raf = 0;
  const go = () => { raf = 0; const v = getVideo(); if (!v || !want) return; if (want.fast && v.fastSeek) v.fastSeek(want.t); else v.currentTime = want.t; want = null; };
  return (t, fast = false) => { want = { t, fast }; if (!raf) raf = requestAnimationFrame(go); };
}
// Timeline hover tooltip: time + the project's sprite thumbnail (sprite.jpg = sprite_count tiles of 160x90, evenly spaced).
function tlTip(tip, x, t, pid, proj) {
  if (!tip) return;
  const n = proj?.sprite_count, d = proj?.meta?.duration;
  const k = S.ui.timeline;
  tip.hidden = false;
  tip.style.left = x + "px";
  tip.innerHTML = `${n && d ? `<div class="tl-thumb" style="background-image:url('/files/${esc(pid)}/sprite.jpg');width:${160 * k}px;height:${90 * k}px;background-size:${n * 160 * k}px ${90 * k}px;background-position:-${Math.max(0, Math.min(n - 1, Math.floor((t / d) * n))) * 160 * k}px 0"></div>` : ""}<span>${fmt(t)}</span>`;
}

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
const routes = { library: Library, pipeline: Pipeline, editor: Editor, studio: Studio, sources: Sources, obs: Obs, lab: Lab, publish: Publish, schedule: Schedule, jobs: Jobs, settings: Settings };
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
  if (name === "studio" && !arg) return (location.hash = localStorage.lastClip ? `#/studio/${localStorage.lastClip}` : "#/library");
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
    if (!S.uiLoaded) { S.uiLoaded = true; applyUi(settings.ui_scale); } // once at boot; afterwards this page is the source of truth
    const running = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
    const b = $("#jobs-badge"); b.hidden = !running; b.textContent = running;
    const row = (ok, label, text, href) => `<div class="row"><span class="pill ${ok ? "ok" : "warn"}">${label}</span><span>${ok ? esc(text) : `<a href="${href}">connect</a>`}</span></div>`;
    $("#side-status").innerHTML = `<button class="btn sm ${settings.autopilot_paused ? "danger" : ""}" id="ap-pause" style="width:100%;margin-bottom:8px"
        title="${settings.autopilot_paused ? "Autopilot is paused everywhere: nothing new is scheduled and its queued posts are held. Click to resume." : "Pause Autopilot everywhere: stops new scheduling and holds its queued posts (nothing is deleted)."}">
        ${settings.autopilot_paused ? "▶ Resume Autopilot" : "⏸ Pause Autopilot"}</button>`
      + row(settings.anthropic_key_set, "Claude AI", "connected", "#/settings")
      + row(conn.youtube.connected, "YouTube", conn.youtube.channel?.title || "connected", "#/publish")
      + row(conn.tiktok.connected, "TikTok", conn.tiktok.name || "connected", "#/publish")
      + row(conn.instagram.connected, "Instagram", conn.instagram.name || "connected", "#/publish");
  } catch { /* server restarting */ }
}

// ---------- autopilot form (shared) ----------
const AP_DEFAULT = { enabled: false, top_n: 3, first_publish: "", interval_hours: 24, privacy: "public", aspect: "9:16", captions: true, jump_cuts: false, mode: "publish_at", platforms: ["youtube"], timing: "best" };
const apLoad = () => { try { return { ...AP_DEFAULT, ...JSON.parse(localStorage.autopilot || "{}") }; } catch { return { ...AP_DEFAULT }; } };
function apForm(ap, withToggle = true) {
  return `
  ${withToggle ? `<div class="row"><h2 class="grow" style="margin:0">⚡ Autopilot</h2><label class="switch"><input type="checkbox" id="ap-enabled" ${ap.enabled ? "checked" : ""}></label></div>
  <p class="muted" style="margin:6px 0 14px;font-size:12.5px">When the scan finishes, the top clips are rendered and scheduled to your channels automatically.</p>` : ""}
  <div class="ap-body stack">
    <div class="plats">${Object.entries(PLATFORM).map(([k, v]) => `<label><input type="checkbox" class="ap-plat" value="${k}" ${ap.platforms.includes(k) ? "checked" : ""}> ${v}</label>`).join("")}</div>
    <label class="field">Clips to publish<input type="number" id="ap-top" min="1" max="30" value="${ap.top_n}"></label>
    <label class="field">When<select id="ap-timing">
      <option value="best" ${ap.timing !== "fixed" ? "selected" : ""}>Best times for each platform (learned from your results + live research)</option>
      <option value="fixed" ${ap.timing === "fixed" ? "selected" : ""}>My own schedule: a start time and a fixed interval</option></select></label>
    <div class="grid2 ap-fixed" ${ap.timing === "fixed" ? "" : "hidden"}>
      <label class="field">First one goes live<input type="datetime-local" id="ap-first" value="${ap.first_publish || tomorrowAt9()}"></label>
      <label class="field">Every (hours)<input type="number" id="ap-int" min="1" max="720" step="1" value="${ap.interval_hours}"></label>
    </div>
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
    mode: $("#ap-mode", root).value, captions: $("#ap-cap", root).checked, jump_cuts: $("#ap-jc", root).checked, timing: $("#ap-timing", root).value,
    platforms: $$(".ap-plat", root).filter((c) => c.checked).map((c) => c.value),
  };
  // only the Library card's own switch changes whether Autopilot is on for new imports
  localStorage.autopilot = JSON.stringify({ ...ap, enabled: $("#ap-enabled", root) ? ap.enabled : apLoad().enabled });
  return { ...ap, first_publish: fromLocalInput(ap.first_publish) };
}

document.addEventListener("change", (e) => {  // Autopilot form: show start/interval only for "my own schedule"
  if (e.target.id === "ap-timing") e.target.closest(".ap-body")?.querySelector(".ap-fixed")?.toggleAttribute("hidden", e.target.value !== "fixed");
});

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
// On-screen action heat strip (signals.visual from the analyzer), drawn once into a 1-px-tall canvas the timeline scales:
// orange = motion relative to this video's busiest moments, white = flash, cyan = on-cam reaction.
function visualStrip(v) {
  const m = v?.motion || [];
  if (!m.length) return null;
  const per = Math.ceil(m.length / 16000), n = Math.ceil(m.length / per); // stay under canvas width limits on long VODs
  const sorted = [...m].sort((a, b) => a - b), top = sorted[Math.floor(sorted.length * 0.98)] || sorted.at(-1) || 1;
  const c = Object.assign(document.createElement("canvas"), { width: n, height: 1 }), g = c.getContext("2d"), img = g.createImageData(n, 1);
  for (let i = 0; i < n; i++) {
    let x = 0; for (let j = i * per; j < (i + 1) * per && j < m.length; j++) x = Math.max(x, m[j]);
    const a = Math.min(1, x / (top || 1));
    img.data.set([255, 106 + 80 * (1 - a), 61, Math.round(255 * a * a)], i * 4);
  }
  for (const s of v.spikes || []) {
    const i = Math.min(n - 1, Math.floor(s.t / per));
    if (s.kind === "flash") img.data.set([255, 255, 255, 255], i * 4);
    else if (s.kind === "face_reaction") img.data.set([80, 220, 255, 255], i * 4);
  }
  g.putImageData(img, 0, 0);
  c.secs = n * per;
  return c;
}

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
    E.vstrip = visualStrip(sig.visual);
  }

  function layout() {
    const p = E.proj, ready = p.status === "ready";
    view.innerHTML = `<div class="editor">
      <div class="ed-top">
        <a class="btn ghost sm" href="#/library">← Library</a>
        <div class="title" title="${esc(p.name)} — click to rename" data-act="rename" style="cursor:text">${esc(p.name)}</div>
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
        <div class="timeline"><div class="tl-tip" id="tl-tip" hidden></div>
          <div class="tl-scroll" id="tls"><div class="tl-inner" id="tli"><div class="tl-sprite" id="spr"></div><canvas id="cv"></canvas></div></div>
          <div class="tl-foot"><span>Zoom</span><button class="btn xs" data-act="zout" title="Zoom out (Ctrl+wheel)">−</button><input type="range" id="zoom" min="1" max="40" step="0.5" value="${E.zoom}" style="width:min(160px,30vw)"><button class="btn xs" data-act="zin" title="Zoom in (Ctrl+wheel)">+</button>
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
    const ts = (kind) => tsOf((k) => e?.[`${kind}_${k}`], tsDefaults(kind, e, OW, OH));
    let mv = CAPTION_POS[e?.caption_pos || "low"][vertical ? 0 : 1] * OH;
    if (e?.layout === "split" && e.caption_pos === "middle") mv = (1 - e.split - 0.07) * OH;
    const cp = $("#cprev");
    styleText(cp, ts("caption"), scale);
    cp.style.bottom = Math.max(mv * scale, 4) + "px";
    cp.style.background = e?.caption_style === "boxed" ? "#0009" : "";
    const tp = $("#tprev"); tp.hidden = !e?.title_text; tp.textContent = e?.title_text || ""; styleText(tp, ts("title"), scale);
    const sp = $("#sprev"); sp.hidden = !e?.sticker_text; sp.textContent = e?.sticker_text || "";
    (cam.hidden ? box : cam).append(sp); // in split layout the top (facecam) panel is the top of the output frame
    if (e?.sticker_text) {
      styleText(sp, ts("sticker"), scale); sp.style.background = S.settings?.caption_color || "#FFD400";
      const pos = e.sticker_pos || "top-right";
      Object.assign(sp.style, { top: pos.startsWith("top") ? "5%" : "", bottom: pos.startsWith("bottom") ? "5%" : "",
        left: pos.endsWith("left") ? "4%" : pos.endsWith("center") ? "50%" : "", right: pos.endsWith("right") ? "4%" : "",
        transform: pos.endsWith("center") ? "translateX(-50%)" : "" });
    }
    updateCaption();
  }

  function captionChunks(c) {
    const vertical = SIZES[c.edit.aspect] && SIZES[c.edit.aspect][1] > SIZES[c.edit.aspect][0];
    return chunkWords(Array.isArray(c.edit.words) ? c.edit.words : E.words.filter((w) => w.s >= c.start - 0.05 && w.e <= c.end + 0.05), vertical);
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
  const scrub = scrubber(() => video), fstep = () => 1 / (E.proj?.meta?.fps || 30);

  // ----- timeline -----
  let RULER, SPR_Y, SPR_H, WAVE_Y, WAVE_H, CLIP_Y, CLIP_H, TLH, TK = 1; // scaled by the Timelines interface size
  const tlScale = () => { TK = S.ui.timeline; [RULER, SPR_Y, SPR_H, WAVE_Y, WAVE_H, CLIP_Y, CLIP_H, TLH] = [16, 18, 44, 66, 40, 110, 14, 128].map((v) => v * TK); };
  tlScale();
  let pps = 1;
  function sizeTimeline() {
    const scroll = $("#tls");
    if (!scroll || !dur()) return;
    tlScale();
    const base = scroll.clientWidth / dur();
    pps = Math.min(base * E.zoom, 30000 / dur());
    const w = Math.max(scroll.clientWidth, Math.round(dur() * pps));
    $("#tli").style.width = w + "px";
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr; canvas.height = TLH * dpr; canvas.style.width = w + "px"; canvas.style.height = TLH + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }
  function draw() {
    if (!ctx || !dur()) return;
    const w = canvas.width / (window.devicePixelRatio || 1);
    ctx.clearRect(0, 0, w, TLH);
    const step = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800].find((s) => s * pps > 70 * TK) || 3600;
    ctx.fillStyle = "#5d6178"; ctx.font = `${10 * TK}px JetBrains Mono, monospace`;
    for (let t = 0; t <= dur(); t += step) { const x = t * pps; ctx.fillRect(x, 10 * TK, 1, 6 * TK); ctx.fillText(fmt(t, 0), x + 3, 9 * TK); }
    ctx.fillStyle = "#6aa8ff88";
    (E.signals.scenes || []).forEach((t) => ctx.fillRect(t * pps, SPR_Y, 1, SPR_H));
    if (E.vstrip) ctx.drawImage(E.vstrip, 0, 0, E.vstrip.width, 1, 0, SPR_Y + SPR_H, E.vstrip.secs * pps, 4 * TK);
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
      if (cw > 50) { ctx.fillStyle = sel ? "#1b0b04" : "#eceef6cc"; ctx.font = `600 ${10 * TK}px Inter, sans-serif`; ctx.fillText(c.title.slice(0, Math.floor(cw / 6 / TK)), x + 5, CLIP_Y + 10.5 * TK); }
    }
    const c = selClip();
    if (c && !c.parts) {
      const x = c.start * pps, cw = (c.end - c.start) * pps;
      ctx.fillStyle = "#ff6a3d14"; ctx.fillRect(x, RULER, cw, TLH - RULER);
      ctx.fillStyle = "#ff6a3d"; ctx.fillRect(x - 1.5, RULER, 3, TLH - RULER); ctx.fillRect(x + cw - 1.5, RULER, 3, TLH - RULER);
      ctx.fillRect(x - 5, RULER + 30, 10, 18); ctx.fillRect(x + cw - 5, RULER + 30, 10, 18);
    }
    ctx.fillStyle = "#37d39a";
    if (E.inMark != null) ctx.fillRect(E.inMark * pps, RULER, 2, TLH - RULER);
    if (E.outMark != null) ctx.fillRect(E.outMark * pps - 2, RULER, 2, TLH - RULER);
    if (E.inMark != null && E.outMark != null) { ctx.fillStyle = "#37d39a1f"; ctx.fillRect(E.inMark * pps, RULER, (E.outMark - E.inMark) * pps, TLH - RULER); }
    if (video) { const x = (E.scrubT ?? video.currentTime) * pps; ctx.fillStyle = "#fff"; ctx.fillRect(x - 1, 0, 2, TLH); ctx.beginPath(); ctx.moveTo(x - 6, 0); ctx.lineTo(x + 6, 0); ctx.lineTo(x, 8); ctx.fill(); }
  }
  function bindTimeline() {
    const opts = { signal: listeners.signal };
    const inner = $("#tli");
    let drag = null;
    const tAt = (ev) => localXY(ev, inner)[0] / pps;
    inner.addEventListener("mousedown", (ev) => {
      const t = tAt(ev), y = localXY(ev, inner)[1], c = selClip();
      if (c && !c.parts && Math.abs(t - c.start) * pps < 8) drag = { edge: "start" };
      else if (c && !c.parts && Math.abs(t - c.end) * pps < 8) drag = { edge: "end" };
      else if (y >= CLIP_Y - 2) {
        const hit = E.clips.find((k) => !k.parts && t >= k.start && t <= k.end);
        if (hit) { select(hit.id); return; }
        drag = { edge: "seek", cur: t, lx: t * pps }; seek(t);
      } else { drag = { edge: "seek", cur: t, lx: t * pps }; seek(t); }
    });
    window.addEventListener("mousemove", (ev) => {
      if (!drag) return;
      const lx = localXY(ev, inner)[0], t = Math.min(Math.max(lx / pps, 0), dur()), c = selClip();
      if (drag.edge === "seek") { // Shift = fine scrub (10x slower); seeks are coalesced to one per frame
        drag.cur = ev.shiftKey ? Math.min(Math.max(drag.cur + (lx - drag.lx) / pps / 10, 0), dur()) : t;
        drag.lx = lx; E.scrubT = drag.cur; scrub(drag.cur, true); return draw();
      }
      if (drag.edge === "start") c.start = Math.min(t, c.end - 0.5);
      else c.end = Math.max(t, c.start + 0.5);
      seek(drag.edge === "start" ? c.start : c.end);
      draw();
    }, opts);
    window.addEventListener("mouseup", () => {
      if (drag?.edge === "seek") { E.scrubT = null; seek(drag.cur); }
      if (drag && drag.edge !== "seek") { const c = selClip(); patchClip(c.id, { start: +c.start.toFixed(2), end: +c.end.toFixed(2) }); if (E.tab === "edit") renderTab(); }
      drag = null;
    }, opts);
    inner.addEventListener("mousemove", (ev) => {
      const t = tAt(ev), c = selClip();
      inner.style.cursor = c && !c.parts && (Math.abs(t - c.start) * pps < 8 || Math.abs(t - c.end) * pps < 8) ? "ew-resize" : localXY(ev, inner)[1] >= CLIP_Y - 2 ? "pointer" : "text";
      tlTip($("#tl-tip"), localXY(ev, $(".timeline"))[0], t, pid, E.proj);
    });
    inner.addEventListener("mouseleave", () => { $("#tl-tip").hidden = true; });
    $("#tls").addEventListener("wheel", (ev) => { if (ev.ctrlKey) { ev.preventDefault(); zoomTo(E.zoom * (ev.deltaY < 0 ? 1.25 : 0.8), ev.clientX); } }, { passive: false });
  }
  function zoomTo(z, clientX) { // keeps the time under the cursor (or the centre) in place
    const sc = $("#tls"), px = clientX == null ? sc.clientWidth / 2 : localXY({ clientX, clientY: 0 }, sc)[0], t = (sc.scrollLeft + px) / pps;
    E.zoom = Math.min(Math.max(z, 1), 40); $("#zoom").value = E.zoom; sizeTimeline(); sc.scrollLeft = t * pps - px;
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
        case "zin": zoomTo(E.zoom * 1.5); break;
        case "zout": zoomTo(E.zoom / 1.5); break;
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
          const [tg, mx] = ($("#qlen")?.value || "").split("/").map(Number);
          lsSet("findLen", $("#qlen")?.value || "");
          await api(`/api/projects/${pid}/find`, { body: { query: q, ...(mx ? { target: tg, max_len: mx } : {}) } });
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
        case "tsk": E.tsk = a.dataset.v; renderTab(); break;
        case "rename": {
          modal(`<h2>Rename project</h2><input type="text" id="rn" maxlength="200" value="${esc(E.proj.name)}"><div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="rn-ok">Rename</button></div>`, (m, close) => {
            const go = async () => {
              const name = $("#rn", m).value.trim(); if (!name) return;
              try { E.proj = { ...E.proj, ...(await api(`/api/projects/${pid}`, { method: "PATCH", body: { name } })) }; close(); $(".ed-top .title").textContent = E.proj.name; $(".ed-top .title").title = E.proj.name; toast("Renamed.", "ok"); }
              catch (err) { fail(err); }
            };
            $("#rn-ok", m).onclick = go; $("#rn", m).onkeydown = (ev) => ev.key === "Enter" && go(); $("#rn", m).select();
          });
          break;
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
        case "publish": openPublish(c, renderActions); break;
      }
    } catch (e) { fail(e); }
  }
  const markTab = () => $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === E.tab));

  function onKey(e) {
    if (e.target.closest?.("input, textarea, select") || !video || $("#modal-root").innerHTML) return;
    const c = selClip();
    if (e.code === "Space") { e.preventDefault(); E.loop = false; video.paused ? video.play() : video.pause(); }
    else if (e.key === "i" || e.key === "I") { E.inMark = video.currentTime; draw(); }
    else if (e.key === "o" || e.key === "O") { E.outMark = video.currentTime; draw(); }
    else if (e.key === "ArrowLeft") seek(video.currentTime - (e.shiftKey ? 1 : fstep()));
    else if (e.key === "ArrowRight") seek(video.currentTime + (e.shiftKey ? 1 : fstep()));
    else if (e.key === "[" && c) seek(c.start);
    else if (e.key === "]" && c) seek(c.end);
  }

  // ----- right panel -----
  function renderTab() {
    const el = $("#tab");
    if (!el) return;
    el.oninput = el.onchange = el.onkeydown = el.onmouseup = el.onclick = el.ondblclick = null;
    if (E.tab === "clips") return renderClips(el);
    if (E.tab === "transcript") return renderTranscript(el);
    return renderInspector(el);
  }

  function renderClips(el) {
    const ready = E.proj.status === "ready";
    const sorted = [...E.clips].sort((a, b) => (a.source === "manual") - (b.source === "manual") || b.score - a.score);
    const srcLabel = { search: "search", manual: "manual", ai: "AI", auto: "auto", montage: "montage" };
    el.innerHTML = `
      <div class="search"><input type="text" id="q" placeholder="Find a moment: “when chat goes crazy”" ${ready ? "" : "disabled"}>
        <select id="qlen" title="Clip length for new picks: aims for the first number, never longer than the second" ${ready ? "" : "disabled"}>${FIND_LENGTHS.map(([v, l]) => `<option value="${v}" ${v === (lsGet("findLen") || "") ? "selected" : ""}>${l}</option>`).join("")}</select>
        <button class="btn primary" data-act="find" ${ready ? "" : "disabled"}>Find</button></div>
      ${!S.settings?.anthropic_key_set ? `<div class="card" style="padding:10px 12px;margin-bottom:12px;font-size:12.5px">Clips below come from the offline heuristic. <a href="#/settings">Add a Claude API key</a> for AI-ranked clips, semantic search, the edit assistant, virality scoring and auto-written titles.</div>` : ""}
      <div class="row" style="margin-bottom:10px;font-size:12px" ${E.picked.size ? "" : "hidden"} id="pickbar"><span class="grow">${E.picked.size} selected</span><button class="btn sm primary" data-act="montage" ${E.picked.size > 1 ? "" : "disabled"}>▤ Make montage</button></div>
      ${sorted.length ? sorted.map((c) => `
        <div class="clip ${c.id === E.sel ? "sel" : ""}" data-act="select" data-id="${c.id}">
          <div class="row"><input type="checkbox" class="pick" data-id="${c.id}" title="Select for a montage" ${E.picked.has(c.id) ? "checked" : ""} ${c.parts ? "disabled" : ""}>
            <div class="t grow">${esc(c.title)}</div>${c.viral ? `<span class="vbadge" title="Virality score">✦${+c.viral.score}</span>` : ""}${c.source !== "manual" && !c.parts ? `<span class="score" title="AI pick score">${+c.score}</span>` : ""}</div>
          ${c.reason ? `<div class="why">${esc(c.reason)}</div>` : ""}
          <div class="meta wrap"><span class="mono">${c.parts ? `${c.parts.length} clips` : `${fmt(c.start)} → ${fmt(c.end)}`}</span><span>${(c.end - c.start).toFixed(1)}s</span>
            <span class="src-tag">${srcLabel[c.source] || esc(c.source)}</span>${c.features?.motion >= 2 ? `<span class="chip" title="Much more on-screen motion than usual for this video" style="font-size:11px">⚡ action</span>` : ""}${c.features?.flashes > 0 ? `<span class="chip" title="Sudden on-screen flash (explosion, jump scare, hit)" style="font-size:11px">✷ flash</span>` : ""}<span class="grow"></span>
            ${Object.entries(c.posts || {}).map(([p, r]) => (/^https:\/\//.test(r.url || "") ? `<a href="${esc(r.url)}" target="_blank" class="plat ${p}">${PLATFORM[p]}</a>` : platTag(p))).join("")}
            ${c.export?.status === "done" && c.export.rev === c.rev ? '<span title="Rendered" style="color:var(--ok)">●</span>' : ""}
            ${c.fit != null ? `<span class="chip" title="How well this matches what you've liked and posted before (learned from your feedback). AI score ${+c.ai_score}." style="font-size:11px">♥ ${+c.fit}% your taste</span>` : ""}
            <span class="rate"><button data-act="rate" data-id="${c.id}" data-v="1" class="${c.rating === 1 ? "on" : ""}" title="Good pick (trains your style profile)">👍</button><button data-act="rate" data-id="${c.id}" data-v="-1" class="${c.rating === -1 ? "on" : ""}" title="Bad pick">👎</button></span>
            ${c.parts ? "" : `<a class="btn xs primary" href="#/studio/${c.id}" title="Open in the Clip Studio (or double-click the card)">Edit clip</a>`}
            <button class="btn xs" data-act="edit" data-id="${c.id}" title="Quick inline edits in this panel">Quick</button>
            <button class="btn xs ghost danger" data-act="del" data-id="${c.id}" data-title="${esc(c.title)}">✕</button></div>
        </div>`).join("") : `<div class="empty"><b>${ready ? "No clips yet" : "Clips appear after analysis"}</b>${ready ? "Search for a moment above, or mark In/Out on the timeline." : ""}</div>`}`;
    el.onkeydown = (e) => { if (e.key === "Enter" && e.target.id === "q") $("[data-act=find]").click(); };
    el.ondblclick = (e) => { // double-click a clip card: open it in the Studio
      const card = e.target.closest(".clip[data-id]");
      if (card && !e.target.closest("button, a, input") && !E.clips.find((k) => k.id === card.dataset.id)?.parts) location.hash = `#/studio/${card.dataset.id}`;
    };
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
        <div class="row"><a class="btn sm primary" href="#/studio/${c.id}" title="Tracking, effects, captions and AI for this clip on one page">✎ Edit clip in Studio</a><button class="btn xs" data-act="viral-clip">✦ Score virality</button>${c.viral ? `<button class="btn xs" data-act="report" data-id="${c.viral.report_id}"><span class="vbadge">✦ ${+c.viral.score}</span> view report</button>` : ""}</div>
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
      <div class="sec"><h3>Text style</h3>
        <div class="seg">${[["caption", "Captions"], ["title", "Title card"], ["sticker", "Sticker"]].map(([v, l]) => `<button class="${(E.tsk || "caption") === v ? "on" : ""}" data-act="tsk" data-v="${v}">${l}</button>`).join("")}</div>
        <div id="ed-ts" class="stack" style="gap:8px"></div>
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
    const kind = E.tsk || "caption", cur = () => selClip() || c, [OW, OH] = SIZES[e.aspect] || [video?.videoWidth || 1920, video?.videoHeight || 1080];
    textStyleEditor($("#ed-ts"), { get: (k) => cur().edit[`${kind}_${k}`], set: (k, v) => patchClip(cur().id, { edit: { [`${kind}_${k}`]: v } }, 450),
      def: tsDefaults(kind, e, OW, OH), sample: kind === "title" ? e.title_text || "Title card" : kind === "sticker" ? e.sticker_text || "@yourchannel" : "Captions look like this" });
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
}

// Publish dialog for one clip (used by the Editor and the Clip Studio).
function openPublish(c, onDone) {
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
      <div class="row" id="ptime-l" hidden><label class="field grow">Date &amp; time<input type="datetime-local" id="ptime" value="${tomorrowAt9()}"></label>
        <button class="btn" id="pbest" style="align-self:flex-end" title="Best upcoming time for the first selected platform (your results → live research → defaults)">★ Best time</button></div>
      <p class="muted" id="pwhen-help" style="font-size:12px;margin:0"></p>
      <label class="switch"><input type="checkbox" id="pkids"> Made for kids (YouTube)</label>
      <div id="ptt" class="card stack" hidden style="padding:12px"></div>
    </div>
    <div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="pgo">Queue post</button></div>`, (m, close) => {
    let mode = "now";
    const help = { now: "Posts as soon as the render is ready.", publish_at: "YouTube: uploads now as private and goes public at this time. TikTok/Instagram: posted at this time (Reelwright must be running).", upload_at: "Everything stays on this computer and is posted at this time (Reelwright must be running)." };
    const setMode = (v) => { mode = v; $$("#pwhen button", m).forEach((b) => b.classList.toggle("on", b.dataset.v === v)); $("#ptime-l", m).hidden = v === "now"; $("#pwhen-help", m).textContent = help[v]; };
    setMode("now");
    $("#pwhen", m).onclick = (e) => e.target.dataset.v && setMode(e.target.dataset.v);
    $("#pbest", m).onclick = async () => {
      const plat = $$(".pp", m).find((x) => x.checked)?.value || "youtube";
      try {
        const r = await api("/api/schedule/suggest", { body: { platform: plat, category_id: $("#pcat", m).value } });
        if (r.times[0]) { $("#ptime", m).value = toLocalInput(new Date(r.times[0])); toast(`Best time for ${PLATFORM[plat]} (${r.source === "yours" ? "from your results" : r.source === "research" ? "from live research" : "default until there's data"}).`); }
      } catch (err) { fail(err); }
    };
    // TikTok's posting guidelines: show the account, let the user pick privacy (no default) and interactions (off by default,
    // greyed out when the creator disabled them), commercial-content disclosure, and TikTok's consent line.
    let tt = null;
    const ttBox = $("#ptt", m);
    const renderTT = () => {
      const on = $$(".pp", m).some((x) => x.checked && x.value === "tiktok");
      ttBox.hidden = !on;
      if (!on || !tt) return;
      if (tt.error) { ttBox.innerHTML = `<div style="color:var(--warn);font-size:12.5px">TikTok: ${esc(tt.error)}</div>`; return; }
      const LBL = { PUBLIC_TO_EVERYONE: "Everyone", MUTUAL_FOLLOW_FRIENDS: "Friends", FOLLOWER_OF_CREATOR: "Followers", SELF_ONLY: "Only me" };
      ttBox.innerHTML = `<div class="row"><b>TikTok</b><span class="dim" style="font-size:12.5px">posting as ${esc(tt.creator_nickname || tt.creator_username || "")}</span></div>
        <label class="field">Who can view this video<select id="tt-priv"><option value="" selected disabled>Choose…</option>${(tt.privacy_level_options || []).map((p) => `<option value="${esc(p)}">${LBL[p] || esc(p)}</option>`).join("")}</select></label>
        <div class="row wrap">${[["allow_comment", "Comment", tt.comment_disabled], ["allow_duet", "Duet", tt.duet_disabled], ["allow_stitch", "Stitch", tt.stitch_disabled]].map(([k, l, off]) =>
          `<label class="switch" ${off ? 'title="Turned off in your TikTok settings" style="opacity:.45"' : ""}><input type="checkbox" id="tt-${k}" ${off ? "disabled" : ""}> Allow ${l}</label>`).join("")}</div>
        <label class="switch"><input type="checkbox" id="tt-commercial"> This video promotes a brand, product or service</label>
        <div id="tt-com" class="stack" hidden style="padding-left:12px">
          <label class="switch"><input type="checkbox" id="tt-brand_organic"> Your brand <span class="dim">(labelled “Promotional content”)</span></label>
          <label class="switch"><input type="checkbox" id="tt-brand_content"> Branded content <span class="dim">(labelled “Paid partnership”; can't be Only me)</span></label></div>
        <p class="dim" style="font-size:11.5px;margin:0" id="tt-consent">By posting, you agree to TikTok's <a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank">Music Usage Confirmation</a>.</p>
        <p class="dim" style="font-size:11.5px;margin:0">${tt.max_video_post_duration_sec ? `Max length for this account: ${+tt.max_video_post_duration_sec}s. ` : ""}After posting, TikTok can take a few minutes to process the video.</p>`;
      $("#tt-commercial", ttBox).onchange = (e) => { $("#tt-com", ttBox).hidden = !e.target.checked; };
      $("#tt-brand_content", ttBox).onchange = () => {
        $("#tt-consent", ttBox).innerHTML = `By posting, you agree to TikTok's ${$("#tt-brand_content", ttBox).checked ? `<a href="https://www.tiktok.com/legal/page/global/bc-policy/en" target="_blank">Branded Content Policy</a> and ` : ""}<a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank">Music Usage Confirmation</a>.`;
      };
    };
    const loadTT = async () => {
      if (tt || !$$(".pp", m).some((x) => x.checked && x.value === "tiktok")) return renderTT();
      try { tt = await api("/api/tiktok/creator"); } catch (err) { tt = { error: err.message }; }
      renderTT();
    };
    $$(".pp", m).forEach((x) => x.addEventListener("change", loadTT));
    loadTT();
    const ttRead = () => {
      if (!$$(".pp", m).some((x) => x.checked && x.value === "tiktok") || !tt || tt.error) return { ok: true };
      const priv = $("#tt-priv", ttBox).value, commercial = $("#tt-commercial", ttBox).checked;
      const opts = { privacy_level: priv, ...Object.fromEntries(["allow_comment", "allow_duet", "allow_stitch", "brand_organic", "brand_content"].map((k) => [k, !!$(`#tt-${k}`, ttBox)?.checked])) };
      if (!priv) return { error: "Choose who can view the video on TikTok." };
      if (commercial && !opts.brand_organic && !opts.brand_content) return { error: "Pick Your brand and/or Branded content, or turn off the promotion switch." };
      if (opts.brand_content && priv === "SELF_ONLY") return { error: "Branded content can't be set to Only me on TikTok." };
      if (!commercial) opts.brand_organic = opts.brand_content = false;
      return { ok: true, opts };
    };
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
      const t2 = ttRead();
      if (t2.error) return toast(t2.error, "err");
      if (t2.opts) body.tiktok = t2.opts;
      try { await api(`/api/clips/${c.id}/upload`, { body }); close(); toast(mode === "now" ? `Queued for ${plats.map((p) => PLATFORM[p]).join(", ")} — posts when the render finishes.` : `Scheduled for ${when(t)}.`, "ok"); onDone?.(); }
      catch (err) { fail(err); }
    };
  });
}

// ======================================================================
// Clip Studio — one clip, full page: tracked framing, captions, AI, render
// ======================================================================
// Tracking path at t (seconds from clip start) — mirrors track.merge(): AI points [t,cx,cy,w,h] interpolate linearly
// (held flat past the ends; no points = static crop_x/crop_y); each manual [t,cx,cy] keyframe adds a correction offset
// that is exact at the keyframe and fades to 0 over ±TRACK_PULL s (offsets run straight across keyframes < 2·TRACK_PULL apart).
const TRACK_PULL = 1.5;
const TRACK_MODES = [["auto", "Auto (face, then most important area)"], ["face", "Face only"], ["target", "Track what I pick"]];
const interp = (xs, ys, t) => { // numpy.interp
  if (t <= xs[0]) return ys[0];
  if (t >= xs.at(-1)) return ys.at(-1);
  let i = 1;
  while (xs[i] < t) i++;
  return xs[i] > xs[i - 1] ? ys[i - 1] + ((ys[i] - ys[i - 1]) * (t - xs[i - 1])) / (xs[i] - xs[i - 1]) : ys[i];
};
function trackAt(points, manual, t, base, baseZ = 1) { // returns {cx, cy, w, h, z}; z = zoom (1..4)
  const ts = points.map((q) => q[0]), col = (j, tt) => interp(ts, points.map((q) => q[j]), tt);
  const ai = (tt) => (points.length ? [col(1, tt), col(2, tt)] : [base.cx, base.cy]);
  const [ax, ay] = ai(t), p = { cx: ax, cy: ay, w: points.length ? col(3, t) : 0, h: points.length ? col(4, t) : 0, z: baseZ };
  if (!manual.length) return p;
  const man = [...manual].sort((x, y) => x[0] - y[0]), knots = [];
  man.forEach((q, i) => {
    const [mt, mx, my] = q;
    if (i === 0) knots.push([mt - TRACK_PULL, 0, 0, 0]);
    else if (mt - man[i - 1][0] > 2 * TRACK_PULL) knots.push([man[i - 1][0] + TRACK_PULL, 0, 0, 0], [mt - TRACK_PULL, 0, 0, 0]);
    const [kx, ky] = ai(mt);
    knots.push([mt, mx - kx, my - ky, (q[3] ?? baseZ) - baseZ]); // zoom offsets ride the same knots as cx/cy
  });
  knots.push([man.at(-1)[0] + TRACK_PULL, 0, 0, 0]);
  // sample like merge() (clamped at each sample), then interpolate between samples as the renderer does
  const kt = knots.map((k) => k[0]), kc = [1, 2, 3].map((j) => knots.map((k) => k[j])), clamp = (v, lo = 0, hi = 1) => Math.min(Math.max(v, lo), hi);
  const st = [...new Set([...ts, ...kt])].sort((x, y) => x - y), sx = [], sy = [], sz = [];
  for (const u of st) {
    const [ux, uy] = ai(u);
    sx.push(clamp(ux + interp(kt, kc[0], u))); sy.push(clamp(uy + interp(kt, kc[1], u))); sz.push(clamp(baseZ + interp(kt, kc[2], u), 1, 4));
  }
  p.cx = interp(st, sx, t); p.cy = interp(st, sy, t); p.z = interp(st, sz, t);
  return p;
}
// Output crop window (fractions of the source frame) for an aspect, centred on (cx, cy), clamped — same maths as media.crop_box.
function cropWin(sw, sh, aspect, zoom, cx, cy) {
  const [OW, OH] = SIZES[aspect] || [sw, sh], ratio = OW / OH, z = Math.max(zoom || 1, 1);
  let w, h;
  if (sw / sh > ratio) { h = sh / z; w = h * ratio; } else { w = sw / z; h = w / ratio; }
  const x = Math.min(Math.max(sw * cx - w / 2, 0), sw - w), y = Math.min(Math.max(sh * cy - h / 2, 0), sh - h);
  return { x: x / sw, y: y / sh, w: w / sw, h: h / sh };
}

// The Studio route only carries the clip id: find its project (last opened project first, then the rest).
async function projectOfClip(cid) {
  const has = async (pid) => { const p = await api(`/api/projects/${pid}`); return p.clips.some((c) => c.id === cid) ? p : null; };
  if (localStorage.lastProject) { const p = await has(localStorage.lastProject).catch(() => null); if (p) return p; }
  for (const p of await api("/api/projects")) {
    if (p.id === localStorage.lastProject || !p.clip_count) continue;
    const hit = await has(p.id).catch(() => null);
    if (hit) return hit;
  }
  return null;
}

function Studio(cid) {
  const T = { proj: null, clip: null, tx: [], words: [], points: [], manual: [], job: null, mode: localStorage.studioMode || "source", drag: null, tdrag: null, range: [0, 1], wi: -1, tmode: "auto", target: null, pick: null, box: null,
    ovs: [], osel: null, assets: null, akind: "", aq: "", plan: null, picked: new Set(), sounds: new Map(), prevRel: null, rz: null, odrag: null, win: null };
  let video, ov, octx, tl, tctx, raf, keyHandler, wordTimer;
  const c = () => T.clip;
  const pid = () => T.proj.id;
  const dur = () => T.proj?.meta?.duration || 0;
  const DONE = ["done", "failed", "canceled"];
  this.every = 2000;

  this.mount = async () => {
    T.proj = await projectOfClip(cid);
    T.clip = T.proj?.clips.find((k) => k.id === cid);
    if (!T.clip || T.clip.parts || !T.proj.source) {
      view.innerHTML = `<div class="page"><a class="btn ghost sm" href="${T.proj ? `#/editor/${esc(T.proj.id)}` : "#/library"}">← Back</a>
        <div class="empty"><b>${!T.clip ? "Clip not found" : T.clip.parts ? "Montages can't be opened in the Studio" : "This project has no source video"}</b>${T.clip?.parts ? "Open one of its clips instead." : ""}</div></div>`;
      if (!T.clip && localStorage.lastClip === cid) { delete localStorage.lastClip; $("#nav-studio").hidden = true; }
      T.clip = null;
      return;
    }
    localStorage.lastProject = T.proj.id;
    localStorage.lastClip = cid; $("#nav-studio").hidden = false;
    $("#nav-editor").hidden = false;
    const [tr, track, assets] = await Promise.all([
      fetch(`/files/${T.proj.id}/transcript.json`).then((r) => (r.ok ? r.json() : { segments: [] })).catch(() => ({ segments: [] })),
      api(`/api/clips/${cid}/track`).catch(() => null), // backend may not have tracking yet: fall back to the clip's stored track
      api("/api/assets").catch(() => null),
    ]);
    if (T.dead) return;
    T.tx = (tr.segments || []).flatMap((s) => s.words || []);
    setTrack(track || c().edit.track || {});
    T.job = track?.job || null;
    T.assets = Array.isArray(assets) ? assets : null;
    T.ovs = structuredClone(c().edit.overlays || []);
    if (track && "follow" in track) c().edit = { ...c().edit, follow: !!track.follow };
    if (track) { T.tmode = TRACK_MODES.some(([m]) => m === track.mode) ? track.mode : "auto"; T.target = track.target || null; }
    loadWords();
    layout();
  };
  this.destroy = () => {
    T.dead = true; cancelAnimationFrame(raf); document.removeEventListener("keydown", keyHandler); video?.pause();
    if (wordTimer) { clearTimeout(wordTimer); saveWords(); } // don't lose a caption edit still waiting on its debounce
    if (T.ovTimer) { clearTimeout(T.ovTimer); patch({ edit: { overlays: T.ovs } }).catch(fail); }
    if (T.frTimer) { clearTimeout(T.frTimer); saveFraming(); }
    if (T.tsTimer) { clearTimeout(T.tsTimer); flushEdit(); }
    T.sounds.forEach((au) => au.pause()); view.oninput = null;
  };

  function setTrack(t) { T.points = [...(t.points || [])].sort((a, b) => a[0] - b[0]); T.manual = [...(t.manual || [])].sort((a, b) => a[0] - b[0]); }
  function loadWords() {
    const e = c().edit, src = Array.isArray(e.words) ? e.words : T.tx.filter((w) => w.s >= c().start - 0.05 && w.e <= c().end + 0.05);
    T.words = src.map((w) => ({ s: +w.s, e: +w.e, w: String(w.w ?? "").trim() }));
    T.custom = Array.isArray(e.words);
  }
  const setRange = () => { const m = Math.max(2, (c().end - c().start) * 0.25); T.full = [Math.max(0, c().start - m), Math.min(dur() || c().end + m, c().end + m)]; T.range = [...T.full]; };
  const setView = (a, span) => { span = Math.min(Math.max(span, 1), T.full[1] - T.full[0]); a = Math.min(Math.max(a, T.full[0]), T.full[1] - span); T.range = [a, a + span]; }; // timeline zoom/pan
  const scrubS = scrubber(() => video);
  const seek = (t) => { if (video) video.currentTime = Math.min(Math.max(t, c().start), c().end); };
  const base = () => ({ cx: +c().edit.crop_x, cy: +c().edit.crop_y });
  const fr = () => c().edit.framing || {};
  const baseZ = () => Math.min(Math.max(+(fr().zoom ?? c().edit.zoom ?? 1) || 1, 1), 4);
  const framing = (rel) => {
    const f = T.drag ? { cx: T.drag.cx, cy: T.drag.cy, w: 0, h: 0, z: T.drag.z } : c().edit.follow ? trackAt(T.points, T.manual, rel, base(), baseZ()) : { ...base(), w: 0, h: 0, z: baseZ() };
    if (T.rz) f.z = T.rz.z;
    return f;
  };
  // crop window: zoom from the path, then headroom (framing.offset_y) shifts it by that share of its own height
  const winAt = (f, sw, sh) => { const w0 = cropWin(sw, sh, c().edit.aspect, f.z, f.cx, f.cy); return cropWin(sw, sh, c().edit.aspect, f.z, f.cx, f.cy + (+fr().offset_y || 0) * w0.h); };

  // text-style edits are many small changes (slider drags): batch them into one PATCH
  function queueEdit(k, v) {
    c().edit = { ...c().edit, [k]: v };
    T.tsPend = { ...(T.tsPend || {}), [k]: v };
    clearTimeout(T.tsTimer); T.tsTimer = setTimeout(flushEdit, 450);
  }
  function flushEdit() { const body = T.tsPend; T.tsPend = null; T.tsTimer = null; if (body) patch({ edit: body }).catch(fail); }
  const outSize = () => SIZES[c().edit.aspect] || [video?.videoWidth || 1920, video?.videoHeight || 1080];
  const locked = () => !!(T.locked || c().locked || Object.keys(c().posts || {}).length);
  function applyLock() { $(".studio")?.classList.toggle("st-locked", locked()); const b = $("#st-lock"); if (b) b.hidden = !locked(); }
  async function patch(body) {
    const fresh = await api(`/api/clips/${cid}`, { method: "PATCH", body }).catch((e) => { if (e.status === 409) { T.locked = true; applyLock(); } throw e; });
    if (fresh && fresh.id) T.clip = { ...c(), ...fresh };
    return T.clip;
  }

  // ----- layout -----
  function layout() {
    setRange();
    view.innerHTML = `<div class="editor studio">
      <div class="ed-top">
        <a class="btn ghost sm" href="#/editor/${esc(pid())}">← Back to project</a>
        <div class="title" id="st-name" title="${esc(c().title)}">${esc(c().title)}</div>
        <span class="muted mono" id="st-span"></span>
        <div class="grow"></div>
        <div class="seg" id="st-mode">${[["source", "Source view"], ["output", "Output view"]].map(([v, l]) => `<button data-act="mode" data-v="${v}" class="${T.mode === v ? "on" : ""}">${l}</button>`).join("")}</div>
      </div>
      <div class="stage">
        <div class="player" id="st-player"><div class="st-frame" id="st-frame">
          <video id="svid" src="/files/${esc(pid())}/${esc(T.proj.source)}" preload="auto" playsinline></video><canvas id="sov"></canvas><div class="st-fx" id="st-fx"></div></div></div>
        <div class="transport">
          <button class="btn sm" data-act="play" title="Space">▶</button>
          <span class="tc" id="stc">0:00.0</span>
          <div class="grow"></div>
          <span class="muted" style="font-size:12px" id="st-hint"></span>
        </div>
        <div class="timeline st-tl"><div class="tl-tip" id="st-tip" hidden></div><canvas id="stl"></canvas>
          <div class="tl-foot"><span class="st-key"><i style="background:var(--accent)"></i>clip</span><span class="st-key"><i style="background:var(--info)"></i>AI path (↔ framing)</span><span class="st-key"><i style="background:var(--ok)"></i>your keyframes</span><span class="st-key"><i style="background:#b07cff"></i>effects</span>
            <span class="grow"></span><span>Drag edges to trim · Shift-drag = fine scrub · Ctrl+wheel zoom</span><button class="btn xs" data-act="tzout" title="Zoom out">−</button><button class="btn xs" data-act="tzin" title="Zoom in">+</button></div>
        </div>
      </div>
      <div class="panel"><div class="st-lock" id="st-lock" hidden>🔒 <span class="grow">Posted — editing locked.</span><button class="btn xs primary" data-act="version">Make a new version</button></div><div class="tab-body" id="st-panel"></div></div></div>`;
    video = $("#svid"); ov = $("#sov"); octx = ov.getContext("2d"); tl = $("#stl"); tctx = tl.getContext("2d");
    video.addEventListener("loadedmetadata", () => { seek(c().start); });
    video.addEventListener("play", () => { $("[data-act=play]").textContent = "❚❚"; });
    video.addEventListener("pause", () => { $("[data-act=play]").textContent = "▶"; T.sounds.forEach((au) => au.pause()); });
    bindFrame(); bindTimeline(); bindDrop();
    view.onclick = onClick; view.oninput = onInput; view.onchange = onChange;
    keyHandler = (e) => {
      if (e.target.closest?.("input, textarea, select") || $("#modal-root").innerHTML) return;
      if (e.code === "Space") { e.preventDefault(); video.paused ? video.play() : video.pause(); }
      else if (e.key === "ArrowLeft") seek(video.currentTime - (e.shiftKey ? 1 : 1 / (T.proj.meta?.fps || 30)));
      else if (e.key === "ArrowRight") seek(video.currentTime + (e.shiftKey ? 1 : 1 / (T.proj.meta?.fps || 30)));
      else if (e.key === "Escape" && (T.pick || T.box)) { T.pick = T.box = null; renderTrack(); }
    };
    document.addEventListener("keydown", keyHandler);
    renderPanel(); buildFx(); setMode(T.mode); applyLock();
    cancelAnimationFrame(raf);
    const loop = () => { frame(); raf = requestAnimationFrame(loop); }; // ponytail: redraws every frame; cheap at this size
    loop();
  }

  function setMode(m) {
    T.mode = m; try { localStorage.studioMode = m; } catch { /* private mode */ }
    $$("#st-mode button").forEach((b) => b.classList.toggle("on", b.dataset.v === m));
    $("#st-frame").classList.toggle("out", m === "output");
    if (m === "source") { $("#st-frame").style.cssText = ""; video.style.cssText = ""; }
    hint();
  }
  const hint = () => { $("#st-hint").textContent = T.mode === "output" ? "Showing the final framing" : T.tmode === "target" ? "Pause and drag a box around what to follow · Esc cancels" : c().edit.follow ? "Pause and drag the frame (or a corner, to zoom) to add a tracking keyframe" : "Drag the frame to set the framing"; };

  // ----- per-frame drawing: overlay or output crop, timecode, timeline, current word -----
  function frame() {
    if (!video || !c()) return;
    const k = c(), t = video.currentTime, rel = t - k.start;
    if (!video.paused && (t >= k.end || t < k.start - 0.25)) video.currentTime = k.start; // loop inside the clip
    $("#stc").textContent = `${fmt(Math.max(0, rel))} / ${fmt(k.end - k.start)}`;
    drawTimeline();
    const wi = T.words.findIndex((w) => t >= w.s && t <= w.e);
    if (wi !== T.wi) { $(`.wd[data-i="${T.wi}"]`)?.classList.remove("now"); $(`.wd[data-i="${wi}"]`)?.classList.add("now"); T.wi = wi; }
    const sw = video.videoWidth, sh = video.videoHeight;
    if (!sw) return;
    const f = framing(rel), win = winAt(f, sw, sh);
    T.win = win;
    fxTick(rel);
    if (T.mode === "output") {
      const pl = $("#st-player"), cw = win.w * sw, ch = win.h * sh, s = Math.min(pl.clientWidth / cw, pl.clientHeight / ch) * 0.98;
      Object.assign($("#st-frame").style, { width: cw * s + "px", height: ch * s + "px" });
      Object.assign(video.style, { width: sw * s + "px", height: sh * s + "px", left: -win.x * sw * s + "px", top: -win.y * sh * s + "px" });
      return;
    }
    const dpr = window.devicePixelRatio || 1, W = video.clientWidth, H = video.clientHeight;
    if (ov.width !== Math.round(W * dpr) || ov.height !== Math.round(H * dpr)) { ov.width = Math.round(W * dpr); ov.height = Math.round(H * dpr); }
    const g = octx;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const x = win.x * W, y = win.y * H, w = win.w * W, h = win.h * H;
    g.fillStyle = "#000000a0";
    g.beginPath(); g.rect(0, 0, W, H); g.rect(x, y, w, h); g.fill("evenodd");
    g.strokeStyle = T.drag ? "#37d39a" : "#ff6a3d"; g.lineWidth = 2; g.strokeRect(x + 1, y + 1, w - 2, h - 2);
    g.fillStyle = T.rz ? "#37d39a" : "#ff6a3d";
    for (const [qx, qy] of [[x, y], [x + w, y], [x, y + h], [x + w, y + h]]) g.fillRect(qx - 5, qy - 5, 10, 10); // corner handles: drag to zoom
    if (f.z > 1.01) { g.fillStyle = "#fff"; g.font = "600 11px Inter, sans-serif"; g.fillText(`${f.z.toFixed(2)}×`, x + 8, y + h - 9); }
    g.strokeStyle = "#ffffff66"; g.lineWidth = 1;
    g.beginPath(); g.moveTo(x + w / 2 - 8, y + h / 2); g.lineTo(x + w / 2 + 8, y + h / 2); g.moveTo(x + w / 2, y + h / 2 - 8); g.lineTo(x + w / 2, y + h / 2 + 8); g.stroke();
    if (k.edit.follow && T.points.length && f.w > 0 && !T.drag) {
      const p = trackAt(T.points, [], rel, base()); // the raw subject box, before manual corrections
      g.setLineDash([5, 4]); g.strokeStyle = "#6aa8ff"; g.strokeRect((p.cx - p.w / 2) * W, (p.cy - p.h / 2) * H, p.w * W, p.h * H); g.setLineDash([]);
    }
    const near = k.edit.follow && T.manual.find((q) => Math.abs(q[0] - rel) < 0.1);
    if (near) { g.fillStyle = "#37d39a"; g.font = "600 11px Inter, sans-serif"; g.fillText("◆ keyframe", x + 6, y + 15); }
    const b = T.box || T.pick || (T.tmode === "target" && T.target && Math.abs(T.target.t - rel) < 0.1 ? T.target : null);
    if (b) {
      g.strokeStyle = "#ffc247"; g.lineWidth = 2; g.strokeRect(b.x * W, b.y * H, b.w * W, b.h * H);
      g.fillStyle = "#ffc247"; g.font = "600 11px Inter, sans-serif"; g.fillText(b === T.target ? "◎ tracked target" : "◎ target", b.x * W + 4, Math.max(b.y * H - 5, 12));
    }
  }

  // drag the crop window (source view): adds a manual keyframe when following, else moves the static framing
  function bindFrame() {
    const frac = (ev) => { const r = ov.getBoundingClientRect(); return [Math.min(Math.max((ev.clientX - r.left) / r.width, 0), 1), Math.min(Math.max((ev.clientY - r.top) / r.height, 0), 1)]; };
    ov.addEventListener("pointerdown", (ev) => {
      if (T.mode !== "source" || !video.videoWidth) return;
      video.pause();
      if (T.tmode === "target") { const [x, y] = frac(ev); T.box = { x0: x, y0: y, x, y, w: 0, h: 0 }; ov.setPointerCapture(ev.pointerId); return; } // picking: draw a new box, don't move the crop
      if (T.win) { // a corner handle: resize the window = zoom at this moment
        const rr = ov.getBoundingClientRect(), [px, py] = frac(ev), wn = T.win;
        if ([[wn.x, wn.y], [wn.x + wn.w, wn.y], [wn.x, wn.y + wn.h], [wn.x + wn.w, wn.y + wn.h]].some(([qx, qy]) => Math.abs(qx - px) * rr.width < 12 && Math.abs(qy - py) * rr.height < 12)) {
          const z0 = framing(video.currentTime - c().start).z, mx = wn.x + wn.w / 2, my = wn.y + wn.h / 2;
          T.rz = { z0, z: z0, mx, my, d0: Math.max(Math.hypot((px - mx) * rr.width, (py - my) * rr.height), 1), moved: false };
          ov.setPointerCapture(ev.pointerId);
          return;
        }
      }
      const r = ov.getBoundingClientRect(), f = framing(video.currentTime - c().start);
      T.drag = { ox: (ev.clientX - r.left) / r.width - f.cx, oy: (ev.clientY - r.top) / r.height - f.cy, cx: f.cx, cy: f.cy, z: f.z, moved: false };
      ov.setPointerCapture(ev.pointerId);
    });
    ov.addEventListener("pointermove", (ev) => {
      if (T.rz) {
        const rr = ov.getBoundingClientRect(), [px, py] = frac(ev), d = Math.hypot((px - T.rz.mx) * rr.width, (py - T.rz.my) * rr.height);
        T.rz.z = Math.min(Math.max((T.rz.z0 * T.rz.d0) / Math.max(d, 1), 1), 4); // bigger window = less zoom
        T.rz.moved = true;
        return;
      }
      if (T.box) { const [x, y] = frac(ev), b = T.box; Object.assign(b, { x: Math.min(x, b.x0), y: Math.min(y, b.y0), w: Math.abs(x - b.x0), h: Math.abs(y - b.y0) }); return; }
      if (!T.drag) return;
      const r = ov.getBoundingClientRect();
      T.drag.cx = Math.min(Math.max((ev.clientX - r.left) / r.width - T.drag.ox, 0), 1);
      T.drag.cy = Math.min(Math.max((ev.clientY - r.top) / r.height - T.drag.oy, 0), 1);
      T.drag.moved = true;
    });
    const end = async () => {
      if (T.rz) {
        const z = +T.rz.z.toFixed(3), moved = T.rz.moved;
        T.rz = null;
        if (!moved) return;
        const rel = +Math.max(0, video.currentTime - c().start).toFixed(2), f = framing(rel);
        if (!c().edit.follow) { // static framing: the zoom is global
          c().edit = { ...c().edit, framing: { ...fr(), zoom: z } };
          const zs = $('[data-fr="zoom"]'); if (zs) { zs.value = z; zs.nextElementSibling.textContent = z.toFixed(2) + "×"; }
          return saveFraming();
        }
        T.manual = [...T.manual.filter((q) => Math.abs(q[0] - rel) >= 0.25), [rel, +f.cx.toFixed(4), +f.cy.toFixed(4), z]].sort((a, b) => a[0] - b[0]);
        renderTrack();
        return saveManual();
      }
      if (T.box) {
        const b = T.box; T.box = null;
        if (b.w > 0.01 && b.h > 0.01) T.pick = { t: +Math.max(0, video.currentTime - c().start).toFixed(2), x: +b.x.toFixed(4), y: +b.y.toFixed(4), w: +b.w.toFixed(4), h: +b.h.toFixed(4) };
        return renderTrack();
      }
      const d = T.drag; T.drag = null;
      if (!d?.moved) return;
      const cx = +d.cx.toFixed(4), cy = +d.cy.toFixed(4);
      if (!c().edit.follow) {
        c().edit = { ...c().edit, crop_x: cx, crop_y: cy };
        return patch({ edit: { crop_x: cx, crop_y: cy } }).catch(fail);
      }
      const rel = +Math.max(0, video.currentTime - c().start).toFixed(2);
      T.manual = [...T.manual.filter((q) => Math.abs(q[0] - rel) >= 0.25), Math.abs(d.z - baseZ()) > 0.01 ? [rel, cx, cy, +d.z.toFixed(3)] : [rel, cx, cy]].sort((a, b) => a[0] - b[0]);
      renderTrack();
      saveManual();
    };
    ov.addEventListener("pointerup", end);
    ov.addEventListener("pointercancel", end);
  }
  async function saveManual() {
    try { await patch({ edit: { track_manual: T.manual } }); } catch (e) { fail(e); }
  }

  // ----- mini timeline of this clip -----
  let TL_H, LANE_Y, LANE_H, KF_Y, FX_Y, FX_H; // scaled by the Timelines interface size
  const tlScale = () => { [TL_H, LANE_Y, LANE_H, KF_Y, FX_Y, FX_H] = [100, 8, 40, 58, 76, 18].map((v) => v * S.ui.timeline); };
  tlScale();
  const tx = (t) => ((t - T.range[0]) / (T.range[1] - T.range[0])) * (tl.clientWidth || 1);
  const tAt = (px) => T.range[0] + (px / (tl.clientWidth || 1)) * (T.range[1] - T.range[0]);
  function drawTimeline() {
    tlScale();
    const dpr = window.devicePixelRatio || 1, W = tl.clientWidth;
    if (tl.width !== Math.round(W * dpr) || tl.height !== Math.round(TL_H * dpr)) { tl.width = Math.round(W * dpr); tl.height = Math.round(TL_H * dpr); }
    const g = tctx, k = c();
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, TL_H);
    const xs = tx(k.start), xe = tx(k.end);
    g.fillStyle = "#ffffff06"; g.fillRect(0, LANE_Y, W, LANE_H);
    g.fillStyle = "#ff6a3d1c"; g.fillRect(xs, LANE_Y, xe - xs, LANE_H);
    if (T.points.length) { // AI path: horizontal framing centre over time (top = left of frame)
      g.strokeStyle = "#6aa8ff"; g.lineWidth = 1.5; g.beginPath();
      T.points.forEach((p, i) => { const X = tx(k.start + p[0]), Y = LANE_Y + p[1] * LANE_H; i ? g.lineTo(X, Y) : g.moveTo(X, Y); });
      g.stroke();
    }
    g.fillStyle = "#ff6a3d";
    for (const X of [xs, xe]) { g.fillRect(X - 1.5, 2, 3, TL_H - 4); g.beginPath(); g.roundRect(X - 5, LANE_Y + LANE_H / 2 - 9, 10, 18, 3); g.fill(); }
    for (const q of T.manual) {
      const X = tx(k.start + q[0]);
      g.fillStyle = "#37d39a"; g.beginPath(); g.moveTo(X, KF_Y - 6); g.lineTo(X + 6, KF_Y); g.lineTo(X, KF_Y + 6); g.lineTo(X - 6, KF_Y); g.fill();
      g.fillRect(X - 0.5, LANE_Y + q[1] * LANE_H - 2, 1, 4);
    }
    g.fillStyle = "#ffffff06"; g.fillRect(0, FX_Y, W, FX_H);
    g.font = `600 ${10 * S.ui.timeline}px Inter, sans-serif`;
    for (const o of T.ovs) {
      const X0 = tx(k.start + o.t), Wd = Math.max(4, tx(k.start + o.t + o.d) - X0), sel = o.id === T.osel;
      g.fillStyle = sel ? "#ffc247" : "#b07cffa0"; g.beginPath(); g.roundRect(X0, FX_Y, Wd, FX_H, 4); g.fill();
      g.save(); g.beginPath(); g.rect(X0, FX_Y, Wd - 6, FX_H); g.clip();
      g.fillStyle = sel ? "#1b0b04" : "#eceef6"; g.fillText(ovLabel(o), X0 + 4, FX_Y + 12.5); g.restore();
      g.fillStyle = "#0006"; g.fillRect(X0 + Wd - 3, FX_Y + 4, 2, FX_H - 8); // resize grip
    }
    if (T.target) { const X = tx(k.start + T.target.t); g.strokeStyle = "#ffc247"; g.lineWidth = 2; g.strokeRect(X - 5, KF_Y - 5, 10, 10); }
    const X = tx(T.scrubT ?? video.currentTime);
    g.fillStyle = "#fff"; g.fillRect(X - 1, 0, 2, TL_H);
  }
  function bindTimeline() {
    tl.addEventListener("pointerdown", (ev) => {
      const x = localXY(ev, tl)[0], k = c();
      if (localXY(ev, tl)[1] >= FX_Y - 2) { // effects lane: select, move (body) or resize (right edge)
        const hit = [...T.ovs].reverse().find((o) => x >= tx(k.start + o.t) - 2 && x <= tx(k.start + o.t + o.d) + 3);
        if (hit) {
          T.osel = hit.id; renderOvInspector();
          T.odrag = { o: hit, mode: Math.abs(x - tx(k.start + hit.t + hit.d)) < 7 ? "d" : "t", x0: x, t0: hit.t, d0: hit.d };
          tl.setPointerCapture(ev.pointerId);
          return;
        }
      }
      if (Math.abs(x - tx(k.start)) < 8) T.tdrag = "start";
      else if (Math.abs(x - tx(k.end)) < 8) T.tdrag = "end";
      else {
        const kf = localXY(ev, tl)[1] > KF_Y - 10 && T.manual.find((q) => Math.abs(tx(k.start + q[0]) - x) < 7);
        if (kf) { video.pause(); seek(k.start + kf[0]); return; }
        if (T.target && localXY(ev, tl)[1] > KF_Y - 10 && Math.abs(tx(k.start + T.target.t) - x) < 7) { video.pause(); seek(k.start + T.target.t); return; }
        T.tdrag = "seek"; T.fine = { cur: Math.min(Math.max(tAt(x), k.start), k.end), lx: x }; seek(T.fine.cur);
      }
      tl.setPointerCapture(ev.pointerId);
    });
    tl.addEventListener("pointermove", (ev) => {
      const x = localXY(ev, tl)[0], k = c();
      if (T.odrag) {
        const D = T.odrag, len = k.end - k.start, dt = ((x - D.x0) / (tl.clientWidth || 1)) * (T.range[1] - T.range[0]);
        if (D.mode === "t") D.o.t = +Math.min(Math.max(D.t0 + dt, 0), Math.max(0, len - 0.2)).toFixed(2);
        else D.o.d = +Math.min(Math.max(D.d0 + dt, 0.2), len - D.o.t).toFixed(2);
        return;
      }
      if (!T.tdrag) { tl.style.cursor = Math.abs(x - tx(k.start)) < 8 || Math.abs(x - tx(k.end)) < 8 ? "ew-resize" : "pointer"; return; }
      const t = Math.min(Math.max(tAt(x), 0), dur() || T.range[1]);
      if (T.tdrag === "seek") { // Shift = fine scrub (10x slower); seeks coalesced to one per frame
        const F = T.fine;
        F.cur = Math.min(Math.max(ev.shiftKey ? F.cur + (((x - F.lx) / (tl.clientWidth || 1)) * (T.range[1] - T.range[0])) / 10 : t, k.start), k.end);
        F.lx = x; T.scrubT = F.cur;
        return scrubS(F.cur, true);
      }
      if (T.tdrag === "start") k.start = +Math.min(t, k.end - 0.5).toFixed(2); else k.end = +Math.max(t, k.start + 0.5).toFixed(2);
      video.currentTime = T.tdrag === "start" ? k.start : k.end;
      spanLabel();
    });
    const up = async () => {
      if (T.odrag) { T.odrag = null; renderOvInspector(); return saveOverlays(); }
      const d = T.tdrag; T.tdrag = null;
      if (d === "seek" && T.fine) { seek(T.fine.cur); T.scrubT = null; T.fine = null; }
      if (d !== "start" && d !== "end") return;
      try { await patch({ start: c().start, end: c().end }); } catch (e) { fail(e); }
      setRange(); spanLabel();
      if (!T.custom) { loadWords(); renderWords(); } // transcript-based captions follow the new in/out
    };
    tl.addEventListener("pointerup", up);
    tl.addEventListener("mousemove", (ev) => tlTip($("#st-tip"), localXY(ev, tl.parentElement)[0], tAt(localXY(ev, tl)[0]), pid(), T.proj));
    tl.addEventListener("mouseleave", () => { $("#st-tip").hidden = true; });
    tl.addEventListener("wheel", (ev) => {
      const [a, b] = T.range, span = b - a;
      if (ev.ctrlKey) { ev.preventDefault(); const tc = tAt(localXY(ev, tl)[0]), ns = span * (ev.deltaY < 0 ? 0.8 : 1.25); setView(tc - ((tc - a) / span) * ns, ns); }
      else if (span < T.full[1] - T.full[0] - 1e-6) { ev.preventDefault(); setView(a + ((ev.deltaX || ev.deltaY) / (tl.clientWidth || 1)) * span, span); } // pan when zoomed
    }, { passive: false });
    tl.addEventListener("pointercancel", up);
  }
  const spanLabel = () => { $("#st-span").textContent = `${fmt(c().start)} → ${fmt(c().end)} · ${(c().end - c().start).toFixed(1)}s`; };

  // ----- side panel -----
  const QUICK = ["Make it punchier", "Tighter on the face", "Add zoom on the punchline", "Clean captions"];
  const segBtns = (k, cur, opts) => `<div class="seg">${opts.map(([v, l]) => `<button class="${String(cur) === v ? "on" : ""}" data-act="set" data-k="${k}" data-v="${v}">${l}</button>`).join("")}</div>`;
  function renderPanel() {
    const k = c(), e = k.edit;
    $("#st-panel").innerHTML = `
      <div class="sec"><input type="text" id="st-title" value="${esc(k.title)}" style="font-weight:600">
        <div class="muted" style="font-size:12px">Output ${esc(e.aspect === "source" ? "original frame" : e.aspect)}${e.layout === "split" ? " · split layout (preview shows the main view)" : ""}${e.fit === "blur" && e.aspect !== "source" ? " · fit + blur (preview shows the crop)" : ""}</div></div>
      <div class="sec"><h3>Framing</h3>
        <label class="slider"><span>Headroom</span><input type="range" data-fr="offset_y" min="-0.4" max="0.4" step="0.01" value="${+(e.framing?.offset_y || 0)}"><output>${(+(e.framing?.offset_y || 0)).toFixed(2)}</output></label>
        <label class="slider"><span>Zoom</span><input type="range" data-fr="zoom" min="1" max="4" step="0.05" value="${baseZ()}"><output>${baseZ().toFixed(2)}×</output></label>
        <div class="muted" style="font-size:12px">Drag a corner of the frame (Source view) to zoom at that moment.</div></div>
      <div class="sec" id="st-track"></div>
      <div class="sec"><h3>Cuts</h3>
        <label class="switch"><input type="checkbox" data-k="remove_silence" ${e.remove_silence ? "checked" : ""}> AI jump cuts — remove silences &amp; dead air</label>
        <label class="switch"><input type="checkbox" data-k="remove_fillers" ${e.remove_fillers ? "checked" : ""}> Cut filler words (“um”, “uh”)</label></div>
      <div class="sec"><h3>Captions</h3>
        <label class="switch"><input type="checkbox" data-k="captions" ${e.captions ? "checked" : ""}> Animated captions</label>
        ${segBtns("caption_style", e.caption_style, [["bold", "Bold"], ["clean", "Clean"], ["boxed", "Boxed"], ["neon", "Neon"]])}
        ${segBtns("caption_pos", e.caption_pos, [["low", "Low"], ["middle", "Middle"], ["high", "High"]])}
        <div class="row"><span class="muted grow" style="font-size:12px" id="st-wsrc"></span><button class="btn xs" data-act="words-reset">Reset to transcript</button></div>
        <div class="st-words" id="st-words"></div>
        <details><summary class="muted" style="font-size:12px;cursor:pointer">Caption font &amp; style</summary><div id="st-cts" class="stack" style="gap:8px;margin-top:8px"></div></details>
        <div class="muted" style="font-size:12px">Switch to Output view to see captions in the frame.</div></div>
      <div class="sec"><h3>Effects</h3>
        <div id="st-ovi" class="stack" style="gap:8px"></div>
        <div class="row"><input type="text" id="st-txt" placeholder="Text or emoji sticker 🔥" maxlength="80"><button class="btn sm" data-act="add-text">＋ Add</button></div>
        <div class="muted" style="font-size:12px">Drag media onto the preview to place it · switch to Output view to see effects.</div></div>
      <div class="sec"><h3>Media library</h3>
        <div class="seg">${[["", "All"], ["image", "Images"], ["gif", "GIFs"], ["video", "Video"], ["sound", "Sounds"]].map(([v, l]) => `<button class="${T.akind === v ? "on" : ""}" data-act="akind" data-v="${v}">${l}</button>`).join("")}</div>
        <input type="text" id="st-aq" placeholder="Search by name or tag" value="${esc(T.aq)}">
        <div class="st-lib" id="st-libl"></div>
        <details><summary class="muted" style="font-size:12px;cursor:pointer">Upload media</summary>
          <form id="st-up" class="stack" style="gap:6px;margin-top:8px"><input type="file" id="st-upf" accept="image/*,video/*,audio/*" required>
            <input type="text" id="st-upn" placeholder="Name (optional)"><input type="text" id="st-upt" placeholder="Tags, comma separated"><button class="btn sm">Upload</button></form></details></div>
      <div class="sec"><h3>AI director</h3>
        <div class="ask"><textarea id="st-ask" placeholder="Describe what you want… (leave empty for smart suggestions)"></textarea>
          <div class="row"><span class="hint grow">You get a checklist — nothing changes until you apply. <span class="kbd">Ctrl</span>+<span class="kbd">Enter</span></span><button class="btn sm primary" data-act="plan">✦ Get suggestions</button></div></div>
        <div id="st-plan"></div>
        <div class="row"><span class="muted grow" style="font-size:12px">Quick edits apply right away:</span><button class="btn xs" data-act="ask" title="Apply the prompt above directly, without a checklist">Apply</button></div>
        <div class="chips">${QUICK.map((q) => `<button class="chip st-chip" data-act="quick" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div></div>
      <div class="actions" id="st-actions"></div>`;
    $("#st-ask").onkeydown = (ev) => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) $("[data-act=plan]").click(); };
    $("#st-up").onsubmit = async (ev) => {
      ev.preventDefault();
      const f = $("#st-upf").files[0], btn = $("#st-up button");
      if (!f) return;
      const fd = new FormData();
      fd.append("file", f); fd.append("name", $("#st-upn").value.trim() || f.name.replace(/\.[^.]+$/, "")); fd.append("tags", $("#st-upt").value);
      btn.disabled = true;
      try { await api("/api/assets", { body: fd }); T.assets = await api("/api/assets"); ev.target.reset(); renderLib(); toast("Added to your media library.", "ok"); }
      catch (e) { fail(e); } finally { btn.disabled = false; }
    };
    textStyleEditor($("#st-cts"), { get: (k) => c().edit[`caption_${k}`], set: (k, v) => queueEdit(`caption_${k}`, v), def: tsDefaults("caption", c().edit, ...outSize()), sample: "Captions look like this" });
    renderLib(); renderOvInspector(); renderPlan();
    spanLabel(); renderTrack(); renderWords(); renderActions();
  }

  // ----- effects (edit.overlays) -----
  const ovLabel = (o) => (o.kind === "text" ? o.text || "text" : T.assets?.find((a) => a.id === o.asset_id)?.name || o.kind);
  const assetOv = (a, extra) => ({ kind: a.kind, asset_id: a.id, d: a.duration ? Math.min(+a.duration, 5) : 2.5, w: a.kind === "video" ? 0.45 : 0.35, ...extra });
  function saveOverlays() {
    clearTimeout(T.ovTimer);
    T.ovTimer = setTimeout(async () => { T.ovTimer = null; try { await patch({ edit: { overlays: T.ovs } }); } catch (e) { fail(e); } }, 500);
  }
  async function saveFraming() {
    clearTimeout(T.frTimer); T.frTimer = null;
    try { await patch({ edit: { framing: fr() } }); } catch (e) { fail(e); }
  }
  function addOverlay(o) {
    const len = c().end - c().start, rel = Math.max(0, video.currentTime - c().start);
    const n = { id: Math.random().toString(36).slice(2, 10), kind: "text", asset_id: null, text: "", t: 0, d: 2.5, x: 0.5, y: 0.35, w: 0.4, anim: "pop", volume: 1, layer: T.ovs.length, cutaway: false, ...o };
    n.t = +Math.min(rel, Math.max(0, len - 0.5)).toFixed(2);
    n.d = +Math.max(0.2, Math.min(n.d, len - n.t)).toFixed(2);
    T.ovs.push(n); T.osel = n.id;
    buildFx(); renderOvInspector(); saveOverlays();
  }
  function bindDrop() {
    const pl = $("#st-player");
    pl.addEventListener("dragover", (e) => { if (e.dataTransfer.types.includes("text/x-asset")) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; } });
    pl.addEventListener("drop", (e) => {
      const a = T.assets?.find((x) => x.id === e.dataTransfer.getData("text/x-asset"));
      if (!a) return;
      e.preventDefault();
      const r = $("#st-frame").getBoundingClientRect(), clamp = (v) => +Math.min(Math.max(v, 0), 1).toFixed(3);
      let x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      if (T.mode === "source" && T.win) { x = (x - T.win.x) / T.win.w; y = (y - T.win.y) / T.win.h; } // source frame -> output frame
      addOverlay(assetOv(a, { x: clamp(x), y: clamp(y) }));
    });
    $("#st-panel").addEventListener("dragstart", (e) => {
      const el = e.target.closest?.("[data-asset]");
      if (el) { e.dataTransfer.setData("text/x-asset", el.dataset.asset); e.dataTransfer.effectAllowed = "copy"; }
    });
  }
  // live preview layer (output view): images/GIFs/text as DOM, video as a muted PiP, sounds as Audio
  function buildFx() {
    const el = $("#st-fx");
    if (!el) return;
    T.sounds.forEach((au) => au.pause());
    T.sounds = new Map();
    el.innerHTML = T.ovs.map((o) => {
      const a = T.assets?.find((x) => x.id === o.asset_id), src = a ? `/assets/${esc(a.file)}` : "";
      const inner = o.kind === "text" ? `<span>${esc(o.text)}</span>` : !a ? `<span class="st-miss">${esc(o.kind)}</span>`
        : o.kind === "video" ? `<video src="${src}" muted loop playsinline preload="metadata"></video>` : `<img src="${src}" alt="">`;
      return o.kind === "sound" ? "" : `<div class="st-ov" data-o="${esc(o.id)}" hidden>${inner}</div>`;
    }).join("") + `<div class="st-cap" id="st-cap" hidden></div>`;
    for (const o of T.ovs) {
      const a = o.kind === "sound" && T.assets?.find((x) => x.id === o.asset_id);
      if (a) { const au = new Audio(`/assets/${encodeURI(a.file)}`); au.volume = Math.min(Math.max(+(o.volume ?? 1), 0), 1); T.sounds.set(o.id, au); }
    }
  }
  function fxTick(rel) {
    const el = $("#st-fx");
    if (!el) return;
    const out = T.mode === "output", fw = el.clientWidth;
    for (const o of T.ovs) {
      if (o.kind === "sound") continue;
      const node = el.querySelector(`[data-o="${CSS.escape(String(o.id))}"]`);
      if (!node) continue;
      const on = out && rel >= o.t && rel <= o.t + o.d;
      if (on === node.hidden) {
        node.hidden = !on;
        const v = node.querySelector("video");
        if (on) { node.className = `st-ov a-${esc(o.anim || "none")}`; v?.play().catch(() => {}); } else v?.pause();
      }
      if (on) {
        const full = o.cutaway && o.kind === "video", w = full ? 1 : +o.w || 0.35;
        Object.assign(node.style, { left: (full ? 0.5 : +o.x) * 100 + "%", top: (full ? 0.5 : +o.y) * 100 + "%", width: w * 100 + "%", zIndex: 3 + (+o.layer || 0) });
        if (o.kind === "text") { const [OW, OH] = outSize(); styleText(node.querySelector("span"), tsOf((k) => o[k], { ...tsDefaults("over", null, OW, OH), size: w * OW * 0.22 }), fw / OW); }
      }
    }
    capTick(out, fw);
    if (!video.paused && T.prevRel != null && rel > T.prevRel && rel - T.prevRel < 0.5) {
      for (const [id, au] of T.sounds) { const o = T.ovs.find((x) => x.id === id); if (o && T.prevRel < o.t && rel >= o.t) { au.currentTime = 0; au.play().catch(() => {}); } }
    }
    T.prevRel = rel;
  }
  // captions drawn in the output view, in the chosen font and style (same chunking as the renderer)
  function capTick(out, fw) {
    const el = $("#st-cap"), e = c().edit;
    if (!el) return;
    const [OW, OH] = outSize(), t = video.currentTime;
    if (T._cw !== T.words || T._ca !== e.aspect) { T._cw = T.words; T._ca = e.aspect; T.chunks = chunkWords(T.words, OH > OW); }
    const chunk = out && e.captions ? T.chunks.find((ch) => t >= ch[0].s && t <= ch.at(-1).e + 0.15) : null;
    el.hidden = !chunk;
    if (!chunk) return;
    const upper = S.settings?.caption_uppercase !== false, hl = hexOr(S.settings?.caption_color, "#FFD400");
    const html = chunk.map((w, i) => { const txt = esc(upper ? String(w.w).trim().toUpperCase() : String(w.w).trim()); return t >= w.s && (i === chunk.length - 1 || t < chunk[i + 1].s) ? `<em style="color:${hl}">${txt}</em>` : txt; }).join(" ");
    if (el._h !== html) { el._h = html; el.innerHTML = html; }
    styleText(el, tsOf((k) => e[`caption_${k}`], tsDefaults("caption", e, OW, OH)), fw / OW);
    el.style.bottom = CAPTION_POS[e.caption_pos || "low"][OH > OW ? 0 : 1] * 100 + "%";
    el.style.background = e.caption_style === "boxed" ? "#0009" : "";
  }
  function renderOvInspector() {
    const el = $("#st-ovi");
    if (!el) return;
    const o = T.ovs.find((x) => x.id === T.osel), len = +(c().end - c().start).toFixed(2);
    if (!o) { el.innerHTML = `<span class="muted" style="font-size:12px">${T.ovs.length ? `${T.ovs.length} effect${T.ovs.length > 1 ? "s" : ""} — click a purple bar on the timeline to edit one.` : "No effects yet."}</span>`; return; }
    const rng = (k, l, min, max, step) => `<label class="slider"><span>${l}</span><input type="range" data-ov="${k}" min="${min}" max="${max}" step="${step}" value="${+o[k] || 0}"><output>${(+o[k] || 0).toFixed(2)}</output></label>`;
    el.innerHTML = `<div class="row"><b class="grow" style="font-size:13px">${esc(ovLabel(o))}</b><span class="dim" style="font-size:11.5px">${esc(o.kind)}</span><button class="btn xs ghost danger" data-act="ov-del">✕ Delete</button></div>
      ${o.kind === "text" ? `<input type="text" data-ov="text" value="${esc(o.text)}" maxlength="80">` : ""}
      ${rng("t", "Starts at", 0, len, 0.05)}${rng("d", "Length", 0.2, len, 0.05)}
      ${o.kind === "sound" ? "" : rng("x", "Position ↔", 0, 1, 0.01) + rng("y", "Position ↕", 0, 1, 0.01) + rng("w", "Size", 0.05, 1, 0.01)}
      ${o.kind === "sound" || o.kind === "video" ? rng("volume", "Volume", 0, 1, 0.05) : ""}
      ${o.kind === "sound" ? "" : `<div class="seg">${["none", "pop", "fade", "shake", "slide"].map((v) => `<button class="${(o.anim || "none") === v ? "on" : ""}" data-act="ov-anim" data-v="${v}">${v}</button>`).join("")}</div>`}
      ${o.kind === "video" ? `<label class="switch"><input type="checkbox" data-ov="cutaway" ${o.cutaway ? "checked" : ""}> Cutaway (fills the frame)</label>` : ""}
      ${o.kind === "text" ? `<details open><summary class="muted" style="font-size:12px;cursor:pointer">Font &amp; style</summary><div id="st-ots" class="stack" style="gap:8px;margin-top:8px"></div></details>` : ""}`;
    if (o.kind === "text") {
      const [OW, OH] = outSize();
      textStyleEditor($("#st-ots"), { get: (k) => o[k], set: (k, v) => { if (v == null) delete o[k]; else o[k] = v; saveOverlays(); },
        def: { ...tsDefaults("over", null, OW, OH), size: Math.round((+o.w || 0.35) * OW * 0.22) }, sample: o.text || "Text" });
    }
  }
  function renderLib() {
    const el = $("#st-libl");
    if (!el) return;
    if (!T.assets) { el.innerHTML = `<span class="muted" style="font-size:12px">The media library isn't available yet.</span>`; return; }
    const q = T.aq.trim().toLowerCase();
    const list = T.assets.filter((a) => a.kind !== "font" && (!T.akind || a.kind === T.akind) && (!q || `${a.name} ${(a.tags || []).join(" ")}`.toLowerCase().includes(q)));
    el.innerHTML = list.length ? list.map((a) => {
      const src = `/assets/${esc(a.file)}`;
      const pv = a.kind === "sound" ? `<audio controls preload="none" src="${src}"></audio>` : a.kind === "video" ? `<video src="${src}#t=0.1" muted preload="metadata"></video>` : `<img src="${src}" alt="" loading="lazy">`;
      return `<div class="st-asset" draggable="true" data-asset="${esc(a.id)}" title="${esc(a.name)} — drag onto the preview">${pv}
        <div class="row" style="gap:4px"><span class="grow st-aname">${esc(a.name)}</span>${a.builtin ? "" : `<button class="st-x" data-act="asset-del" data-id="${esc(a.id)}" title="Delete from library">✕</button>`}</div>
        <button class="btn xs" data-act="asset-add" data-id="${esc(a.id)}">＋ At playhead</button></div>`;
    }).join("") : `<span class="muted" style="font-size:12px">No media matches.</span>`;
  }
  // ----- AI director: prompt -> plan (checklist) -> apply selected -----
  function planPreview(it) {
    const p = it.patch || {}, e = p.edit || {}, bits = [];
    if ("start" in p || "end" in p) bits.push(`trim ${fmt(p.start ?? c().start)} → ${fmt(p.end ?? c().end)}`);
    for (const o of e.overlays || []) bits.push(`${o.kind === "text" ? `“${o.text}”` : T.assets?.find((a) => a.id === o.asset_id)?.name || o.kind} at ${fmt(+o.t || 0)}`);
    const rest = Object.keys(e).filter((k) => k !== "overlays");
    if (rest.length) bits.push(rest.map((k) => (e[k] !== null && typeof e[k] === "object" ? k : `${k}: ${e[k]}`)).join(", "));
    const out = bits.join(" · ");
    return out.length > 160 ? out.slice(0, 157) + "…" : out;
  }
  function renderPlan() {
    const el = $("#st-plan");
    if (!el) return;
    const p = T.plan;
    if (!p) { el.innerHTML = ""; return; }
    el.innerHTML = `<div class="card stack" style="padding:12px;gap:8px;margin-top:8px">${p.summary ? `<div style="font-size:12.5px">${esc(p.summary)}</div>` : ""}
      ${(p.items || []).map((it) => `<label class="st-item"><input type="checkbox" data-plan="${esc(it.id)}" ${T.picked.has(it.id) ? "checked" : ""}>
        <span><b>${esc(it.title)}</b>${it.why ? `<span class="muted"> — ${esc(it.why)}</span>` : ""}<span class="dim mono st-pv">${esc(planPreview(it))}</span></span></label>`).join("") || `<span class="muted" style="font-size:12px">No suggestions this time.</span>`}
      <div class="row"><button class="btn xs ghost" data-act="plan-close">Dismiss</button><span class="grow"></span>
        <button class="btn sm primary" data-act="plan-apply" ${T.picked.size ? "" : "disabled"}>Apply selected (${T.picked.size})</button></div></div>`;
  }
  function reloadFromClip(fresh) {
    clearTimeout(T.ovTimer); T.ovTimer = null;
    T.clip = { ...c(), ...fresh };
    setTrack(c().edit.track || {});
    T.ovs = structuredClone(c().edit.overlays || []);
    if (!T.ovs.some((o) => o.id === T.osel)) T.osel = null;
    loadWords(); setRange(); renderPanel(); buildFx();
  }

  function renderTrack() {
    const el = $("#st-track"); if (!el) return;
    const e = c().edit, n = e.track?.source_counts || {}, running = T.job && !DONE.includes(T.job.status);
    const counts = [["face", "face"], ["anime", "VTuber face"], ["salient", "important area"], ["motion", "motion"]].filter(([key]) => n[key]).map(([key, l]) => `<span class="chip">${+n[key]} ${l}</span>`).join("");
    el.innerHTML = `<h3>Tracking</h3>
      <label class="switch"><input type="checkbox" id="st-follow" ${e.follow ? "checked" : ""}> Follow subject (moving crop)</label>
      ${running ? `<div class="stack" style="gap:6px"><div class="row" style="font-size:12px"><span class="spin"></span> ${esc(T.job.message || "Tracking…")} ${Math.round((T.job.progress || 0) * 100)}%</div>${bar(T.job.progress)}</div>`
        : `<div class="seg">${TRACK_MODES.map(([v, l]) => `<button class="${T.tmode === v ? "on" : ""}" data-act="tmode" data-v="${v}">${l}</button>`).join("")}</div>
          ${T.tmode === "target"
            ? `<div class="row wrap"><button class="btn sm" data-act="track-target" ${T.pick ? "" : "disabled"}>◎ Track this</button>${T.pick ? `<button class="btn xs ghost" data-act="pick-cancel">Cancel</button><span class="muted" style="font-size:12px">Box picked at ${fmt(T.pick.t, 2)}</span>` : `<span class="muted" style="font-size:12px">Pause, then drag a box around what to follow</span>`}</div>`
            : `<div class="row"><button class="btn sm" data-act="track">◎ ${T.points.length ? "Re-track with AI" : "Track with AI"}</button></div>`}
          <span class="muted" style="font-size:12px">${T.points.length ? `${T.points.length} path points` : "No AI path yet"}${T.target ? ` · target picked at ${fmt(+T.target.t, 2)}` : ""}</span>`}
      ${counts ? `<div class="chips" title="What the tracker locked on to, by sample">${counts}</div>` : ""}
      <div class="row"><span class="muted grow" style="font-size:12px">${T.manual.length ? `${T.manual.length} manual keyframe${T.manual.length > 1 ? "s" : ""}` : "No manual keyframes — pause and drag the frame to add one"}</span>
        ${T.manual.length ? `<button class="btn xs ghost danger" data-act="kf-clear">Clear all</button>` : ""}</div>
      ${T.manual.length ? `<div class="chips">${T.manual.map((q, i) => `<span class="chip st-kf"><button class="st-x" data-act="kf-jump" data-i="${i}" title="Jump here">◆ ${fmt(+q[0], 2)}${q[3] ? ` · ${(+q[3]).toFixed(1)}×` : ""}</button><button class="st-x" data-act="kf-del" data-i="${i}" title="Delete keyframe">✕</button></span>`).join("")}</div>` : ""}`;
    hint();
  }

  function renderWords() {
    const el = $("#st-words"); if (!el) return;
    T.wi = -1;
    $("#st-wsrc").textContent = T.custom ? "Edited captions (saved on this clip)" : "From the transcript — edit any word";
    el.innerHTML = T.words.length
      ? T.words.map((w, i) => `<input type="text" class="wd" data-i="${i}" value="${esc(w.w)}" title="${fmt(w.s, 2)}" style="width:calc(${Math.max(1, w.w.length)}ch + 16px)" spellcheck="false">`).join("")
      : `<span class="muted" style="font-size:12px">No words in this clip's range.</span>`;
  }
  async function saveWords() {
    wordTimer = null;
    try { await patch({ edit: { words: T.words.filter((w) => w.w.trim()) } }); } catch (e) { fail(e); }
  }

  function renderActions() {
    const el = $("#st-actions"); if (!el) return;
    const k = c(), ex = k.export || {}, current = ex.status === "done" && ex.rev === k.rev;
    el.innerHTML = ex.status === "rendering"
      ? `<div class="grow stack" style="gap:6px"><div class="row" style="font-size:12px"><span class="spin"></span> Rendering… ${Math.round((ex.progress || 0) * 100)}%</div>${bar(ex.progress)}</div>`
      : `<button class="btn" data-act="render">${current ? "↻ Re-render" : "⬇ Render"}</button>
         ${current ? `<a class="btn" href="/files/${esc(pid())}/${esc(ex.file)}" download="${esc(k.title.replace(/[\\/:*?"<>|]/g, "_"))}.mp4">Download</a>` : ""}
         ${current && ex.cover ? `<a href="/files/${esc(pid())}/${esc(ex.cover)}?r=${+k.rev}" target="_blank" title="Auto-picked cover (YouTube thumbnail / TikTok cover) at ${(+ex.cover_t || 0).toFixed(1)}s"><img src="/files/${esc(pid())}/${esc(ex.cover)}?r=${+k.rev}" alt="Cover" style="height:36px;border-radius:5px;border:1px solid var(--line-2);vertical-align:middle"></a>` : ""}
         ${ex.status === "failed" ? `<span style="color:var(--err);font-size:12px" title="${esc(ex.error)}">Render failed</span>` : ""}
         <div class="grow"></div><button class="btn primary" data-act="publish">⇪ Publish</button>`;
  }

  // ----- events -----
  async function onClick(ev) {
    const w = ev.target.closest(".wd");
    if (w) { video.pause(); seek(T.words[+w.dataset.i].s); return; }
    const a = ev.target.closest("[data-act]");
    if (!a) return;
    const k = c();
    try {
      switch (a.dataset.act) {
        case "play": video.paused ? video.play() : video.pause(); break;
        case "mode": setMode(a.dataset.v); break;
        case "set": k.edit = { ...k.edit, [a.dataset.k]: a.dataset.v }; $$(`[data-k="${a.dataset.k}"]`, a.parentElement).forEach((b) => b.classList.toggle("on", b === a)); await patch({ edit: { [a.dataset.k]: a.dataset.v } }); break;
        case "tmode":
          T.tmode = a.dataset.v;
          if (T.tmode === "target") setMode("source"); else T.pick = null; // picking happens on the source-view overlay
          renderTrack(); break;
        case "pick-cancel": T.pick = null; renderTrack(); break;
        case "track":
        case "track-target": {
          const target = a.dataset.act === "track-target" ? T.pick : null;
          if (a.dataset.act === "track-target" && !target) return;
          a.disabled = true;
          T.job = await api(`/api/clips/${cid}/track`, { method: "POST", body: target ? { mode: "target", target } : { mode: T.tmode } });
          if (target) { T.target = target; T.pick = null; }
          renderTrack(); toast(target ? "Tracking what you picked… the path appears when it's done." : "Tracking the subject… the path appears when it's done.", "ok");
          break;
        }
        case "kf-jump": video.pause(); seek(k.start + T.manual[+a.dataset.i][0]); break;
        case "kf-del": T.manual.splice(+a.dataset.i, 1); renderTrack(); await saveManual(); break;
        case "kf-clear": if (await confirmBox("Clear manual keyframes?", "The framing goes back to the AI path (or the static framing).", "Clear")) { T.manual = []; renderTrack(); await saveManual(); } break;
        case "words-reset":
          clearTimeout(wordTimer); wordTimer = null;
          await patch({ edit: { words: null } });
          c().edit = { ...c().edit, words: null }; loadWords(); renderWords(); toast("Captions reset to the transcript.", "ok");
          break;
        case "quick": $("#st-ask").value = a.dataset.q; // fall through: apply right away
        // eslint-disable-next-line no-fallthrough
        case "ask": {
          const txt = $("#st-ask").value.trim(); if (!txt) return;
          const b = $("[data-act=ask]"); b.disabled = true; b.innerHTML = `<span class="spin"></span>`;
          try {
            const r = await api(`/api/clips/${cid}/assist`, { body: { instruction: txt } });
            if (r.clip?.id) reloadFromClip(r.clip);
            toast(r.explanation || "Done.", "ok");
          } finally { const b2 = $("[data-act=ask]"); if (b2) { b2.disabled = false; b2.textContent = "Apply"; } }
          break;
        }
        case "tzin": case "tzout": { const span = T.range[1] - T.range[0], ns = a.dataset.act === "tzin" ? span / 1.5 : span * 1.5; setView(video.currentTime - ns / 2, ns); break; }
        case "version": {
          a.disabled = true;
          const nc = await api(`/api/clips/${cid}/version`, { method: "POST" });
          location.hash = `#/studio/${nc.id || nc.clip?.id}`;
          break;
        }
        case "akind": T.akind = a.dataset.v; $$("[data-act=akind]").forEach((b) => b.classList.toggle("on", b === a)); renderLib(); break;
        case "asset-add": { const as = T.assets?.find((x) => x.id === a.dataset.id); if (as) addOverlay(assetOv(as, {})); break; }
        case "asset-del": {
          const as = T.assets?.find((x) => x.id === a.dataset.id);
          if (!as) break;
          const used = Array.isArray(as.used_by) ? as.used_by.length : +as.used_by || 0;
          if (!(await confirmBox("Delete from your library?", `“${as.name}” will be removed${used ? ` — it's used in ${used} clip${used > 1 ? "s" : ""}` : ""}.`))) break;
          await api(`/api/assets/${encodeURIComponent(as.id)}`, { method: "DELETE" });
          T.assets = T.assets.filter((x) => x !== as); renderLib(); break;
        }
        case "add-text": { const v = $("#st-txt").value.trim(); if (!v) return; addOverlay({ kind: "text", text: v, w: 0.6 }); $("#st-txt").value = ""; break; }
        case "ov-anim": { const o = T.ovs.find((x) => x.id === T.osel); if (o) { o.anim = a.dataset.v; $$("[data-act=ov-anim]").forEach((b) => b.classList.toggle("on", b === a)); saveOverlays(); } break; }
        case "ov-del": T.ovs = T.ovs.filter((x) => x.id !== T.osel); T.osel = null; buildFx(); renderOvInspector(); saveOverlays(); break;
        case "plan": {
          a.disabled = true; a.innerHTML = `<span class="spin"></span> Thinking…`;
          try { T.plan = await api(`/api/clips/${cid}/plan`, { body: { prompt: $("#st-ask").value.trim() } }); T.picked = new Set(); renderPlan(); }
          finally { a.disabled = false; a.textContent = "✦ Get suggestions"; }
          break;
        }
        case "plan-close": T.plan = null; renderPlan(); break;
        case "plan-apply": {
          if (!T.plan || !T.picked.size) return;
          a.disabled = true;
          const n = T.picked.size, r = await api(`/api/clips/${cid}/apply`, { body: { plan_id: T.plan.plan_id, item_ids: [...T.picked] } });
          T.plan = null; T.picked = new Set();
          reloadFromClip(r.clip || r);
          toast(`Applied ${n} change${n > 1 ? "s" : ""}.`, "ok");
          break;
        }
        case "render": await api(`/api/clips/${cid}/export`, { method: "POST" }); k.export = { status: "rendering", progress: 0, rev: k.rev }; renderActions(); break;
        case "publish": openPublish(k, renderActions); break;
      }
    } catch (e) { fail(e); if (a.dataset.act.startsWith("track")) renderTrack(); }
  }
  function onInput(ev) {
    const t = ev.target;
    if (t.dataset.fr) {
      c().edit = { ...c().edit, framing: { ...fr(), [t.dataset.fr]: +t.value } };
      t.nextElementSibling.textContent = (+t.value).toFixed(2) + (t.dataset.fr === "zoom" ? "×" : "");
      clearTimeout(T.frTimer); T.frTimer = setTimeout(saveFraming, 400);
      return;
    }
    if (t.dataset.ov && (t.type === "range" || t.type === "text")) {
      const o = T.ovs.find((x) => x.id === T.osel);
      if (!o) return;
      o[t.dataset.ov] = t.type === "range" ? +t.value : t.value;
      if (t.type === "range") t.nextElementSibling.textContent = (+t.value).toFixed(2);
      if (t.dataset.ov === "text" || t.dataset.ov === "volume") buildFx();
      return saveOverlays();
    }
    if (t.id === "st-aq") { T.aq = t.value; return renderLib(); }
    if (!t.classList.contains("wd")) return;
    T.words[+t.dataset.i].w = t.value;
    t.style.width = `calc(${Math.max(1, t.value.length)}ch + 16px)`;
    T.custom = true; $("#st-wsrc").textContent = "Edited captions (saved on this clip)";
    clearTimeout(wordTimer);
    wordTimer = setTimeout(saveWords, 700);
  }
  async function onChange(ev) {
    const t = ev.target, k = c();
    if (t.dataset.plan) { t.checked ? T.picked.add(t.dataset.plan) : T.picked.delete(t.dataset.plan); return renderPlan(); }
    if (t.dataset.ov === "cutaway") { const o = T.ovs.find((x) => x.id === T.osel); if (o) { o.cutaway = t.checked; saveOverlays(); } return; }
    try {
      if (t.id === "st-title") { k.title = t.value; $("#st-name").textContent = t.value; await patch({ title: t.value }); }
      else if (t.id === "st-follow") { k.edit = { ...k.edit, follow: t.checked }; hint(); await patch({ edit: { follow: t.checked } }); renderTrack(); } // show what the server kept
      else if (t.type === "checkbox" && t.dataset.k) { k.edit = { ...k.edit, [t.dataset.k]: t.checked }; await patch({ edit: { [t.dataset.k]: t.checked } }); }
    } catch (e) { fail(e); }
  }

  // ----- light polling: only while a tracking job or a render is in flight -----
  this.refresh = async () => {
    if (!c() || T.dead) return;
    if (T.job && !DONE.includes(T.job.status)) {
      const r = await api(`/api/clips/${cid}/track`);
      T.job = r.job || { status: "done" };
      if (T.job.status === "done") {
        if (TRACK_MODES.some(([m]) => m === r.mode)) T.tmode = r.mode;
        if ("target" in r) T.target = r.target || null;
        setTrack(r); c().edit = { ...c().edit, follow: !!r.follow, track: { ...(c().edit.track || {}), points: r.points, manual: r.manual } };
        const p = await api(`/api/projects/${pid()}`).catch(() => null), fresh = p?.clips.find((x) => x.id === cid);
        if (fresh) c().edit.track = fresh.edit.track || c().edit.track; // picks up source_counts
        toast(`Tracking done — ${T.points.length} path points.`, "ok");
      } else if (T.job.status === "failed") toast(`Tracking failed: ${T.job.error || T.job.message || "unknown error"}`, "err", 7000);
      renderTrack();
    }
    if (c().export?.status === "rendering") {
      const p = await api(`/api/projects/${pid()}`), fresh = p.clips.find((x) => x.id === cid);
      if (fresh) { c().export = fresh.export; c().rev = fresh.rev; renderActions(); }
    }
  };
}

// ======================================================================
// Pipeline — every video from harvest to posted
// ======================================================================
function Pipeline() {
  let data = null, q = "", sig = "";
  this.every = 5000;
  const href = (u) => (/^#\/[\w\/-]*$/.test(u || "") ? u : null); // edit links stay inside the app
  const img = (u) => (/^(\/(?!\/)|https?:\/\/)/.test(u || "") ? u : null);
  this.mount = async () => {
    view.innerHTML = `<div class="page"><div class="row wrap" style="align-items:flex-end"><div class="grow"><h1>Pipeline</h1>
        <p class="sub" style="margin:0">Every video from harvest to posted. Posted videos are locked — make a new version to change one.</p></div>
        <input type="text" id="pl-q" placeholder="Filter by title" style="max-width:260px"></div><div id="pl-body" style="margin-top:18px"></div></div>`;
    $("#pl-q").oninput = (e) => { q = e.target.value.trim().toLowerCase(); render(); };
    view.onclick = async (e) => {
      const b = e.target.closest("[data-ver]");
      if (!b) return;
      b.disabled = true;
      try { const nc = await api(`/api/clips/${b.dataset.ver}/version`, { method: "POST" }); location.hash = `#/studio/${nc.id || nc.clip?.id}`; }
      catch (err) { fail(err); b.disabled = false; }
    };
    await this.refresh();
  };
  this.refresh = async () => {
    try { data = await api("/api/pipeline"); } catch (e) { data = { error: e.status === 404 ? "" : e.message }; }
    const next = JSON.stringify(data);
    if (next !== sig) { sig = next; render(); }
  };
  function card(it, uploaded) {
    const th = img(it.thumb), edit = href(it.edit_url);
    const posts = (it.posts || []).map((p) => /^https?:\/\//.test(p.url || "")
      ? `<a class="pl-link" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(PLATFORM[p.platform] || p.platform)} ↗</a>`
      : `<span class="dim">${esc(PLATFORM[p.platform] || p.platform)}${p.publish_at ? ` · public ${esc(when(p.publish_at))}` : ""}</span>`).join("");
    return `<div class="pl-card">${th ? `<img src="${esc(th)}" alt="" loading="lazy">` : `<div class="pl-ph">▶</div>`}
      <div class="pl-t" title="${esc(it.title)}">${esc(it.title || "Untitled")}</div>
      <div class="pl-meta">${it.duration ? `<span>${fmt(+it.duration, 0)}</span>` : ""}${it.aspect ? `<span class="chip">${esc(it.aspect)}</span>` : ""}${it.type ? `<span>${esc(it.type)}</span>` : ""}</div>
      ${(it.pending || []).length ? `<div class="chips">${it.pending.map((p) => `<span class="chip">${esc(PLATFORM[p.platform] || p.platform)} · ${esc(p.status)}${p.when ? ` · ${esc(when(p.when))}` : ""}</span>`).join("")}</div>` : ""}
      ${uploaded ? `<div class="stack" style="gap:4px">${posts}<span class="dim" style="font-size:11.5px">🔒 Posted — locked</span>
          ${!it.type || it.type === "clip" ? `<button class="btn xs" data-ver="${esc(it.id)}">Make a new version</button>` : ""}</div>`
        : edit ? `<a class="btn xs" href="${esc(edit)}">Edit</a>` : ""}</div>`;
  }
  function render() {
    const el = $("#pl-body");
    if (!el) return;
    if (!data || data.error !== undefined) { el.innerHTML = `<div class="card empty"><b>The pipeline view isn't available yet</b>${esc(data?.error || "It appears once the server supports it.")}</div>`; return; }
    const match = (it) => !q || String(it.title || "").toLowerCase().includes(q);
    el.innerHTML = `<div class="pl-board">${(data.stages || []).map((st) => {
      const items = (st.items || []).filter(match), up = st.key === "uploaded";
      return `<div class="pl-col"><h3><span>${esc(st.label || st.key)}</span><span class="chip">${items.length}${q ? ` / ${(st.items || []).length}` : ""}</span></h3>
        ${items.map((it) => card(it, up)).join("") || `<div class="dim" style="font-size:12px;padding:8px 2px">Nothing here${q ? " matches" : ""}.</div>`}</div>`;
    }).join("")}</div>`;
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
    const [conn, ups, yt] = await Promise.all([api("/api/connections"), api("/api/uploads"),
      S.conn?.youtube?.connecting || S.conn?.youtube?.needs_reconnect ? api("/api/youtube").catch(() => null) : null]); // live YouTube sign-in status while Google is open
    if (yt) conn.youtube = { ...conn.youtube, ...yt };
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
    const ytBody = yt.connected ? (yt.needs_reconnect ? `<p style="color:var(--warn);font-size:12.5px;margin:0 0 8px">Reelwright needs one more YouTube permission (to move videos that are already scheduled). Disconnect, then <b>Sign in with Google</b> again and approve it. Uploads keep working meanwhile.${yt.missing_scopes?.length ? `<br><span class="dim mono" style="font-size:11px">Missing: ${yt.missing_scopes.map(esc).join(", ")}</span>` : ""}</p>` : "") : !yt.has_secret ? `<ol class="steps"><li>In <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank">Google Cloud</a>, enable <b>YouTube Data API v3</b>.</li>
        <li>Configure the OAuth consent screen and add yourself as a test user.</li><li>Create an OAuth client of type <b>Desktop app</b>, download the JSON, and upload it:</li></ol>
        <div style="margin-top:10px"><input type="file" id="secret" accept=".json" hidden><button class="btn primary sm" data-yt="pick">Upload client JSON</button></div>`
      : `<div class="row wrap"><button class="btn primary sm" data-yt="connect" ${yt.connecting ? "disabled" : ""}>${yt.connecting ? '<span class="spin"></span> Waiting for Google…' : "Sign in with Google"}</button>
        <button class="btn ghost sm" data-yt="pick">Replace client JSON</button><input type="file" id="secret" accept=".json" hidden></div>`;
    const ttBody = tt.connected ? "" : `<p class="muted" style="font-size:12.5px;margin:0 0 8px">Create an app at <a href="https://developers.tiktok.com/apps" target="_blank">TikTok for Developers</a> with Login Kit + Content Posting API (scopes <code>user.info.basic</code>, <code>video.publish</code>). Add this redirect URI:</p>
        <div class="mono" style="background:#0d0e14;padding:6px 8px;border-radius:6px;margin-bottom:8px;word-break:break-all">${esc(tt.redirect_uri)}</div>
        <div class="two"><input type="text" id="ttk" placeholder="Client key"><input type="password" id="tts" placeholder="Client secret"></div>
        <div class="row" style="margin-top:8px"><button class="btn sm" data-tt="save">Save app</button><button class="btn primary sm" data-tt="connect" ${tt.has_app ? "" : "disabled"}>Sign in with TikTok</button></div>
        <p class="dim" style="font-size:11.5px;margin:8px 0 0">Until TikTok audits your app, posts are private (only you can see them).</p>`;
    const igBody = ig.connected ? "" : `<p class="muted" style="font-size:12.5px;margin:0 0 6px">Needs an Instagram <b>Business or Creator</b> account. Either way works:</p>
        <ul class="list" style="font-size:12.5px;margin:0 0 8px"><li><b>Instagram Login</b> (simplest, no Facebook Page): in your <a href="https://developers.facebook.com/apps" target="_blank">Meta app</a> → Instagram → <i>API setup with Instagram login</i>, add your account under <i>Generate access tokens</i>. Use the <b>Instagram App ID</b> shown there (not the Meta/Facebook App ID) and scopes <code>instagram_business_basic</code>, <code>instagram_business_content_publish</code>. Paste the token; the account ID is optional.</li>
          <li><b>Facebook Login</b>: Instagram account linked to a Facebook Page; token with <code>instagram_basic</code>, <code>instagram_content_publish</code>, <code>pages_show_list</code>, <code>pages_read_engagement</code>. Reelwright finds the Instagram account through your Page.</li></ul>
        <p class="dim" style="font-size:11.5px;margin:0 0 8px">“Invalid platform app” from Meta means the Facebook App ID was used where the Instagram App ID belongs, or old scopes were requested.</p>
        <input type="password" id="igt" placeholder="Access token" style="margin-bottom:8px"><input type="text" id="igu" placeholder="Instagram account ID (optional — found automatically)">
        <div style="margin-top:8px"><button class="btn primary sm" data-ig="save">Connect Instagram</button></div>`;
    $("#conns").innerHTML = `<p class="muted" style="font-size:12.5px;margin:0 0 4px">Every permission each platform needs, and step-by-step portal setup: <a href="/guide/connect.html" target="_blank">Connection guide</a>.</p>
      <div class="card">${head("youtube", yt.connected, yt.channel?.title, yt.channel?.thumb, yt.channel ? `${num(yt.channel.subscribers)} subscribers` : "")}${ytBody}${yt.error ? `<p style="color:var(--err);font-size:12px">${esc(yt.error)}</p>` : ""}</div>
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
// Schedule: every pending post on a week calendar — move, re-platform, remove
// ======================================================================
function Schedule() {
  this.every = 5000;
  let week = 0, filter = "all", data = null, sig = "";
  const SRC = { yours: "from your own results", research: "from live research", default: "default — no data yet" };
  const dayStart = (offset) => { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() + offset); return d; };
  this.mount = async () => {
    view.innerHTML = `<div class="page"><h1>Schedule</h1><p class="sub">Every post waiting to go out. Drag a post to another hour to move it, or click it to change its time, platform or remove it. Shading shows the best hours for the selected platform.</p>
      <div id="sch-banner"></div>
      <div class="row wrap" style="margin-bottom:12px"><button class="btn sm" data-wk="-1">‹ Prev</button><button class="btn sm" data-wk="0">This week</button><button class="btn sm" data-wk="1">Next ›</button>
        <b id="sch-range" style="margin:0 8px"></b><span class="grow"></span>
        <div class="seg" id="sch-filter">${["all", ...Object.keys(PLATFORM)].map((p) => `<button data-f="${p}" class="${p === filter ? "on" : ""}">${p === "all" ? "All" : PLATFORM[p]}</button>`).join("")}</div></div>
      <div class="dim" id="sch-source" style="font-size:12px;margin-bottom:8px"></div>
      <div class="card" style="overflow-x:auto;padding:10px"><div id="sch-grid" class="sch-grid"></div></div>
      <div class="card" style="margin-top:16px"><h2>Posting as soon as ready</h2><div id="sch-asap" class="stack"></div></div></div>`;
    view.onclick = onClick;
    await this.refresh();
  };
  this.refresh = async () => {
    data = await api("/api/schedule");
    const s = JSON.stringify([data, week, filter]);
    if (s === sig) return; sig = s;
    render();
  };
  const heatAt = (d, h) => {
    const plats = filter === "all" ? Object.keys(PLATFORM) : [filter];
    return Math.max(...plats.map((p) => data.heat[p].scores[d * 24 + h][2]));
  };
  const chip = (it) => `<div class="sch-post plat-${it.platform}" draggable="true" data-post="${esc(it.id)}" title="${esc(it.title)} — ${esc(PLATFORM[it.platform])}, ${esc(UPLOAD_STATUS[it.status]?.[1] || it.status)}${it.error ? " — " + esc(it.error) : ""}">
      ${it.cover ? `<img src="${esc(it.cover)}" alt="">` : ""}<span>${esc(PLATFORM[it.platform].slice(0, 2))}</span> ${esc((it.title || "").slice(0, 26))}${it.auto ? " ⚡" : ""}${it.status === "held" ? " ⏸" : ""}${it.error ? " ⚠" : ""}</div>`;
  const render = () => {
    $("#sch-banner").innerHTML = data.paused ? `<div class="card row" style="border-color:var(--warn);margin-bottom:12px"><span class="grow">⏸ <b>Autopilot is paused.</b> Its posts are held (⏸) and won't go out until you resume.</span><button class="btn sm primary" id="sch-resume">Resume Autopilot</button></div>`
      : `<div class="row" style="margin-bottom:12px"><span class="grow dim" style="font-size:12.5px">⚡ Autopilot is running — posts marked ⚡ go out on their own.</span><button class="btn sm" id="sch-pause" title="Stops new scheduling and holds Autopilot's queued posts (nothing is deleted)">⏸ Pause Autopilot</button></div>`;
    $$("#sch-filter button").forEach((b) => b.classList.toggle("on", b.dataset.f === filter));
    const first = dayStart(week * 7), last = dayStart(week * 7 + 6);
    $("#sch-range").textContent = `${first.toLocaleDateString([], { month: "short", day: "numeric" })} – ${last.toLocaleDateString([], { month: "short", day: "numeric" })}`;
    $("#sch-source").textContent = "Best-time shading: " + (filter === "all" ? Object.keys(PLATFORM).map((p) => `${PLATFORM[p]} ${SRC[data.heat[p].source]}`).join(" · ") : `${PLATFORM[filter]} ${SRC[data.heat[filter].source]}`);
    const items = data.items.filter((it) => filter === "all" || it.platform === filter);
    const byCell = {};
    for (const it of items) {
      if (!it.when) continue;
      const t = new Date(it.when), day = Math.floor((t - first) / 864e5);
      if (day < 0 || day > 6) continue;
      (byCell[`${day}:${t.getHours()}`] ||= []).push(it);
    }
    let html = `<div class="sch-h"></div>` + [...Array(7)].map((_, d) => { const dt = dayStart(week * 7 + d); return `<div class="sch-h">${dt.toLocaleDateString([], { weekday: "short", day: "numeric" })}</div>`; }).join("");
    for (let h = 0; h < 24; h++) {
      html += `<div class="sch-hour">${String(h).padStart(2, "0")}:00</div>`;
      for (let d = 0; d < 7; d++) {
        const dt = dayStart(week * 7 + d), wd = (dt.getDay() + 6) % 7, past = new Date(dt.getTime() + (h + 1) * 36e5) < new Date();
        const heat = heatAt(wd, h);
        html += `<div class="sch-cell${past ? " past" : ""}" data-day="${d}" data-hour="${h}" style="--heat:${heat.toFixed(2)}">${(byCell[`${d}:${h}`] || []).map(chip).join("")}</div>`;
      }
    }
    $("#sch-grid").innerHTML = html;
    const asap = items.filter((it) => !it.when);
    $("#sch-asap").innerHTML = asap.length ? asap.map((it) => `<div class="row">${chip(it)}<span class="dim" style="font-size:12px">${esc(UPLOAD_STATUS[it.status]?.[1] || it.status)}</span></div>`).join("") : `<div class="dim" style="font-size:12.5px">Nothing waiting.</div>`;
    wireDnD();
  };
  const move = async (id, body) => {
    try { await api(`/api/uploads/${id}`, { method: "PATCH", body }); sig = ""; await this.refresh(); toast("Updated.", "ok"); }
    catch (err) { fail(err); }
  };
  const wireDnD = () => {
    $$(".sch-post").forEach((el) => el.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", el.dataset.post); el.classList.add("dragging"); }));
    $$(".sch-cell").forEach((cell) => {
      cell.addEventListener("dragover", (e) => { if (!cell.classList.contains("past")) { e.preventDefault(); cell.classList.add("over"); } });
      cell.addEventListener("dragleave", () => cell.classList.remove("over"));
      cell.addEventListener("drop", (e) => {
        e.preventDefault(); cell.classList.remove("over");
        const id = e.dataTransfer.getData("text/plain"), it = data.items.find((x) => x.id === id);
        const t = dayStart(week * 7 + +cell.dataset.day); t.setHours(+cell.dataset.hour, it?.when ? new Date(it.when).getMinutes() : 0, 0, 0);
        move(id, { when: t.toISOString() });
      });
    });
  };
  const edit = (it) => {
    modal(`<h2>${esc(it.title || "Post")}</h2><p class="muted" style="margin-top:-4px">${pill(UPLOAD_STATUS, it.status)} ${it.auto ? '<span class="chip">⚡ Autopilot</span>' : ""}</p>
      <div class="stack">
        <label class="field">Platform<select id="se-plat">${Object.entries(PLATFORM).map(([k, v]) => `<option value="${k}" ${k === it.platform ? "selected" : ""}>${v}${data.connected[k] ? "" : " (not connected)"}</option>`).join("")}</select></label>
        <div class="row"><label class="field grow">Date &amp; time<input type="datetime-local" id="se-time" value="${it.when ? toLocalInput(new Date(it.when)) : ""}"></label>
          <button class="btn" id="se-best" style="align-self:flex-end">★ Best time</button></div>
        <p class="dim" style="font-size:12px;margin:0">YouTube posts upload ahead and go public at this time; TikTok and Instagram post at this time (Reelwright must be running).</p>
        ${it.error ? `<div style="color:var(--err);font-size:12.5px">${esc(it.error)}</div>` : ""}
      </div>
      <div class="foot"><button class="btn danger" id="se-del">Remove</button><button class="btn" id="se-now">Post as soon as ready</button><span class="grow"></span><button class="btn" data-close>Cancel</button><button class="btn primary" id="se-save">Save</button></div>`, (m, close) => {
      $("#se-best", m).onclick = async () => {
        try {
          const r = await api("/api/schedule/suggest", { body: { platform: $("#se-plat", m).value, category_id: it.category_id, current: it.when } });
          if (r.times[0]) $("#se-time", m).value = toLocalInput(new Date(r.times[0]));
          toast(`Best time ${SRC[r.source]}.`);
        } catch (err) { fail(err); }
      };
      $("#se-save", m).onclick = async () => {
        const v = $("#se-time", m).value;
        if (v && new Date(v) < new Date()) return toast("Pick a time in the future.", "err");
        close(); await move(it.id, { when: v ? fromLocalInput(v) : null, platform: $("#se-plat", m).value });
      };
      $("#se-now", m).onclick = async () => { close(); await move(it.id, { when: null, platform: $("#se-plat", m).value }); };
      $("#se-del", m).onclick = async () => {
        close();
        if (!(await confirmBox("Remove this post?", "It won't be posted. The clip itself stays in your project.", "Remove"))) return;
        try { await api(`/api/uploads/${it.id}`, { method: "DELETE" }); sig = ""; await this.refresh(); } catch (err) { fail(err); }
      };
    });
  };
  const onClick = async (e) => {
    const t = e.target;
    if (t.closest("#sch-resume, #sch-pause")) { await api("/api/autopilot", { method: "PUT", body: { paused: !!t.closest("#sch-pause") } }).catch(fail); sideStatus(); sig = ""; return this.refresh(); }
    if (t.dataset.wk !== undefined) { week = +t.dataset.wk === 0 ? 0 : week + +t.dataset.wk; sig = ""; return this.refresh(); }
    if (t.dataset.f) { filter = t.dataset.f; sig = ""; return this.refresh(); }
    const p = t.closest("[data-post]");
    if (p) { const it = data.items.find((x) => x.id === p.dataset.post); if (it) edit(it); }
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
          <label class="switch"><input type="checkbox" id="vj" ${s.visual_judge !== false ? "checked" : ""}> Let Claude see a few small frames when ranking clips from long videos <span class="muted" style="font-size:12px">(better picks on gaming/VTuber streams; uses more tokens — about 4 images for each of up to 12 candidates)</span></label>
          ${s.anthropic_key_set ? `<div><button type="button" class="btn sm danger" id="rmkey">Remove key</button></div>` : ""}
        </div>
        <div class="card stack"><h2>Transcription</h2>
          <div class="row wrap"><label class="field grow">Whisper model<select id="wm">${opt([["large-v3-turbo", "large-v3-turbo — best speed/accuracy (default)"], ["large-v3", "large-v3 — most accurate"], ["medium", "medium"], ["small", "small — fast, lighter"], ["base", "base — fastest"]], s.whisper_model)}</select></label>
          <label class="field grow">Run on<select id="wd">${opt([["auto", "Auto (GPU if available)"], ["cuda", "NVIDIA GPU"], ["cpu", "CPU"]], s.whisper_device)}</select></label></div>
        </div>
        <div class="card stack"><h2>Clip finding</h2>
          <div class="row wrap"><label class="field grow">Clips per video<input type="number" id="cc" min="1" max="40" value="${+s.clip_count}"></label>
          <label class="field grow">Min length (s)<input type="number" id="cmin" min="3" max="600" value="${+s.clip_min}"></label>
          <label class="field grow" title="Clips aim for about this length. 0 = halfway between min and max.">Target length (s)<input type="number" id="ctg" min="0" max="900" value="${+s.clip_target || 0}"></label>
          <label class="field grow" title="Hard cap: no AI-found clip is ever longer than this.">Max length (s)<input type="number" id="cmax" min="5" max="900" value="${+s.clip_max}"></label></div>
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
      <div class="card stack" style="margin-top:20px" id="uiscale"><div class="row wrap"><h2 class="grow" style="margin:0">Interface size</h2><button class="btn sm" data-uireset="all" title="Every part back to 100% of the automatic size for this window">⤢ Fit to screen / Reset</button></div>
        <p class="muted" style="margin:0;font-size:12.5px">The base size follows your window automatically (now ${Math.round(uiAuto() * 100)}% for ${innerWidth} px); these multiply on top. Changes apply as you drag and are kept across restarts and updates. <span class="kbd">Ctrl</span>+<span class="kbd">=</span> / <span class="kbd">Ctrl</span>+<span class="kbd">-</span> / <span class="kbd">Ctrl</span>+<span class="kbd">0</span> change the whole app.</p>
        ${Object.entries(UI_KEYS).map(([k, l]) => `<div class="row"><label class="slider grow ui-row"><span>${l}</span><input type="range" data-ui="${k}" min="60" max="200" step="5" value="${Math.round(S.ui[k] * 100)}"><output>${Math.round(S.ui[k] * 100)}%</output></label><button class="btn xs ghost" data-uireset="${k}" title="Back to 100%">Reset</button></div>`).join("")}</div>
      <div class="card stack" style="margin-top:20px"><div class="row wrap"><h2 class="grow" style="margin:0">Diagnostics</h2>
        <button class="btn sm" id="doc">Run checks</button><button class="btn sm" id="docd" title="Also verifies the API key with Anthropic">Deep check</button></div>
        <div id="docout" class="dim" style="font-size:12.5px">Checks FFmpeg, GPU encoding and transcription, Claude, disk space, the video downloader, the OBS link, publishing and learning.</div></div>
      <div class="card stack" style="margin-top:20px"><h2 style="margin:0">Legal</h2>
        <p class="muted" style="margin:0;font-size:13px">Reelwright runs on this computer and has no servers of its own. See exactly what is stored here and what is sent to the services you connect.</p>
        <div class="row wrap"><a class="btn sm" href="/legal/privacy.html" target="_blank">Privacy Policy</a><a class="btn sm" href="/legal/terms.html" target="_blank">Terms of Use</a><a class="btn sm" href="/guide/connect.html" target="_blank">Connection guide (permissions &amp; setup)</a></div></div></div>`;
    const runDoctor = async (deep) => {
      $("#docout").innerHTML = `<span class="spin"></span> Checking…`;
      try {
        const res = await api(`/api/doctor${deep ? "?deep=true" : ""}`);
        const P = { ok: ["ok", "OK"], warn: ["warn", "Check"], fail: ["err", "Problem"] };
        $("#docout").innerHTML = `<table style="width:100%"><tbody>${res.map((r) => `<tr><td style="width:96px">${pill(P, r.status)}</td><td style="width:150px"><b>${esc(r.name)}</b></td><td>${esc(r.detail)}${r.fix ? `<div class="dim" style="font-size:11.5px">→ ${esc(r.fix)}</div>` : ""}</td></tr>`).join("")}</tbody></table>`;
      } catch (e) { $("#docout").textContent = e.message; }
    };
    const ui = $("#uiscale");
    ui.oninput = (e) => { const t = e.target; if (!t.dataset.ui) return; t.nextElementSibling.textContent = t.value + "%"; applyUi({ ...S.ui, [t.dataset.ui]: t.value / 100 }); saveUi(); };
    ui.onclick = (e) => {
      const k = e.target.dataset.uireset; if (!k) return;
      applyUi(k === "all" ? {} : { ...S.ui, [k]: 1 }); saveUi();
      $$("[data-ui]", ui).forEach((r) => { r.value = Math.round(S.ui[r.dataset.ui] * 100); r.nextElementSibling.textContent = r.value + "%"; });
    };
    $("#doc").onclick = () => runDoctor(false);
    $("#docd").onclick = () => runDoctor(true);
    $("#rmkey")?.addEventListener("click", async () => { await api("/api/settings", { method: "PUT", body: { anthropic_api_key: "" } }).catch(fail); toast("Key removed."); sideStatus(); route(); });
    $("#sf").onsubmit = async (e) => {
      e.preventDefault();
      const body = {
        model: $("#model").value, whisper_model: $("#wm").value, whisper_device: $("#wd").value, clip_count: +$("#cc").value, clip_min: +$("#cmin").value,
        clip_max: +$("#cmax").value, clip_target: +$("#ctg").value || 0, default_aspect: $("#da").value, default_privacy: $("#dp").value, caption_uppercase: $("#cu").checked, caption_color: $("#ccol").value,
      };
      if (body.clip_min >= body.clip_max) return toast("Min length must be below max length.", "err");
      if (body.clip_target && (body.clip_target < body.clip_min || body.clip_target > body.clip_max)) return toast("Target length must be between min and max (or 0 for automatic).", "err");
      if ($("#key").value.trim()) body.anthropic_api_key = $("#key").value.trim();
      body.visual_judge = $("#vj").checked;
      try { S.settings = await api("/api/settings", { method: "PUT", body }); toast("Settings saved.", "ok"); sideStatus(); route(); } catch (err) { fail(err); }
    };
  };
}

$("#side-status").addEventListener("click", async (e) => {
  if (!e.target.closest("#ap-pause")) return;
  try {
    const r = await api("/api/autopilot", { method: "PUT", body: { paused: !S.settings?.autopilot_paused } });
    toast(r.paused ? "Autopilot paused everywhere. Its queued posts are held until you resume." : `Autopilot resumed${r.held ? "" : ""}.`, "ok");
    await sideStatus();
    current?.refresh?.();
  } catch (err) { fail(err); }
});

// ---------- boot ----------
sideStatus().then(route);
setInterval(sideStatus, 5000);
if (localStorage.lastProject) $("#nav-editor").hidden = false;
if (localStorage.lastClip) $("#nav-studio").hidden = false;
