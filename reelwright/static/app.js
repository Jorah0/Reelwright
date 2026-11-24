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
  if (kind === "headline") return hlTsDefaults(e); // the theme's look
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

// ---------- headline banner: themes from /api/headline/themes; spec/fit/layout mirror media.headline_spec/fit_headline/headline_layout ----------
const HL = { themes: null, auto: {}, custom: null, default_on: false, vt: "" }; // vt = the open project's video type (theme "auto")
const HL_DEF = { on: false, text: "", kicker: "", theme: "auto", position: "top", show: "whole", seconds: 3, push: false };
let hlP = null;
const loadHeadline = (force = false) => { if (force) hlP = null; return (hlP ||= api("/api/headline/themes").then((d) => Object.assign(HL, d)).catch(() => HL)); };
const vtOf = (p) => (typeof p?.video_type === "string" ? p.video_type : p?.video_type?.type) || "";
const hlId = (h) => (h.theme === "auto" ? HL.auto[HL.vt] || "bold_bar" : h.theme);
const hlTheme = (id) => ({ ...HL.themes.bold_bar, ...(id === "custom" ? HL.custom || {} : HL.themes[id] || {}) });
const hlName = (id) => (id === "auto" ? `Auto (${hlTheme(HL.auto[HL.vt] || "bold_bar").name})` : id === "custom" ? "My theme" : HL.themes?.[id]?.name || id);
function hlSpec(e, force = false) { // force: draw even when off or empty (gallery cards)
  const h = { ...HL_DEF, ...(e?.headline || {}) };
  if (!HL.themes || (!force && (!h.on || !String(h.text || "").trim()))) return null;
  const over = Object.fromEntries(TS_KEYS.filter((k) => e?.[`headline_${k}`] != null).map((k) => [k, e[`headline_${k}`]]));
  return { ...hlTheme(hlId(h)), ...over, ...h, theme: hlId(h) };
}
const hlTsDefaults = (e) => { const th = HL.themes ? hlTheme(hlId({ ...HL_DEF, ...(e?.headline || {}) })) : tsDefaults("title", e, 1080, 1920); return Object.fromEntries(TS_KEYS.map((k) => [k, th[k]])); };
function hlFit(text, size, cw, maxW) {
  const words = text.split(/\s+/).filter(Boolean), low = Math.round(size * 0.5);
  let s = Math.round(size), n = 1, lines = [];
  for (;;) {
    n = Math.max(Math.floor(maxW / (s * cw)), 1);
    lines = words.reduce((a, w) => { if (a.length && a.at(-1).length + 1 + w.length <= n) a[a.length - 1] += " " + w; else a.push(w); return a; }, []);
    if ((lines.length <= 2 && lines.every((l) => l.length <= n)) || s <= low) break;
    s = Math.max(low, Math.floor(s * 0.92));
  }
  if (lines.length > 2 || lines.some((l) => l.length > n)) { lines = lines.slice(0, 2).map((l) => l.slice(0, n)); lines[lines.length - 1] = lines.at(-1).slice(0, Math.max(n - 1, 1)).trimEnd() + "…"; }
  return [lines, s];
}
function hlLayout(sp, W, H, seam) {
  const k = W / 1080, up = (s) => (sp.uppercase ? s.toUpperCase() : s), even = (v) => Math.max(Math.round(v) - (Math.round(v) % 2), 2);
  const text = up(String(sp.text || "").trim()), kick = up(String(sp.kicker || "").trim());
  const m = Math.round(40 * k), pad = Math.round(sp.pad * k), gap = Math.round(H * 0.02), kfs = Math.round(Math.max(sp.size * (sp.kicker_scale || 0.5), 32) * k);
  const [kw, kh] = kick ? [Math.round(kick.length * kfs * 0.66 + kfs * 1.2), Math.round(kfs * 1.55)] : [0, 0];
  const left = !!kick && sp.kicker_layout === "left", bx = m, bw = W - 2 * m, tx = bx + pad + (left ? kw + pad : 0);
  const [lines, fs] = hlFit(text, sp.size * k, sp.cw, bx + bw - pad - tx);
  const bh = Math.max(Math.round(lines.length * fs * 1.18 + 2 * pad), left ? kh + 2 * pad : 0);
  const lift = kick && !left ? (sp.kicker_bg ? Math.round(kh * 0.6) : kh) : 0, pos = sp.position === "seam" && !seam ? "top" : sp.position;
  let by, band = null;
  if (pos === "seam") by = Math.round(seam * H - bh / 2);
  else if (sp.push) { const b = even(bh + lift + 2 * gap); band = pos === "top" ? [0, b] : [H - b, b]; by = band[0] + gap + lift; }
  else by = pos === "bottom" ? H - Math.round(H * 0.07) - bh : Math.round(H * 0.07) + lift;
  by = Math.min(Math.max(by, lift), H - bh);
  return { k, lines, fs, kick, kfs, kh, left, tx, band, bar: [bx, by, bw, bh], kbox: left ? [bx, by, kw + pad, bh] : kick ? [Math.floor((W - kw) / 2), by - lift, kw, kh] : null };
}
// Draws the banner into el (an absolutely-positioned layer over a W-wide output frame); s = screen px per output px.
function hlRender(el, sp, L, s) {
  const px = (v) => (v * s).toFixed(1) + "px", [x, y, w, h] = L.bar, r = sp.radius * L.k * s;
  const rgba = (hex, a) => { const n = parseInt(hexOr(hex, "#000000").slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };
  const box = (b) => `left:${px(b[0])};top:${px(b[1])};width:${px(b[2])};height:${px(b[3])}`;
  const bar = sp.bar ? `<div class="hl-bar" style="${box(L.bar)};border-radius:${r}px;background:${sp.bar2 ? `linear-gradient(90deg,${rgba(sp.bar, sp.bar_opacity)},${rgba(sp.bar2, sp.bar_opacity)})` : rgba(sp.bar, sp.bar_opacity)};${
    sp.bar_border ? `border:${px(sp.bar_border * L.k)} solid ${hexOr(sp.bar_border_color, "#000000")};` : ""}${sp.glow ? `box-shadow:0 0 ${px(sp.glow * 4 * L.k)} ${hexOr(sp.bar_border_color, "#ffffff")};` : ""}${sp.blur ? "backdrop-filter:blur(8px);" : ""}">${
    sp.accent ? `<i style="background:${hexOr(sp.accent, "#ffffff")};${sp.accent_side === "left" ? `left:0;top:0;bottom:0;width:${px(10 * L.k)}` : `left:0;right:0;bottom:0;height:${px(7 * L.k)}`}"></i>` : ""}</div>` : "";
  const kb = L.kbox, kick = kb ? `<div class="hl-kick" style="${box(kb)};border-radius:${L.left ? r : (kb[3] * s) / 2}px;${sp.kicker_bg ? `background:${hexOr(sp.kicker_bg, "#000000")}` : ""}"><span>${esc(L.kick)}</span></div>` : "";
  const txt = `<div class="hl-txt${L.left ? " left" : ""}" style="left:${px(L.left ? L.tx : x)};top:${px(y)};width:${px(L.left ? x + w - L.tx : w)};height:${px(h)}"><span>${L.lines.map(esc).join("<br>")}</span></div>`;
  const band = L.band ? `<div class="hl-band" style="left:0;right:0;top:${px(L.band[0])};height:${px(L.band[1])}"></div>` : "";
  el.innerHTML = `${band}<div class="hl-g" style="transform-origin:${px(x + w / 2)} ${px(y + h / 2)};${sp.tilt ? `transform:rotate(${-sp.tilt}deg)` : ""}">${bar}${kick}${txt}</div>`;
  // ASS s is the full line cell; CSS font-size the em box (smaller), so ~0.72 of it (measured against a render) draws the same glyph size as the render
  styleText($(".hl-txt span", el), { font: sp.font, size: L.fs * 0.72, weight: sp.weight, color: sp.color, outline: sp.outline * L.k, outline_color: sp.outline_color, shadow: sp.shadow * L.k, spacing: sp.spacing * L.k }, s);
  if (sp.glow) $(".hl-txt span", el).style.textShadow = `0 0 ${px(sp.glow * 2)} ${hexOr(sp.outline_color, "#ffffff")}`;
  if (kb) styleText($(".hl-kick span", el), { font: sp.kicker_font, size: L.kfs * 0.72, weight: "bold", color: sp.kicker_color, outline: sp.kicker_bg ? 0 : Math.max(sp.outline, 2) * L.k, outline_color: sp.outline_color, shadow: sp.kicker_bg ? 0 : 2 * L.k, spacing: 0 }, s);
}
// Theme gallery card: the theme drawn live (its fonts, colours, bar) on a small stand-in frame.
function hlCard(el, id, text, kicker) {
  const sp = { ...hlTheme(id === "auto" ? HL.auto[HL.vt] || "bold_bar" : id), text: text || "Your headline here", kicker: kicker || "TOP LINE", position: "top", push: false };
  const W = 1080, H = 640, s = (el.clientWidth || 130) / W;
  hlRender(el, sp, hlLayout(sp, W, H, null), s);
}
// The headline controls (Studio section, or compact in the Editor). clip() = the live clip; set(editPatch, delay) saves edit keys.
function headlineEditor(box, { clip, set, compact = false }) {
  if (!box) return;
  const h = () => ({ ...HL_DEF, ...(clip().edit.headline || {}) });
  const put = (p, d = 250) => set({ headline: { ...(clip().edit.headline || {}), ...p } }, d);
  const draw = () => {
    const H = h(), ids = ["auto", ...Object.keys(HL.themes || {}), ...(HL.custom ? ["custom"] : [])], cur = H.theme;
    const segH = (key, opts) => `<div class="seg">${opts.map(([v, l]) => `<button type="button" class="${String(H[key]) === v ? "on" : ""}" data-hset="${key}" data-v="${v}">${l}</button>`).join("")}</div>`;
    box.innerHTML = `<label class="switch"><input type="checkbox" data-h="on" ${H.on ? "checked" : ""}> Show a headline banner</label>
      <label class="field">Headline<input type="text" data-h="text" maxlength="90" value="${esc(H.text)}" placeholder="${esc(clip().hook || clip().title || "The hook, in a few words")}"></label>
      <label class="field">Top line (kicker: a name, “PART 2”, a date)<input type="text" data-h="kicker" maxlength="40" value="${esc(H.kicker)}" placeholder="Optional"></label>
      <div class="row wrap"><button type="button" class="btn sm" data-ha="write" title="Claude suggests 3 honest headlines from the transcript">✨ Write headline</button><span class="muted grow" style="font-size:12px" data-hmsg></span></div>
      <div class="chips hl-opts" data-hopts></div>
      ${compact ? `<label class="field">Theme<select data-h="theme">${ids.map((id) => `<option value="${esc(id)}" ${cur === id ? "selected" : ""}>${esc(hlName(id))}</option>`).join("")}</select></label>`
        : `<div class="hl-gallery" role="listbox" aria-label="Headline themes">${ids.map((id) => `<button type="button" role="option" class="hl-card" aria-selected="${cur === id}" data-ht="${esc(id)}" title="${esc(hlName(id))}"><div class="hl-mini" data-hmini="${esc(id)}"></div><span>${esc(hlName(id))}</span></button>`).join("")}</div>`}
      ${segH("position", [["top", "Top"], ["seam", "At seam"], ["bottom", "Bottom"]])}
      ${compact ? "" : `<div class="row wrap">${segH("show", [["whole", "Whole clip"], ["first_n_seconds", "First seconds"]])}${H.show !== "whole" ? `<label class="field" style="flex-direction:row;align-items:center;gap:6px">for <input type="number" data-h="seconds" min="0.5" max="30" step="0.5" value="${+H.seconds}" style="width:72px"> s</label>` : ""}</div>
        <label class="switch"><input type="checkbox" data-h="push" ${H.push ? "checked" : ""}> Push the video down (reserve a band so the banner covers nothing)</label>
        <div class="row wrap"><button type="button" class="btn xs" data-ha="save-theme" title="Save this look (theme + font and colour changes) as “My theme”">★ Save as my theme</button>
          <label class="switch" style="font-size:12px"><input type="checkbox" data-ha="default" ${HL.default_on ? "checked" : ""}> Headline on new clips (each clip's hook)</label></div>`}`;
    const t = clip().edit.headline || {};
    $$("[data-hmini]", box).forEach((el) => hlCard(el, el.dataset.hmini, t.text, t.kicker));
  };
  const busy = (b, on) => { b.disabled = on; };
  box.oninput = (e) => { e.stopPropagation(); const k = e.target.dataset.h; if (k === "text" && e.target.value.trim()) $("[data-h=on]", box).checked = true; if (k === "text" || k === "kicker") put({ [k]: e.target.value, ...(k === "text" && e.target.value.trim() ? { on: true } : {}) }, 600); };
  box.onchange = async (e) => {
    e.stopPropagation();
    const t = e.target, k = t.dataset.h;
    if (k === "on" || k === "push") put({ [k]: t.checked });
    else if (k === "theme") put({ theme: t.value });
    else if (k === "seconds") put({ seconds: Math.min(Math.max(+t.value || 3, 0.5), 30) });
    else if (t.dataset.ha === "default") {
      try { await new Promise((r) => setTimeout(r, 700)); await api(`/api/clips/${clip().id}/headline/default`, { body: { on: t.checked } }); HL.default_on = t.checked; toast(t.checked ? "New clips will get this headline look with their own hook." : "New clips start without a headline.", "ok"); }
      catch (err) { t.checked = !t.checked; fail(err); }
      return;
    } else if (k === "text" || k === "kicker") return; // saved on input; a redraw here would swallow the click that blurred it
    draw();
  };
  box.onclick = async (e) => {
    e.stopPropagation();
    const b = e.target.closest("[data-ht],[data-hset],[data-ha],[data-hpick],[data-hkick]");
    if (!b) return;
    if (b.dataset.ht) { put({ theme: b.dataset.ht }); return draw(); }
    if (b.dataset.hset) { put({ [b.dataset.hset]: b.dataset.v }); return draw(); }
    if (b.dataset.hpick != null) { put({ text: b.dataset.hpick, on: true }); return draw(); }
    if (b.dataset.hkick != null) { put({ kicker: b.dataset.hkick }); return draw(); }
    if (b.dataset.ha === "write") {
      const msg = $("[data-hmsg]", box); busy(b, true); msg.textContent = "Writing…";
      try {
        const r = await api(`/api/clips/${clip().id}/headline/write`, { method: "POST" });
        $("[data-hopts]", box).innerHTML = r.options.map((o) => `<button type="button" class="chip" data-hpick="${esc(o)}">${esc(o)}</button>`).join("")
          + (r.kicker ? `<button type="button" class="chip" data-hkick="${esc(r.kicker)}">Top line: ${esc(r.kicker)}</button>` : "");
        msg.textContent = !r.options.length ? "Nothing to suggest yet — type a headline." : r.ai ? "Pick one:" : "AI unavailable — the clip's hook and title:";
      } catch (err) { msg.textContent = ""; fail(err); } finally { busy(b, false); }
    }
    if (b.dataset.ha === "save-theme") {
      busy(b, true);
      try { await new Promise((r) => setTimeout(r, 700)); HL.custom = await api(`/api/clips/${clip().id}/headline/theme`, { method: "POST" }); put({ theme: "custom" }); draw(); toast("Saved as “My theme”.", "ok"); } // ponytail: waits out the debounced edit saves (≤ 600 ms) so the server sees the latest look
      catch (err) { fail(err); } finally { busy(b, false); }
    }
  };
  loadHeadline().then(draw);
}
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
    storageNotice(settings); // unplugged storage drive banner; the first-run space saver choice
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
// Autopilot posts only to accounts approved for uploads AND allowed for Autopilot (both switches on the Publish page).
function apAccounts(ap) {
  const all = Object.keys(PLATFORM).flatMap((p) => S.conn?.[p]?.accounts || []);
  if (!all.length) return `<div class="plats">${Object.entries(PLATFORM).map(([k, v]) => `<label><input type="checkbox" class="ap-plat" value="${k}" ${ap.platforms.includes(k) ? "checked" : ""}> ${v}</label>`).join("")}</div>
    <p class="dim" style="font-size:11.5px;margin:0">Link accounts on the Publish page and turn on “Allow Autopilot to post here”: Autopilot only posts to those.</p>`;
  const can = (a) => a.approved && a.autopilot;
  return `<div class="plats">${all.map((a) => `<label ${can(a) ? "" : 'style="opacity:.45" title="Turn on “Approved for uploads” and “Allow Autopilot to post here” for this account on the Publish page"'}>
      <input type="checkbox" class="ap-acc" value="${esc(a.id)}" data-plat="${a.platform}" ${!can(a) ? "disabled" : (ap.accounts ? ap.accounts.includes(a.id) : ap.platforms.includes(a.platform)) ? "checked" : ""}> ${platTag(a.platform)} ${esc(a.name || a.handle || "")}</label>`).join("")}</div>`;
}
function apForm(ap, withToggle = true) {
  return `
  ${withToggle ? `<div class="row"><h2 class="grow" style="margin:0">⚡ Autopilot</h2><label class="switch"><input type="checkbox" id="ap-enabled" ${ap.enabled ? "checked" : ""}></label></div>
  <p class="muted" style="margin:6px 0 14px;font-size:12.5px">When the scan finishes, the top clips are rendered and scheduled to your channels automatically.</p>` : ""}
  <div class="ap-body stack">
    ${apAccounts(ap)}
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
  const accs = $$(".ap-acc", root);
  if (accs.length) { // accounts listed: they decide (their platforms are kept for older readers)
    ap.accounts = accs.filter((c) => c.checked).map((c) => c.value);
    ap.platforms = [...new Set(accs.filter((c) => c.checked).map((c) => c.dataset.plat))];
  }
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
// video types — ponytail: mirrors ai.VIDEO_TYPES / SUB_GENRES (the server validates); option value "type" or "gaming:sub"
const VTYPES = [["gaming", "Gaming"], ["gaming:fps", "Gaming — FPS / competitive"], ["gaming:horror", "Gaming — Horror"],
  ["gaming:variety", "Gaming — Variety / casual"], ["gaming:speedrun", "Gaming — Speedrun"], ["vtuber", "VTuber"],
  ["just_chatting", "Just chatting / IRL"], ["podcast", "Podcast / interview"], ["reaction", "Reaction"], ["tutorial", "Tutorial / educational"],
  ["sports", "Sports"], ["music", "Music / performance"], ["vlog", "Vlog"], ["comedy", "Comedy / sketch"], ["other", "Other"]];
const vtKey = (vt) => (vt?.type ? vt.type + (vt.sub_genre ? ":" + vt.sub_genre : "") : "auto");
const vtLabel = (vt) => (VTYPES.find(([k]) => k === vtKey(vt)) || [, vt?.type || ""])[1];
const vtOptions = (sel, autoLabel = "Auto-detect type") => `<option value="auto">${autoLabel}</option>`
  + VTYPES.map(([k, l]) => `<option value="${k}" ${k === sel ? "selected" : ""}>${l}</option>`).join("");
const vtBody = (key) => { const [video_type, sub_genre = ""] = key.split(":"); return { video_type, sub_genre }; };
const vtTip = (vt) => !vt ? "Detected during analysis" : vt.source === "user" ? "Set by you" : `Detected (${vt.source === "ai" ? "AI" : "offline guess"}, ${Math.round((vt.confidence || 0) * 100)}% sure)${vt.reasons?.length ? ": " + vt.reasons.join("; ") : ""}`;

// ---------- storage & space saver (storage.py): Keep pins, due dates, removed sources, imported shorts ----------
Object.assign(JOB_LABEL, { cleanup: "Space saver clean-up" });
const VIDEO_RE = /\.(mp4|mov|mkv|webm|avi|m4v|mts|ts|flv|wmv)$/i; // mirrors media.VIDEO_EXT
const gb = (n) => `${((+n || 0) / 1e9).toFixed(1)} GB`;
function dueText(d, src = false) {
  if (!d?.at) return "";
  const days = Math.ceil((new Date(d.at) - Date.now()) / 864e5), arch = d.action === "archive";
  const at = days <= 0 ? "at the next clean-up" : days === 1 ? "in 1 day" : `in ${days} days`;
  return src ? `Source ${arch ? "moves to archive" : "deleted"} ${at}` : `${arch ? "Moves to archive" : "Removed"} ${at}`;
}
const dueChip = (d, src = false) => (d?.at ? `<span class="chip due" title="Space saver (Settings › Storage). 📌 Keep exempts it.">${esc(dueText(d, src))}</span>` : "");
const keepBtn = (kind, id, on, label) => `<button class="btn xs keep${on ? " on" : ""}" data-keep="${esc(kind)}:${esc(id)}" aria-pressed="${!!on}"
  title="${on ? "Kept: the space saver never removes or archives this. Click to un-pin." : "Keep: the space saver never removes or archives this"}">${label || (on ? "📌 Kept" : "📌 Keep")}</button>`;
async function toggleKeep(btn) { // a [data-keep] button anywhere: pin / un-pin (Keep on an expired item restores it)
  const [kind, id] = btn.dataset.keep.split(":"), on = !btn.classList.contains("on");
  const url = { project: `/api/projects/${id}`, clip: `/api/clips/${id}`, longform: `/api/longform/${id}` }[kind];
  btn.disabled = true;
  try {
    await api(url, { method: "PATCH", body: { keep: on } });
    toast(on ? "Kept: the space saver won't touch it." : "No longer pinned.", "ok", 1800);
    if (btn.isConnected) btn.outerHTML = keepBtn(kind, id, on);
  } catch (e) { fail(e); btn.disabled = false; }
}
function sourceNote(p) { // a source the space saver removed, and the way back
  if (p.source_state !== "removed") return "";
  const r = p.source_removed || {};
  return `<div class="src-gone"><b>Source removed</b> <span class="dim">to save space${r.archive ? ` — archived to ${esc(r.archive)}` : ""}</span>
    ${p.can_redownload ? `<button class="btn xs" data-redl="${esc(p.id)}">Re-download</button>` : `<button class="btn xs" data-reimp="${esc(p.id)}">Re-import file</button>`}
    ${r.error ? `<div style="color:var(--err);font-size:11.5px">${esc(r.error)}</div>` : ""}</div>`;
}
async function sourceAction(target, done) { // [data-redl] / [data-reimp] anywhere; true when it handled the click
  const re = target.closest("[data-redl]"), ri = target.closest("[data-reimp]");
  if (re) {
    re.disabled = true;
    try { await api(`/api/projects/${re.dataset.redl}/redownload`, { method: "POST" }); toast("Re-downloading the source — clips and analysis stay as they are.", "ok"); done?.(); }
    catch (e) { fail(e); re.disabled = false; }
    return true;
  }
  if (!ri) return false;
  const inp = Object.assign(document.createElement("input"), { type: "file", accept: "video/*,.mkv" });
  inp.onchange = async () => {
    const fd = new FormData(); fd.append("file", inp.files[0]);
    ri.disabled = true; ri.innerHTML = `<span class="spin"></span> Uploading…`;
    try { await api(`/api/projects/${ri.dataset.reimp}/source`, { body: fd }); toast("Source restored.", "ok"); done?.(); }
    catch (e) { fail(e); ri.disabled = false; ri.textContent = "Re-import file"; }
  };
  inp.click();
  return true;
}
async function pickPath(kind = "folder", current = "") { // the desktop app's native picker, else a typed path
  const app = S.app || (S.app = await api("/api/app").catch(() => ({})));
  if (!app.desktop) { const v = prompt(kind === "folder" ? "Folder path (e.g. D:\\Reelwright):" : "File path:", current); return v ? [v.trim()] : []; }
  return (await api("/api/storage/pick", { body: { kind } })).paths;
}
function openShortImport(vt, done) {
  modal(`<h2>Import shorts</h2><p class="muted">Shorts you made before: each becomes its own project with <b>one whole clip</b>, analysed as usual (captions, headline, layouts all work), ready to re-edit and post. The same video is never imported twice. Your originals stay where they are and the space saver never deletes imported originals unless you opt in.</p>
    <div class="row wrap"><button class="btn primary" id="sh-files">Choose videos…</button><button class="btn" id="sh-dir">Choose a folder…</button>
      <input type="file" id="sh-fi" accept="video/*,.mkv" multiple hidden><input type="file" id="sh-di" webkitdirectory hidden></div>
    <div id="sh-out" class="stack" style="margin-top:12px;font-size:12.5px"></div><div class="foot"><button class="btn" data-close>Close</button></div>`, async (m) => {
    const out = $("#sh-out", m);
    const report = (r) => {
      out.innerHTML = `<div><b>${r.added.length}</b> short${r.added.length === 1 ? "" : "s"} imported: analysis runs one at a time (see the Library), then each opens in the Studio.</div>`
        + (r.duplicates.length ? `<div class="dim">${r.duplicates.length} already imported: ${r.duplicates.map((d) => esc(d.name)).join(", ")}</div>` : "")
        + (r.skipped.length ? `<div style="color:var(--err)">${r.skipped.length} skipped: ${r.skipped.map((s) => `${esc(s.path)} (${esc(s.reason)})`).join("; ")}</div>` : "");
      done();
    };
    const app = S.app || (S.app = await api("/api/app").catch(() => ({})));
    if (app.desktop) { // native pickers: the files are copied in on this PC (no upload)
      const go = async (kind) => {
        try { const paths = await pickPath(kind); if (!paths.length) return; out.innerHTML = `<span class="spin"></span> Checking ${paths.length === 1 ? "" : paths.length + " "}selection for duplicates…`; report(await api("/api/shorts/paths", { body: { paths, ...vt } })); }
        catch (e) { out.textContent = ""; fail(e); }
      };
      $("#sh-files", m).onclick = () => go("files"); $("#sh-dir", m).onclick = () => go("folder");
      return;
    }
    $("#sh-files", m).onclick = () => $("#sh-fi", m).click(); $("#sh-dir", m).onclick = () => $("#sh-di", m).click();
    const send = async (list) => { // the browser uploads them one after another
      const files = [...list].filter((f) => VIDEO_RE.test(f.name)), res = { added: [], duplicates: [], skipped: [] };
      for (const [i, f] of files.entries()) {
        out.innerHTML = `<div>Importing ${i + 1} of ${files.length} — ${esc(f.name)}</div>${bar(i / files.length)}`;
        const fd = new FormData(); fd.append("file", f); Object.entries(vt).forEach(([k, v]) => fd.append(k, v));
        try { const r = await api("/api/shorts", { body: fd }); r.duplicate ? res.duplicates.push({ name: f.name }) : res.added.push(r.project); }
        catch (e) { res.skipped.push({ path: f.name, reason: e.message }); }
      }
      report(res);
    };
    $("#sh-fi", m).onchange = (e) => send(e.target.files); $("#sh-di", m).onchange = (e) => send(e.target.files);
  });
}

function storageNotice(s) { // from sideStatus: a banner while the storage drive is away; the one-time space saver choice
  let b = $("#storage-banner");
  if (s.storage_missing) {
    if (!b) { b = Object.assign(document.createElement("div"), { id: "storage-banner", className: "storage-banner" }); b.setAttribute("role", "alert"); document.body.append(b); }
    b.innerHTML = `⚠ The storage folder <b>${esc(s.storage_dir || "(default)")}</b> isn't connected. Renders, imports and uploads wait until it's back — nothing was moved or deleted. <a href="#/settings">Storage settings</a>`;
  } else b?.remove();
  if (s.retention_mode === "" && !$("#modal-root").innerHTML) retentionPrompt();
}

function retentionPrompt() { // first run after the update: a choice is required before any clean-up can run
  const opt = (v, title, sub, on) => `<label class="row" style="gap:10px;align-items:flex-start;cursor:pointer"><input type="radio" name="rtf" value="${v}" ${on ? "checked" : ""}>
    <span><b>${title}</b><br><span class="dim" style="font-size:12.5px">${sub}</span></span></label>`;
  modal(`<div id="rt-first"><h2>Save disk space automatically?</h2>
    <p class="muted">Downloaded streams and rendered clips fill a drive fast. Reelwright can clean them up 14 days after you're done with them.
      <b>Nothing already on this PC is touched today</b>: every item's 14 days start now. Clips, transcripts, posts and what Reelwright learned always stay,
      anything waiting to render or post is never touched, and anything you pin with 📌 Keep is kept.</p>
    <div class="stack" style="gap:10px">${opt("delete", "Delete after 14 days", "Downloaded long videos and clip renders are deleted. A removed download can be downloaded again from its link.", true)}
      ${opt("archive", "Archive to a folder", "Moved to folders you choose in Settings › Storage, with readable names. Nothing moves until you pick them.")}
      ${opt("off", "Keep everything", "Nothing is ever removed. Change this any time in Settings › Storage.")}</div>
    <div class="foot"><button class="btn primary" id="rtf-ok">Continue</button></div></div>`, (m, close) => {
    $(".modal-bg").addEventListener("mousedown", (e) => e.target === e.currentTarget && e.stopImmediatePropagation(), true); // no click-away
    $("#rtf-ok", m).onclick = async () => {
      const v = $("input[name=rtf]:checked", m).value;
      try {
        S.settings = await api("/api/settings", { method: "PUT", body: { retention_mode: v } });
        close();
        toast(v === "archive" ? "Choose the archive folders in Settings › Storage." : v === "delete" ? "Space saver on: nothing is removed for 14 days." : "Keeping everything.", "ok", 6000);
        if (v === "archive") location.hash = "#/settings";
      } catch (e) { fail(e); }
    };
  });
}

async function openCleanup() { // "Clean up now": always a preview first
  const pv = await api("/api/storage/cleanup");
  if (!pv.mode) return toast("Turn the space saver on (Delete or Archive) and save it first.", "err");
  const KIND = { source: "Source video", clip: "Clip", longform: "Long form" };
  const row = (it) => `<tr><td style="width:96px">${KIND[it.kind] || esc(it.kind)}</td><td>${esc(it.title)}</td><td class="mono" style="width:70px">${gb(it.bytes)}</td>${it.reason ? `<td class="dim">${esc(it.reason)}</td>` : ""}</tr>`;
  const total = pv.items.reduce((a, it) => a + it.bytes, 0), verb = pv.mode === "archive" ? "Archive" : "Delete";
  modal(`<h2>Clean up now?</h2>${pv.items.length ? `<p class="muted">${verb}s ${pv.items.length} item${pv.items.length === 1 ? "" : "s"} past their date (${gb(total)}):</p>
      <table style="width:100%;font-size:12.5px"><tbody>${pv.items.map(row).join("")}</tbody></table>` : `<p class="muted">Nothing is due right now.</p>`}
    ${pv.blocked.length ? `<h3 style="margin-top:14px">Skipped for now (${pv.blocked.length})</h3><table style="width:100%;font-size:12.5px"><tbody>${pv.blocked.map(row).join("")}</tbody></table>` : ""}
    <div class="foot"><button class="btn" data-close>Cancel</button>${pv.items.length ? `<button class="btn primary danger" id="cl-go">${verb} ${pv.items.length}</button>` : ""}</div>`, (m, close) => {
    const go = $("#cl-go", m);
    if (go) go.onclick = async () => { close(); try { await api("/api/storage/cleanup", { method: "POST" }); toast("Cleaning up — follow it in Activity.", "ok"); } catch (e) { fail(e); } };
  }, true);
}

function storageCards() { // Settings › Storage + Space saver, inserted above "App window & startup"
  const anchor = $("#appwin", view);
  if (!anchor || $("#storage-card", view)) return;
  anchor.insertAdjacentHTML("beforebegin", `<div class="card stack" style="margin-top:20px" id="storage-card"><span class="dim">Loading storage…</span></div>
    <div class="card stack" style="margin-top:20px" id="saver-card"></div>`);
  const sc = $("#storage-card", view), sv = $("#saver-card", view);
  let st = null, timer = 0;
  const drawStorage = () => {
    const u = st.usage, m = st.move || {};
    sc.innerHTML = `<div class="row wrap"><h2 class="grow" style="margin:0">Storage</h2>${st.missing ? '<span class="pill err">not connected</span>' : ""}</div>
      <p class="muted" style="margin:0;font-size:12.5px">Your projects' videos live here: sources, renders and analysis. Settings, the database and the asset library stay in <span class="mono">${esc(st.data)}</span>. A big second drive keeps long videos off your system disk.</p>
      <div class="row wrap"><div class="grow"><div class="mono" style="word-break:break-all">${esc(st.path)}</div><div class="dim" style="font-size:12px">${st.custom ? "Your chosen folder" : "The default folder"}</div></div>
        <button class="btn sm" id="st-change" ${m.active ? "disabled" : ""}>Change folder…</button></div>
      ${st.missing ? `<div style="color:var(--err);font-size:12.5px">This folder isn't available (is the drive connected?). Renders, imports and uploads wait until it's back; nothing was moved or deleted. Reconnect it, or pick another folder (projects stored here stay here).</div>`
        : `<div style="font-size:13px">Free: <b>${gb(st.free)}</b>${u ? ` · Reelwright uses <b>${gb(u.total)}</b> — sources ${gb(u.sources)}, renders ${gb(u.renders)}, other ${gb(u.other)}` : ""}</div>`}
      ${m.active ? `<div style="font-size:12.5px">${esc(m.message || "Moving…")} → <span class="mono">${esc(m.to)}</span></div>${bar(m.progress)}
          <div class="dim" style="font-size:12px">Renders, imports and uploads wait until the move finishes. Closing Reelwright is safe: the move continues next time.</div>`
        : m.error ? `<div style="color:var(--err);font-size:12.5px">The move stopped: ${esc(m.error)} ${m.phase === "cleanup"
          ? "Your projects are safe in the new folder; some old copies may be left in the old one." : "Nothing was deleted; your projects are still in the old folder."}</div>`
        : m.message ? `<div class="dim" style="font-size:12.5px">${esc(m.message)}</div>` : ""}`;
  };
  const load = async () => {
    st = await api(`/api/storage?usage=${st?.move?.active ? "false" : "true"}`).then((x) => ({ ...x, usage: x.usage || st?.usage }));
    if (sc.isConnected) drawStorage();
    clearTimeout(timer);
    if (st.move?.active && sc.isConnected) timer = setTimeout(() => load().catch(() => {}), 2000);
  };
  sc.onclick = async (e) => {
    if (e.target.id !== "st-change") return;
    try {
      const [path] = await pickPath("folder", st.path);
      if (!path) return;
      const chk = await api("/api/storage/check", { body: { path } });
      if (!chk.ok) return toast(chk.error, "err", 7000);
      const ok = await confirmBox(chk.missing ? "Switch the storage folder?" : "Move your projects?", chk.missing
        ? `The current folder isn't connected, so nothing can be moved: Reelwright switches to ${chk.path}. Projects on the old drive stay there; switch back once it's connected.`
        : `Your projects (${gb(chk.needed)}) are copied to ${chk.path} (${gb(chk.free)} free), every copy is checked, and only then are the originals removed. Renders, imports and uploads wait meanwhile.`,
        chk.missing ? "Switch" : "Move");
      if (!ok) return;
      st = { ...st, ...(await api("/api/storage/move", { body: { path: chk.path } })) };
      await load();
      sideStatus();
    } catch (err) { fail(err); }
  };
  const SCOPES = [["sources", "Downloaded long videos (links and channel imports)"], ["unfinished", "Unfinished clips' renders (rendered, never uploaded)"],
    ["uploaded", "Uploaded clips' renders"], ["longform", "Long-form exports"], ["local_sources", "Originals from OBS, the network receiver, files you added and imported shorts"]];
  const ARCH = [["archive_uploaded_dir", "Uploaded clips go to"], ["archive_unfinished_dir", "Unfinished clips go to"], ["archive_other_dir", "Sources & long forms go to (blank = the uploaded-clips folder)"]];
  const drawSaver = () => {
    const s = S.settings, mode = s.retention_mode || "off";
    const sc0 = { sources: true, unfinished: true, uploaded: true, longform: true, local_sources: false, ...(s.retention_scope || {}) };
    sv.innerHTML = `<h2 style="margin:0">Space saver</h2>
      <p class="muted" style="margin:0;font-size:12.5px">After the days below, Reelwright deletes past-due videos and renders, or moves them to archive folders. Clips, transcripts, posts and what Reelwright learned always stay. Never touched: anything pinned with 📌 Keep (a clip, long form or whole project) and anything waiting to render or post.</p>
      <div class="row wrap" style="gap:14px">${[["off", "Off — keep everything"], ["delete", "Delete"], ["archive", "Archive to folders"]].map(([v, l]) => `<label class="row" style="gap:6px"><input type="radio" name="rt-mode" value="${v}" ${mode === v ? "checked" : ""}> ${l}</label>`).join("")}
        <label class="row" style="gap:6px">after <input type="number" id="rt-days" min="1" max="3650" value="${+s.retention_days || 14}" style="width:80px"> days</label></div>
      <div class="stack" style="gap:4px">${SCOPES.map(([k, l]) => `<label class="row" style="gap:8px;font-size:13px"><input type="checkbox" data-scope="${k}" ${sc0[k] ? "checked" : ""}> ${l}</label>`).join("")}</div>
      <div class="stack" id="rt-arch" style="gap:6px" ${mode === "archive" ? "" : "hidden"}>${ARCH.map(([k, l]) => `<div class="row wrap" style="gap:6px"><label class="field grow">${l}<input type="text" data-arch="${k}" value="${esc(s[k] || "")}" placeholder="e.g. D:\\Reelwright archive"></label><button class="btn sm" type="button" data-pick="${k}" style="align-self:flex-end">Choose…</button></div>`).join("")}
        <p class="dim" style="margin:0;font-size:12px">Archived files get readable names (date, title, platform) and a small .json beside each with the title, caption, hashtags and post links. Each copy is checked before the original is removed; while a folder isn't set or connected, nothing is deleted instead.</p></div>
      <p class="dim" style="margin:0;font-size:12px">The clock: a downloaded video counts from its import, an unfinished clip from its last render, a posted clip or long form from its last upload — never from before you switched the space saver on${s.retention_since ? ` (${esc(when(s.retention_since))})` : ""}. Cards show the date (“Removed in 3 days”). It runs a few minutes after Reelwright starts, then daily.</p>
      <div class="row wrap"><button class="btn sm primary" type="button" id="rt-save">Save space saver</button><button class="btn sm" type="button" id="rt-clean">Clean up now…</button>
        <span class="dim" style="font-size:12px">${s.retention_at ? `Last clean-up ${esc(when(s.retention_at))}` : ""}</span></div>`;
  };
  sv.onchange = (e) => { if (e.target.name === "rt-mode") $("#rt-arch", sv).hidden = e.target.value !== "archive"; };
  sv.onclick = async (e) => {
    const pk = e.target.closest("[data-pick]");
    try {
      if (pk) { const inp = $(`[data-arch="${pk.dataset.pick}"]`, sv); const [path] = await pickPath("folder", inp.value); if (path) inp.value = path; return; }
      if (e.target.id === "rt-clean") return openCleanup();
      if (e.target.id !== "rt-save") return;
      S.settings = await api("/api/settings", { method: "PUT", body: {
        retention_mode: $("input[name=rt-mode]:checked", sv)?.value || "off", retention_days: +$("#rt-days", sv).value || 14,
        retention_scope: Object.fromEntries($$("[data-scope]", sv).map((c) => [c.dataset.scope, c.checked])),
        ...Object.fromEntries($$("[data-arch]", sv).map((i) => [i.dataset.arch, i.value.trim()])) } });
      toast("Space saver saved.", "ok");
      drawSaver();
    } catch (err) { fail(err); }
  };
  drawSaver();
  load().catch((err) => { sc.innerHTML = `<h2 style="margin:0">Storage</h2><span class="dim">${esc(err.message)}</span>`; });
}

function Library() {
  this.mount = async () => {
    const ap = apLoad();
    view.innerHTML = `<div class="page">
      <h1>Library</h1><p class="sub">Drop in long-form video or a stream VOD — Reelwright transcribes it, finds the best moments, and gets them ready to post.</p>
      <div class="hero">
        <div class="drop" id="drop">
          <div class="big">Drop a video here</div>
          <div class="muted">MP4, MOV, MKV, WebM… any length. Processing happens on this computer.</div>
          <div class="row wrap" style="justify-content:center"><button class="btn primary" id="browse">Choose file</button><input type="file" id="file" accept="video/*,.mkv" hidden multiple>
            <button class="btn" id="shorts" type="button" title="Shorts you made before: each becomes its own project with one whole clip, ready to re-edit and post">⇪ Import shorts…</button></div>
          <div class="or">or import a link — YouTube, Twitch, Kick VODs &amp; clips, 1,000+ sites</div>
          <form id="urlform"><input type="url" id="url" placeholder="https://www.twitch.tv/videos/…" required><button class="btn">Import</button></form>
          <label class="muted" style="font-size:12.5px" title="The type steers what the AI looks for (e.g. kills and callouts in an FPS, quotable lines in a podcast). Auto-detect looks at the title and a few frames first.">Video type <select id="vtype">${vtOptions(localStorage.vtype || "auto", "Auto-detect (recommended)")}</select></label>
          <a href="#/sources" class="muted" style="font-size:12.5px">Connect a Twitch / Kick / YouTube channel to auto-import every new stream →</a>
          <div id="upl" class="stack" style="width:100%;max-width:460px"></div>
        </div>
        <div class="card autopilot ${ap.enabled ? "" : "off"}" id="ap">${apForm(ap)}</div>
      </div>
      <div class="row" style="margin-bottom:12px"><h2 class="grow" style="margin:0">Projects</h2></div>
      <div id="projects" class="grid"></div></div>`;
    const drop = $("#drop"), file = $("#file");
    $("#browse").onclick = () => file.click();
    $("#shorts").onclick = () => openShortImport(vtBody($("#vtype").value), () => this.refresh());
    file.onchange = () => [...file.files].forEach(upload);
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => [...e.dataTransfer.files].forEach(upload));
    $("#ap-enabled").onchange = (e) => { $("#ap").classList.toggle("off", !e.target.checked); apRead($("#ap")); };
    $("#ap").addEventListener("change", () => apRead($("#ap")));
    $("#vtype").onchange = (e) => { try { localStorage.vtype = e.target.value; } catch {} };
    $("#urlform").onsubmit = async (e) => {
      e.preventDefault();
      try {
        const p = await api("/api/projects/url", { body: { url: $("#url").value, autopilot: apRead($("#ap")), ...vtBody($("#vtype").value) } });
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
    Object.entries(vtBody($("#vtype").value)).forEach(([k, v]) => fd.append(k, v));
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
            ${p.viral ? `<span class="vbadge" title="Virality score">✦ ${+p.viral.score}</span>` : ""}${p.origin?.platform ? `<span class="chip">${esc(p.origin.platform)}</span>` : p.origin?.kind === "imported_short" ? '<span class="chip">Imported short</span>' : ""}
            ${p.video_type ? `<span class="chip" title="${esc(vtTip(p.video_type))}">${esc(vtLabel(p.video_type))}</span>` : ""}
            ${p.autopilot?.enabled ? '<span class="pill run" title="Autopilot on">Autopilot</span>' : ""}</div>
          ${p.status !== "ready" && p.status !== "failed" ? bar(p.progress) : ""}
          ${p.error ? `<div style="color:var(--err);font-size:12px">${esc(p.error).slice(0, 180)}</div>` : ""}
          ${sourceNote(p)}
          <div class="row wrap" style="gap:6px">${keepBtn("project", p.id, p.keep)}${dueChip(p.source_due, true)}
            ${p.origin?.kind === "imported_short" && p.status === "ready" && p.first_clip ? `<a class="btn xs primary" href="#/studio/${esc(p.first_clip)}">Open in Studio</a>` : ""}</div>
        </div></div>`).join("");
    el.onclick = async (e) => {
      const kb = e.target.closest("[data-keep]");
      if (kb) { e.stopPropagation(); await toggleKeep(kb); return this.refresh(); }
      if (e.target.closest("a")) return; // "Open in Studio"
      if (await sourceAction(e.target, () => this.refresh())) return;
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
  const sorted = [...m].sort((a, b) => a - b), top = Math.max(0.02, sorted[Math.floor(sorted.length * 0.98)] || 0); // floor: a static video's noise stays dim
  const c = Object.assign(document.createElement("canvas"), { width: n, height: 1 }), g = c.getContext("2d"), img = g.createImageData(n, 1);
  for (let i = 0; i < n; i++) {
    let x = 0; for (let j = i * per; j < (i + 1) * per && j < m.length; j++) x = Math.max(x, m[j]);
    const a = Math.min(1, x / top);
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
  const E = { pid, proj: null, clips: [], segs: [], words: [], signals: {}, sel: null, tab: "clips", zoom: 1, inMark: null, outMark: null, loop: false, pending: {}, sig: "", picked: new Set(),
    hist: RWUndo.History(100, 1000) }; // undo/redo of the selected clip's edits (selecting another clip starts over)
  let video, canvas, ctx, raf, keyHandler, listeners, ro;
  const dur = () => E.proj?.meta?.duration || 0;
  const selClip = () => E.clips.find((c) => c.id === E.sel);
  const self = this;
  const loadProj = async () => { // expired clips (space saver) are listed apart, only when "Show expired" is on
    const p = await api(`/api/projects/${pid}${E.showExp ? "?expired=1" : ""}`);
    E.expired = p.clips.filter((c) => c.expired);
    p.clips = p.clips.filter((c) => !c.expired);
    return p;
  };

  this.mount = async () => {
    E.proj = await loadProj();
    HL.vt = vtOf(E.proj); loadHeadline().then(() => !E.dead && applyPreview());
    localStorage.lastProject = pid;
    $("#nav-editor").hidden = false;
    if (!E.proj.proxy && E.proj.source_state === "ok") api(`/api/projects/${pid}/proxy`, { method: "POST" }).catch(() => {}); // HEVC/MKV/4K/long: a light preview copy for next time
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
        <select id="ed-vt" class="chip" title="Video type: ${esc(vtTip(p.video_type))}. It steers what the AI looks for.">${vtOptions(vtKey(p.video_type), p.video_type ? "Auto-detect again on re-analyze" : "Type: auto-detect")}</select>
        ${p.viral ? `<button class="btn xs" data-act="report" data-id="${p.viral.report_id}"><span class="vbadge">✦ ${+p.viral.score}</span> report</button>` : ""}
        ${keepBtn("project", pid, p.keep)}<span id="ed-due">${dueChip(p.source_due, true)}</span>
        <div class="grow"></div>
        <button class="btn sm" data-act="viral-video" ${ready ? "" : "disabled"} title="Score this whole video for virality against similar videos">✦ Virality</button>
        <button class="btn sm" data-act="autopilot" ${ready ? "" : "disabled"}>⚡ Autopilot</button>
        <button class="btn sm" data-act="longform" ${ready ? "" : "disabled"} title="A separate landscape edit of the whole video: condensed, highlights or a clip compilation">▬ Long form</button>
        <button class="btn sm" data-act="add-angle" title="Add another camera's recording of this video: it is lined up by sound, then usable in layouts and angle cuts (Studio)">＋ Camera angle${p.angles?.length ? ` (${p.angles.length})` : ""}</button>
        <button class="btn sm" data-act="hud" ${p.source && p.source_state !== "removed" ? "" : "disabled"} title="Box something on screen (YOU DIED, a kill feed, VICTORY) and Reelwright finds it in every video">◎ Teach screen event</button>
        <button class="btn sm" data-act="reanalyze">↻ Re-analyze</button>
        <a class="btn sm" href="#/publish">Publish queue</a>
      </div>
      <div class="stage">
        <div class="player">
          ${p.source_state === "removed" ? `<div class="overlay-msg">${sourceNote(p)}<div class="muted" style="max-width:520px;font-size:12.5px">Your clips, transcript, analysis and post history are all still here. Previews, edits that change the video and renders need the source back.</div></div>` : ""}
          ${p.source && p.source_state !== "removed" ? `<div class="vwrap"><video id="vid" src="/files/${pid}/${esc(p.proxy || p.source)}" preload="auto" playsinline></video>
            <div class="cambox" id="cam" hidden></div>
            <div class="cropbox" id="crop"><div class="title-preview" id="tprev" hidden></div><div class="sticker-preview" id="sprev" hidden></div><div class="cap-preview" id="cprev"></div><div class="hl-prev" id="hprev" hidden></div></div></div>` : ""}
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
    $("#ed-vt").onchange = async (e) => {
      const key = e.target.value, auto = key === "auto";
      const refind = !auto && E.proj.status === "ready" && await confirmBox("Re-find clips with this type?",
        `The AI looks again with the ${(VTYPES.find(([k]) => k === key) || [, key])[1]} playbook. AI picks you haven't touched are replaced; rated, edited and posted clips stay. Cancel just saves the type for next time.`, "Re-find clips");
      try {
        E.proj = { ...E.proj, ...(await api(`/api/projects/${pid}`, { method: "PATCH", body: { ...vtBody(key), refind } })) };
        e.target.title = `Video type: ${vtTip(E.proj.video_type)}`;
        toast(refind ? "Re-finding clips with the new type…" : auto ? "Type will be detected on the next analysis." : "Video type saved.", "ok");
      } catch (err) { fail(err); e.target.value = vtKey(E.proj.video_type); }
    };
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
    const due = $("#ed-due"); if (due) due.innerHTML = dueChip(p.source_due, true);
    const vt = $("#ed-vt");  // detection lands early in the analysis: show it as soon as it does
    if (vt && document.activeElement !== vt) { vt.value = vtKey(p.video_type); vt.title = `Video type: ${vtTip(p.video_type)}`; }
  }

  // ----- refresh loop -----
  this.refresh = async () => {
    const p = await loadProj();
    const wasReady = E.proj.status === "ready", wasGone = E.proj.source_state === "removed";
    E.proj = p;
    E.clips = p.clips.map((c) => (E.pending[c.id] ? { ...c, ...E.pending[c.id].local } : c));
    if ((!wasReady || wasGone) && p.status === "ready" && p.source_state !== "removed") {
      await loadAnalysis(); E.sel = E.sel || E.clips[0]?.id; layout();
      toast(wasGone ? "Source restored — previews and renders work again." : `Analysis complete — ${E.clips.length} clips found.`, "ok"); return;
    }
    if (!wasGone && p.source_state === "removed") return layout(); // the space saver removed it while this was open
    updateOverlay();
    const sig = JSON.stringify([E.showExp, E.expired.length, ...E.clips.map((c) => [c.id, c.title, c.start, c.end, c.score, c.export?.status, c.export?.rev, c.rev, c.posts, c.viral, c.rating, c.keep, c.due?.at])]);
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
    } else if (e.aspect !== "source" && (e.layout === "single" ? e.fit === "blur" : !legacySplit(e))) { // multi-region: arranged in the Studio
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
    const seamAt = seamOf(e);
    if (seamAt != null && e.caption_pos === "middle") mv = (1 - seamAt - 0.07) * OH;
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
    const hp = $("#hprev"), hs = box.classList.contains("blur") ? null : hlSpec(e); // ponytail: over the crop box like the title card (not when the box is the whole source of a split layout); the Studio's Output view is the exact preview
    hp.hidden = !hs;
    if (hs) hlRender(hp, hs, hlLayout(hs, OW, OH, seamAt), scale);
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
    drawSignalLane(ctx, E.signals, pps, WAVE_Y, WAVE_Y + WAVE_H, 3 * TK);
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

  const clipLocked = (c) => !!(c.locked || Object.keys(c.posts || {}).length); // posted: the server answers 409
  function patchClip(id, body, delay = 0, record = true) {
    const c = E.clips.find((k) => k.id === id);
    if (!c) return;
    const undo = record && !clipLocked(c) ? RWUndo.inverse(body, c) : null; // before the optimistic update below
    if (undo) E.hist.push(id, undo, body);
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
      catch (e) { if (e.status === 409) E.hist.clear(); fail(e); }
    }, delay);
  }
  function undoRedo(redo) {
    const c = selClip();
    if (!c || c.parts) return;
    if (clipLocked(c)) return toast("Posted — make a new version to edit it.", "err");
    const body = redo ? E.hist.redo(c.id) : E.hist.undo(c.id);
    if (!body) return toast(redo ? "Nothing to redo." : "Nothing to undo.");
    patchClip(c.id, body, 0, false);
    if (!c.parts && ("start" in body || "end" in body)) seek(c.start);
    if (E.tab === "edit" || E.tab === "clips") renderTab();
  }
  const replaceClip = (fresh) => { const c = E.clips.find((k) => k.id === fresh.id); if (c) Object.assign(c, fresh); applyPreview(); draw(); };

  async function onClick(ev) {
    const kb = ev.target.closest("[data-keep]"); // space saver: Keep pins (Keep on an expired clip restores it)
    if (kb) { await toggleKeep(kb); return self.refresh().catch(fail); }
    if (ev.target.closest("[data-showexp]")) { E.showExp = !E.showExp; return self.refresh().then(renderTab).catch(fail); }
    if (await sourceAction(ev.target, () => self.refresh().catch(fail))) return;
    const a = ev.target.closest("[data-act]");
    if (!a) return;
    const act = a.dataset.act, c = selClip();
    const busy = (label) => { a.disabled = true; a.innerHTML = `<span class="spin"></span>${label ? " " + label : ""}`; };
    try {
      switch (act) {
        case "play": if (video) { E.loop = false; video.paused ? video.play() : video.pause(); } break;
        case "playclip": if (c && video && !c.parts) { E.loop = true; seek(c.start); video.play(); } break;
        case "undo": case "redo": undoRedo(act === "redo"); break;
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
        case "hud": openHudTeach(pid, video?.currentTime || 0); break;
        case "longform": openLongCapture(pid, [...E.picked].filter((id) => E.clips.some((k) => k.id === id && !k.parts))); break;
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
        case "add-angle": pickAngleFile(pid, async () => { E.proj = { ...E.proj, angles: (await api(`/api/projects/${pid}`)).angles }; a.textContent = `＋ Camera angle (${E.proj.angles.length})`; }); break;
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
        case "detect-regions": busy("Detecting regions…"); try { replaceClip(await api(`/api/clips/${c.id}/regions/detect`, { method: "POST" })); toast("Regions suggested — fine-tune them in the Studio.", "ok"); } finally { renderTab(); } break;
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
    const z = (e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase();
    if (z === "z" || z === "y") { e.preventDefault(); undoRedo(z === "y" || e.shiftKey); }
    else if (e.code === "Space") { e.preventDefault(); E.loop = false; video.paused ? video.play() : video.pause(); }
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
      <div class="row" style="margin-bottom:10px;font-size:12px" ${E.picked.size ? "" : "hidden"} id="pickbar"><span class="grow">${E.picked.size} selected</span><button class="btn sm primary" data-act="montage" ${E.picked.size > 1 ? "" : "disabled"}>▤ Make montage</button><button class="btn sm" data-act="longform" title="Long form from the ticked clips, in the order ticked">▬ Long form</button></div>
      ${sorted.length ? sorted.map((c) => `
        <div class="clip ${c.id === E.sel ? "sel" : ""}" data-act="select" data-id="${c.id}">
          <div class="row"><input type="checkbox" class="pick" data-id="${c.id}" title="Select for a montage" ${E.picked.has(c.id) ? "checked" : ""} ${c.parts ? "disabled" : ""}>
            <div class="t grow">${esc(c.title)}</div>${c.viral ? `<span class="vbadge" title="Virality score">✦${+c.viral.score}</span>` : ""}${c.source !== "manual" && !c.parts ? `<span class="score" title="AI pick score">${+c.score}</span>` : ""}</div>
          ${c.reason ? `<div class="why">${esc(c.reason)}</div>` : ""}
          <div class="meta wrap"><span class="mono">${c.parts ? `${c.parts.length} clips` : `${fmt(c.start)} → ${fmt(c.end)}`}</span><span>${(c.end - c.start).toFixed(1)}s</span>
            <span class="src-tag">${srcLabel[c.source] || esc(c.source)}</span>${c.features?.motion >= 2 ? `<span class="chip" title="Much more on-screen motion than usual for this video" style="font-size:11px">⚡ action</span>` : ""}${c.features?.flashes > 0 ? `<span class="chip" title="Sudden on-screen flash (explosion, jump scare, hit)" style="font-size:11px">✷ flash</span>` : ""}${c.consensus ? consensusChip(c.consensus) : ""}<span class="grow"></span>
            ${Object.entries(c.posts || {}).map(([p, r]) => (/^https:\/\//.test(r.url || "") ? `<a href="${esc(r.url)}" target="_blank" class="plat ${p}">${PLATFORM[p]}</a>` : platTag(p))).join("")}
            ${c.export?.status === "done" && c.export.rev === c.rev ? '<span title="Rendered" style="color:var(--ok)">●</span>' : ""}
            ${c.fit != null ? `<span class="chip" title="How well this matches what you've liked and posted before (learned from your feedback). AI score ${+c.ai_score}." style="font-size:11px">♥ ${+c.fit}% your taste</span>` : ""}
            <span class="rate"><button data-act="rate" data-id="${c.id}" data-v="1" class="${c.rating === 1 ? "on" : ""}" title="Good pick (trains your style profile)">👍</button><button data-act="rate" data-id="${c.id}" data-v="-1" class="${c.rating === -1 ? "on" : ""}" title="Bad pick">👎</button></span>
            ${c.parts ? "" : `<a class="btn xs primary" href="#/studio/${c.id}" title="Open in the Clip Studio (or double-click the card)">Edit clip</a>`}
            <button class="btn xs" data-act="edit" data-id="${c.id}" title="Quick inline edits in this panel">Quick</button>
            <button class="btn xs ghost danger" data-act="del" data-id="${c.id}" data-title="${esc(c.title)}">✕</button></div>
          <div class="row wrap" style="gap:6px;margin-top:4px">${keepBtn("clip", c.id, c.keep)}${dueChip(c.due)}</div>
        </div>`).join("") : `<div class="empty"><b>${ready ? "No clips yet" : "Clips appear after analysis"}</b>${ready ? "Search for a moment above, or mark In/Out on the timeline." : ""}</div>`}
      ${E.proj.expired_count ? `<button class="btn xs ghost" data-showexp style="margin-top:8px">${E.showExp ? "Hide" : "Show"} expired (${E.proj.expired_count})</button>` : ""}
      ${E.showExp ? E.expired.map((c) => `<div class="clip expired"><div class="row"><div class="t grow">${esc(c.title)}</div>${keepBtn("clip", c.id, false, "↺ Restore")}</div>
        <div class="why">Expired ${esc(when(c.expired.at))} — ${c.expired.archive ? `its render was archived to ${esc(c.expired.archive)}` : "its render was deleted to save space"}. Restore it to edit or re-render. Its posts, history and what Reelwright learned from it are kept.</div></div>`).join("") : ""}`;
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
        ${reframed ? `${seg("layout", e.layout, LAYOUTS)}
          ${isMulti(e) && !legacySplit(e) ? `<div class="row wrap"><button class="btn sm" data-act="detect-regions">◎ Detect regions</button><a class="btn sm" href="#/studio/${c.id}">Arrange regions in Studio</a></div>` : ""}
          ${legacySplit(e) ? `<div class="row wrap"><button class="btn sm" data-act="facecam">◎ Detect facecam</button><a class="btn xs ghost" href="#/studio/${c.id}">More regions in Studio</a><span class="muted" style="font-size:12px">Blue = facecam (top panel); orange = main view (bottom).</span></div>
            ${slider("cam_x", "Cam ↔", 0, 1, 0.01, e.cam_x)}${slider("cam_y", "Cam ↕", 0, 1, 0.01, e.cam_y)}${slider("cam_w", "Cam width", 0.05, 1, 0.01, e.cam_w)}${slider("cam_h", "Cam height", 0.05, 1, 0.01, e.cam_h)}${slider("split", "Cam share", 0.2, 0.6, 0.01, e.split)}`
          : e.layout === "single" ? `<div class="row wrap">${seg("fit", e.fit, [["crop", "Crop to fill"], ["blur", "Fit + blur"]])}<button class="btn sm" data-act="reframe" ${e.fit === "blur" ? "disabled" : ""}>◎ Auto-reframe on face</button></div>` : ""}` : ""}
        ${!reframed || e.fit !== "blur" || e.layout !== "single" ? slider("crop_x", "Framing ↔", 0, 1, 0.01, e.crop_x) + slider("crop_y", "Framing ↕", 0, 1, 0.01, e.crop_y) + slider("zoom", "Zoom", 1, 2.5, 0.05, e.zoom, "×") : ""}
      </div>
      <div class="sec"><h3>Captions &amp; text</h3>
        <label class="switch"><input type="checkbox" data-k="captions" ${e.captions ? "checked" : ""}> Animated captions (word-by-word highlight)</label>
        ${seg("caption_style", e.caption_style, [["bold", "Bold"], ["clean", "Clean"], ["boxed", "Boxed"], ["neon", "Neon"]])}
        ${seg("caption_pos", e.caption_pos, [["low", "Low"], ["middle", e.layout === "split" ? "At seam" : "Middle"], ["high", "High"]])}
        ${seg("caption_anim", e.caption_anim || "highlight", CAP_ANIMS)}
        <label class="switch"><input type="checkbox" data-k="caption_emphasis" ${e.caption_emphasis ? "checked" : ""}> Emphasise keywords (numbers, SHOUTING, long words)</label>
        <label class="field">Title card (top of frame)<input type="text" data-k="title_text" value="${esc(e.title_text)}" placeholder="Optional headline overlay"></label>
        <div class="two"><label class="field">Sticker / handle<input type="text" data-k="sticker_text" value="${esc(e.sticker_text)}" placeholder="@yourchannel"></label>
          <label class="field">Sticker spot<select data-k="sticker_pos">${["top-left", "top-center", "top-right", "bottom-left", "bottom-right"].map((p) => `<option ${p === e.sticker_pos ? "selected" : ""}>${p}</option>`).join("")}</select></label></div>
      </div>
      <div class="sec"><h3>Text style</h3>
        <div class="seg">${[["caption", "Captions"], ["title", "Title card"], ["sticker", "Sticker"], ["headline", "Headline"]].map(([v, l]) => `<button class="${(E.tsk || "caption") === v ? "on" : ""}" data-act="tsk" data-v="${v}">${l}</button>`).join("")}</div>
        ${E.tsk === "headline" ? `<div id="ed-hl" class="stack" style="gap:8px"></div><a class="btn xs ghost" href="#/studio/${c.id}">Theme gallery, timing &amp; more in Studio</a>` : ""}
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
        ${e.music ? `<label class="switch"><input type="checkbox" data-k="music_duck" ${e.music_duck !== false ? "checked" : ""}> Duck the music under the voice</label>` : ""}
        <div class="row wrap"><span class="muted" style="font-size:12.5px">Bleep swearing</span>${seg("bleep", e.bleep || "off", [["off", "Off"], ["beep", "Beep"], ["mute", "Mute"]])}</div>
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
      def: tsDefaults(kind, e, OW, OH), sample: kind === "title" ? e.title_text || "Title card" : kind === "sticker" ? e.sticker_text || "@yourchannel" : kind === "headline" ? e.headline?.text || "Your headline" : "Captions look like this" });
    if (kind === "headline") headlineEditor($("#ed-hl"), { clip: cur, compact: true, set: (ed, d) => { const th = cur().edit.headline?.theme; patchClip(cur().id, { edit: ed }, d); if (ed.headline?.theme !== th) renderTab(); } });
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
         <button class="btn xs ghost" data-act="undo" title="Undo this clip's last edit (Ctrl+Z)">↶</button><button class="btn xs ghost" data-act="redo" title="Redo (Ctrl+Y or Ctrl+Shift+Z)">↷</button>${nleLinks(`/api/clips/${esc(c.id)}`)}<div class="grow"></div><button class="btn primary" data-act="publish">⇪ Publish</button>${current && ex.qa ? qaList(ex.qa) : ""}`;
  }

  function openAutopilot() {
    const ap = { ...apLoad(), ...(E.proj.autopilot || {}) };
    ap.platforms = ap.platforms || ["youtube"];
    if (ap.first_publish && /Z|\+00:00$/.test(ap.first_publish)) ap.first_publish = toLocalInput(new Date(ap.first_publish));
    modal(`<h2>⚡ Autopilot this video</h2><p class="muted">Render the top clips and schedule them to your channels in one go.</p>${apForm(ap, false)}
      <div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="ap-go">Schedule clips</button></div>`, (m, close) => {
      $("#ap-go", m).onclick = async () => {
        const body = apRead(m);
        if (!body.accounts?.length) return toast("Pick at least one account. Autopilot only posts to accounts with “Approved for uploads” and “Allow Autopilot to post here” on (Publish page).", "info", 7000);
        try { await api(`/api/projects/${pid}/autopilot/run`, { body }); close(); toast("Clips are rendering and will be scheduled — follow along on the Publish page.", "ok", 6000); }
        catch (e) { fail(e); }
      };
    });
  }
}

// Publish dialog for one clip (used by the Editor and the Clip Studio).
function openPublish(c, onDone, lf = null) { // lf: a long-form doc → a regular (not #Shorts) video; c = {id, title, edit, hook, hashtags}
  const vertical = !lf && ["9:16", "4:5"].includes(c.edit.aspect), conn = S.conn || {};
  const base = lf ? `/api/longform/${encodeURIComponent(lf.id)}` : `/api/clips/${c.id}`, secs = lf ? (lf.current && lf.export?.duration) || lf.length : 0;
  const tooLong = (a) => (lf && a.platform === "instagram" && secs > 900 ? `Instagram Reels can be at most 15 minutes (this is ${Math.round(secs / 60)} min)` : "");
  const tags = (c.hashtags || []).map((h) => h.replace(/^#/, ""));
  // only accounts the user approved on the Publish page; tick several to post the clip to each (two channels = two uploads)
  const all = Object.keys(PLATFORM).flatMap((p) => conn[p]?.accounts || []), ok = all.filter((a) => a.approved);
  const firstOf = Object.fromEntries(Object.keys(PLATFORM).map((p) => [p, ok.find((a) => a.platform === p && a.connected && !tooLong(a) && (!lf || p === "youtube"))?.id]));
  const waiting = all.length - ok.length;
  modal(`<h2>${lf ? "Publish long form" : "Publish"}</h2>
    <div class="stack">
      <div class="plats">${ok.length ? ok.map((a) => `<label title="${esc(tooLong(a)) || (a.connected ? "" : "Signed out: reconnect it on the Publish page, or it waits")}" ${tooLong(a) ? 'style="opacity:.45"' : ""}><input type="checkbox" class="pp" value="${esc(a.id)}" data-plat="${a.platform}" ${firstOf[a.platform] === a.id ? "checked" : ""} ${tooLong(a) ? "disabled" : ""}> ${platTag(a.platform)} ${esc(a.name || a.handle || PLATFORM[a.platform])} ${a.connected ? '<span style="color:var(--ok)">●</span>' : '<span class="dim">○</span>'}</label>`).join("")
        : `<span class="dim" style="font-size:12.5px">No account is approved for uploads yet: link one and turn on “Approved for uploads” on the <a href="#/publish">Publish page</a>.</span>`}</div>
      ${waiting ? `<p class="dim" style="font-size:11.5px;margin:-4px 0 0">${waiting} linked account${waiting === 1 ? " isn't" : "s aren't"} listed: not approved for uploads yet (Publish page).</p>` : ""}
      <div class="row"><label class="field grow">Title (YouTube)<input type="text" id="pt" maxlength="100" value="${esc(c.title)}"></label>
        <button class="btn" id="pai" style="align-self:flex-end" ${S.settings?.ai_ready?.meta ? "" : "disabled title='Add a Claude key or route Titles & captions to another AI in Settings'"}>✨ Write with AI</button></div>
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
      <label class="switch" title="YouTube: altered or synthetic content label. TikTok: AI-generated content label. Instagram's API has no such label: add it in the Instagram app if it applies."><input type="checkbox" id="paic" ${c.ai_media?.length || S.settings?.ai_disclosure === "always" ? "checked" : ""}> Contains AI-generated or altered realistic content <span class="dim">(YouTube &amp; TikTok label; not available for Instagram)</span></label>
      <label class="switch"><input type="checkbox" id="ptpl" checked> Add each account's description template <span class="dim" id="ptpl-ex"></span></label>
      <div id="ptt" class="card stack" hidden style="padding:12px"></div>
    </div>
    <div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="pgo">Queue post</button></div>`, (m, close) => {
    let mode = "now";
    if (!lf) api(`/api/publish/template?clip_id=${encodeURIComponent(c.id)}`).then((t) => { // what the template adds (edit on the Publish page)
      const v = t.vars || {};
      $("#ptpl-ex", m).textContent = v.source_url ? `(source link at ${v.vod_time}: ${v.source_url})` : "(this clip has no source link)";
    }).catch(() => {});
    const help = { now: "Posts as soon as the render is ready.", publish_at: "YouTube: uploads now as private and goes public at this time. TikTok/Instagram: posted at this time (Reelwright must be running).", upload_at: "Everything stays on this computer and is posted at this time (Reelwright must be running)." };
    const setMode = (v) => { mode = v; $$("#pwhen button", m).forEach((b) => b.classList.toggle("on", b.dataset.v === v)); $("#ptime-l", m).hidden = v === "now"; $("#pwhen-help", m).textContent = help[v]; };
    setMode("now");
    $("#pwhen", m).onclick = (e) => e.target.dataset.v && setMode(e.target.dataset.v);
    $("#pbest", m).onclick = async () => {
      const plat = $$(".pp", m).find((x) => x.checked)?.dataset.plat || "youtube";
      try {
        const r = await api("/api/schedule/suggest", { body: { platform: plat, category_id: $("#pcat", m).value } });
        if (r.times[0]) { $("#ptime", m).value = toLocalInput(new Date(r.times[0])); toast(`Best time for ${PLATFORM[plat]} (${r.source === "yours" ? "from your results" : r.source === "research" ? "from live research" : "default until there's data"}).`); }
      } catch (err) { fail(err); }
    };
    // TikTok's posting guidelines: show the account, let the user pick privacy (no default) and interactions (off by default,
    // greyed out when the creator disabled them), commercial-content disclosure, and TikTok's consent line.
    // One card per ticked TikTok account: each account has its own creator settings (TikTok's posting guidelines).
    const ttInfo = {}, ttBox = $("#ptt", m);
    const ttIds = () => $$(".pp", m).filter((x) => x.checked && x.dataset.plat === "tiktok").map((x) => x.value);
    const ttCard = (el, tt) => {
      if (tt.error) { el.innerHTML = `<div style="color:var(--warn);font-size:12.5px">TikTok: ${esc(tt.error)}</div>`; return; }
      const LBL = { PUBLIC_TO_EVERYONE: "Everyone", MUTUAL_FOLLOW_FRIENDS: "Friends", FOLLOWER_OF_CREATOR: "Followers", SELF_ONLY: "Only me" };
      el.innerHTML = `<div class="row"><b>TikTok</b><span class="dim" style="font-size:12.5px">posting as ${esc(tt.creator_nickname || tt.creator_username || "")}</span></div>
        <label class="field">Who can view this video<select class="tt-priv"><option value="" selected disabled>Choose…</option>${(tt.privacy_level_options || []).map((p) => `<option value="${esc(p)}">${LBL[p] || esc(p)}</option>`).join("")}</select></label>
        <div class="row wrap">${[["allow_comment", "Comment", tt.comment_disabled], ["allow_duet", "Duet", tt.duet_disabled], ["allow_stitch", "Stitch", tt.stitch_disabled]].map(([k, l, off]) =>
          `<label class="switch" ${off ? 'title="Turned off in your TikTok settings" style="opacity:.45"' : ""}><input type="checkbox" class="tt-${k}" ${off ? "disabled" : ""}> Allow ${l}</label>`).join("")}</div>
        <label class="switch"><input type="checkbox" class="tt-commercial"> This video promotes a brand, product or service</label>
        <div class="stack tt-com" hidden style="padding-left:12px">
          <label class="switch"><input type="checkbox" class="tt-brand_organic"> Your brand <span class="dim">(labelled “Promotional content”)</span></label>
          <label class="switch"><input type="checkbox" class="tt-brand_content"> Branded content <span class="dim">(labelled “Paid partnership”; can't be Only me)</span></label></div>
        <p class="dim tt-consent" style="font-size:11.5px;margin:0">By posting, you agree to TikTok's <a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank">Music Usage Confirmation</a>.</p>
        <p class="dim" style="font-size:11.5px;margin:0">${tt.max_video_post_duration_sec ? `Max length for this account: ${+tt.max_video_post_duration_sec}s. ` : ""}After posting, TikTok can take a few minutes to process the video.</p>`;
      $(".tt-commercial", el).onchange = (e) => { $(".tt-com", el).hidden = !e.target.checked; };
      $(".tt-brand_content", el).onchange = () => {
        $(".tt-consent", el).innerHTML = `By posting, you agree to TikTok's ${$(".tt-brand_content", el).checked ? `<a href="https://www.tiktok.com/legal/page/global/bc-policy/en" target="_blank">Branded Content Policy</a> and ` : ""}<a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank">Music Usage Confirmation</a>.`;
      };
    };
    const renderTT = () => { // keep cards already filled in; add the newly ticked, drop the unticked
      const ids = ttIds();
      ttBox.hidden = !ids.length;
      $$(".tt-acc", ttBox).forEach((el) => { if (!ids.includes(el.dataset.acc)) el.remove(); });
      for (const id of ids) {
        if (!ttInfo[id] || $$(".tt-acc", ttBox).some((el) => el.dataset.acc === id)) continue;
        const el = Object.assign(document.createElement("div"), { className: "tt-acc stack" });
        el.dataset.acc = id;
        ttBox.append(el);
        ttCard(el, ttInfo[id]);
      }
    };
    const loadTT = async () => {
      for (const id of ttIds()) {
        if (ttInfo[id]) continue;
        try { ttInfo[id] = await api(`/api/tiktok/creator?account_id=${encodeURIComponent(id)}`); } catch (err) { ttInfo[id] = { error: err.message }; }
      }
      renderTT();
    };
    $$(".pp", m).forEach((x) => x.addEventListener("change", loadTT));
    if (lf) $$('.pp[data-plat="tiktok"]', m).forEach(async (x) => { // a long form goes to TikTok only within that account's max length
      try { ttInfo[x.value] ||= await api(`/api/tiktok/creator?account_id=${encodeURIComponent(x.value)}`); } catch (err) { ttInfo[x.value] = { error: err.message }; }
      const max = +ttInfo[x.value].max_video_post_duration_sec || 0;
      if (max && secs > max) { x.checked = false; x.disabled = true; x.parentElement.style.opacity = ".45"; x.parentElement.title = `TikTok allows this account up to ${Math.round(max / 60)} min; this is ${Math.round(secs / 60)} min.`; renderTT(); }
    });
    loadTT();
    const ttRead = () => {
      const byAcc = {};
      for (const el of $$(".tt-acc", ttBox)) {
        const tt = ttInfo[el.dataset.acc], who = tt?.creator_nickname || tt?.creator_username || "TikTok";
        if (!tt || tt.error) continue;
        const priv = $(".tt-priv", el).value, commercial = $(".tt-commercial", el).checked;
        const opts = { privacy_level: priv, ...Object.fromEntries(["allow_comment", "allow_duet", "allow_stitch", "brand_organic", "brand_content"].map((k) => [k, !!$(`.tt-${k}`, el)?.checked])) };
        if (!priv) return { error: `Choose who can view the video on TikTok (${who}).` };
        if (commercial && !opts.brand_organic && !opts.brand_content) return { error: `Pick Your brand and/or Branded content for ${who}, or turn off the promotion switch.` };
        if (opts.brand_content && priv === "SELF_ONLY") return { error: "Branded content can't be set to Only me on TikTok." };
        if (!commercial) opts.brand_organic = opts.brand_content = false;
        byAcc[el.dataset.acc] = opts;
      }
      return { ok: true, byAcc };
    };
    $("#pai", m).onclick = async (e) => {
      const b = e.currentTarget; b.disabled = true; b.innerHTML = `<span class="spin"></span> Writing…`;
      try {
        const md = await api(`${base}/metadata`, { method: "POST" });
        $("#pt", m).value = md.title; $("#pd", m).value = md.description; $("#ptags", m).value = md.tags.join(", "); $("#pc", m).value = md.social_caption || md.title;
        if (CATEGORIES[md.category_id]) $("#pcat", m).value = md.category_id;
      } catch (err) { fail(err); }
      b.disabled = false; b.textContent = "✨ Write with AI";
    };
    $("#pgo", m).onclick = async () => {
      const t = fromLocalInput($("#ptime", m).value), picked = $$(".pp", m).filter((x) => x.checked);
      if (!picked.length) return toast("Pick at least one account.");
      if (mode !== "now" && (!t || new Date(t) < new Date())) return toast("Pick a time in the future.");
      const body = {
        accounts: picked.map((x) => x.value), title: $("#pt", m).value.trim(), description: $("#pd", m).value, caption: $("#pc", m).value.trim(),
        tags: $("#ptags", m).value.split(",").map((s) => s.trim()).filter(Boolean),
        category_id: $("#pcat", m).value, privacy: $("#ppriv", m).value, made_for_kids: $("#pkids", m).checked,
        ai_content: $("#paic", m).checked, template: $("#ptpl", m).checked,
        publish_at: mode === "publish_at" ? t : null, upload_at: mode === "upload_at" ? t : null,
      };
      if (!body.title) return toast("Add a title.");
      const t2 = ttRead();
      if (t2.error) return toast(t2.error, "err");
      body.tiktok_by_account = t2.byAcc;
      const names = picked.map((x) => ok.find((a) => a.id === x.value)?.name || PLATFORM[x.dataset.plat]);
      try { await api(`${base}/upload`, { body }); close(); toast(mode === "now" ? `Queued for ${names.join(", ")} — posts when the render finishes.` : `Scheduled for ${when(t)}.`, "ok"); onDone?.(); }
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

// ---------- split-screen layouts: regions (0..1 boxes of the source) placed by the layout; mirrors media.layout_slots ----------
const LAYOUTS = [["single", "Single"], ["split", "Stack"], ["split_rev", "Stack flipped"], ["stack3", "Three tiers"], ["pip", "Corner cam"],
  ["duo", "Duo + main"], ["blurcam", "Blur fit + cam"], ["free", "Freeform"]];
const ROLES = ["main", "cam", "chat", "guest", "extra"];
const ROLE_COLOR = { main: "#ff6a3d", cam: "#6aa8ff", chat: "#37d39a", guest: "#ffc247", extra: "#b07cff" };
const CAMS = ["cam", "guest", "extra", "chat"];
const LAYOUT_DEFAULTS = { split: 0.4, split2: 0.25, pip_corner: "tr", pip_size: 0.38, pip_radius: 0.15, pip_border: 6 };
const FREE_OUT = { x: 0.05, y: 0.05, w: 0.9, h: 0.3 }, FULL_BOX = { x: 0, y: 0, w: 1, h: 1 };
const isMulti = (e) => !!e && e.aspect !== "source" && !!e.layout && e.layout !== "single";
const legacySplit = (e) => e.layout === "split" && !e.regions?.length; // an old split clip: cam_* sliders still drive it
const seamOf = (e) => (e && e.aspect !== "source" && ["split", "split_rev", "stack3", "duo"].includes(e.layout) ? (e.layout === "split_rev" ? 1 - e.split : +e.split) : null);
const evenPx = (v) => { v = Math.round(v); return Math.max(v - (v % 2), 2); };
function clampBox(b) {
  const w = Math.min(Math.max(+b.w, 0.02), 1), h = Math.min(Math.max(+b.h, 0.02), 1);
  return { x: Math.min(Math.max(+b.x, 0), 1 - w), y: Math.min(Math.max(+b.y, 0), 1 - h), w, h };
}
function regionsOf(e) {
  const rs = (e.regions?.length ? e.regions : [{ id: "cam", role: "cam", x: e.cam_x, y: e.cam_y, w: e.cam_w, h: e.cam_h }]).map((r) => ({ follow: false, ...r }));
  if (!rs.some((r) => r.role === "main")) rs.push({ id: "main", role: "main", ...FULL_BOX, follow: false });
  return rs.map((r) => ({ ...r, ...clampBox(r) }));
}
function cropPx(sw, sh, ratio, cx, cy, zoom = 1) { // media.crop_box: [w, h, x, y]
  const z = Math.max(+zoom || 1, 1);
  let w, h;
  if (sw / sh > ratio) { h = sh / z; w = h * ratio; } else { w = sw / z; h = w / ratio; }
  w = Math.round(w); h = Math.round(h); w -= w % 2; h -= h % 2;
  return [w, h, Math.min(Math.max(Math.round(sw * cx - w / 2), 0), sw - w), Math.min(Math.max(Math.round(sh * cy - h / 2), 0), sh - h)];
}
function regionCrop(r, ratio, sw, sh, ax = 0.5, ay = 0.5, zoom = 1) {
  const rx = Math.round(r.x * sw), ry = Math.round(r.y * sh);
  const rw = Math.min(Math.max(Math.round(r.w * sw), 2), sw - rx), rh = Math.min(Math.max(Math.round(r.h * sh), 2), sh - ry);
  const [w, h, x, y] = cropPx(rw, rh, ratio, ax, ay, zoom);
  return [w, h, rx + x, ry + y];
}
function layoutSlots(edit, W, H, sw, sh, sizeOf = () => [sw, sh]) { // {base: null | "main" | "blur", items: [{r, box [x, y, w, h], crop [w, h, x, y], pip}]}
  // sizeOf(r): the frame size of the camera a region reads (a camera angle's own size; media.layout_slots does the same)
  const e = { ...LAYOUT_DEFAULTS, ...edit }, rs = regionsOf(e), used = new Set(), lay = e.layout;
  const pick = (roles) => { for (const role of roles) for (const r of rs) if (!used.has(r) && r.role === role) { used.add(r); return r; } return null; };
  const main = pick(["main"]), s = evenPx(H * e.split), slots = [];
  let base = null;
  if (lay === "split_rev") slots.push([main, [0, 0, W, H - s]], [pick(CAMS) || main, [0, H - s, W, s]]);
  else if (lay === "pip" || lay === "blurcam") {
    const cam = pick(CAMS);
    if (lay === "pip") { base = "main"; slots.push([main, [0, 0, W, H]]); } else {
      const [mw, mh] = sizeOf(main), ra = (main.w * mw) / (main.h * mh);
      let fw = W, fh = evenPx(W / ra);
      if (fh > H) { fw = evenPx(H * ra); fh = H; }
      base = "blur"; slots.push([main, [Math.floor((W - fw) / 2), Math.floor((H - fh) / 2), fw, fh]]);
    }
    if (cam) {
      const [cw, ch] = sizeOf(cam), ra = (cam.w * cw) / (cam.h * ch), m = evenPx(W * 0.04), c = e.pip_corner;
      let w = evenPx(W * e.pip_size), h = evenPx(w / ra);
      if (h > Math.floor(H / 2)) { h = evenPx(H / 2); w = evenPx(h * ra); }
      slots.push([cam, [c[1] === "l" ? m : W - w - m, c[0] === "t" ? m : H - h - m, w, h], true]);
    }
  } else if (lay === "free") {
    base = "blur";
    for (const r of [main, ...rs.filter((q) => q !== main)]) {
      const o = clampBox(r.out || (r === main ? FULL_BOX : FREE_OUT)), x = Math.round(o.x * W), y = Math.round(o.y * H);
      slots.push([r, [x, y, Math.min(evenPx(o.w * W), W - x - (x % 2)), Math.min(evenPx(o.h * H), H - y - (y % 2))]]);
    }
  } else { // split / stack3 / duo: top tier, main, (stack3) bottom tier
    const cam = pick(CAMS) || main, guest = lay === "duo" ? pick(["guest", "extra", "chat", "cam"]) : null, half = evenPx(W / 2);
    let third = lay === "stack3" ? pick(["chat", "guest", "extra", "cam"]) : null, t = third ? Math.min(evenPx(H * e.split2), H - s - evenPx(H * 0.15)) : 0;
    if (t < 2) { third = null; t = 0; }
    slots.push(...(guest ? [[cam, [0, 0, half, s]], [guest, [half, 0, W - half, s]]] : [[cam, [0, 0, W, s]]]), [main, [0, s, W, H - s - t]]);
    if (third) slots.push([third, [0, H - t, W, t]]);
  }
  const anchor = [+(e.crop_x ?? 0.5), +(e.crop_y ?? 0.5), Math.max(+e.zoom || 1, 1)];
  return { base, items: slots.map(([r, box, pip]) => ({ r, box, pip: !!pip, crop: regionCrop(r, box[2] / box[3], ...sizeOf(r), ...(r === main ? anchor : [])) })) };
}
// gallery thumbnail: the layout drawn with sample regions, coloured by role
const DEMO_REGIONS = [{ role: "cam", x: 0.7, y: 0.05, w: 0.28, h: 0.32, out: { x: 0.08, y: 0.05, w: 0.55, h: 0.22 } },
  { role: "guest", x: 0, y: 0.6, w: 0.25, h: 0.4, out: { x: 0.4, y: 0.3, w: 0.55, h: 0.2 } }, { role: "main", x: 0, y: 0, w: 0.75, h: 1 },
  { role: "chat", x: 0.75, y: 0.4, w: 0.25, h: 0.6, out: { x: 0.08, y: 0.7, w: 0.84, h: 0.24 } }];
function layoutThumb(lay) {
  const W = 90, H = 160, rect = ([x, y, w, h], fill, extra) => `<rect x="${x + 2}" y="${y + 2}" width="${Math.max(w - 4, 2)}" height="${Math.max(h - 4, 2)}" rx="6" fill="${fill}" ${extra}/>`;
  const { base, items } = lay === "single" ? { base: null, items: [{ r: { role: "main" }, box: [0, 0, W, H] }] }
    : layoutSlots({ layout: lay, regions: DEMO_REGIONS, crop_x: 0.5, crop_y: 0.5, zoom: 1, pip_size: 0.5 }, W, H, 1920, 1080);
  return `<svg viewBox="0 0 ${W} ${H}" aria-hidden="true">${base === "blur" ? rect([0, 0, W, H], ROLE_COLOR.main, 'opacity=".3"') : ""}${items.map((it) =>
    rect(it.box, ROLE_COLOR[it.r.role], it.pip ? 'stroke="#fff" stroke-width="4"' : 'opacity=".9"')).join("")}</svg>`;
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

// "+ Add camera angle": upload another camera's recording of the project; the server lines it up by sound (multicam.py)
function pickAngleFile(pid, done) {
  const inp = Object.assign(document.createElement("input"), { type: "file", accept: "video/*,.mkv,.mts,.ts,.flv" });
  inp.onchange = async () => {
    const f = inp.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f); fd.append("name", f.name.replace(/\.[^.]+$/, ""));
    toast(`Uploading ${f.name}…`);
    try { await api(`/api/projects/${pid}/angles`, { body: fd }); toast("Camera angle added — lining it up by sound.", "ok"); await done?.(); } catch (e) { fail(e); }
  };
  inp.click();
}

// NLE export (routes_editor.py): the timeline for Resolve / Final Cut (FCPXML), Premiere (EDL) and its captions (SRT)
const nleLinks = (base) => `<span class="nle-dl" title="Download the edit as a timeline for another editor: source file, in/out points, cuts, markers">Timeline:
  ${[["fcpxml", "FCPXML"], ["edl", "EDL"], ["srt", "SRT"]].map(([f, l]) => `<a class="btn xs ghost" href="${base}/nle/${f}" download>${l}</a>`).join("")}</span>`;

function Studio(cid) {
  const T = { proj: null, clip: null, tx: [], words: [], points: [], manual: [], job: null, mode: localStorage.studioMode || "source", drag: null, tdrag: null, range: [0, 1], wi: -1, tmode: "auto", target: null, pick: null, box: null,
    ovs: [], osel: null, assets: null, akind: "", aq: "", plan: null, picked: new Set(), sounds: new Map(), prevRel: null, rz: null, odrag: null, win: null,
    hist: RWUndo.History(100, 1000), srv: null, csel: null };
  let video, ov, octx, tl, tctx, raf, keyHandler, wordTimer;
  const c = () => T.clip;
  const pid = () => T.proj.id;
  const dur = () => T.proj?.meta?.duration || 0;
  const DONE = ["done", "failed", "canceled"];
  this.every = 2000;

  this.mount = async () => {
    T.proj = await projectOfClip(cid);
    T.clip = T.proj?.clips.find((k) => k.id === cid);
    HL.vt = vtOf(T.proj); await loadHeadline();
    if (T.clip && T.proj.source_state === "removed") { // the space saver removed the source: say so, offer the way back
      view.innerHTML = `<div class="page"><a class="btn ghost sm" href="#/editor/${esc(T.proj.id)}">← Back to project</a>
        <div class="empty"><b>This clip's source video was removed to save space</b>${sourceNote(T.proj)}<div class="muted">The clip, its edits and history are kept: bring the source back to keep editing it.</div></div></div>`;
      view.onclick = (e) => sourceAction(e.target, () => route());
      T.clip = null;
      return;
    }
    if (!T.clip || T.clip.parts || !T.proj.source) {
      view.innerHTML = `<div class="page"><a class="btn ghost sm" href="${T.proj ? `#/editor/${esc(T.proj.id)}` : "#/library"}">← Back</a>
        <div class="empty"><b>${!T.clip ? "Clip not found" : T.clip.parts ? "Montages can't be opened in the Studio" : "This project has no source video"}</b>${T.clip?.parts ? "Open one of its clips instead." : ""}</div></div>`;
      if (!T.clip && localStorage.lastClip === cid) { delete localStorage.lastClip; $("#nav-studio").hidden = true; }
      T.clip = null;
      return;
    }
    localStorage.lastProject = T.proj.id;
    localStorage.lastClip = cid; $("#nav-studio").hidden = false;
    T.srv = structuredClone(T.clip); // the clip as the server has it: what undo goes back to
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
    if (T.rgTimer) { clearTimeout(T.rgTimer); patch({ edit: { regions: regs() } }).catch(fail); } // a region drag still waiting to save
    if (T.tsTimer) { clearTimeout(T.tsTimer); flushEdit(); }
    T.sounds.forEach((au) => au.pause()); view.oninput = null;
    Object.values(T.av || {}).forEach((v) => { v.pause(); v.removeAttribute("src"); v.load(); });
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
  async function patch(body, record = true) {
    const undo = record && !locked() ? RWUndo.inverse(body, T.srv || c()) : null; // posted clips refuse edits: nothing to undo
    const fresh = await api(`/api/clips/${cid}`, { method: "PATCH", body }).catch((e) => { if (e.status === 409) { T.locked = true; T.hist.clear(); applyLock(); } throw e; });
    if (fresh && fresh.id) { T.clip = { ...c(), ...fresh }; T.srv = structuredClone(fresh); }
    if (undo) T.hist.push(cid, undo, body);
    return T.clip;
  }
  async function flushPending() { // edits still waiting on a debounce become their own undo step first
    const jobs = [];
    if (wordTimer) { clearTimeout(wordTimer); jobs.push(saveWords()); }
    if (T.ovTimer) { clearTimeout(T.ovTimer); T.ovTimer = null; jobs.push(patch({ edit: { overlays: T.ovs } })); }
    if (T.frTimer) jobs.push(saveFraming());
    if (T.rgTimer) jobs.push(flushRegions());
    if (T.tsTimer) { clearTimeout(T.tsTimer); flushEdit(); }
    await Promise.all(jobs);
  }
  async function undoRedo(redo) {
    if (locked()) return toast("Posted — make a new version to edit it.", "err");
    try { await flushPending(); } catch (e) { return fail(e); }
    const body = redo ? T.hist.redo(cid) : T.hist.undo(cid);
    if (!body) return toast(redo ? "Nothing to redo." : "Nothing to undo.");
    try { await patch(body, false); } catch (e) { T.hist.clear(); return fail(e); }
    T.ovs = structuredClone(c().edit.overlays || []); T.osel = null; T.csel = null;
    setTrack(c().edit.track || {}); loadWords(); renderPanel(); buildFx();
  }

  // ----- layout -----
  function layout() {
    setRange();
    view.innerHTML = `<div class="editor studio">
      <div class="ed-top">
        <a class="btn ghost sm" href="#/editor/${esc(pid())}">← Back to project</a>
        <div class="title" id="st-name" title="${esc(c().title)}">${esc(c().title)}</div>
        <span class="muted mono" id="st-span"></span>${keepBtn("clip", cid, c().keep)}${dueChip(c().due)}
        <div class="grow"></div>
        <div class="seg" id="st-mode">${[["source", "Source view"], ["output", "Output view"]].map(([v, l]) => `<button data-act="mode" data-v="${v}" class="${T.mode === v ? "on" : ""}">${l}</button>`).join("")}</div>
        <button class="btn sm" data-act="safe" id="st-safe-btn" title="Output view: shade where each app's own buttons and captions cover the frame">Safe zones: ${SAFE_NAMES[T.safe] || "off"}</button>
      </div>
      <div class="stage">
        <div class="player" id="st-player"><div class="st-frame" id="st-frame">
          <video id="svid" src="/files/${esc(pid())}/${esc(T.proj.proxy || T.proj.source)}" preload="auto" playsinline></video><canvas id="sov"></canvas><div class="st-fx" id="st-fx"></div></div></div>
        <div class="transport">
          <button class="btn sm" data-act="play" title="Space">▶</button>
          <span class="tc" id="stc">0:00.0</span>
          <button class="btn xs ghost" data-act="undo" title="Undo (Ctrl+Z)">↶</button><button class="btn xs ghost" data-act="redo" title="Redo (Ctrl+Y or Ctrl+Shift+Z)">↷</button>
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
    bindBoxes(ov, () => (!multiOn() || T.tmode === "target" ? null : T.mode === "source" ? regs() : c().edit.layout === "free" ? freeOuts() : null),
      (i, b) => (T.mode === "source" ? Object.assign(regs()[i], b) : (regs()[i].out = b)));
    view.onclick = onClick; view.oninput = onInput; view.onchange = onChange;
    keyHandler = (e) => {
      if (e.target.closest?.("input, textarea, select") || $("#modal-root").innerHTML) return;
      const z = (e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase();
      if (z === "z" || z === "y") { e.preventDefault(); undoRedo(z === "y" || e.shiftKey); }
      else if ((e.key === "Delete" || e.key === "Backspace") && T.csel) { e.preventDefault(); cutSel(); }
      else if (e.code === "Space") { e.preventDefault(); video.paused ? video.play() : video.pause(); }
      else if (e.key.startsWith("Arrow") && multiOn() && regs()[T.rsel]) { e.preventDefault(); nudgeRegion(e.key, e.shiftKey ? 0.02 : 0.004); }
      else if (e.key === "ArrowLeft") seek(video.currentTime - (e.shiftKey ? 1 : 1 / (T.proj.meta?.fps || 30)));
      else if (e.key === "ArrowRight") seek(video.currentTime + (e.shiftKey ? 1 : 1 / (T.proj.meta?.fps || 30)));
      else if (e.key === "Escape" && (T.pick || T.box)) { T.pick = T.box = null; renderTrack(); }
      else if (/^[0-6]$/.test(e.key) && readyAngs().length && !e.ctrlKey && !e.altKey && !e.metaKey) { // angle cuts at the playhead
        const id = e.key === "0" ? "main" : readyAngs()[+e.key - 1]?.id;
        if (id) cutTo(id);
      }
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
  const hint = () => { $("#st-hint").textContent = T.mode === "output" ? (multiOn() && c().edit.layout === "free" ? "Drag a box to place it in the frame" : "Showing the final framing")
    : multiOn() && T.tmode !== "target" ? "Drag the coloured boxes to mark each region · a corner resizes · arrow keys nudge" : T.tmode === "target" ? "Pause and drag a box around what to follow · Esc cancels" : c().edit.follow ? "Pause and drag the frame (or a corner, to zoom) to add a tracking keyframe" : "Drag the frame to set the framing"; };

  // ----- per-frame drawing: overlay or output crop, timecode, timeline, current word -----
  function frame() {
    if (!video || !c()) return;
    const k = c(), t = video.currentTime, rel = t - k.start;
    if (!video.paused && (t >= k.end || t < k.start - 0.25)) video.currentTime = k.start; // loop inside the clip
    const cut = !video.paused && (k.edit.cuts || []).find(([a, b]) => t >= a && t < b - 0.02);
    if (cut) video.currentTime = cut[1]; // text cuts: the preview jumps over them like the render
    if (angs().length) { syncAngles(t); drawCompare(); }
    $("#stc").textContent = `${fmt(Math.max(0, rel))} / ${fmt(k.end - k.start)}`;
    drawTimeline();
    const wi = T.words.findIndex((w) => t >= w.s && t <= w.e);
    if (wi !== T.wi) { $(`.wd[data-i="${T.wi}"]`)?.classList.remove("now"); $(`.wd[data-i="${wi}"]`)?.classList.add("now"); T.wi = wi; }
    const sw = video.videoWidth, sh = video.videoHeight;
    if (!sw) return;
    const f = framing(rel), win = winAt(f, sw, sh);
    T.win = win;
    fxTick(rel);
    const multi = multiOn();
    $("#st-frame").classList.toggle("multi", multi);
    if (multi) { // split-screen: region boxes on the source, the composition in the panel (and as the Output view)
      drawComp($("#st-comp"), rel);
      if (T.mode === "output") {
        const pl = $("#st-player"), [OW, OH] = outSize(), s = Math.min(pl.clientWidth / OW, pl.clientHeight / OH) * 0.98;
        Object.assign($("#st-frame").style, { width: OW * s + "px", height: OH * s + "px" });
        return drawComp(ov, rel);
      }
      if (T.tmode !== "target") return drawRegions();
    }
    if (T.mode === "output") {
      const pl = $("#st-player"), cw = win.w * sw, ch = win.h * sh, s = Math.min(pl.clientWidth / cw, pl.clientHeight / ch) * 0.98;
      Object.assign($("#st-frame").style, { width: cw * s + "px", height: ch * s + "px" });
      Object.assign(video.style, { width: sw * s + "px", height: sh * s + "px", left: -win.x * sw * s + "px", top: -win.y * sh * s + "px" });
      return drawLiveAngle(rel);
    }
    $("#st-frame").classList.remove("angle");
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
      if (T.mode !== "source" || !video.videoWidth || (multiOn() && T.tmode !== "target")) return; // split-screen: bindBoxes drags the regions
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
  let SW_Y, SW_H; // angle-switch lane
  const tlScale = () => { [TL_H, LANE_Y, LANE_H, KF_Y, FX_Y, FX_H, SW_Y, SW_H] = [T.proj?.angles?.length ? 116 : 100, 8, 40, 58, 76, 18, 99, 14].map((v) => v * S.ui.timeline); };
  tlScale();
  const tx = (t) => ((t - T.range[0]) / (T.range[1] - T.range[0])) * (tl.clientWidth || 1);
  const tAt = (px) => T.range[0] + (px / (tl.clientWidth || 1)) * (T.range[1] - T.range[0]);
  function drawTimeline() {
    tlScale();
    const dpr = window.devicePixelRatio || 1, W = tl.clientWidth;
    if (tl.width !== Math.round(W * dpr) || tl.height !== Math.round(TL_H * dpr)) { tl.width = Math.round(W * dpr); tl.height = Math.round(TL_H * dpr); tl.style.height = TL_H + "px"; }
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
    if (angs().length) drawSwitchLane(g, W);
    const X = tx(T.scrubT ?? video.currentTime);
    g.fillStyle = "#fff"; g.fillRect(X - 1, 0, 2, TL_H);
  }
  function bindTimeline() {
    tl.addEventListener("pointerdown", (ev) => {
      const x = localXY(ev, tl)[0], k = c();
      if (angs().length && localXY(ev, tl)[1] >= SW_Y - 2) { // switch lane: drag a cut point, or click to seek
        const i = cuts().findIndex((q) => Math.abs(tx(k.start + q.t) - x) < 7);
        if (i >= 0) { T.swdrag = { i }; tl.setPointerCapture(ev.pointerId); } else seek(tAt(x));
        return;
      }
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
      if (T.swdrag) { // a cut moves between its neighbours
        const sw = cuts(), i = T.swdrag.i, lo = i ? sw[i - 1].t + 0.1 : 0, hi = i + 1 < sw.length ? sw[i + 1].t - 0.1 : k.end - k.start;
        sw[i] = { ...sw[i], t: +Math.min(Math.max(tAt(x) - k.start, lo), hi).toFixed(2) };
        return;
      }
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
      if (T.swdrag) { T.swdrag = null; return saveCuts(); }
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

  // ----- split-screen layout: gallery, one box per region on the source, live composition -----
  const multiOn = () => isMulti(c()?.edit);
  const regs = () => T.regs || (T.regs = regionsOf(c().edit));
  const freeOuts = () => { const rs = regs(), m = rs.findIndex((r) => r.role === "main"); return rs.map((r, i) => clampBox(r.out || (i === m ? FULL_BOX : FREE_OUT))); };
  function saveRegions() { // the first change turns an old split clip's derived boxes into saved regions
    c().edit = { ...c().edit, regions: regs() };
    clearTimeout(T.rgTimer);
    T.rgTimer = setTimeout(() => { T.rgTimer = null; patch({ edit: { regions: regs() } }).catch(fail); }, 400);
  }
  async function flushRegions() { if (T.rgTimer) { clearTimeout(T.rgTimer); T.rgTimer = null; await patch({ edit: { regions: regs() } }); } }
  function selectRegion(i) { T.rsel = i; $$(".lay-reg").forEach((r) => r.classList.toggle("on", +r.dataset.i === i)); }
  function nudgeRegion(key, step) {
    const out = T.mode === "output" && c().edit.layout === "free", r = regs()[T.rsel], b = out ? freeOuts()[T.rsel] : r;
    const [dx, dy] = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[key] || [0, 0];
    const nb = clampBox({ ...b, x: b.x + dx, y: b.y + dy });
    if (out) r.out = nb; else Object.assign(r, nb);
    saveRegions();
  }
  // drag boxes (0..1 of el) on a canvas: the body moves one, a corner resizes it; pointer maths via localXY (CSS-zoom safe)
  function bindBoxes(el, boxes, apply) {
    let d = null;
    el.addEventListener("pointerdown", (ev) => {
      const bs = boxes();
      if (!bs || !video.videoWidth) return;
      const [lx, ly] = localXY(ev, el), W = el.offsetWidth || 1, H = el.offsetHeight || 1, px = lx / W, py = ly / H;
      // a corner handle wins, else the smallest box under the pointer (a full-frame main view must not hide the others)
      const hits = bs.map((b, i) => {
        const near = ([, qx, qy]) => Math.abs(qx - px) * W < 10 && Math.abs(qy - py) * H < 10;
        const corner = [["nw", b.x, b.y], ["ne", b.x + b.w, b.y], ["sw", b.x, b.y + b.h], ["se", b.x + b.w, b.y + b.h]].find(near)?.[0];
        return (corner || (px >= b.x && px <= b.x + b.w && py >= b.y && py <= b.y + b.h)) && { i, corner, rank: corner ? -1 : b.w * b.h };
      }).filter(Boolean).sort((a, b) => a.rank - b.rank);
      if (!hits.length) return selectRegion(null);
      const { i, corner } = hits[0];
      video.pause(); el.setPointerCapture(ev.pointerId);
      d = { i, corner, b0: { ...bs[i] }, px, py, W, H, moved: false };
      selectRegion(i);
    });
    el.addEventListener("pointermove", (ev) => {
      if (!d) return;
      const [lx, ly] = localXY(ev, el), dx = lx / d.W - d.px, dy = ly / d.H - d.py, b = { ...d.b0 };
      if (!d.corner) { b.x += dx; b.y += dy; } else {
        if (d.corner[1] === "w") { b.x += dx; b.w -= dx; } else b.w += dx;
        if (d.corner[0] === "n") { b.y += dy; b.h -= dy; } else b.h += dy;
      }
      apply(d.i, clampBox(b)); d.moved = true;
    });
    const end = () => { if (d?.moved) saveRegions(); d = null; };
    el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
  }
  function drawRegions() { // source view: one colour-coded box per region
    const dpr = window.devicePixelRatio || 1, W = video.clientWidth, H = video.clientHeight, g = octx;
    if (ov.width !== Math.round(W * dpr) || ov.height !== Math.round(H * dpr)) { ov.width = Math.round(W * dpr); ov.height = Math.round(H * dpr); }
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    g.font = "700 11px Inter, sans-serif";
    regs().forEach((r, i) => {
      const col = ROLE_COLOR[r.role], x = r.x * W, y = r.y * H, w = r.w * W, h = r.h * H, sel = i === T.rsel, label = r.role.toUpperCase() + (r.follow ? " · follows" : "");
      g.fillStyle = col + (sel ? "30" : "18"); g.fillRect(x, y, w, h);
      g.strokeStyle = col; g.lineWidth = sel ? 3 : 2; g.setLineDash(sel ? [] : [6, 4]); g.strokeRect(x + 1, y + 1, w - 2, h - 2); g.setLineDash([]);
      g.fillStyle = col; g.fillRect(x + 1, y + 1, g.measureText(label).width + 10, 16); g.fillStyle = "#111"; g.fillText(label, x + 6, y + 13);
      g.fillStyle = col;
      for (const [qx, qy] of [[x, y], [x + w, y], [x, y + h], [x + w, y + h]]) g.fillRect(qx - (sel ? 6 : 4), qy - (sel ? 6 : 4), sel ? 12 : 8, sel ? 12 : 8);
    });
  }
  function drawComp(cv, rel) { // the output frame as the renderer composes it (following regions ride the tracked path)
    const cw = cv?.clientWidth, ch = cv?.clientHeight, sw = video.videoWidth, sh = video.videoHeight;
    if (!cw || !ch || !sw) return;
    const dpr = window.devicePixelRatio || 1, [OW, OH] = outSize(), g = cv.getContext("2d"), k = cw / OW;
    if (cv.width !== Math.round(cw * dpr) || cv.height !== Math.round(ch * dpr)) { cv.width = Math.round(cw * dpr); cv.height = Math.round(ch * dpr); }
    g.setTransform(dpr * k, 0, 0, dpr * k, 0, 0);
    g.fillStyle = "#000"; g.fillRect(0, 0, OW, OH);
    const e = { ...LAYOUT_DEFAULTS, ...c().edit, regions: regs() }, sizeOf = (r) => angSize(r.source) || [sw, sh], { base, items } = layoutSlots(e, OW, OH, sw, sh, sizeOf);
    const draw = (src, sx, sy, sW, sH, x, y, w, h) => { if (src) g.drawImage(src[0], sx, sy, sW, sH, x, y, w, h); else { g.fillStyle = "#000"; g.fillRect(x, y, w, h); } };
    if (base === "blur") { const [w, h, x, y] = regionCrop(items[0].r, OW / OH, ...sizeOf(items[0].r)); g.filter = `blur(${Math.round(10 * dpr)}px)`; draw(srcOf(angSize(items[0].r.source) ? items[0].r.source : "main"), x, y, w, h, 0, 0, OW, OH); g.filter = "none"; }
    for (const it of items) {
      const own = angSize(it.r.source) ? it.r.source : "main", sid = it.r.id === e.switch_region && cuts().length ? liveAt(rel) : own, src = srcOf(sid);
      let [w0, h0, x0, y0] = sid === own ? it.crop : regionCrop(it.r, it.box[2] / it.box[3], ...(angSize(sid) || [sw, sh]));
      if (it.r.follow && sid === "main" && T.points.length) { const p = trackAt(T.points, T.manual, rel, base0()); x0 = Math.min(Math.max(p.cx * sw - w0 / 2, 0), sw - w0); y0 = Math.min(Math.max(p.cy * sh - h0 / 2, 0), sh - h0); }
      const [x, y, w, h] = it.box;
      g.save();
      if (it.pip) {
        const b = Math.min(+e.pip_border || 0, Math.min(w, h) / 4);
        g.beginPath(); g.roundRect(x, y, w, h, (+e.pip_radius || 0) * Math.min(w, h)); g.clip();
        g.fillStyle = "#fff"; g.fillRect(x, y, w, h);
        draw(src, x0, y0, w0, h0, x + b, y + b, w - 2 * b, h - 2 * b);
      } else draw(src, x0, y0, w0, h0, x, y, w, h);
      if (sid !== "main") angTag(g, angName(sid), x, y, 1 / k);
      g.restore();
      if (e.layout === "free") { g.strokeStyle = ROLE_COLOR[it.r.role]; g.lineWidth = (regs().indexOf(it.r) === T.rsel ? 3 : 1.5) / k; g.strokeRect(x, y, w, h); }
    }
  }
  const base0 = () => ({ cx: +c().edit.crop_x, cy: +c().edit.crop_y });
  function renderLayout() {
    const el = $("#st-layout"), e = { ...LAYOUT_DEFAULTS, ...c().edit };
    if (!el) return;
    if (!T.rgTimer) T.regs = regionsOf(e);
    if (e.aspect === "source") { el.innerHTML = `<h3>Layout</h3><div class="muted" style="font-size:12px">Split-screen layouts need a 9:16, 1:1, 4:5 or 16:9 output — pick one in the project's Edit tab.</div>`; return; }
    const multi = multiOn(), lay = e.layout, [OW, OH] = outSize(), rs = regs();
    // camera angles (project.angles, once multicam import exists): a source picker per region; hidden without any
    const angles = (T.proj.angles || []).map((a) => (typeof a === "string" ? [a, a] : [a.id, a.name || a.id])).filter(([v]) => v);
    const rl = (k, label, min, max, step, unit = "") => `<label class="slider"><span>${label}</span><input type="range" data-k="${k}" min="${min}" max="${max}" step="${step}" value="${e[k]}"><output>${(+e[k]).toFixed(step < 1 ? 2 : 0)}${unit}</output></label>`;
    el.innerHTML = `<h3>Layout</h3>
      <div class="lay-gal">${LAYOUTS.map(([v, l]) => `<button class="lay-card${lay === v ? " on" : ""}" data-act="layout" data-v="${v}" title="${l}" aria-pressed="${lay === v}">${layoutThumb(v)}<span>${l}</span></button>`).join("")}</div>
      ${multi ? `<canvas id="st-comp" class="lay-out" style="aspect-ratio:${OW}/${OH}" title="Output preview${lay === "free" ? " — drag a box to place it" : ""}"></canvas>
        ${["split", "split_rev", "stack3", "duo"].includes(lay) ? rl("split", lay === "split_rev" ? "Cam share (bottom)" : "Top share", 0.15, 0.7, 0.01) : ""}
        ${lay === "stack3" ? rl("split2", "Bottom share", 0.1, 0.5, 0.01) : ""}
        ${lay === "pip" || lay === "blurcam" ? `<div class="row"><span class="muted" style="font-size:12px">Cam corner</span>${segBtns("pip_corner", e.pip_corner, [["tl", "↖"], ["tr", "↗"], ["bl", "↙"], ["br", "↘"]])}</div>
          ${rl("pip_size", "Cam size", 0.15, 0.6, 0.01)}${rl("pip_radius", "Rounding", 0, 0.5, 0.01)}${rl("pip_border", "Border", 0, 24, 1, "px")}` : ""}
        <div class="lay-regs">${rs.map((r, i) => `<div class="lay-reg${T.rsel === i ? " on" : ""}" data-act="rg-sel" data-i="${i}"><i style="background:${ROLE_COLOR[r.role]}"></i>
          <select data-rg="${i}" data-f="role" aria-label="Region ${i + 1} role">${ROLES.map((ro) => `<option value="${ro}"${ro === r.role ? " selected" : ""}>${ro}</option>`).join("")}</select>
          ${angles.length ? `<select data-rg="${i}" data-f="source" aria-label="Region ${i + 1} camera">${[["main", "Main video"], ...angles].map(([v, l]) =>
            `<option value="${esc(v)}"${v === (r.source || "main") ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>` : ""}
          <label class="switch grow" title="This box follows the tracked subject (run tracking below)"><input type="checkbox" data-rg="${i}" data-f="follow"${r.follow ? " checked" : ""}> follow</label>
          <button class="st-x" data-act="rg-del" data-i="${i}" title="Remove this region"${rs.length < 2 ? " disabled" : ""}>✕</button></div>`).join("")}</div>
        <div class="row wrap"><button class="btn xs" data-act="rg-add"${rs.length >= 4 ? " disabled" : ""}>＋ Region</button><button class="btn xs" data-act="rg-detect">◎ Detect regions</button></div>
        <div class="muted" style="font-size:12px">Source view: drag a box to move it, a corner to resize it; arrow keys nudge the selected box (Shift = faster).${lay === "free" ? " Drag the boxes on the preview to place them in the frame." : ""}</div>` : ""}
      <div class="row wrap"><button class="btn xs" data-act="lay-all">Apply to all clips in this project</button><button class="btn xs" data-act="lay-default">Save as my default layout</button></div>`;
    const comp = $("#st-comp");
    if (comp) bindBoxes(comp, () => (c().edit.layout === "free" ? freeOuts() : null), (i, b) => { regs()[i].out = b; });
  }


  // ----- camera angles (project.angles, multicam.py): the Angles panel, hidden players on the main clock, angle cuts -----
  const ANG_COL = ["#8a8fa3", "#4fc3f7", "#f06292", "#aed581", "#ffb74d", "#ba68c8", "#4db6ac"]; // main, then angles 1-6
  const angs = () => (T.proj?.angles || []).filter((a) => a && a.id);
  const readyAngs = () => angs().filter((a) => a.status === "ready" && a.meta);
  const angName = (id) => (id === "main" ? "Main" : angs().find((a) => a.id === id)?.name || "Removed angle");
  const angCol = (id) => ANG_COL[id === "main" ? 0 : angs().findIndex((a) => a.id === id) + 1] || ANG_COL[0];
  const angSize = (id) => { const a = id && readyAngs().find((x) => x.id === id); return a ? [a.meta.width, a.meta.height] : null; };
  const cuts = () => c().edit.switches || [];
  const swDefault = () => (c().edit.switch_region && regs().find((r) => r.id === c().edit.switch_region)?.source) || "main";
  const liveAt = (rel) => { let cur = swDefault(); for (const q of cuts()) if (q.t <= rel) cur = q.angle; return angSize(cur) ? cur : "main"; };
  function angVid(a) { // one muted hidden player per ready angle
    T.av = T.av || {};
    let v = T.av[a.id];
    if (!v) { v = T.av[a.id] = Object.assign(document.createElement("video"), { muted: true, preload: "auto", playsInline: true, src: `/files/${pid()}/${a.file}` }); }
    return v;
  }
  function syncAngles(t) { // angle time = main time − offset; outside the angle's recording it shows black (like the render)
    for (const a of readyAngs()) {
      const v = angVid(a), want = t - (+a.offset_s || 0);
      v._on = want >= 0 && want <= (+a.meta.duration || v.duration || 0);
      if (!v._on || v.readyState < 1) { if (!v.paused) v.pause(); continue; }
      if (Math.abs(v.currentTime - want) > (video.paused ? 0.04 : 0.3)) v.currentTime = want;
      if (video.paused !== v.paused) video.paused ? v.pause() : v.play().catch(() => {});
    }
  }
  // what a camera shows right now: [element, width, height], or null = black (that angle isn't recording at this moment)
  const srcOf = (id) => { if (!id || id === "main") return [video, video.videoWidth, video.videoHeight]; const v = T.av?.[id]; return v?._on && v.videoWidth ? [v, v.videoWidth, v.videoHeight] : null; };
  const angTag = (g, text, x, y, s = 1) => {
    g.save(); g.font = `700 ${11 * s}px Inter, sans-serif`;
    g.fillStyle = "#000b"; g.fillRect(x + 4 * s, y + 4 * s, g.measureText(text).width + 10 * s, 17 * s);
    g.fillStyle = "#fff"; g.fillText(text, x + 9 * s, y + 16.5 * s); g.restore();
  };
  function drawLiveAngle(rel) { // single layout, Output view: the live angle (cover-cropped, centred) laid over the main framing
    const id = cuts().length ? liveAt(rel) : "main", fr = $("#st-frame");
    fr.classList.toggle("angle", id !== "main");
    if (id === "main") return;
    const W = fr.clientWidth, H = fr.clientHeight, dpr = window.devicePixelRatio || 1, g = octx, src = srcOf(id);
    if (ov.width !== Math.round(W * dpr) || ov.height !== Math.round(H * dpr)) { ov.width = Math.round(W * dpr); ov.height = Math.round(H * dpr); }
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.fillStyle = "#000"; g.fillRect(0, 0, W, H);
    if (src) { const [w, h, x, y] = cropPx(src[1], src[2], W / H, 0.5, 0.5); g.drawImage(src[0], x, y, w, h, 0, 0, W, H); }
    angTag(g, angName(id) + (src ? "" : " · not recording here (renders black)"), 0, 0);
  }
  function drawCompare() { // "Check sync": the main video and one angle side by side at the playhead
    const cv = $("#st-cmp");
    if (!cv || !T.cmp) return;
    const W = cv.clientWidth, H = cv.clientHeight, dpr = window.devicePixelRatio || 1, g = cv.getContext("2d");
    if (!W) return;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.fillStyle = "#000"; g.fillRect(0, 0, W, H);
    [["main", 0], [T.cmp, W / 2]].forEach(([id, x0]) => {
      const s = srcOf(id);
      if (s && s[1]) { const k = Math.min(W / 2 / s[1], H / s[2]), w = s[1] * k, h = s[2] * k; g.drawImage(s[0], x0 + (W / 2 - w) / 2, (H - h) / 2, w, h); }
      angTag(g, angName(id) + (s ? "" : " · not recording"), x0, 0);
    });
  }
  function drawSwitchLane(g, W) { // which camera is live along the clip; white ticks = cuts (drag them), yellow = suggestions
    const k = c(), len = k.end - k.start, segs = [[0, swDefault()], ...cuts().map((q) => [q.t, q.angle])];
    g.fillStyle = "#ffffff06"; g.fillRect(0, SW_Y, W, SW_H);
    g.font = `600 ${9 * S.ui.timeline}px Inter, sans-serif`;
    segs.forEach(([t0, id], i) => {
      const X0 = tx(k.start + t0), X1 = tx(k.start + (i + 1 < segs.length ? segs[i + 1][0] : len));
      if (X1 <= X0) return;
      g.fillStyle = angCol(id) + (id === "main" ? "66" : "cc"); g.beginPath(); g.roundRect(X0, SW_Y, X1 - X0, SW_H, 3); g.fill();
      g.save(); g.beginPath(); g.rect(X0, SW_Y, X1 - X0 - 2, SW_H); g.clip(); g.fillStyle = "#0b0c10"; g.fillText(angName(id), X0 + 4, SW_Y + SW_H - 4); g.restore();
    });
    g.fillStyle = "#fff";
    for (const q of cuts()) g.fillRect(tx(k.start + q.t) - 1, SW_Y - 2, 2, SW_H + 4);
    if (T.sugg) { g.fillStyle = "#ffc247"; for (const q of T.sugg.switches) g.fillRect(tx(k.start + q.t) - 1.5, SW_Y - 4, 3, 5); }
  }
  function cutTo(id) { // a cut at the playhead (replacing one within 0.1 s of it)
    const rel = +Math.min(Math.max(video.currentTime - c().start, 0), c().end - c().start).toFixed(2);
    c().edit.switches = [...cuts().filter((q) => Math.abs(q.t - rel) >= 0.1), { t: rel, angle: id }].sort((a, b) => a.t - b.t);
    toast(`Cut to ${angName(id)} at ${fmt(rel)}`);
    saveCuts();
  }
  async function saveCuts() { try { await patch({ edit: { switches: cuts() } }); } catch (e) { fail(e); } renderAngles(); }
  async function saveAngle(ag, body) { Object.assign(ag, await api(`/api/projects/${pid()}/angles/${ag.id}`, { method: "PATCH", body })); renderAngles(); }
  async function reloadAngles(full) { // full: the clip's regions / cuts were changed on the server (an angle was removed)
    const p = await api(`/api/projects/${pid()}`).catch(() => null);
    if (!p || T.dead) return;
    const was = angs().filter((a) => a.status === "importing" || a.status === "syncing").map((a) => a.id);
    T.proj.angles = p.angles || [];
    for (const id of Object.keys(T.av || {})) if (!readyAngs().some((a) => a.id === id)) { T.av[id].pause(); delete T.av[id]; }
    for (const a of angs().filter((x) => was.includes(x.id))) {
      if (a.status === "ready") toast(`${a.name}: ${a.sync?.method === "audio" ? "lined up by sound" : a.sync?.note || "ready"}`, a.sync?.method === "audio" ? "ok" : "info", 8000);
      else if (a.status === "failed") toast(`${a.name}: ${a.error || "import failed"}`, "err", 8000);
    }
    const fresh = p.clips.find((x) => x.id === cid);
    if (full && fresh) { c().edit = fresh.edit; c().rev = fresh.rev; T.regs = null; }
    renderAngles(); renderLayout();
  }
  async function showObsPicker() {
    const el = $("#st-ang-obs"), list = await api(`/api/projects/${pid()}/angles/obs`);
    el.innerHTML = list.length ? list.map((s) => `<div class="muted ang-note">${esc(s.name)} · ${esc(s.folder)}</div>
      <div class="ang-obs">${s.files.map((f) => `<button class="btn xs ghost" data-act="ang-obs-pick" data-obs="${esc(s.obs_id)}" data-file="${esc(f.name)}" title="${(f.size / 1e6).toFixed(0)} MB · ${esc(new Date(f.mtime * 1000).toLocaleString())}">${esc(f.name)}</button>`).join("") || `<span class="muted ang-note">No recordings there.</span>`}</div>`).join("")
      : `<div class="muted ang-note">No OBS recordings folder known yet: connect OBS in Settings and keep it connected (or let it import one recording first).</div>`;
  }
  function renderAngles() {
    const el = $("#st-angles");
    if (!el) return;
    const as = angs(), ready = readyAngs(), e = c().edit, multi = multiOn(), full = as.length >= 6;
    const nudge = (a, d) => `<button class="btn xs" data-act="ang-nudge" data-id="${esc(a.id)}" data-d="${d}" title="Move this angle ${Math.abs(d)} s ${d < 0 ? "earlier" : "later"} on the main video's clock">${d > 0 ? "+" : "−"}${Math.abs(d)}</button>`;
    const status = (a) => (a.status === "ready" ? (a.sync?.method === "audio" ? `✓ Lined up by sound (confidence ${(+a.sync.confidence).toFixed(0)})`
      : a.sync?.method === "manual" ? "✎ Offset set by hand" : `⚠ ${esc(a.sync?.note || "Not synced: set the offset by hand.")}`)
      : a.status === "failed" ? `✕ ${esc(a.error || "Import failed")}` : `<span class="spin"></span> ${a.status === "syncing" ? "Lining up the sound…" : "Importing…"}`);
    el.innerHTML = `<h3>Camera angles <span class="muted">${as.length}/6</span></h3>
      ${as.map((a, i) => `<div class="ang">
        <div class="ang-top"><i style="background:${angCol(a.id)}"></i>${a.thumb ? `<img src="/files/${esc(pid())}/${esc(a.thumb)}" alt="">` : `<span class="ang-ph"></span>`}
          <input type="text" data-ang="${esc(a.id)}" data-f="name" value="${esc(a.name)}" maxlength="60" aria-label="Angle name">${a.status === "ready" ? `<span class="kbd" title="Press ${i + 1} to cut to this angle at the playhead">${i + 1}</span>` : ""}</div>
        <div class="muted ang-note">${status(a)}</div>
        ${a.status === "ready" ? `<div class="ang-off"><span class="muted">Offset</span>${[-1, -0.1, -0.01].map((d) => nudge(a, d)).join("")}<input type="number" step="0.01" data-ang="${esc(a.id)}" data-f="offset_s" value="${(+a.offset_s).toFixed(2)}" aria-label="Offset in seconds" title="Where this angle starts on the main video's clock (s)">${[0.01, 0.1, 1].map((d) => nudge(a, d)).join("")}</div>` : ""}
        <div class="row wrap"><button class="btn xs${T.cmp === a.id ? " primary" : ""}" data-act="ang-cmp" data-id="${esc(a.id)}"${a.status === "ready" ? "" : " disabled"} title="Main video and this angle side by side: scrub the timeline to check the sync">⇆ Check sync</button>
          <button class="btn xs" data-act="ang-resync" data-id="${esc(a.id)}"${["ready", "failed"].includes(a.status) ? "" : " disabled"}>↻ Re-sync</button><button class="btn xs" data-act="ang-del" data-id="${esc(a.id)}">Remove</button></div></div>`).join("")}
      ${T.cmp && ready.some((a) => a.id === T.cmp) ? `<canvas id="st-cmp" class="ang-cmp"></canvas>` : ""}
      <div class="row wrap"><button class="btn xs" data-act="ang-add"${full ? " disabled" : ""}>＋ Add camera angle</button><button class="btn xs" data-act="ang-url"${full ? " disabled" : ""}>＋ From link</button><button class="btn xs" data-act="ang-obs"${full ? " disabled" : ""}>＋ From OBS</button></div>
      <div id="st-ang-obs" class="stack" style="gap:4px"></div>
      ${!as.length ? `<div class="muted ang-note">Another camera's recording of the same moment (a facecam, a wide shot, a second phone): it's lined up by sound, then a region can show it or you can cut between cameras.</div>` : ""}
      ${ready.length ? `<h3 style="margin-top:4px">Angle cuts</h3>
        ${multi ? `<label class="field">Cuts switch this region<select id="st-swreg"><option value="">— pick a region —</option>${regs().map((r) => `<option value="${esc(r.id)}"${r.id === e.switch_region ? " selected" : ""}>${esc(r.role)}${r.source ? ` (${esc(angName(r.source))})` : ""}</option>`).join("")}</select></label>`
          : `<div class="muted ang-note">Cuts switch the whole frame.</div>`}
        <div class="row wrap"><select id="st-cutto" aria-label="Camera to cut to">${[["main", "Main"], ...ready.map((a) => [a.id, a.name])].map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("")}</select>
          <button class="btn xs" data-act="cut-to" title="Keys 1–6 cut to an angle, 0 back to main">✂ Cut to angle at playhead</button><button class="btn xs" data-act="auto-switch" title="Suggest cuts: the loudest camera's mic, else the camera with the most movement">✦ Auto-switch</button></div>
        ${T.sugg ? `<div class="ang-sugg"><div><b>Suggested cuts</b> <span class="muted">${esc(T.sugg.note)}</span></div>
          <div class="ang-cuts">${T.sugg.switches.map((q) => `<span class="chip"><i style="background:${angCol(q.angle)}"></i>${fmt(q.t)} → ${esc(angName(q.angle))}</span>`).join("") || `<span class="muted">None.</span>`}</div>
          <div class="row"><button class="btn xs primary" data-act="sugg-apply"${T.sugg.switches.length ? "" : " disabled"}>Use these cuts</button><button class="btn xs" data-act="sugg-drop">Discard</button><span class="muted ang-note">Replaces your cuts; edit them after.</span></div></div>` : ""}
        <div class="ang-cuts">${cuts().map((q, i) => `<span class="chip"><i style="background:${angCol(q.angle)}"></i>${fmt(q.t)} → ${esc(angName(q.angle))}<button class="st-x" data-act="cut-del" data-i="${i}" title="Remove this cut" aria-label="Remove cut at ${fmt(q.t)}">✕</button></span>`).join("")
          || `<span class="muted ang-note">No cuts: press 1–6 to cut to an angle at the playhead, 0 back to main. Drag a cut on the timeline's bottom lane to move it.</span>`}</div>
        ${multi && cuts().length && !e.switch_region ? `<div class="ang-note warn">Pick the region the cuts switch, or they won't show in this layout.</div>` : ""}
        <label class="field">Sound from<select id="st-asrc">${[["main", "Main video"], ...ready.filter((a) => a.meta.has_audio).map((a) => [a.id, a.name])].map(([v, l]) => `<option value="${esc(v)}"${v === (e.audio_source || "main") ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
        <div class="muted ang-note">The preview follows the angles live; the render is frame-exact. Where an angle wasn't recording, its picture is black.</div>` : ""}`;
  }

  // ----- side panel -----
  const QUICK = ["Make it punchier", "Tighter on the face", "Add zoom on the punchline", "Clean captions"];
  const segBtns = (k, cur, opts) => `<div class="seg">${opts.map(([v, l]) => `<button class="${String(cur) === v ? "on" : ""}" data-act="set" data-k="${k}" data-v="${v}">${l}</button>`).join("")}</div>`;
  function renderPanel() {
    const k = c(), e = k.edit;
    $("#st-panel").innerHTML = `
      <div class="sec"><input type="text" id="st-title" value="${esc(k.title)}" style="font-weight:600">
        <div class="muted" style="font-size:12px">Output ${esc(e.aspect === "source" ? "original frame" : e.aspect)}${e.fit === "blur" && e.layout === "single" && e.aspect !== "source" ? " · fit + blur (preview shows the crop)" : ""}</div></div>
      <div class="sec" id="st-layout"></div>
      <div class="sec" id="st-angles"></div>
      <div class="sec"><h3>Framing</h3>
        <label class="slider"><span>Headroom</span><input type="range" data-fr="offset_y" min="-0.4" max="0.4" step="0.01" value="${+(e.framing?.offset_y || 0)}"><output>${(+(e.framing?.offset_y || 0)).toFixed(2)}</output></label>
        <label class="slider"><span>Zoom</span><input type="range" data-fr="zoom" min="1" max="4" step="0.05" value="${baseZ()}"><output>${baseZ().toFixed(2)}×</output></label>
        <div class="muted" style="font-size:12px">Drag a corner of the frame (Source view) to zoom at that moment.</div></div>
      <div class="sec" id="st-track"></div>
      <div class="sec"><h3>Cuts</h3>
        <label class="switch"><input type="checkbox" data-k="remove_silence" ${e.remove_silence ? "checked" : ""}> AI jump cuts — remove silences &amp; dead air</label>
        <label class="switch"><input type="checkbox" data-k="remove_fillers" ${e.remove_fillers ? "checked" : ""}> Cut filler words (“um”, “uh”)</label>
        <div class="muted" style="font-size:12px">Cut by text: click a word, Shift-click another, then press Delete or Cut. Click a struck-out word to restore it.</div>
        <div class="st-words st-cutw" id="st-cutw"></div>
        <div class="row"><span class="muted grow" style="font-size:12px" id="st-cutn"></span><button class="btn xs" data-act="cut">✂ Cut</button><button class="btn xs ghost" data-act="cut-clear">Restore all</button></div></div>
      <div class="sec"><h3>Captions</h3>
        <label class="switch"><input type="checkbox" data-k="captions" ${e.captions ? "checked" : ""}> Animated captions</label>
        ${segBtns("caption_style", e.caption_style, [["bold", "Bold"], ["clean", "Clean"], ["boxed", "Boxed"], ["neon", "Neon"]])}
        ${segBtns("caption_pos", e.caption_pos, [["low", "Low"], ["middle", "Middle"], ["high", "High"]])}
        ${segBtns("caption_anim", e.caption_anim || "highlight", CAP_ANIMS)}
        <label class="switch"><input type="checkbox" data-k="caption_emphasis" ${e.caption_emphasis ? "checked" : ""}> Emphasise keywords</label>
        <div class="row"><span class="muted grow" style="font-size:12px" id="st-wsrc"></span><button class="btn xs" data-act="words-reset">Reset to transcript</button></div>
        <div class="st-words" id="st-words"></div>
        <details><summary class="muted" style="font-size:12px;cursor:pointer">Caption font &amp; style</summary><div id="st-cts" class="stack" style="gap:8px;margin-top:8px"></div></details>
        <div class="muted" style="font-size:12px">Switch to Output view to see captions in the frame.</div></div>
      <div class="sec"><h3>Headline</h3><div id="st-hl-ed" class="stack" style="gap:8px"></div>
        <details><summary class="muted" style="font-size:12px;cursor:pointer">Headline font &amp; style (over the theme)</summary><div id="st-hts" class="stack" style="gap:8px;margin-top:8px"></div></details>
        <div class="muted" style="font-size:12px">Switch to Output view to see the banner in the frame.</div></div>
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
    const hlStyle = () => textStyleEditor($("#st-hts"), { get: (k) => c().edit[`headline_${k}`], set: (k, v) => queueEdit(`headline_${k}`, v), def: hlTsDefaults(c().edit), sample: c().edit.headline?.text || "Your headline" });
    headlineEditor($("#st-hl-ed"), { clip: c, set: (ed) => { const th = c().edit.headline?.theme; Object.entries(ed).forEach(([k, v]) => queueEdit(k, v)); if (ed.headline?.theme !== th) hlStyle(); } });
    hlStyle();
    renderLib(); renderOvInspector(); renderPlan(); renderLayout(); renderAngles();
    spanLabel(); renderTrack(); renderWords(); renderCuts(); renderActions();
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
    }).join("") + `<div class="st-cap" id="st-cap" hidden></div><div class="hl-prev" id="st-hl" hidden></div><div class="st-safe" id="st-safe" hidden></div>`;
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
    hlTick(out, fw, rel);
    safeTick(out);
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
    const anim = CAP_ANIMS.some(([k]) => k === e.caption_anim) ? e.caption_anim : "highlight", cur = chunk.findIndex((w, i) => t >= w.s && (i === chunk.length - 1 || t < chunk[i + 1].s));
    const html = chunk.map((w, i) => { const raw = String(w.w).trim(), txt = esc(upper ? raw.toUpperCase() : raw);
      if (i === cur) return anim === "box" ? `<em class="ca-box" style="background:${hl}">${txt}</em>` : `<em class="ca-${anim}" style="color:${hl}">${txt}</em>`;
      if (anim === "reveal" && cur >= 0 && i > cur) return `<span style="visibility:hidden">${txt}</span>`;
      return e.caption_emphasis && capKeyword(raw) ? `<b class="ca-emph" style="color:${hl}">${txt}</b>` : txt; }).join(" ");
    if (el._h !== html) { el._h = html; el.innerHTML = html; }
    styleText(el, tsOf((k) => e[`caption_${k}`], tsDefaults("caption", e, OW, OH)), fw / OW);
    let mv = CAPTION_POS[e.caption_pos || "low"][OH > OW ? 0 : 1] * OH;
    const hs = hlSpec(e);
    if (hs) { // keep clear of the banner, like media.build_ass
      const L = hlLayout(hs, OW, OH, seamOf(e)), y0 = L.kbox && !L.left ? L.kbox[1] : L.bar[1], y1 = L.bar[1] + L.bar[3], gap = OH * 0.015;
      const capH = 2.4 * tsOf((k) => e[`caption_${k}`], tsDefaults("caption", e, OW, OH)).size;
      if (OH - mv > y0 - gap && OH - mv - capH < y1 + gap) mv = y0 + y1 < OH ? OH - (y1 + gap + capH) : OH - (y0 - gap);
    }
    el.style.bottom = (mv / OH) * 100 + "%";
    el.style.background = e.caption_style === "boxed" ? "#0009" : "";
  }
  // platform safe zones over the output view (vertical only): the app UI that would cover text, from qa.SAFE_ZONES
  function safeTick(out) {
    const el = $("#st-safe"), z = SAFE_ZONES[T.safe], [OW, OH] = outSize(), on = !!(out && z && OH > OW);
    if (!el) return;
    if (el.hidden === on) el.hidden = !on;
    if (!on || el._k === T.safe) return;
    el._k = T.safe;
    el.innerHTML = `<i style="top:0;left:0;right:0;height:${z.top * 100}%"></i><i style="bottom:0;left:0;right:0;height:${z.bottom * 100}%"></i>`
      + `<i style="top:40%;bottom:${z.bottom * 100}%;right:0;width:${z.right * 100}%"></i><span>${esc(SAFE_NAMES[T.safe])} UI</span>`;
  }
  // the headline banner in the output view (only while it shows: whole clip or its first seconds)
  function hlTick(out, fw, rel) {
    const el = $("#st-hl"), e = c().edit, sp = out ? hlSpec(e) : null;
    const on = !!sp && (sp.show === "whole" || rel / (+e.speed || 1) <= +sp.seconds);
    if (!el) return;
    if (el.hidden === on) el.hidden = !on;
    if (!on) return;
    const [OW, OH] = outSize(), key = JSON.stringify(sp) + fw + OH;
    if (el._k !== key) { el._k = key; hlRender(el, sp, hlLayout(sp, OW, OH, seamOf(e)), fw / OW); }
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
    clearTimeout(T.rgTimer); T.rgTimer = null; T.regs = null; // regions come from the fresh clip
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
  // ----- text cuts (edit.cuts: [[s, e], …] source seconds; the server merges and clamps them) -----
  const cutWords = () => T.tx.filter((w) => w.s >= c().start - 0.05 && w.e <= c().end + 0.05);
  const cutOf = (w) => (c().edit.cuts || []).findIndex(([a, b]) => (w.s + w.e) / 2 >= a && (w.s + w.e) / 2 < b);
  function renderCuts() {
    const el = $("#st-cutw"); if (!el) return;
    const ws = cutWords(), [i, j] = T.csel ? [Math.min(...T.csel), Math.max(...T.csel)] : [-1, -2], cuts = c().edit.cuts || [];
    el.innerHTML = ws.length ? ws.map((w, k) => `<span class="cw${cutOf(w) >= 0 ? " cut" : ""}${k >= i && k <= j ? " sel" : ""}" data-ci="${k}" title="${fmt(w.s, 2)}">${esc(String(w.w).trim())}</span>`).join("")
      : `<span class="muted" style="font-size:12px">No transcript words in this clip.</span>`;
    $("#st-cutn").textContent = cuts.length ? `${cuts.length} cut${cuts.length > 1 ? "s" : ""} · ${fmt(cuts.reduce((n, [a, b]) => n + b - a, 0), 1)} removed` : "";
  }
  async function setCuts(cuts) {
    if (locked()) return toast("Posted — make a new version to edit it.", "err");
    try { await patch({ edit: { cuts: cuts.length ? cuts : null } }); } catch (e) { fail(e); }
    renderCuts();
  }
  function cutSel() {
    const ws = cutWords(); if (!T.csel) return;
    const a = ws[Math.min(...T.csel)], b = ws[Math.max(...T.csel)]; T.csel = null;
    if (a && b) setCuts([...(c().edit.cuts || []), [a.s, b.e]]);
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
         ${nleLinks(`/api/clips/${esc(cid)}`)}<div class="grow"></div><button class="btn primary" data-act="publish">⇪ Publish</button>${current && ex.qa ? qaList(ex.qa) : ""}`;
  }

  // ----- events -----
  async function onClick(ev) {
    const kb = ev.target.closest("[data-keep]"); // space saver pin for this clip
    if (kb) { const on = !kb.classList.contains("on"); await toggleKeep(kb); if (!kb.isConnected) c().keep = on; return; }
    const w = ev.target.closest(".wd");
    if (w) { video.pause(); seek(T.words[+w.dataset.i].s); return; }
    const cw = ev.target.closest(".cw");
    if (cw) {
      const k = +cw.dataset.ci, word = cutWords()[k], ci = word ? cutOf(word) : -1;
      if (ci >= 0) return setCuts((c().edit.cuts || []).filter((_, x) => x !== ci)); // a struck-out word: restore its cut
      T.csel = ev.shiftKey && T.csel ? [T.csel[0], k] : [k, k];
      video.pause(); if (word) seek(word.s);
      return renderCuts();
    }
    const a = ev.target.closest("[data-act]");
    if (!a) return;
    const k = c();
    try {
      switch (a.dataset.act) {
        case "play": video.paused ? video.play() : video.pause(); break;
        case "undo": case "redo": await undoRedo(a.dataset.act === "redo"); break;
        case "cut": if (T.csel) cutSel(); else toast("Click a word (Shift-click to extend), then Cut."); break;
        case "cut-clear": T.csel = null; await setCuts([]); break;
        case "mode": setMode(a.dataset.v); break;
        case "safe": { const all = ["", ...Object.keys(SAFE_ZONES)]; T.safe = all[(all.indexOf(T.safe || "") + 1) % all.length]; a.textContent = `Safe zones: ${SAFE_NAMES[T.safe] || "off"}`; if (T.safe && T.mode !== "output") setMode("output"); break; }
        case "layout": {
          await flushRegions();
          k.edit = { ...k.edit, layout: a.dataset.v }; T.rsel = null;
          renderLayout(); hint();
          await patch({ edit: { layout: a.dataset.v } });
          break;
        }
        case "rg-sel": selectRegion(+a.dataset.i); break;
        case "ang-add": pickAngleFile(pid(), () => reloadAngles()); break;
        case "ang-url": {
          const u = prompt("Link to the other camera's video (YouTube, Twitch VOD, any link yt-dlp can download)");
          if (u?.trim()) { await api(`/api/projects/${pid()}/angles/url`, { body: { url: u.trim() } }); await reloadAngles(); }
          break;
        }
        case "ang-obs": await showObsPicker(); break;
        case "ang-obs-pick": await api(`/api/projects/${pid()}/angles/obs`, { body: { obs_id: a.dataset.obs, file: a.dataset.file } }); T.obs = null; await reloadAngles(); break;
        case "ang-nudge": { const ag = angs().find((x) => x.id === a.dataset.id); if (ag) await saveAngle(ag, { offset_s: +(+ag.offset_s + +a.dataset.d).toFixed(3) }); break; }
        case "ang-cmp": T.cmp = T.cmp === a.dataset.id ? null : a.dataset.id; renderAngles(); break;
        case "ang-resync": await api(`/api/projects/${pid()}/angles/${a.dataset.id}/resync`, { method: "POST" }); await reloadAngles(); break;
        case "ang-del":
          if (await confirmBox("Remove this camera angle?", "Its file is deleted from this project. Regions that show it go back to the main video and cuts to it become cuts to main.", "Remove")) {
            await api(`/api/projects/${pid()}/angles/${a.dataset.id}`, { method: "DELETE" }); if (T.cmp === a.dataset.id) T.cmp = null; await reloadAngles(true);
          }
          break;
        case "cut-to": cutTo($("#st-cutto").value); break;
        case "cut-del": c().edit.switches = cuts().filter((_, i) => i !== +a.dataset.i); await saveCuts(); break;
        case "auto-switch": {
          a.disabled = true; a.innerHTML = `<span class="spin"></span> Listening…`;
          try { T.sugg = await api(`/api/clips/${cid}/switches/suggest`, { method: "POST" }); if (!T.sugg.switches.length) toast("No cuts suggested: one camera stays the best all through."); }
          finally { renderAngles(); }
          break;
        }
        case "sugg-apply": c().edit.switches = T.sugg?.switches || []; T.sugg = null; await saveCuts(); break;
        case "sugg-drop": T.sugg = null; renderAngles(); break;
        case "rg-add": {
          if (regs().length >= 4) break;
          const taken = new Set(regs().map((r) => r.role)), role = ["cam", "chat", "guest", "extra"].find((ro) => !taken.has(ro)) || "extra";
          regs().push({ id: "r" + Date.now().toString(36), role, x: 0.35, y: 0.35, w: 0.3, h: 0.3, follow: false });
          T.rsel = regs().length - 1; saveRegions(); renderLayout(); break;
        }
        case "rg-del": regs().splice(+a.dataset.i, 1); T.rsel = null; saveRegions(); renderLayout(); break;
        case "rg-detect": {
          a.disabled = true; a.innerHTML = `<span class="spin"></span> Detecting…`;
          try {
            await flushRegions();
            const fresh = await api(`/api/clips/${cid}/regions/detect`, { method: "POST" });
            T.clip = { ...c(), ...fresh }; T.regs = null; T.rsel = null;
            toast(`Found: ${regs().map((r) => r.role).join(", ")} — drag the boxes to fine-tune.`, "ok");
          } finally { renderLayout(); hint(); }
          break;
        }
        case "lay-all": {
          if (!(await confirmBox("Use this layout on every clip?", "Every other clip in this project that isn't posted gets this layout and these regions.", "Apply"))) break;
          await flushRegions();
          const r = await api(`/api/clips/${cid}/layout/apply-all`, { method: "POST" });
          toast(`Layout applied to ${r.updated} more clip${r.updated === 1 ? "" : "s"}.`, "ok"); break;
        }
        case "lay-default": await flushRegions(); await api(`/api/clips/${cid}/layout/default`, { method: "POST" }); toast("New clips will start with this layout.", "ok"); break;
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
    if (t.dataset.k && t.type === "range" && t.closest("#st-layout")) { // layout ratios / corner-cam look: live preview, batched save
      queueEdit(t.dataset.k, +t.value);
      t.nextElementSibling.textContent = (+t.value).toFixed(+t.step < 1 ? 2 : 0) + (t.dataset.k === "pip_border" ? "px" : "");
      return;
    }
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
    if (t.dataset.ang) { const ag = angs().find((x) => x.id === t.dataset.ang); if (ag) await saveAngle(ag, t.dataset.f === "name" ? { name: t.value } : { offset_s: +t.value }).catch(fail); return; }
    if (t.id === "st-swreg" || t.id === "st-asrc") {
      const key = t.id === "st-swreg" ? "switch_region" : "audio_source";
      k.edit = { ...k.edit, [key]: t.value };
      try { await patch({ edit: { [key]: t.value } }); } catch (e) { fail(e); }
      return renderAngles();
    }
    if (t.dataset.rg != null) { // a region's role or follow switch
      const r = regs()[+t.dataset.rg];
      if (!r) return;
      if (t.dataset.f === "role") r.role = t.value;
      else if (t.dataset.f === "source") { if (t.value === "main") delete r.source; else r.source = t.value; }
      else r.follow = t.checked;
      saveRegions(); return renderLayout();
    }
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
    if (angs().some((a) => a.status === "importing" || a.status === "syncing")) await reloadAngles();
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
  let data = null, q = "", sig = "", showExp = false;
  const sel = new Set(), this_ = this; // sel: "type:id" of the ticked cards
  this.every = 5000;
  const href = (u) => (/^#\/[\w\/-]*$/.test(u || "") ? u : null); // edit links stay inside the app
  const img = (u) => (/^(\/(?!\/)|https?:\/\/)/.test(u || "") ? u : null);
  const key = (it) => `${it.type}:${it.id}`;
  const all = () => (data?.stages || []).flatMap((st) => st.items || []);
  this.mount = async () => {
    view.innerHTML = `<div class="page"><div class="row wrap" style="align-items:flex-end"><div class="grow"><h1>Pipeline</h1>
        <p class="sub" style="margin:0">Every video from harvest to posted. Posted videos are locked — make a new version to change one.</p></div>
        <label class="row" style="gap:6px;font-size:12.5px" title="Clips and long forms the space saver removed (their posts and history are kept)"><input type="checkbox" id="pl-exp"> Show expired</label>
        <input type="text" id="pl-q" placeholder="Filter by title" style="max-width:260px"></div>
        <div id="pl-bulk" class="pl-bulk" hidden></div><div id="pl-body" style="margin-top:18px"></div></div>`;
    $("#pl-q").oninput = (e) => { q = e.target.value.trim().toLowerCase(); render(); };
    $("#pl-exp").onchange = (e) => { showExp = e.target.checked; this.refresh(); };
    view.onchange = (e) => {
      const cb = e.target.closest("[data-sel]");
      if (!cb) return;
      cb.checked ? sel.add(cb.dataset.sel) : sel.delete(cb.dataset.sel);
      cb.closest(".pl-card")?.classList.toggle("sel", cb.checked);
      renderBulk();
    };
    view.onkeydown = (e) => { // Delete on a focused card (not while typing in the filter)
      const c = e.target.closest?.(".pl-card[data-key]");
      if (e.key !== "Delete" || !c || e.target.matches("input[type=text], textarea")) return;
      e.preventDefault();
      const it = all().find((x) => key(x) === c.dataset.key);
      if (it) del([it]);
    };
    view.onclick = async (e) => {
      const kb = e.target.closest("[data-keep]");
      if (kb) { await toggleKeep(kb); sig = ""; return this_.refresh(); }
      if (e.target.closest("[data-bulk-del]")) return del(all().filter((it) => sel.has(key(it))));
      if (e.target.closest("[data-bulk-clear]")) { sel.clear(); return render(); }
      const d = e.target.closest("[data-del]");
      if (d) { const it = all().find((x) => key(x) === d.dataset.del); return it && del([it]); }
      const b = e.target.closest("[data-ver]");
      if (!b) return;
      b.disabled = true;
      const lf = b.dataset.vtype === "longform";
      try { const nc = await api(`/api/${lf ? "longform" : "clips"}/${encodeURIComponent(b.dataset.ver)}/version`, { method: "POST" }); location.hash = `#/${lf ? "long" : "studio"}/${nc.id || nc.clip?.id}`; }
      catch (err) { fail(err); b.disabled = false; }
    };
    await this.refresh();
  };
  this.refresh = async () => {
    try { data = await api(`/api/pipeline${showExp ? "?expired=1" : ""}`); } catch (e) { data = { error: e.status === 404 ? "" : e.message }; }
    const next = JSON.stringify(data);
    if (next !== sig) { sig = next; render(); }
  };
  function card(it, uploaded) {
    const th = img(it.thumb), edit = href(it.edit_url);
    const posts = (it.posts || []).map((p) => /^https?:\/\//.test(p.url || "")
      ? `<a class="pl-link" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(PLATFORM[p.platform] || p.platform)} ↗</a>`
      : `<span class="dim">${esc(PLATFORM[p.platform] || p.platform)}${p.publish_at ? ` · public ${esc(when(p.publish_at))}` : ""}</span>`).join("");
    const k = key(it), on = sel.has(k);
    return `<div class="pl-card${on ? " sel" : ""}" tabindex="0" data-key="${esc(k)}" aria-label="${esc(it.title || "Untitled")} (press Delete to delete)">
      <div class="pl-top"><label class="pl-check" title="Select"><input type="checkbox" data-sel="${esc(k)}" ${on ? "checked" : ""} aria-label="Select ${esc(it.title || "Untitled")}"></label>
        <button class="btn xs ghost danger" data-del="${esc(k)}" title="Delete" aria-label="Delete ${esc(it.title || "Untitled")}">🗑</button></div>
      ${th ? `<img src="${esc(th)}" alt="" loading="lazy">` : `<div class="pl-ph">▶</div>`}
      <div class="pl-t" title="${esc(it.title)}">${esc(it.title || "Untitled")}</div>
      <div class="pl-meta">${it.duration ? `<span>${fmt(+it.duration, 0)}</span>` : ""}${it.aspect ? `<span class="chip">${esc(it.aspect)}</span>` : ""}${it.type === "longform" ? `<span class="chip lf-badge" title="A long-form edit: captured → edited → rendered → uploaded">Long form · ${esc(it.lf_stage || "")}</span>` : it.type ? `<span>${esc(it.type)}</span>` : ""}</div>
      <div class="pl-meta">${it.expired ? `<span class="chip" title="${esc(it.expired.archive ? `Archived to ${it.expired.archive}` : "Removed by the space saver")}">Expired</span>${keepBtn(it.type, it.id, false, "↺ Restore")}`
        : `${keepBtn(it.type || "clip", it.id, it.keep)}${dueChip(it.due, it.type === "project")}`}</div>
      ${(it.pending || []).length ? `<div class="chips">${it.pending.map((p) => `<span class="chip">${esc(PLATFORM[p.platform] || p.platform)} · ${esc(p.status)}${p.when ? ` · ${esc(when(p.when))}` : ""}</span>`).join("")}</div>` : ""}
      ${uploaded ? `<div class="stack" style="gap:4px">${posts}<span class="dim" style="font-size:11.5px">🔒 Posted — locked</span>
          ${!it.type || it.type === "clip" || it.type === "longform" ? `<button class="btn xs" data-ver="${esc(it.id)}" data-vtype="${esc(it.type || "clip")}">Make a new version</button>` : ""}</div>`
        : edit ? `<a class="btn xs" href="${esc(edit)}">Edit</a>` : ""}</div>`;
  }
  function render() {
    const el = $("#pl-body");
    if (!el) return;
    if (!data || data.error !== undefined) { el.innerHTML = `<div class="card empty"><b>The pipeline view isn't available yet</b>${esc(data?.error || "It appears once the server supports it.")}</div>`; return; }
    const match = (it) => !q || String(it.title || "").toLowerCase().includes(q);
    const focused = document.activeElement?.closest?.(".pl-card")?.dataset.key;
    el.innerHTML = `<div class="pl-board">${(data.stages || []).map((st) => {
      const items = (st.items || []).filter(match), up = st.key === "uploaded";
      return `<div class="pl-col"><h3><span>${esc(st.label || st.key)}</span><span class="chip">${items.length}${q ? ` / ${(st.items || []).length}` : ""}</span></h3>
        ${items.map((it) => card(it, up)).join("") || `<div class="dim" style="font-size:12px;padding:8px 2px">Nothing here${q ? " matches" : ""}.</div>`}</div>`;
    }).join("")}</div>`;
    const keys = new Set(all().map(key));
    [...sel].forEach((k) => keys.has(k) || sel.delete(k)); // gone from the board: no longer selected
    if (focused) $(`.pl-card[data-key="${CSS.escape(focused)}"]`, el)?.focus(); // a refresh keeps keyboard focus
    renderBulk();
  }
  function renderBulk() {
    const bar = $("#pl-bulk");
    if (!bar) return;
    bar.hidden = !sel.size;
    bar.innerHTML = `<b>${sel.size} selected</b> · <button class="btn sm danger" data-bulk-del>Delete</button><button class="btn sm ghost" data-bulk-clear>Clear</button>`;
  }
  function describe(it) {
    const t = `<b>${esc(it.title || "Untitled")}</b>`, name = (p) => esc(PLATFORM[p] || p);
    if (it.type === "project") return { ok: true, html: `${t}: the project, its files and all its clips.` };
    const busy = (it.pending || []).filter((p) => p.status === "uploading");
    if (busy.length) return { ok: false, html: `${t}: can't be deleted now, it is uploading to ${busy.map((p) => name(p.platform)).join(", ")}. Wait for it to finish or let it fail.` };
    const n = (it.pending || []).length, posts = it.posts || [];
    const lines = posts.map((p) => p.platform === "youtube"
      ? `<label class="switch"><input type="checkbox" data-remote="${esc(p.upload_id)}"> Also delete the video from YouTube${p.account ? ` (${esc(p.account)})` : ""}</label>`
      : `<div class="dim">${name(p.platform)} doesn't let apps delete posts: ${/^https?:\/\//.test(p.url || "") ? `<a class="pl-link" href="${esc(p.url)}" target="_blank" rel="noopener">open the post ↗</a> and ` : ""}remove it in the app.</div>`);
    return { ok: true, html: `${t}: the ${it.type === "longform" ? "long-form edit" : "clip"} and its rendered files.${n ? ` Its ${n} pending post${n > 1 ? "s" : ""} will be canceled first.` : ""}${posts.length
      ? ` It is removed from Reelwright only; the post${posts.length > 1 ? "s stay" : " stays"} online unless you tick below.<div class="stack" style="gap:4px;margin-top:6px">${lines.join("")}</div>` : ""}` };
  }
  function del(items) {
    if (!items.length) return;
    const rows = items.map((it) => ({ it, ...describe(it) })), go = rows.filter((r) => r.ok);
    modal(`<h2>Delete ${items.length === 1 ? "this" : `${items.length} items`}?</h2><p class="muted">This can't be undone. What happens:</p>
      <div class="stack pl-del-list">${rows.map((r) => `<div class="pl-del${r.ok ? "" : " no"}">${r.html}</div>`).join("")}</div>
      <div class="foot"><button class="btn" data-close>Cancel</button>${go.length ? `<button class="btn primary danger" id="pl-del-ok">Delete${go.length < items.length ? ` ${go.length}` : ""}</button>` : ""}</div>`,
    (m, close) => { const ok = $("#pl-del-ok", m); if (ok) ok.onclick = () => {
      const remote = Object.fromEntries($$("[data-remote]", m).filter((c) => c.checked).map((c) => [c.dataset.remote, true]));
      close(); send(go.map((r) => r.it), remote);
    }; }, true);
  }
  async function send(items, remote) {
    const gone = new Set(items.map(key));
    data.stages.forEach((st) => (st.items = (st.items || []).filter((it) => !gone.has(key(it))))); // optimistic
    gone.forEach((k) => sel.delete(k));
    render();
    let res;
    try { res = (await api("/api/pipeline/delete", { body: { items: items.map((it) => ({ type: it.type, id: it.id })), options: { remote } } })).results; }
    catch (e) { fail(e); sig = ""; return this_.refresh(); }
    const bad = res.filter((r) => !r.ok), remoteBad = res.flatMap((r) => r.remote || []).filter((x) => !x.ok);
    const yt = res.flatMap((r) => r.remote || []).filter((x) => x.ok).length, canceled = res.reduce((s, r) => s + (r.canceled || 0), 0);
    const title = (r) => esc(items.find((it) => it.type === r.type && it.id === r.id)?.title || r.id);
    if (!bad.length && !remoteBad.length) toast(`Deleted ${res.length}${canceled ? ` · ${canceled} pending post${canceled > 1 ? "s" : ""} canceled` : ""}${yt ? ` · ${yt} removed from YouTube` : ""}`, "ok");
    else modal(`<h2>Deleted ${res.length - bad.length} of ${res.length}</h2><div class="stack pl-del-list">
        ${bad.map((r) => `<div class="pl-del no"><b>${title(r)}</b> wasn't deleted: ${esc(r.error)}</div>`).join("")}
        ${remoteBad.map((x) => `<div class="pl-del${x.manual ? "" : " no"}">${esc(PLATFORM[x.platform] || x.platform)}: ${esc(x.error)}${/^https?:\/\//.test(x.url || "") ? ` <a class="pl-link" href="${esc(x.url)}" target="_blank" rel="noopener">Open the post ↗</a>` : ""}</div>`).join("")}
        ${yt ? `<div class="pl-del">${yt} video${yt > 1 ? "s" : ""} deleted from YouTube.</div>` : ""}</div><div class="foot"><button class="btn primary" data-close>OK</button></div>`);
    sig = "";
    this_.refresh();
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
        <button class="btn sm" data-oact="mark" data-id="${c.id}" title="Mark this moment in the running recording: the analysis looks there first (needs “Import finished recordings”)">⚑ Mark</button>
        <button class="btn sm" data-otest="${c.id}">Test</button></div>
      <div class="dim" style="font-size:12px;margin-top:8px">${c.imported || 0} clips imported${c.last_at ? ` · last ${when(c.last_at)}: ${esc((c.last_file || "").split(/[\\/]/).pop())}` : ""} · ${c.import_replays !== false ? "replays" : ""}${c.import_recordings ? " + recordings" : ""}${c.autopilot?.enabled ? " · Autopilot" : ""}${c.recording ? ` · <b style="color:var(--err)">● recording</b>, ${+c.recording.marks} mark(s)` : ""}</div>
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
          if (t.dataset.oact === "mark") {
            const r = await api(`/api/obs/${id}/action`, { body: { action: "mark" } });
            const m = r.marks[0];
            toast(m ? `Marked at ${fmt(m.t)}${m.imports ? "" : " — turn on “Import finished recordings” for this OBS to use it"}.` : "That moment is before the recording started.", m?.imports ? "ok" : "info");
            sig = ""; return this.refresh();
          }
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
      <div class="stack"><div class="card" id="learned"></div><div class="card" id="works"></div><div class="card" id="evals" style="overflow-x:auto"></div><div id="profiles" class="stack"></div><div class="card" style="overflow-x:auto"><h2>Reports</h2><div id="reports"></div></div></div></div></div>`;
    loadLearned(); loadWorks(); loadEvals();
    const projects = await api("/api/projects");
    $("#lp").innerHTML = projects.filter((p) => p.status === "ready").map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("") || "<option value='' disabled selected>No analyzed projects yet</option>";
    $("#lgo").onclick = async () => {
      if (!$("#lp").value) return toast("Analyze a video in the Library first.");
      try { await api(`/api/projects/${$("#lp").value}/virality`, { body: { query: $("#lq").value } }); toast("Analysis started — the report appears below in a minute.", "ok"); sig = ""; this.refresh(); }
      catch (e) { fail(e); }
    };
    view.onchange = async (e) => {
      if (e.target.closest("[data-wset]")) return saveWorks();
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
  // "What works for you": taste per platform/account, experiments, retention; "Evals": the saved eval baselines
  const WDIM = { hook: "Hook", length: "Length", slot: "Posting time" };
  const WOPT = { question: "question", statement: "statement", short: "up to target length", long: "over target length", morning: "morning", afternoon: "afternoon", evening: "evening", night: "night" };
  const saveWorks = async () => {
    const on = (k) => !!$(`[data-wset="${k}"]`)?.checked;
    const experiments = { enabled: on("exp") };
    for (const dim of Object.keys(WDIM)) experiments[dim] = $$(`[data-wexp="${dim}"]`).filter((x) => x.checked).map((x) => x.value);
    try {
      S.settings = await api("/api/settings", { method: "PUT", body: { context_guard: on("guard"), context_cards: on("cards") ? "auto" : "off", tiktok_metrics: on("ttm"), experiments } });
      toast("Saved.", "ok");
    } catch (err) { fail(err); }
  };
  const curveSvg = (r) => {
    const pts = r.curve || [], W = 220, H = 56, maxS = Math.max(1, ...pts.map((q) => +q[0]));
    const xy = (sec, v) => `${((+sec / maxS) * W).toFixed(1)},${(H - 2 - (Math.min(+v, 1.2) / 1.2) * (H - 4)).toFixed(1)}`;
    const dx = r.drop_s != null ? ((+r.drop_s / maxS) * W).toFixed(1) : null;
    const drop = dx != null ? `<line x1="${dx}" x2="${dx}" y1="0" y2="${H}" stroke="var(--err)" stroke-dasharray="3 3"/>` : "";
    return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Share of viewers still watching over the clip" style="flex:none;background:var(--panel-2);border-radius:4px">${drop}<polyline fill="none" stroke="var(--ok)" stroke-width="2" points="${pts.map((q) => xy(q[0], q[1])).join(" ")}"/></svg>`;
  };
  const loadWorks = async () => {
    const box = $("#works");
    if (!box) return;
    let W;
    try { W = await api("/api/learn/insights"); } catch (e) { box.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
    const top = (m) => Object.entries(m.weights || {}).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 3).map(([k, v]) => `<span class="chip" style="font-size:11.5px">${v >= 0 ? "↑" : "↓"} ${esc(FEAT[k] || k)}</span>`).join("");
    const row = (label, m, n) => `<tr><td>${label}</td><td class="dim">${+n || 0}</td><td>${m ? (m.useful ? '<span class="pill ok">Active</span>' : '<span class="pill warn">Learning</span>') + (m.shrunk ? ' <span class="dim" style="font-size:11px" title="Few lessons of its own yet: pulled toward your overall taste">≈ overall</span>' : "") : '<span class="dim">needs ~12 lessons</span>'}</td><td>${m ? top(m) : ""}</td></tr>`;
    const ex = W.experiments.settings || {}, st = W.settings || {};
    const sw = (k, on, label, tip) => `<label class="switch" title="${esc(tip)}"><input type="checkbox" data-wset="${k}" ${on ? "checked" : ""}> ${label}</label>`;
    const scopes = [row("Overall", W.global, W.global?.n), ...Object.entries(W.platforms).map(([pl, m]) => row(platTag(pl), m, m?.n)),
      ...W.accounts.map((a) => row(`${platTag(a.platform)} ${esc(a.name || "")}`, a.model, a.lessons))];
    const dims = Object.entries(W.experiments.dims).map(([dim, vals]) => `<div class="row wrap" style="font-size:12.5px;gap:10px"><span class="muted" style="width:96px">${WDIM[dim] || esc(dim)}</span>${vals.map((v) => `<label class="switch"><input type="checkbox" data-wset="x" data-wexp="${dim}" value="${esc(v)}" ${(ex[dim] || []).includes(v) ? "checked" : ""}> ${WOPT[v] || esc(v)}</label>`).join("")}</div>`).join("");
    const results = W.experiments.accounts.map((a) => `<div style="font-size:12.5px">${platTag(a.platform)} <b>${esc(a.name || "")}</b> ${Object.entries(a.stats).map(([dim, o]) => `<span class="muted">${WDIM[dim] || esc(dim)}:</span> ${Object.entries(o).map(([v, [w, l]]) => `${WOPT[v] || esc(v)} <b>${+w}</b> won · ${+l} lost`).join(", ")}`).join(" · ")}</div>`).join("");
    const retInfo = (r) => (r.available ? `${Math.round(+r.hold_3s * 100)}% still watching at 3 s${r.drop_s != null ? ` · biggest drop at ${+r.drop_s}s` : ""}${(r.trim || [])[0] || (r.trim || [])[1] ? ` · suggests start ${+r.trim[0] >= 0 ? "+" : ""}${+r.trim[0]}s, end ${+r.trim[1]}s` : ""}` : esc(r.reason || ""));
    const retention = (W.retention || []).map((r) => `<div class="row" style="gap:10px;font-size:12px;margin-top:6px">${r.available ? curveSvg(r) : ""}<div class="grow"><b>${esc(r.title)}</b> ${platTag(r.platform)}<div class="dim">${retInfo(r)}</div></div></div>`).join("");
    box.innerHTML = `<h2 style="margin:0">What works for you</h2>
      <p class="muted" style="font-size:12.5px;margin:6px 0 10px">Learned from real results: each post's views against <i>that account's</i> usual for posts of the same age (${+W.with_lift} lessons so far), plus where viewers stop watching.</p>
      ${Object.entries(W.notes || {}).map(([aid, msg]) => `<div style="color:var(--warn);font-size:12.5px;margin-bottom:6px">⚠ ${esc(W.account_names[aid] || aid)}: ${esc(msg)}</div>`).join("")}
      <div class="row wrap" style="gap:14px;font-size:12.5px">${sw("guard", st.context_guard !== false, "Start picks at their setup", "A pick that opens mid-story (he…, and then…) is extended back to the sentence that sets it up, when it fits the max length")}
        ${sw("cards", st.context_cards !== "off", "“Earlier: …” card when it doesn't fit", "One small AI call writes a 2-second line shown as the clip's headline (never replaces your own headline)")}
        ${sw("ttm", !!st.tiktok_metrics, "Ask TikTok for view counts", "Requests TikTok's video.list permission when you (re)connect: add the Display API to your TikTok app first, then Reconnect")}</div>
      <h3 style="margin-top:12px">Taste by platform and account</h3>
      <table><thead><tr><th></th><th>Lessons</th><th>Model</th><th>Favours</th></tr></thead><tbody>${scopes.join("")}</tbody></table>
      <h3 style="margin-top:12px">Autopilot experiments</h3>
      <div class="stack" style="gap:6px">${sw("exp", ex.enabled, "Let Autopilot test what works on each account", "Thompson sampling: tries the options you tick below, then favours what wins (beats that account's usual). At most a quarter of posts explore.")}
        ${dims}<div class="dim" style="font-size:11.5px">Tick 2+ options to test a dimension. Posting time is only tested when Autopilot picks the best times.</div>
        ${results || `<div class="dim" style="font-size:12.5px">No experiment results yet.</div>`}</div>
      <h3 style="margin-top:12px">Where viewers stop watching</h3>
      ${retention || `<div class="dim" style="font-size:12.5px">YouTube posts get a retention curve once they are a week old (needs the yt-analytics.readonly permission: Reconnect on the Publish page).</div>`}`;
  };
  const loadEvals = async () => {
    const box = $("#evals");
    if (!box) return;
    let E;
    try { E = await api("/api/learn/evals"); } catch (e) { box.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
    const M = { precision_at_k: "Precision@k", iou: "Overlap (IoU)", complete: "Setup + payoff", bad_rate: "Bad picks ↓" };
    const pct = (v) => (v == null ? "—" : `${Math.round(+v * 100)}%`);
    const table = (b, label) => (b ? `<h3>${label} <span class="dim" style="font-weight:400;font-size:12px">· ${when(b.at)} · ${(b.cases || []).length} cases${b.guard === false ? " · setup guard off" : ""}</span></h3>
      <table><thead><tr><th>Finder</th>${Object.values(M).map((m) => `<th>${m}</th>`).join("")}</tr></thead><tbody>${Object.entries(b.finders || {}).map(([f, r]) => `<tr><td>${esc(f)}</td>${Object.keys(M).map((k) => `<td>${pct(r.summary?.[k])}</td>`).join("")}</tr>`).join("")}</tbody></table>` : "");
    box.innerHTML = `<h2 style="margin:0">Evals</h2><p class="muted" style="font-size:12.5px;margin:6px 0 10px">Golden-set checks of clip finding: hand-made transcripts with the moments a creator would post and ones they wouldn't. After changing prompts, models or the finder run <code>python scripts/eval.py</code> (offline, free); <code>--save</code> updates the baseline, <code>--live</code> tests your AI settings and asks before spending.</p>
      ${table(E.baseline, "Offline baseline")}${table(E.baseline_live, "Live baseline")}${!E.baseline && !E.baseline_live ? `<div class="dim" style="font-size:12.5px">No baseline saved yet.</div>` : ""}`;
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
      <span class="grow" style="font-size:13px">${esc(u.title)}${u.account ? ` <span class="dim">· ${esc(u.account)}</span>` : ""}</span><span class="dim" style="font-size:11px">${u.publish_at ? "goes live" : "posts"}</span></div>`).join("")}</div>`).join("")
      : `<div class="empty" style="padding:24px"><b>Nothing scheduled</b>Schedule from a clip's Publish button, or turn on Autopilot.</div>`;
    $("#queue").innerHTML = ups.length ? `<table><thead><tr><th>Post</th><th>Status</th><th>Schedule</th><th></th></tr></thead><tbody>${ups.map((u) => `
      <tr><td>${platTag(u.platform)} <b>${esc(u.title)}</b>${u.account ? ` <span class="dim" style="font-size:12px">→ ${esc(u.account)}</span>` : ""}<div class="dim mono">${u.clip ? `${fmt(u.clip.start)} → ${fmt(u.clip.end)}` : "clip deleted"} · ${esc(u.privacy)}</div></td>
      <td style="min-width:150px">${pill(UPLOAD_STATUS, u.status)}${u.status === "uploading" ? `<div style="margin-top:6px">${bar(u.progress)}</div>` : ""}${u.error ? `<div style="color:var(--err);font-size:11.5px;margin-top:4px">${esc(u.error).slice(0, 180)}</div>` : ""}</td>
      <td style="font-size:12.5px">${u.publish_at ? `Live ${when(u.publish_at)}` : u.upload_at ? `Posts ${when(u.upload_at)}` : "Immediately"}</td>
      <td style="text-align:right;white-space:nowrap">${/^https:\/\//.test(u.url || "") ? `<a class="btn xs" href="${esc(u.url)}" target="_blank">Open ↗</a> ` : ""}
        ${["failed", "needs_auth", "canceled"].includes(u.status) ? `<button class="btn xs" data-retry="${u.id}">Retry</button> ` : ""}
        ${u.status !== "uploading" ? `<button class="btn xs ghost" data-cancel="${u.id}" title="${u.status === "done" ? "Remove from list" : "Cancel"}">✕</button>` : ""}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty"><b>Nothing posted yet</b>Open a clip in the editor and press Publish.</div>`;
  };
  // Each platform card lists its linked accounts (each with its own sign-in and its own "okay"), plus "+ Add another account".
  let adding = "", igTarget = ""; // Instagram's token form: open to add an account / the account being reconnected
  const HINT = {
    youtube: "Google asks which account to use: to add another channel, pick a different Google account (or Brand Account) in its chooser. If it signs straight in, sign out of Google in your browser first.",
    tiktok: "TikTok signs in whoever is logged in on tiktok.com in your browser: to add a different account, log out (or switch accounts) on tiktok.com first, then click Add.",
    instagram: "Each Instagram account needs its own access token: generate one for the other account. With a Facebook Login token, Reelwright picks the next linked Instagram account not added yet (type its account ID to choose one).",
  };
  function renderConns(c) {
    const yt = c.youtube, tt = c.tiktok, ig = c.instagram;
    const safeImg = (u) => (/^https:\/\//.test(u || "") ? `<img src="${esc(u)}" alt="">` : "");
    const accs = (p) => c[p].accounts || [];
    const row = (p, a) => `<div class="chan" style="margin-bottom:12px;align-items:flex-start">${safeImg(a.avatar)}<div class="grow"><b>${esc(a.name || a.handle || "Account")}</b>
        <div class="muted" style="font-size:12px">${esc([a.handle && a.handle !== a.name ? a.handle : "", a.subscribers != null ? `${num(a.subscribers)} subscribers` : "", a.followers != null ? `${num(a.followers)} followers` : ""].filter(Boolean).join(" · "))}</div>
        ${a.connected ? "" : `<div style="color:var(--warn);font-size:12px">${a.locked ? "This sign-in was saved on another PC or Windows user, so it can't be read here: Reconnect to keep posting here." : "Signed out: Reconnect to keep posting here."}</div>`}
        <details style="margin-top:6px"><summary class="dim" style="font-size:12px">Description template ${a.description_template != null ? "(own)" : "(default)"}</summary>
          <textarea rows="2" data-tplacc="${esc(a.id)}" placeholder="Empty = use the default template below">${esc(a.description_template ?? "")}</textarea>
          <button class="btn xs" data-tplsave="${esc(a.id)}">Save</button></details>
        ${a.needs_reconnect ? `<div style="color:var(--warn);font-size:12px">Needs one more YouTube permission (to move videos that are already scheduled): Reconnect and approve it. Uploads keep working meanwhile.${a.missing_scopes?.length ? `<br><span class="dim mono" style="font-size:11px">Missing: ${a.missing_scopes.map(esc).join(", ")}</span>` : ""}</div>` : ""}
        ${a.error ? `<div style="color:var(--err);font-size:12px">${esc(a.error)}</div>` : ""}
        <div class="row wrap" style="gap:14px;margin-top:6px">
          <label class="switch" title="Nothing is posted to an account until you okay it here"><input type="checkbox" data-appr="${esc(a.id)}" ${a.approved ? "checked" : ""}> Approved for uploads</label>
          <label class="switch" ${a.approved ? 'title="Autopilot only posts to accounts with both switches on"' : 'style="opacity:.45" title="Approve the account for uploads first"'}><input type="checkbox" data-apauto="${esc(a.id)}" ${a.autopilot && a.approved ? "checked" : ""} ${a.approved ? "" : "disabled"}> Allow Autopilot to post here</label></div></div>
        <div class="stack" style="gap:6px"><button class="btn sm ghost" data-recon="${esc(a.id)}" data-plat="${p}">Reconnect</button><button class="btn sm ghost" data-rm="${esc(a.id)}">Remove</button></div></div>`;
    const head = (p) => {
      const n = accs(p).length, waiting = accs(p).filter((a) => !a.approved).length;
      return `<div class="row" style="margin-bottom:10px">${platTag(p)}<span class="grow"></span>${waiting ? `<span class="pill warn">${waiting} awaiting approval</span>` : ""}${n ? `<span class="pill ok">${n} linked</span>` : `<span class="pill warn">Not connected</span>`}</div>${accs(p).map((a) => row(p, a)).join("")}`;
    };
    const addBtn = (p, first, dis = "") => `<button class="btn ${accs(p).length ? "" : "primary"} sm" data-add="${p}" ${dis}>${accs(p).length ? "+ Add another account" : first}</button>`;
    const hint = (p) => (accs(p).length ? `<p class="dim" style="font-size:11.5px;margin:8px 0 0">${HINT[p]} A new account isn't used until you turn on <b>Approved for uploads</b>.</p>` : "");
    const ytBody = !yt.has_secret ? `<ol class="steps"><li>In <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank">Google Cloud</a>, enable <b>YouTube Data API v3</b>.</li>
        <li>Configure the OAuth consent screen and add yourself as a test user.</li><li>Create an OAuth client of type <b>Desktop app</b>, download the JSON, and upload it:</li></ol>
        <div style="margin-top:10px"><input type="file" id="secret" accept=".json" hidden><button class="btn primary sm" data-yt="pick">Upload client JSON</button></div>`
      : `<div class="row wrap">${yt.connecting ? `<button class="btn sm" disabled><span class="spin"></span> Waiting for Google…</button>` : addBtn("youtube", "Sign in with Google")}
        <button class="btn ghost sm" data-yt="pick">Replace client JSON</button><input type="file" id="secret" accept=".json" hidden></div>${hint("youtube")}`;
    const ttBody = accs("tiktok").length && tt.has_app ? `<div class="row wrap">${addBtn("tiktok")}</div>${hint("tiktok")}`
      : `<p class="muted" style="font-size:12.5px;margin:0 0 8px">Create an app at <a href="https://developers.tiktok.com/apps" target="_blank">TikTok for Developers</a> with Login Kit + Content Posting API (scopes <code>user.info.basic</code>, <code>video.publish</code>). Add this redirect URI:</p>
        <div class="mono" style="background:#0d0e14;padding:6px 8px;border-radius:6px;margin-bottom:8px;word-break:break-all">${esc(tt.redirect_uri)}</div>
        <div class="two"><input type="text" id="ttk" placeholder="Client key"><input type="password" id="tts" placeholder="Client secret"></div>
        <div class="row" style="margin-top:8px"><button class="btn sm" data-tt="save">Save app</button>${addBtn("tiktok", "Sign in with TikTok", tt.has_app ? "" : "disabled")}</div>
        <p class="dim" style="font-size:11.5px;margin:8px 0 0">Until TikTok audits your app, posts are private (only you can see them).</p>`;
    const reconIg = accs("instagram").find((a) => a.id === igTarget);
    const igBody = accs("instagram").length && !adding && !reconIg ? `<div class="row wrap">${addBtn("instagram")}</div>${hint("instagram")}`
      : `${reconIg ? `<p style="font-size:12.5px;margin:0 0 6px"><b>Reconnect ${esc(reconIg.name || "this account")}</b>: paste a new token for it.</p>` : ""}
        <p class="muted" style="font-size:12.5px;margin:0 0 6px">Needs an Instagram <b>Business or Creator</b> account. Either way works:</p>
        <ul class="list" style="font-size:12.5px;margin:0 0 8px"><li><b>Instagram Login</b> (simplest, no Facebook Page): in your <a href="https://developers.facebook.com/apps" target="_blank">Meta app</a> → Instagram → <i>API setup with Instagram login</i>, add your account under <i>Generate access tokens</i>. Use the <b>Instagram App ID</b> shown there (not the Meta/Facebook App ID) and scopes <code>instagram_business_basic</code>, <code>instagram_business_content_publish</code>. Paste the token; the account ID is optional.</li>
          <li><b>Facebook Login</b>: Instagram account linked to a Facebook Page; token with <code>instagram_basic</code>, <code>instagram_content_publish</code>, <code>pages_show_list</code>, <code>pages_read_engagement</code>. Reelwright finds the Instagram account through your Page.</li></ul>
        <p class="dim" style="font-size:11.5px;margin:0 0 8px">“Invalid platform app” from Meta means the Facebook App ID was used where the Instagram App ID belongs, or old scopes were requested.</p>
        <input type="password" id="igt" placeholder="Access token" style="margin-bottom:8px"><input type="text" id="igu" placeholder="Instagram account ID (optional — found automatically)">
        <div class="row" style="margin-top:8px"><button class="btn primary sm" data-ig="save">${reconIg ? "Reconnect" : accs("instagram").length ? "Add account" : "Connect Instagram"}</button>${accs("instagram").length ? `<button class="btn ghost sm" data-ig="cancel">Cancel</button>` : ""}</div>${hint("instagram")}`;
    $("#conns").innerHTML = `<p class="muted" style="font-size:12.5px;margin:0 0 4px">Link as many accounts per platform as you like; each one is only posted to after you turn on <b>Approved for uploads</b>. Every permission each platform needs, and step-by-step portal setup: <a href="/guide/connect.html" target="_blank">Connection guide</a>.</p>
      <div class="card">${head("youtube")}${ytBody}${yt.error ? `<p style="color:var(--err);font-size:12px">${esc(yt.error)}</p>` : ""}</div>
      <div class="card">${head("tiktok")}${ttBody}</div>
      <div class="card">${head("instagram")}${igBody}</div>
      <div class="card" id="secvault"></div>`;
    securityCard();
    const f = $("#secret");
    if (f) f.onchange = async () => { const fd = new FormData(); fd.append("file", f.files[0]); try { await api("/api/youtube/secret", { body: fd }); toast("Client saved — now sign in.", "ok"); connSig = ""; self.refresh(); } catch (e) { fail(e); } };
  }
  // Security & templates (WP3): whether sign-ins/keys are encrypted (DPAPI), the ones sealed on another PC, the default template
  async function securityCard() {
    const box = $("#secvault");
    if (!box) return;
    $$("[data-tplsave]", $("#conns")).forEach((b) => (b.onclick = async () => {
      const v = $(`[data-tplacc="${CSS.escape(b.dataset.tplsave)}"]`).value;
      try { await api("/api/publish/template", { method: "PUT", body: { account_id: b.dataset.tplsave, template: v.trim() ? v : null } }); toast("Template saved.", "ok"); redraw(); } catch (err) { fail(err); }
    }));
    let v, t;
    try { [v, t] = await Promise.all([api("/api/vault"), api("/api/publish/template")]); } catch (err) { box.textContent = err.message; return; }
    box.innerHTML = `<h3 style="margin:0 0 6px">Security &amp; post text</h3>
      <p style="font-size:12.5px;margin:0 0 6px">${v.encrypted ? "🔒 Sign-ins, keys and phone pairings are encrypted for your Windows account (they can't be read on another PC or user)." : `⚠ Sign-ins and keys are stored as plain text: ${esc(v.scheme)}.`}
        ${v.available ? `<button class="btn xs ghost" data-vault="${v.encrypted ? "rollback" : "enable"}">${v.encrypted ? "Store as plain files" : "Encrypt"}</button>` : ""}</p>
      ${v.locked.length ? `<div style="color:var(--warn);font-size:12.5px">Saved on another PC or Windows user, so unreadable here — enter or reconnect these:<ul class="list">${v.locked.map((x) => `<li>${esc(x.what)}</li>`).join("")}</ul><button class="btn xs" data-vault="forget">Forget the unreadable copies</button></div>` : ""}
      ${v.backups.length ? `<p class="dim" style="font-size:11.5px;margin:4px 0">Plain-text backups from before encryption (${v.backups.map(esc).join(", ")}) are deleted after 5 clean starts.</p>` : ""}
      <label class="field">Default description template <span class="dim">({title} {hook} {description} {source_url} {vod_time} {channel} {hashtags}; a line whose variables are empty is left out)</span>
        <textarea rows="2" id="tpl-def">${esc(t.default || "")}</textarea></label><button class="btn xs" data-vault="tpl">Save template</button>`;
    box.onclick = async (e) => {
      const a = e.target.dataset.vault;
      if (!a) return;
      try {
        if (a === "tpl") { await api("/api/publish/template", { method: "PUT", body: { template: $("#tpl-def").value } }); toast("Template saved.", "ok"); return; }
        if (a === "rollback" && !confirm("Store sign-ins and keys as plain files again (for an older Reelwright, or moving to another PC)? Encryption stays off until you turn it back on.")) return;
        await api(`/api/vault/${a}`, { method: "POST", body: { confirm: a } });
        securityCard();
      } catch (err) { fail(err); }
    };
  }
  const redraw = () => { connSig = ""; renderConns(S.conn); };
  const onClick = async (e) => {
    const t = e.target.closest("[data-yt],[data-tt],[data-ig],[data-add],[data-recon],[data-rm],[data-appr],[data-apauto],[data-retry],[data-cancel]");
    if (!t) return;
    try {
      const id = t.dataset.recon || t.dataset.rm, plat = t.dataset.add || t.dataset.plat;
      if (t.dataset.yt === "pick") return $("#secret").click();
      if (t.dataset.appr) await api(`/api/accounts/${encodeURIComponent(t.dataset.appr)}`, { method: "PATCH", body: { approved: t.checked } });
      if (t.dataset.apauto) await api(`/api/accounts/${encodeURIComponent(t.dataset.apauto)}`, { method: "PATCH", body: { autopilot: t.checked } });
      if (plat === "instagram" && (t.dataset.add || t.dataset.recon)) { adding = t.dataset.add ? "instagram" : ""; igTarget = t.dataset.recon || ""; return redraw(); }
      if (plat === "youtube" && (t.dataset.add || t.dataset.recon)) {
        await api("/api/youtube/connect", { body: { account_id: t.dataset.recon || null } });
        toast(t.dataset.recon ? "Sign in to that channel's Google account in the browser window that opened." : "Choose the Google account / channel to add in the browser window that opened.", "info", 7000);
      }
      if (plat === "tiktok" && (t.dataset.add || t.dataset.recon)) {
        const { url } = await api("/api/connections/tiktok/connect", { body: { account_id: t.dataset.recon || null } });
        window.open(url, "_blank");
        toast(t.dataset.recon ? "Approve Reelwright in the TikTok tab, signed in as that account." : "Approve Reelwright in the TikTok tab (to add a different account, log out on tiktok.com first), then come back here.", "info", 7000);
      }
      if (t.dataset.tt === "save") { await api("/api/connections/tiktok/app", { method: "PUT", body: { client_key: $("#ttk").value, client_secret: $("#tts").value } }); toast("TikTok app saved.", "ok"); }
      if (t.dataset.ig === "cancel") { adding = igTarget = ""; return redraw(); }
      if (t.dataset.ig === "save") {
        await api("/api/connections/instagram", { method: "PUT", body: { access_token: $("#igt").value, ig_user_id: $("#igu").value, account_id: igTarget || null } });
        toast(igTarget ? "Instagram account reconnected." : "Instagram account linked: turn on “Approved for uploads” to post to it.", "ok", 6000);
        adding = igTarget = "";
      }
      if (t.dataset.rm) {
        const a = Object.values(S.conn || {}).flatMap((x) => x.accounts || []).find((x) => x.id === id);
        if (!(await confirmBox(`Remove ${a?.name || "this account"}?`, "Its posts that haven't gone out yet are stopped, never moved to another account (you can move them on the Schedule page). Its sign-in is deleted from this PC.", "Remove"))) return;
        const r = await api(`/api/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
        toast(r.held ? `Removed. ${r.held} post${r.held === 1 ? " that was waiting was" : "s that were waiting were"} stopped: move ${r.held === 1 ? "it" : "them"} to another account on the Schedule page.` : "Account removed.", r.held ? "info" : "ok", 8000);
      }
      if (t.dataset.retry) await api(`/api/uploads/${t.dataset.retry}/retry`, { method: "POST" });
      if (t.dataset.cancel) await api(`/api/uploads/${t.dataset.cancel}`, { method: "DELETE" });
      connSig = ""; self.refresh(); sideStatus();
    } catch (err) { fail(err); connSig = ""; self.refresh(); }
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
    $("#usage").innerHTML = `<div class="row wrap"><h2 class="grow" style="margin:0">AI usage · last 30 days</h2><span class="dim" style="font-size:12px">estimated from per-token prices; local models (Ollama) cost nothing and other providers are unpriced; web searches during live research are billed separately and not included</span></div>
      ${u.calls ? `<div class="row wrap" style="gap:22px;margin:8px 0;font-size:13px"><span><b>${+u.calls}</b> calls</span><span><b>${money(u.cost_usd)}</b> est.${u.unpriced_calls ? ` <span class="dim">(+${+u.unpriced_calls} unpriced)</span>` : ""}</span>
          <span title="Share of input tokens served from the prompt cache (transcripts re-read at ~5% of the price)"><b>${u.cache_hit_ratio == null ? "—" : Math.round(u.cache_hit_ratio * 100) + "%"}</b> cache hits</span>
          <span class="dim">${num(u.input_tokens)} in · ${num(u.output_tokens)} out</span></div>
        <table><thead><tr><th>Feature</th><th>Calls</th><th>Input</th><th>Cached</th><th>Output</th><th>Est. cost</th></tr></thead><tbody>${Object.entries(u.by_kind).map(([k, v]) =>
          `<tr><td>${esc(k)}</td><td>${+v.calls}</td><td>${num(v.input)}</td><td>${num(v.cache_read)}</td><td>${num(v.output)}</td><td>${money(v.cost)}${v.unpriced ? " +?" : ""}</td></tr>`).join("")}</tbody></table>
        ${u.by_model ? `<div class="dim" style="font-size:12px;margin-top:6px">By model: ${Object.entries(u.by_model).map(([m, v]) => `${esc(m)} · ${+v.calls} calls · ${m.startsWith("ollama:") ? "free (local)" : v.unpriced === v.calls ? "unpriced" : money(v.cost)}`).join(" &nbsp;|&nbsp; ")}</div>` : ""}`
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
    view.innerHTML = `<div class="page"><h1>Schedule</h1><p class="sub">Every post waiting to go out. Drag a post to another hour to move it, or click it to change its time, move it to another approved account, or remove it. Shading shows the best hours for the selected platform.</p>
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
  const chip = (it) => `<div class="sch-post plat-${it.platform}" draggable="true" data-post="${esc(it.id)}" title="${esc(it.title)} — ${esc(PLATFORM[it.platform])}${it.account ? " · " + esc(it.account) : ""}, ${esc(UPLOAD_STATUS[it.status]?.[1] || it.status)}${it.error ? " — " + esc(it.error) : ""}">
      ${it.cover ? `<img src="${esc(it.cover)}" alt="">` : ""}<span>${esc(PLATFORM[it.platform].slice(0, 2))}</span> ${esc((it.title || "").slice(0, 26))}${it.account ? `<i class="dim"> · ${esc(it.account.slice(0, 18))}</i>` : ""}${it.auto ? " ⚡" : ""}${it.status === "held" ? " ⏸" : ""}${it.error ? " ⚠" : ""}</div>`;
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
        <label class="field">Account<select id="se-acc">${data.accounts.some((a) => a.id === it.account_id) ? "" : `<option value="" selected>${esc(PLATFORM[it.platform])} · ${esc(it.account || "no account")} (${it.account ? "not approved" : "removed or not chosen"}: pick another)</option>`}
          ${data.accounts.map((a) => `<option value="${esc(a.id)}" data-plat="${a.platform}" ${a.id === it.account_id ? "selected" : ""}>${esc(PLATFORM[a.platform])} · ${esc(a.name || a.handle || "account")}</option>`).join("")}</select></label>
        <div class="row"><label class="field grow">Date &amp; time<input type="datetime-local" id="se-time" value="${it.when ? toLocalInput(new Date(it.when)) : ""}"></label>
          <button class="btn" id="se-best" style="align-self:flex-end">★ Best time</button></div>
        <p class="dim" style="font-size:12px;margin:0">YouTube posts upload ahead and go public at this time; TikTok and Instagram post at this time (Reelwright must be running).</p>
        ${it.error ? `<div style="color:var(--err);font-size:12.5px">${esc(it.error)}</div>` : ""}
      </div>
      <div class="foot"><button class="btn danger" id="se-del">Remove</button><button class="btn" id="se-now">Post as soon as ready</button><span class="grow"></span><button class="btn" data-close>Cancel</button><button class="btn primary" id="se-save">Save</button></div>`, (m, close) => {
      const sel = () => $("#se-acc", m).selectedOptions[0];
      const plat = () => sel()?.dataset.plat || it.platform;
      const target = () => (sel()?.value && sel().value !== it.account_id ? { account_id: sel().value } : {}); // another approved account (its platform follows)
      $("#se-best", m).onclick = async () => {
        try {
          const r = await api("/api/schedule/suggest", { body: { platform: plat(), category_id: it.category_id, current: it.when } });
          if (r.times[0]) $("#se-time", m).value = toLocalInput(new Date(r.times[0]));
          toast(`Best time ${SRC[r.source]}.`);
        } catch (err) { fail(err); }
      };
      $("#se-save", m).onclick = async () => {
        const v = $("#se-time", m).value;
        if (v && new Date(v) < new Date()) return toast("Pick a time in the future.", "err");
        close(); await move(it.id, { when: v ? fromLocalInput(v) : null, ...target() });
      };
      $("#se-now", m).onclick = async () => { close(); await move(it.id, { when: null, ...target() }); };
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
// clip card: which two AIs agreed on a consensus-mode pick, both reasons in the tooltip
function consensusChip(k) {
  const who = (x) => `${({ anthropic: "Claude", ollama: "Ollama", openai: "OpenAI-compatible" })[x?.provider] || x?.provider}${x?.model ? ` (${x.model})` : ""}`;
  const tip = `${who(k.scout)} found it${k.scout?.score != null ? ` (${k.scout.score})` : ""}: ${k.scout?.reason || ""}\n${who(k.reviewer)} ${k.verdict === "adjust" ? "adjusted the cut" : "agreed"} (${k.reviewer?.score}): ${k.reviewer?.reason || ""}`;
  return `<span class="chip" style="font-size:11px;color:var(--ok)" title="${esc(tip)}">✓ 2 AIs ${k.verdict === "adjust" ? "agreed · re-cut" : "agreed"}</span>`;
}

// Settings → "Other AIs & consensus": per-task provider routing, Ollama / OpenAI-compatible setup, the two-AI consensus mode
const AI_TASKS = [["find", "Finding clips"], ["judge", "Ranking clips (long videos)"], ["classify", "Video type"], ["meta", "Titles & captions"], ["plan", "Edit plans"], ["assist", "Edit assistant"], ["other", "Everything else"]];
const AI_NAMES = [["anthropic", "Claude"], ["ollama", "Ollama (this PC)"], ["openai", "OpenAI-compatible"]];
const OAI_PRESETS = [["https://api.openai.com/v1", "OpenAI"], ["https://openrouter.ai/api/v1", "OpenRouter"], ["https://api.groq.com/openai/v1", "Groq"], ["https://generativelanguage.googleapis.com/v1beta/openai", "Google Gemini"], ["http://127.0.0.1:1234/v1", "LM Studio (this PC)"]];
function aiCard(s, opt) {
  const r = s.ai_routes || {}, ol = s.ai_providers?.ollama || {}, oa = s.ai_providers?.openai || {};
  const test = (p) => `<div class="row"><button type="button" class="btn sm" data-aitest="${p}">Test</button><span class="dim" style="font-size:12px" data-aiout="${p}"></span></div>`;
  return `<div class="card stack"><h2>Other AIs &amp; consensus</h2>
    <p class="muted" style="margin:0;font-size:13px">Run any task on a local model with <a href="https://ollama.com" target="_blank">Ollama</a> (free, nothing leaves this PC) or an OpenAI-compatible service. <b>Local first, Claude agrees</b>: the local model scans the whole video and proposes extra candidates, then Claude agrees, adjusts or rejects each one — fewer Claude tokens on long videos. Your clip length settings still apply after.</p>
    <label class="field">Consensus mode<select id="aicons">${opt([["off", "Off — one AI finds clips"], ["local_then_claude", "Local first, Claude agrees (Ollama → Claude)"], ["ollama:openai", "Ollama → OpenAI-compatible agrees"], ["openai:anthropic", "OpenAI-compatible → Claude agrees"], ["anthropic:openai", "Claude → OpenAI-compatible agrees"]], s.ai_consensus || "off")}</select></label>
    <div class="row wrap">${AI_TASKS.map(([k, l]) => `<label class="field grow" style="min-width:170px">${l}<select data-airoute="${k}">${opt(AI_NAMES, r[k] || "anthropic")}</select></label>`).join("")}</div>
    <h3 style="margin:6px 0 0">Ollama</h3>
    <div class="row wrap"><label class="field grow">Base URL<input id="ol-url" value="${esc(ol.base_url || "")}"></label>
      <label class="field grow">Model<select id="ol-model"><option value="${esc(ol.model || "")}">${esc(ol.model || "— pick a model —")}</option></select></label>
      <button type="button" class="btn sm" id="ol-refresh" style="align-self:flex-end" title="Reload the models installed in Ollama">↻</button>
      <label class="field" title="Tokens of transcript the model reads at once. Long videos need more; more uses more GPU memory.">Context<input type="number" id="ol-ctx" min="2048" max="262144" step="1024" value="${+ol.num_ctx || 16384}"></label></div>
    <span class="dim" style="font-size:12px" id="ol-note"></span>
    ${test("ollama")}
    <h3 style="margin:6px 0 0">OpenAI-compatible</h3>
    <div class="row wrap"><label class="field grow">Service<select id="oa-preset"><option value="">Custom URL</option>${opt(OAI_PRESETS, oa.base_url)}</select></label>
      <label class="field grow">Base URL<input id="oa-url" value="${esc(oa.base_url || "")}"></label>
      <label class="field grow">Model<input id="oa-model" value="${esc(oa.model || "")}" placeholder="e.g. gpt-4.1-mini, meta-llama/llama-3.3-70b-instruct"></label></div>
    <div class="row wrap"><label class="field grow">API key ${oa.api_key_set ? `<span class="pill ok">saved ${esc(oa.api_key_hint)}</span>` : ""}<input type="password" id="oa-key" placeholder="${oa.api_key_set ? "Leave blank to keep the current key" : "Not needed for local servers"}" autocomplete="off"></label>
      <label class="switch" style="align-self:flex-end"><input type="checkbox" id="oa-vision" ${oa.vision !== false ? "checked" : ""}> Send still frames (vision model)</label>
      ${oa.api_key_set ? `<button type="button" class="btn sm danger" id="oa-rmkey" style="align-self:flex-end">Remove key</button>` : ""}</div>
    <p class="muted" style="margin:0;font-size:12px">A custom base URL receives transcripts and frames for the tasks you route to it — only use one you trust. See the Privacy Policy.</p>
    ${test("openai")}
  </div>`;
}
function aiBody() {
  const routes = Object.fromEntries($$("[data-airoute]").map((x) => [x.dataset.airoute, x.value]));
  const openai = { base_url: $("#oa-url").value.trim(), model: $("#oa-model").value.trim(), vision: $("#oa-vision").checked };
  if ($("#oa-key").value.trim()) openai.api_key = $("#oa-key").value.trim();
  return { ai_consensus: $("#aicons").value, ai_routes: routes,
    ai_providers: { ollama: { base_url: $("#ol-url").value.trim(), model: $("#ol-model").value, num_ctx: +$("#ol-ctx").value || 16384 }, openai } };
}
function aiCardWire(s) {
  const loadModels = async () => {
    $("#ol-note").textContent = "Looking for Ollama models…";
    const res = await api("/api/ai/ollama/models").catch((e) => ({ models: [], error: e.message }));
    const cur = $("#ol-model").value;
    $("#ol-model").innerHTML = `<option value="">— pick a model —</option>` + res.models.map((m) => `<option value="${esc(m.name)}" ${m.name === cur ? "selected" : ""}>${esc(m.name)}${m.size ? ` · ${esc(m.size)}` : ""}${m.vision ? " · vision" : ""}</option>`).join("")
      + (cur && !res.models.some((m) => m.name === cur) ? `<option value="${esc(cur)}" selected>${esc(cur)} (not installed)</option>` : "");
    $("#ol-note").textContent = res.error ? `${res.error} — install it from ollama.com and run “ollama pull qwen3:8b”.` : `${res.models.length} model${res.models.length === 1 ? "" : "s"} installed.`;
  };
  $("#ol-refresh").onclick = loadModels;
  loadModels();
  $("#oa-preset").onchange = (e) => { if (e.target.value) $("#oa-url").value = e.target.value; };
  $("#oa-rmkey")?.addEventListener("click", async () => { await api("/api/settings", { method: "PUT", body: { ai_providers: { openai: { api_key: "" } } } }).catch(fail); toast("Key removed."); route(); });
  $$("[data-aitest]").forEach((b) => b.onclick = async () => {
    const p = b.dataset.aitest, out = $(`[data-aiout="${p}"]`);
    out.innerHTML = `<span class="spin"></span> Saving and testing…`;
    try {
      const body = aiBody();
      if (p === "anthropic" && $("#key").value.trim()) body.anthropic_api_key = $("#key").value.trim();
      S.settings = await api("/api/settings", { method: "PUT", body });
      const r = await api("/api/ai/test", { method: "POST", body: { provider: p } });
      out.innerHTML = r.ok ? `<span class="pill ok">works</span> ${esc(r.model)} replied “${esc(r.result)}”` : `<span class="pill err">failed</span> ${esc(r.error)}`;
    } catch (e) { out.innerHTML = `<span class="pill err">failed</span> ${esc(e.message)}`; }
  });
}

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
          <div class="row"><button type="button" class="btn sm" data-aitest="anthropic">Test Claude</button><span class="dim" style="font-size:12px" data-aiout="anthropic"></span></div>
        </div>
        ${aiCard(s, opt)}
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
      ${signalsCard()}
      ${renderCard()}
      <div class="card stack" style="margin-top:20px" id="appwin"><div class="row wrap"><h2 class="grow" style="margin:0">App window &amp; startup</h2>
          <button class="btn sm danger" id="appquit" hidden title="Close Reelwright completely (stops renders, uploads and Autopilot until you open it again)">⏻ Quit Reelwright</button></div>
        <label class="row" style="gap:8px"><input type="checkbox" id="c2t"> <span>Clicking ✕ minimizes to the taskbar instead of closing <span class="dim">— renders, uploads and Autopilot keep running</span></span></label>
        <label class="row" style="gap:8px"><input type="checkbox" id="sob"> <span>Start Reelwright when I sign in to Windows <span class="dim">— opens minimized in the taskbar</span></span></label>
        <div class="dim" id="appnote" style="font-size:12px"></div></div>
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
    const appShow = (a) => {
      $("#c2t").checked = a.close_to_taskbar; $("#sob").checked = a.start_on_boot; $("#appquit").hidden = !a.desktop;
      $("#appnote").textContent = a.desktop ? "" : "These apply to the Reelwright desktop app (Reelwright.exe); this page is open in a browser.";
    };
    api("/api/app").then(appShow).catch(() => {});
    $("#appwin").onchange = async (e) => {
      const key = { c2t: "close_to_taskbar", sob: "start_on_boot" }[e.target.id];
      if (!key) return;
      try { appShow(await api("/api/app", { method: "PUT", body: { [key]: e.target.checked } })); toast("Saved.", "ok", 1200); }
      catch (err) { e.target.checked = !e.target.checked; fail(err); }
    };
    $("#appquit").onclick = async () => {
      if (await confirmBox("Quit Reelwright?", "Renders, uploads and Autopilot stop until you open it again. Scheduled posts wait and go out when it's running.", "Quit")) {
        await api("/api/app/quit", { method: "POST" }).catch(fail);
      }
    };
    $("#rmkey")?.addEventListener("click", async () => { await api("/api/settings", { method: "PUT", body: { anthropic_api_key: "" } }).catch(fail); toast("Key removed."); sideStatus(); route(); });
    aiCardWire(s);
    storageCards(); // Settings › Storage + Space saver (storage.py), above App window
    phoneRemoteCard(); // its own card, appended below (section at the end of this file)
    liveCards(); // Live integrations + Claude Desktop (MCP): their own cards (section at the end of this file)
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
      Object.assign(body, aiBody());
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

// ======================================================================
// Long form — a separate edit of a project: ordered source segments played back to back as one landscape video
// (longform.py). Captured from the existing analysis (condensed / highlights / from clips), edited here, rendered,
// published as a regular YouTube video (TikTok / Instagram only within their length limits).
// ======================================================================
// ----- Review: one AI pick at a time, across projects, judged with the keyboard (GET /api/triage) -----
// Y keep / N skip go through PATCH /api/clips/{id} {rating}, the same path as 👍/👎, so the learning memory records them.
routes.review = Review;
function Review() {
  const R = { list: [], total: 0, i: 0, done: [], projects: [],
    f: { project: lsGet("rvProject") || "", new_only: lsGet("rvNew") !== "0", min_score: lsGet("rvMin") || "" } };
  let video, keyH;
  const cur = () => R.list[R.i];

  this.mount = async () => {
    R.projects = await api("/api/projects").catch(() => []);
    await load();
    keyH = onKey;
    document.addEventListener("keydown", keyH);
  };
  this.destroy = () => { document.removeEventListener("keydown", keyH); video?.pause(); };

  async function load() {
    const q = new URLSearchParams({ project: R.f.project, new_only: R.f.new_only ? "1" : "0", limit: "200" });
    if (R.f.min_score !== "") q.set("min_score", R.f.min_score);
    const r = await api(`/api/triage?${q}`);
    R.list = r.clips; R.total = r.total; R.i = 0;
    render();
  }

  function render() {
    const c = cur();
    view.innerHTML = `<div class="page review">
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <h1 style="margin:0" class="grow">Review</h1>
        <select id="rv-proj" title="Project"><option value="">All projects</option>${R.projects.map((p) => `<option value="${esc(p.id)}" ${p.id === R.f.project ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
        <label class="switch" title="Only picks you haven't rated yet"><input type="checkbox" id="rv-new" ${R.f.new_only ? "checked" : ""}> New only</label>
        <label class="row" style="gap:6px;font-size:12.5px">Min score <input type="number" id="rv-min" min="0" max="100" step="5" value="${esc(R.f.min_score)}" style="width:70px"></label>
      </div>
      ${c ? `<div class="rv-stage"><video id="rv-vid" src="${esc(c.src || "")}" ${c.rendered ? "loop" : ""} autoplay playsinline preload="auto"></video></div>
        <div class="row" style="gap:10px;margin-top:10px;align-items:flex-start">
          <div class="grow"><b style="font-size:16px">${esc(c.title)}</b>
            <div class="dim" style="font-size:12.5px">${esc(c.project || "")} · ${fmt(c.end - c.start, 1)} · ${c.rendered ? "rendered" : "source range"}${c.rating ? ` · rated ${c.rating > 0 ? "👍" : "👎"}` : ""}</div>
            ${c.hook ? `<div style="margin-top:4px">${esc(c.hook)}</div>` : ""}
            <div class="muted" style="font-size:12.5px;margin-top:4px">${esc(c.text || "")}</div></div>
          <div class="stack" style="gap:6px;align-items:flex-end">
            ${c.score != null ? `<span class="vbadge">✦ ${Math.round(+c.score)}</span>` : ""}<span class="dim mono">${R.i + 1} / ${R.list.length}${R.total > R.list.length ? ` of ${R.total}` : ""}</span></div></div>
        <div class="row" style="gap:6px;margin-top:10px;flex-wrap:wrap">
          <button class="btn primary" data-act="keep"><span class="kbd">Y</span> Keep</button><button class="btn" data-act="skip"><span class="kbd">N</span> Skip</button>
          <button class="btn ghost" data-act="undo"><span class="kbd">U</span> Undo</button><button class="btn ghost" data-act="studio"><span class="kbd">E</span> Open in Studio</button>
          <span class="grow"></span><span class="hint"><span class="kbd">J</span> <span class="kbd">K</span> <span class="kbd">L</span> back · play/pause · forward &nbsp; <span class="kbd">←</span> <span class="kbd">→</span> previous / next</span></div>`
        : `<div class="card empty" style="margin-top:14px"><b>Nothing left to review</b>${R.done.length ? `${R.done.length} judged this session. ` : ""}New AI picks land here after a scan; change the filters to see more.
          ${R.done.length ? `<div style="margin-top:8px"><button class="btn sm" data-act="undo"><span class="kbd">U</span> Undo the last one</button></div>` : ""}</div>`}</div>`;
    view.onclick = onClick;
    view.onchange = onChange;
    video = $("#rv-vid");
    if (video && c && !c.rendered) { // the pick inside its source video: play just that range, looped
      video.addEventListener("loadedmetadata", () => { video.currentTime = c.start; });
      video.ontimeupdate = () => { if (video.currentTime >= c.end || video.currentTime < c.start - 0.5) video.currentTime = c.start; };
    }
  }

  function onChange(ev) {
    const t = ev.target;
    if (t.id === "rv-proj") { R.f.project = t.value; lsSet("rvProject", t.value); }
    else if (t.id === "rv-new") { R.f.new_only = t.checked; lsSet("rvNew", t.checked ? "1" : "0"); }
    else if (t.id === "rv-min") { R.f.min_score = t.value; lsSet("rvMin", t.value); }
    else return;
    load().catch(fail);
  }
  function onClick(ev) {
    const a = ev.target.closest("[data-act]");
    if (!a) return;
    ({ keep: () => decide(1), skip: () => decide(-1), undo, studio })[a.dataset.act]?.();
  }
  function onKey(e) {
    if (e.target.closest?.("input, textarea, select") || $("#modal-root").innerHTML || e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (e.repeat && "ynu".includes(k)) return e.preventDefault(); // a held key judges one clip, not the whole queue
    const act = { y: () => decide(1), n: () => decide(-1), u: undo, e: studio, arrowright: () => move(1), arrowleft: () => move(-1),
      j: () => scrub(e.shiftKey ? -5 : -1), l: () => scrub(e.shiftKey ? 5 : 1), k: playPause, " ": playPause }[k];
    if (act) { e.preventDefault(); act(); }
  }
  function move(d) { if (!R.list.length) return; R.i = Math.min(Math.max(R.i + d, 0), R.list.length - 1); render(); }
  function playPause() { if (video) video.paused ? video.play() : video.pause(); }
  function scrub(d) {
    const c = cur(); if (!video || !c) return;
    const [a, b] = c.rendered ? [0, video.duration || 0] : [c.start, c.end];
    video.currentTime = Math.min(Math.max(video.currentTime + d, a), Math.max(a, b - 0.05));
  }
  function studio() { const c = cur(); if (c) location.hash = `#/studio/${c.id}`; }
  async function decide(rating) {
    const c = cur(); if (!c || R.busy) return;
    R.busy = true;
    try { await api(`/api/clips/${c.id}`, { method: "PATCH", body: { rating } }); } catch (e) { return fail(e); } finally { R.busy = false; }
    R.done.push({ card: c, i: R.i, prev: c.rating || 0 });
    c.rating = rating;
    if (R.f.new_only) R.list.splice(R.i, 1); // judged: out of the "new" queue
    else R.i++;
    R.i = Math.min(R.i, Math.max(0, R.list.length - 1));
    render();
  }
  async function undo() {
    if (R.busy) return;
    const h = R.done.pop();
    if (!h) return toast("Nothing to undo.");
    R.busy = true;
    try { await api(`/api/clips/${h.card.id}`, { method: "PATCH", body: { rating: h.prev } }); } catch (e) { R.done.push(h); return fail(e); } finally { R.busy = false; }
    h.card.rating = h.prev;
    if (R.f.new_only) R.list.splice(Math.min(h.i, R.list.length), 0, h.card);
    R.i = Math.min(h.i, R.list.length - 1);
    render();
  }
}

routes.long = LongForm;
Object.assign(JOB_LABEL, { longform_capture: "Capture long form", longform_export: "Render long form" });
const LF_MODES = [["condensed", "Condensed", "The whole video minus dead air: silences, idle menu/AFK screens with no speech, and “um”s."],
  ["highlights", "Highlights", "The AI picks the best moments (with the video-type playbook) up to a target length, in order, one chapter each."],
  ["clips", "From clips", "The clips you ticked in AI Clips, in the order you ticked them."]];
const LF_STAGE = { captured: ["info", "Captured"], edited: ["warn", "Edited"], rendered: ["ok", "Rendered"], uploaded: ["ok", "Uploaded"] };
const lfOffsets = (segs) => { let acc = 0; return segs.map(([a, b]) => { const o = acc; acc += b - a; return o; }); };
const lfLen = (segs) => segs.reduce((s, [a, b]) => s + b - a, 0);
const lowerBound = (xs, v) => { let lo = 0, hi = xs.length; while (lo < hi) { const m = (lo + hi) >> 1; if (xs[m] < v) lo = m + 1; else hi = m; } return lo; };
function lfSeqWords(words, starts, segs) { // mirrors longform.seq_words (words sorted by start)
  const offs = lfOffsets(segs), out = [];
  segs.forEach(([a, b], i) => {
    for (let j = lowerBound(starts, a - 30), n = lowerBound(starts, b); j < n; j++) {
      const w = words[j], m = (w.s + w.e) / 2;
      if (m >= a && m < b) out.push({ ...w, s: offs[i] + w.s - a, e: offs[i] + Math.min(w.e, b) - a });
    }
  });
  return out;
}

// Editor → "Long form": existing long forms of this project and the three capture modes.
async function openLongCapture(pid, pickedIds = []) {
  const list = await api(`/api/projects/${pid}/longform`).catch(() => []);
  modal(`<h2>Long form</h2><p class="muted" style="margin-top:-6px">A separate landscape edit of this video, with chapters, its own render and its own posts. Clips are not changed.</p>
    ${list.length ? `<div class="stack" style="gap:6px;margin-bottom:14px">${list.map((d) => `<a class="row lf-row" href="#/long/${esc(d.id)}"><b class="grow">${esc(d.title)}</b>
        <span class="dim">${fmt(d.length, 0)} · ${d.segments.length} segments</span>${pill(LF_STAGE, d.lf_stage)}</a>`).join("")}</div>` : ""}
    <h3 style="margin-bottom:8px">New long-form edit</h3>
    <div class="seg" id="lfm">${LF_MODES.map(([k, l]) => `<button data-v="${k}">${l}</button>`).join("")}</div>
    <p class="muted" id="lfm-help" style="font-size:12.5px"></p><div id="lfm-opts" class="stack"></div>
    <label class="field">Title<input type="text" id="lf-title" maxlength="100" placeholder="Automatic"></label>
    <label class="slider"><span>Transitions</span><input type="range" id="lf-tr" min="0" max="1" step="0.05" value="0"><output>off</output></label>
    <p class="dim" style="font-size:11.5px;margin:0">A short fade between separate moments (none inside a condensed run).</p>
    <div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="lf-go">Capture</button></div>`, (m, close) => {
    let mode = pickedIds.length ? "clips" : "condensed";
    const opts = () => ({
      condensed: `<label class="slider"><span>Keep padding</span><input type="range" id="lf-pad" min="0" max="1" step="0.05" value="0.25"><output>0.25 s</output></label>
        <p class="dim" style="font-size:11.5px;margin:0">Air kept around speech at each cut. Uses the silences, on-screen motion and word timings already analysed (no re-scan).</p>`,
      highlights: `<label class="field">Target length (minutes)<input type="number" id="lf-target" min="1" max="240" step="1" value="10"></label>
        ${S.settings?.anthropic_key_set ? "" : `<p class="dim" style="font-size:11.5px;margin:0">No Claude key: the offline picker chooses the moments.</p>`}`,
      clips: pickedIds.length ? `<p style="margin:0">${pickedIds.length} clip${pickedIds.length > 1 ? "s" : ""} in the order you ticked them (reorder later on the timeline).</p>`
        : `<p class="dim" style="margin:0">Tick clips in the AI Clips list first (their order is kept), then open this again.</p>` })[mode];
    const set = (v) => { mode = v; $$("#lfm button", m).forEach((b) => b.classList.toggle("on", b.dataset.v === v));
      $("#lfm-help", m).textContent = LF_MODES.find(([k]) => k === v)[2]; $("#lfm-opts", m).innerHTML = opts(); $("#lf-go", m).disabled = v === "clips" && !pickedIds.length; };
    set(mode);
    $("#lfm", m).onclick = (e) => e.target.dataset.v && set(e.target.dataset.v);
    m.oninput = (e) => { if (e.target.type === "range") e.target.nextElementSibling.textContent = e.target.id === "lf-tr" ? (+e.target.value ? `${e.target.value} s` : "off") : `${e.target.value} s`; };
    $("#lf-go", m).onclick = async () => {
      const body = { mode, title: $("#lf-title", m).value.trim(), transition: +$("#lf-tr", m).value };
      if (mode === "condensed") body.pad = +$("#lf-pad", m).value;
      if (mode === "highlights") body.target = Math.max(1, +$("#lf-target", m).value || 10) * 60;
      if (mode === "clips") body.clip_ids = pickedIds;
      try { const d = await api(`/api/projects/${pid}/longform`, { body }); close(); location.hash = `#/long/${d.id}`; } catch (err) { fail(err); }
    };
  });
}

function LongForm(lid) {
  const L = { d: null, proj: null, words: [], starts: [], seq: [], offs: [], sel: 0, cur: 0, t: 0, tab: "segments", zoom: 1, plan: null, picked: new Set(), assets: null, akind: "", tsk: "caption",
    hist: RWUndo.History(100, 1000), srv: null }; // srv = the doc as the server has it (trim drags change segments in place)
  let video, raf, keyH, saveT, drag = null, sig = "";
  const segs = () => L.d.segments, total = () => lfLen(segs()), locked = () => !!L.d.locked;
  const pid = () => L.d.project_id, dur = () => L.proj?.meta?.duration || 0;
  const reindex = () => { L.offs = lfOffsets(segs()); L.seq = lfSeqWords(L.words, L.starts, segs()); L.sel = Math.max(0, Math.min(L.sel, segs().length - 1)); L._ck = null; };
  const segAt = (t) => Math.max(0, lowerBound(L.offs, t + 1e-6) - 1);

  this.mount = async () => {
    if (!lid) return listPage();
    L.d = await api(`/api/longform/${lid}`);
    L.srv = structuredClone(L.d);
    L.proj = await api(`/api/projects/${L.d.project_id}`);
    const tr = await fetch(`/files/${L.d.project_id}/transcript.json`).then((r) => (r.ok ? r.json() : { segments: [] })).catch(() => ({ segments: [] }));
    L.words = (tr.segments || []).flatMap((s) => s.words || []).sort((a, b) => a.s - b.s);
    L.starts = L.words.map((w) => w.s);
    reindex();
    layout();
    keyH = (e) => {
      if (e.target.closest("input, textarea, select, [contenteditable]") || $("#modal-root").innerHTML) return;
      const z = (e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase();
      if (z === "z" || z === "y") { e.preventDefault(); undoRedo(z === "y" || e.shiftKey); }
      else if (e.code === "Space") { e.preventDefault(); toggle(); }
    };
    document.addEventListener("keydown", keyH);
  };
  this.destroy = () => { cancelAnimationFrame(raf); video?.pause(); if (keyH) document.removeEventListener("keydown", keyH); };
  this.refresh = async () => {
    if (!L.d) return;
    const d = await api(`/api/longform/${lid}`);
    const was = L.d.status;
    L.d = { ...d, segments: drag ? L.d.segments : d.segments };
    if (was !== d.status) { reindex(); return layout(); }
    const pb = $("#ovl .bar div"); if (pb) pb.style.width = Math.round((d.job?.progress || 0) * 100) + "%";
    const s = JSON.stringify([d.export, d.job, d.uploads, d.locked, d.lf_stage]);
    if (s !== sig) { sig = s; renderTop(); if (L.tab === "publish") renderTab(); }
  };

  async function listPage() {
    const all = await api("/api/longform");
    view.innerHTML = `<div class="page"><h1>Long form</h1><p class="sub">Separate landscape edits of whole videos: condensed cuts, highlight reels and clip compilations. Start one from a project's Editor (“▬ Long form”).</p>
      ${all.length ? `<div class="stack">${all.map((d) => `<a class="card row lf-row" href="#/long/${esc(d.id)}">${d.export?.cover ? `<img src="/files/${esc(d.project_id)}/${esc(d.export.cover)}" alt="" style="height:54px;border-radius:6px">` : ""}
          <div class="grow"><b>${esc(d.title)}</b><div class="dim" style="font-size:12px">${esc(d.project_name || "")} · ${esc(LF_MODES.find(([k]) => k === d.mode)?.[1] || d.mode)} · ${fmt(d.length, 0)}</div></div>
          ${pill(LF_STAGE, d.lf_stage)}</a>`).join("")}</div>` : `<div class="card empty"><b>No long-form edits yet</b>Open a project in the Editor and choose “▬ Long form”.</div>`}</div>`;
  }

  // ----- save -----
  function save(body, delay = 0, record = true) {
    if (locked() && Object.keys(body).some((k) => k !== "title")) return toast("Already posted — make a new version to edit it.", "err");
    clearTimeout(saveT);
    L.pendingBody = { ...(L.pendingBody || {}), ...body, ...(body.edit ? { edit: { ...(L.pendingBody?.edit || {}), ...body.edit } } : {}) };
    L.pendingRec = (L.pendingRec ?? true) && record;
    saveT = setTimeout(async () => {
      const b = L.pendingBody, undo = L.pendingRec && L.srv ? RWUndo.inverse(b, L.srv) : null; L.pendingBody = L.pendingRec = null;
      try {
        const d = await api(`/api/longform/${lid}`, { method: "PATCH", body: b }); L.srv = structuredClone(d);
        if (undo) L.hist.push(lid, undo, b);
        L.d = { ...d, segments: drag ? L.d.segments : d.segments }; reindex(); renderTop(); paintTimeline(); if (!b.edit || b.edit.overlays) renderTab();
      }
      catch (err) { fail(err); L.d = await api(`/api/longform/${lid}`); L.srv = structuredClone(L.d); reindex(); renderTab(); paintTimeline(); }
    }, delay);
  }
  async function undoRedo(redo) {
    if (locked()) return toast("Already posted — make a new version to edit it.", "err");
    if (L.pendingBody) { // an edit still on its debounce: save (and record) it first, so it stays undoable
      clearTimeout(saveT);
      const p = L.pendingBody, undo = L.pendingRec !== false && L.srv ? RWUndo.inverse(p, L.srv) : null; L.pendingBody = L.pendingRec = null;
      try { L.srv = structuredClone(await api(`/api/longform/${lid}`, { method: "PATCH", body: p })); if (undo) L.hist.push(lid, undo, p); } catch (e) { return fail(e); }
    }
    const b = redo ? L.hist.redo(lid) : L.hist.undo(lid);
    if (!b) return toast(redo ? "Nothing to redo." : "Nothing to undo.");
    L.d = { ...L.d, ...b, edit: { ...L.d.edit, ...(b.edit || {}) } }; reindex(); applyPreview(); paintTimeline(); renderTab();
    save(b, 0, false);
  }
  const setSegs = (next, delay = 0) => { L.d.segments = next; reindex(); paintTimeline(); renderTab(); save({ segments: next }, delay); };
  const setEdit = (patch, delay = 250) => { L.d.edit = { ...L.d.edit, ...patch }; applyPreview(); save({ edit: patch }, delay); };

  // ----- layout -----
  function layout() {
    const d = L.d, p = L.proj, capturing = d.status === "capturing";
    view.innerHTML = `<div class="editor lf">
      <div class="ed-top">
        <a class="btn ghost sm" href="#/editor/${esc(pid())}">← Editor</a>
        <div class="title" title="${esc(d.title)} — click to rename" data-act="rename" style="cursor:text">${esc(d.title)}</div>
        <span class="chip">Long form · ${esc(LF_MODES.find(([k]) => k === d.mode)?.[1] || d.mode)}</span>
        <span id="lf-st"></span><div class="grow"></div><div id="lf-top" class="row"></div>
      </div>
      <div class="stage">
        <div class="player">${p.source ? `<div class="vwrap lf-frame" id="lf-frame"><video id="vid" src="/files/${esc(pid())}/${esc(p.proxy || p.source)}" preload="auto" playsinline></video>
            <div class="title-preview" id="tprev" hidden></div><div class="sticker-preview" id="sprev" hidden></div><div class="cap-preview" id="cprev"></div></div>` : ""}
          <div class="overlay-msg" id="ovl" ${capturing || d.status === "failed" ? "" : "hidden"}>${d.status === "failed"
            ? `<b style="color:var(--err)">Capture failed</b><div class="muted" style="max-width:520px">${esc(d.error)}</div>`
            : `<div class="spin" style="width:28px;height:28px;color:var(--accent)"></div><b>Capturing from the analysis…</b><div style="width:min(320px,70vw)">${bar(d.job?.progress)}</div>`}</div></div>
        <div class="transport">
          <button class="btn sm" data-act="play" title="Space">▶</button>
          <span class="tc" id="tc">0:00.0 / ${fmt(total())}</span>
          <span class="dim" style="font-size:12px" id="lf-segno"></span><div class="grow"></div>
          <button class="btn xs" data-act="split" title="Split the segment under the playhead">✂ Split</button>
          <button class="btn xs" data-act="chapter-here" title="Add a chapter at the playhead">＋ Chapter</button>
        </div>
        <div class="timeline"><div class="tl-scroll" id="lf-scroll"><div class="lf-track" id="lf-track"></div></div>
          <div class="tl-foot"><span>Zoom</span><input type="range" id="lf-zoom" min="1" max="60" step="0.5" value="${L.zoom}" style="width:min(160px,30vw)">
            <span class="grow"></span><span>Drag a segment to reorder · drag its edges to trim · click to seek · <span class="kbd">Space</span> play</span></div></div>
      </div>
      <div class="panel"><div class="tabs">${[["segments", "Segments"], ["chapters", "Chapters"], ["style", "Style"], ["overlays", "Overlays"], ["ai", "AI"], ["publish", "Render & post"]]
        .map(([k, l]) => `<button data-tab="${k}" class="${L.tab === k ? "on" : ""}">${l}</button>`).join("")}</div><div class="tab-body" id="tab"></div></div></div>`;
    video = $("#vid");
    if (video) {
      video.addEventListener("loadedmetadata", () => { applyPreview(); seekSeq(L.t); });
      video.addEventListener("play", () => { $("[data-act=play]").textContent = "❚❚"; L.cur = segAt(L.t); if (!inSeg()) seekSeq(L.t); tick(); });
      video.addEventListener("pause", () => { $("[data-act=play]").textContent = "▶"; });
      video.addEventListener("seeked", () => { L.seeking = false; paint(); });
      new ResizeObserver(() => { applyPreview(); paintTimeline(); }).observe($(".player"));
    }
    $("#lf-zoom").oninput = (e) => { L.zoom = +e.target.value; paintTimeline(); };
    $(".tabs").onclick = (e) => { const t = e.target.closest("[data-tab]"); if (t) { L.tab = t.dataset.tab; $$(".tabs button").forEach((b) => b.classList.toggle("on", b === t)); renderTab(); } };
    view.onclick = onClick;
    bindTimeline();
    renderTop(); renderTab(); paintTimeline();
  }

  function renderTop() {
    const d = L.d, ex = d.export || {}, el = $("#lf-top");
    if (!el) return;
    $("#lf-st").innerHTML = pill(LF_STAGE, d.lf_stage);
    const rendering = ex.status === "rendering" || d.job?.kind === "longform_export";
    el.innerHTML = d.locked ? `<span class="dim" style="font-size:12px">🔒 Posted — locked</span><button class="btn sm" data-act="version">Make a new version</button>`
      : `${rendering ? `<span class="row" style="gap:6px;font-size:12px"><span class="spin"></span> Rendering ${Math.round((ex.progress || 0) * 100)}%</span>`
        : d.current ? `<a class="btn sm" href="/files/${esc(pid())}/${esc(ex.file)}?r=${+d.rev}" target="_blank">▶ Watch render</a>`
          : `<button class="btn sm" data-act="render" ${d.status === "ready" ? "" : "disabled"}>⬇ Render</button>`}
        ${ex.status === "failed" ? `<span style="color:var(--err);font-size:12px" title="${esc(ex.error)}">Render failed</span>` : ""}
        <button class="btn xs ghost" data-act="undo" title="Undo (Ctrl+Z)">↶</button><button class="btn xs ghost" data-act="redo" title="Redo (Ctrl+Y or Ctrl+Shift+Z)">↷</button>
        <button class="btn sm primary" data-act="publish" ${d.status === "ready" ? "" : "disabled"}>⇪ Publish</button>`;
    el.insertAdjacentHTML("afterbegin", nleLinks(`/api/longform/${esc(lid)}`));
  }

  // ----- preview: output frame shape, captions/title/sticker -----
  function applyPreview() {
    const fr = $("#lf-frame"), e = L.d.edit;
    if (!fr || !video?.videoWidth) return;
    const [OW, OH] = SIZES[e.aspect] || [video.videoWidth, video.videoHeight];
    const box = $(".player"), k = Math.min(box.clientWidth / OW, box.clientHeight / OH) * Math.min(1, S.ui.preview || 1);
    Object.assign(fr.style, { width: OW * k + "px", height: OH * k + "px", overflow: "hidden" });
    Object.assign(video.style, { width: "100%", height: "100%", maxWidth: "none", maxHeight: "none", objectFit: e.aspect === "source" || e.fit === "blur" ? "contain" : "cover",
      objectPosition: `${e.crop_x * 100}% ${e.crop_y * 100}%`, filter: `brightness(${1 + e.brightness}) contrast(${e.contrast}) saturate(${e.saturation})`, background: "#000" });
    const scale = (OH * k) / OH, ts = (kind) => tsOf((x) => e[`${kind}_${x}`], tsDefaults(kind, e, OW, OH));
    const cp = $("#cprev"); styleText(cp, ts("caption"), scale);
    cp.style.bottom = CAPTION_POS[e.caption_pos || "low"][OH > OW ? 0 : 1] * OH * scale + "px";
    cp.style.background = e.caption_style === "boxed" ? "#0009" : "";
    const tp = $("#tprev"); styleText(tp, ts("title"), scale); tp.textContent = e.title_text || "";
    const sp = $("#sprev"); styleText(sp, ts("sticker"), scale); sp.textContent = e.sticker_text || ""; sp.hidden = !e.sticker_text;
    if (e.sticker_text) Object.assign(sp.style, { background: S.settings?.caption_color || "#FFD400", top: e.sticker_pos.startsWith("top") ? "5%" : "", bottom: e.sticker_pos.startsWith("bottom") ? "5%" : "",
      left: e.sticker_pos.endsWith("left") ? "4%" : e.sticker_pos.endsWith("center") ? "50%" : "", right: e.sticker_pos.endsWith("right") ? "4%" : "", transform: e.sticker_pos.endsWith("center") ? "translateX(-50%)" : "" });
    paint();
  }
  function paintCaption() {
    const el = $("#cprev"), e = L.d.edit;
    if (!el) return;
    const tp = $("#tprev"); if (tp) tp.hidden = !(e.title_text && L.t < (L.offs[1] ?? total())); // the title card rides on the first part
    if (!e.captions) return (el.innerHTML = "");
    const OW = (SIZES[e.aspect] || [16, 9])[0], OH = (SIZES[e.aspect] || [16, 9])[1];
    const key = L.d.rev + ":" + e.aspect;
    if (L._ck !== key) { L._ck = key; L._chunks = chunkWords(L.seq, OH > OW); }
    const t = L.t, ch = L._chunks.find((c) => t >= c[0].s && t <= c.at(-1).e + 0.15);
    const upper = S.settings?.caption_uppercase !== false, color = S.settings?.caption_color || "#FFD400";
    el.innerHTML = ch ? ch.map((w, i) => { const x = esc(upper ? w.w.trim().toUpperCase() : w.w.trim()); return t >= w.s && (i === ch.length - 1 || t < ch[i + 1].s) ? `<em style="color:${esc(color)}">${x}</em>` : x; }).join(" ") : "";
  }

  // ----- sequence playback: one <video> of the source, jumping from segment to segment -----
  const inSeg = () => { const s = segs()[L.cur]; return s && video.currentTime >= s[0] - 0.05 && video.currentTime < s[1]; };
  function seekSeq(t) {
    if (!segs().length || !video) return;
    L.t = Math.min(Math.max(t, 0), Math.max(total() - 0.01, 0));
    L.cur = segAt(L.t);
    L.seeking = true;
    video.currentTime = segs()[L.cur][0] + L.t - L.offs[L.cur];
    paint();
  }
  function tick() {
    cancelAnimationFrame(raf);
    if (!video || video.paused) return;
    const s = segs()[L.cur];
    if (s && !L.seeking) {
      if (video.currentTime >= s[1] - 0.03) {
        if (L.cur + 1 < segs().length) { const n = segs()[++L.cur]; if (Math.abs(n[0] - video.currentTime) > 0.08) { L.seeking = true; video.currentTime = n[0]; } }
        else { video.pause(); L.t = total(); }
      }
      if (!L.seeking) L.t = L.offs[L.cur] + Math.max(0, video.currentTime - segs()[L.cur][0]);
    }
    paint();
    raf = requestAnimationFrame(tick);
  }
  const toggle = () => { if (!video) return; if (video.paused) { if (L.t >= total() - 0.05) seekSeq(0); video.play(); } else video.pause(); };
  function paint() {
    const tc = $("#tc"); if (tc) tc.textContent = `${fmt(L.t)} / ${fmt(total())}`;
    const no = $("#lf-segno"); if (no && segs().length) no.textContent = `segment ${L.cur + 1} of ${segs().length} · source ${fmt(segs()[L.cur][0] + L.t - L.offs[L.cur])}`;
    const ph = $("#lf-ph"); if (ph) ph.style.left = L.t * L.pps + "px";
    paintCaption();
  }

  // ----- timeline: blocks sized by length; drag to reorder, edges to trim, click to seek -----
  function paintTimeline() {
    const tr = $("#lf-track"), sc = $("#lf-scroll");
    if (!tr || !sc) return;
    const width = Math.max(sc.clientWidth - 2, 200) * L.zoom;
    if (!drag) L.pps = width / Math.max(total(), 0.1);
    const pps = L.pps, sel = L.sel;
    tr.style.width = Math.max(width, total() * pps) + "px";
    tr.innerHTML = segs().map(([a, b], i) => `<div class="lf-seg${i === sel ? " sel" : ""}${i % 2 ? " alt" : ""}" data-i="${i}" style="left:${L.offs[i] * pps}px;width:${Math.max((b - a) * pps, 2)}px"
        title="${i + 1}: ${fmt(a)} → ${fmt(b)} (${(b - a).toFixed(1)} s)">${(b - a) * pps > 46 ? `<span>${i + 1}</span>` : ""}${locked() ? "" : `<i class="lf-h" data-side="l"></i><i class="lf-h" data-side="r"></i>`}</div>`).join("")
      + (L.d.chapters || []).map((c, i) => `<div class="lf-chm" data-ch="${i}" style="left:${c.t * pps}px" title="${esc(fmt(c.t, 0))} ${esc(c.title)}"><span>${esc(c.title)}</span></div>`).join("")
      + `<div class="lf-ph" id="lf-ph" style="left:${L.t * pps}px"></div><div class="lf-ins" id="lf-ins" hidden></div>`;
  }
  function bindTimeline() {
    const tr = $("#lf-track");
    if (!tr) return;
    const x = (ev) => localXY(ev, tr)[0];
    tr.onpointerdown = (ev) => {
      if (ev.button !== 0) return;
      const blk = ev.target.closest(".lf-seg"), h = ev.target.closest(".lf-h"), chm = ev.target.closest(".lf-chm");
      if (chm) return seekSeq(L.d.chapters[+chm.dataset.ch].t);
      const i = blk ? +blk.dataset.i : -1;
      if (h && !locked()) drag = { kind: "trim", i, side: h.dataset.side, x0: x(ev), orig: [...segs()[i]] };
      else if (blk && !locked()) drag = { kind: "move", i, x0: x(ev), moved: false };
      else { drag = { kind: "scrub" }; seekSeq(x(ev) / L.pps); }
      tr.setPointerCapture(ev.pointerId);
      ev.preventDefault();
    };
    tr.onpointermove = (ev) => {
      if (!drag) return;
      const px = x(ev);
      if (drag.kind === "scrub") return seekSeq(px / L.pps);
      if (drag.kind === "trim") {
        const s = segs()[drag.i], dt = (px - drag.x0) / L.pps;
        if (drag.side === "l") s[0] = +Math.min(Math.max(drag.orig[0] + dt, 0), s[1] - 0.1).toFixed(3);
        else s[1] = +Math.min(Math.max(drag.orig[1] + dt, s[0] + 0.1), dur()).toFixed(3);
        reindex(); paintTimeline();
        if (video) { video.pause(); video.currentTime = drag.side === "l" ? s[0] : Math.max(s[1] - 0.05, s[0]); }
        return;
      }
      if (Math.abs(px - drag.x0) > 5) drag.moved = true;
      if (!drag.moved) return;
      drag.to = lowerBound(L.offs.map((o, j) => o + (segs()[j][1] - segs()[j][0]) / 2), px / L.pps); // insert before the first block whose middle is right of the pointer
      const ins = $("#lf-ins"); ins.hidden = false; ins.style.left = (drag.to < segs().length ? L.offs[drag.to] : total()) * L.pps + "px";
    };
    tr.onpointerup = () => {
      const g = drag; drag = null;
      if (!g || g.kind === "scrub") return;
      if (g.kind === "trim") { L.sel = g.i; return setSegs(segs().map((s) => [...s])); }
      if (!g.moved) { L.sel = g.i; paintTimeline(); renderTab(); return seekSeq(L.offs[g.i]); }
      const next = segs().map((s) => [...s]), [m] = next.splice(g.i, 1), to = g.to > g.i ? g.to - 1 : g.to;
      next.splice(to, 0, m);
      L.sel = to;
      if (to !== g.i) setSegs(next); else paintTimeline();
    };
    tr.onpointercancel = () => { drag = null; paintTimeline(); };
  }

  // ----- actions -----
  async function onClick(ev) {
    const a = ev.target.closest("[data-act]");
    if (!a) return;
    const act = a.dataset.act, i = +a.dataset.i;
    try {
      switch (act) {
        case "play": toggle(); break;
        case "rename": { const t = prompt("Long-form title", L.d.title); if (t && t.trim()) { save({ title: t.trim() }); $(".ed-top .title").textContent = t.trim(); } break; }
        case "split": {
          const s = segs()[L.cur], at = s[0] + L.t - L.offs[L.cur];
          if (at - s[0] < 0.2 || s[1] - at < 0.2) return toast("Move the playhead inside a segment, away from its edges.");
          const next = segs().map((x) => [...x]); next.splice(L.cur, 1, [s[0], +at.toFixed(3)], [+at.toFixed(3), s[1]]); L.sel = L.cur + 1; setSegs(next); break;
        }
        case "seg-go": L.sel = i; seekSeq(L.offs[i]); paintTimeline(); renderTab(); break;
        case "seg-up": case "seg-down": { const j = act === "seg-up" ? i - 1 : i + 1; if (j < 0 || j >= segs().length) break; const next = segs().map((x) => [...x]); [next[i], next[j]] = [next[j], next[i]]; L.sel = j; setSegs(next); break; }
        case "seg-del": if (segs().length < 2) return toast("A long form needs at least one segment."); L.sel = Math.max(0, i - 1); setSegs(segs().filter((_, j) => j !== i)); break;
        case "chapter-here": { const ch = [...(L.d.chapters || []), { t: +L.t.toFixed(2), title: `Chapter ${(L.d.chapters || []).length + 1}` }].sort((x, y) => x.t - y.t); L.d.chapters = ch; save({ chapters: ch }); L.tab = "chapters"; $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === "chapters")); renderTab(); paintTimeline(); break; }
        case "ch-go": seekSeq(L.d.chapters[i].t); break;
        case "ch-del": L.d.chapters = L.d.chapters.filter((_, j) => j !== i); save({ chapters: L.d.chapters }); renderTab(); paintTimeline(); break;
        case "ch-auto": a.disabled = true; a.innerHTML = `<span class="spin"></span> Writing…`; L.d = await api(`/api/longform/${lid}/chapters/auto`, { method: "POST" }); renderTab(); paintTimeline(); break;
        case "set": { const v = a.dataset.v; setEdit({ [a.dataset.k]: v === "" || isNaN(+v) ? v : +v }, 0); renderTab(); break; }
        case "layout": setEdit({ layout: a.dataset.v }, 0); renderTab(); break;
        case "tsk": L.tsk = a.dataset.v; renderTab(); break;
        case "music": $("#lf-musicf").click(); break;
        case "nomusic": setEdit({ music: "" }, 0); renderTab(); break;
        case "ov-text": case "ov-emoji": {
          const text = $(act === "ov-text" ? "#lf-ovt" : "#lf-ove").value.trim();
          if (!text) return toast("Type the text first.");
          addOverlay({ kind: act === "ov-text" ? "text" : "emoji", text, d: 2.5, x: 0.5, y: act === "ov-text" ? 0.2 : 0.3, w: act === "ov-text" ? 0.5 : 0.12, anim: "pop" }); break;
        }
        case "ov-asset": { const as = L.assets.find((x) => x.id === a.dataset.id); addOverlay({ kind: as.kind, asset_id: as.id, x: 0.8, y: 0.25, w: 0.25, anim: as.kind === "sound" ? "none" : "pop" }); break; }
        case "ov-go": seekSeq(+L.d.edit.overlays[i].t); break;
        case "ov-del": setEdit({ overlays: (L.d.edit.overlays || []).filter((_, j) => j !== i) }, 0); renderTab(); break;
        case "akind": L.akind = a.dataset.v; renderTab(); break;
        case "plan": {
          a.disabled = true; a.innerHTML = `<span class="spin"></span> Planning…`;
          L.plan = await api(`/api/longform/${lid}/plan`, { body: { prompt: $("#lf-ask").value } });
          L.picked = new Set(L.plan.items.map((x) => x.id)); renderTab(); break;
        }
        case "plan-apply": L.d = await api(`/api/longform/${lid}/apply`, { body: { plan_id: L.plan.plan_id, item_ids: [...L.picked] } }); L.plan = null; reindex(); applyPreview(); renderTab(); toast("Applied.", "ok"); break;
        case "render": await api(`/api/longform/${lid}/export`, { method: "POST" }); L.d.export = { status: "rendering", progress: 0, rev: L.d.rev }; renderTop(); toast("Rendering the long form — it keeps going if you leave this page.", "ok"); break;
        case "publish": openPublish({ id: lid, title: L.d.title, edit: L.d.edit, hook: L.d.chapters_text || "", hashtags: [] }, () => this_refresh(), L.d); break;
        case "version": { const v = await api(`/api/longform/${lid}/version`, { method: "POST" }); location.hash = `#/long/${v.id}`; break; }
        case "undo": case "redo": undoRedo(act === "redo"); break;
      }
    } catch (err) { fail(err); renderTab(); }
  }
  const this_refresh = () => api(`/api/longform/${lid}`).then((d) => { L.d = d; renderTop(); renderTab(); }).catch(() => {});
  function addOverlay(o) {
    if (L.t >= total() - 0.1) return toast("Move the playhead inside the video first.");
    setEdit({ overlays: [...(L.d.edit.overlays || []), { ...o, t: +L.t.toFixed(2) }] }, 0);
    toast(`Added at ${fmt(L.t)}.`, "ok", 1500);
  }

  // ----- side panel -----
  function renderTab() {
    const el = $("#tab");
    if (!el || !L.d) return;
    el.oninput = el.onchange = null;
    const e = L.d.edit, ro = locked() ? "disabled" : "";
    const seg = (k, cur, opts) => `<div class="seg">${opts.map(([v, l]) => `<button class="${String(cur) === String(v) ? "on" : ""}" data-act="set" data-k="${k}" data-v="${v}" ${ro}>${l}</button>`).join("")}</div>`;
    const sw = (k, l) => `<label class="switch"><input type="checkbox" data-k="${k}" ${e[k] ? "checked" : ""} ${ro}> ${l}</label>`;
    const rng = (k, l, min, max, step) => `<label class="slider"><span>${l}</span><input type="range" data-k="${k}" min="${min}" max="${max}" step="${step}" value="${+e[k]}" ${ro}><output>${(+e[k]).toFixed(2)}</output></label>`;
    if (L.tab === "segments") {
      const shown = segs().length > 400 ? segs().slice(Math.max(0, L.sel - 150), L.sel + 250) : segs(), first = segs().indexOf(shown[0]);
      el.innerHTML = `<div class="row" style="margin-bottom:8px;font-size:12.5px"><b class="grow">${segs().length} segments · ${fmt(total(), 0)}</b><span class="dim">from ${fmt(dur(), 0)} of source</span></div>
        ${segs().length > shown.length ? `<p class="dim" style="font-size:11.5px">Showing ${shown.length} around the selected one (click the timeline to move).</p>` : ""}
        <div class="stack lf-list" style="gap:4px">${shown.map(([a, b], k) => { const j = first + k; return `<div class="lf-item${j === L.sel ? " sel" : ""}">
          <button class="btn xs ghost" data-act="seg-go" data-i="${j}" title="Play from here">${j + 1}</button>
          <input type="text" class="mono" data-seg="${j}" data-end="0" value="${fmt(a, 2)}" ${ro} aria-label="Start of segment ${j + 1}"><span class="dim">→</span>
          <input type="text" class="mono" data-seg="${j}" data-end="1" value="${fmt(b, 2)}" ${ro} aria-label="End of segment ${j + 1}"><span class="dim grow">${(b - a).toFixed(1)}s</span>
          <button class="btn xs ghost" data-act="seg-up" data-i="${j}" ${ro || (j ? "" : "disabled")} title="Earlier">▲</button><button class="btn xs ghost" data-act="seg-down" data-i="${j}" ${ro || (j < segs().length - 1 ? "" : "disabled")} title="Later">▼</button>
          <button class="btn xs ghost danger" data-act="seg-del" data-i="${j}" ${ro} title="Remove">✕</button></div>`; }).join("")}</div>`;
      el.onchange = (ev) => { const t = ev.target; if (t.dataset.seg == null) return; const j = +t.dataset.seg, v = parseTime(t.value);
        if (!isFinite(v)) return renderTab(); const next = segs().map((s) => [...s]); next[j][+t.dataset.end] = +Math.min(Math.max(v, 0), dur()).toFixed(3);
        if (next[j][1] - next[j][0] < 0.1) { toast("A segment must be at least 0.1 s long."); return renderTab(); } L.sel = j; setSegs(next); };
    } else if (L.tab === "chapters") {
      const ch = L.d.chapters || [];
      el.innerHTML = `<p class="muted" style="font-size:12.5px;margin-top:0">Chapters go into the YouTube description. YouTube needs the first at 0:00, at least 3, each at least 10 s.</p>
        <div class="stack" style="gap:4px">${ch.map((c, j) => `<div class="lf-item"><button class="btn xs ghost" data-act="ch-go" data-i="${j}">▶</button>
          <input type="text" class="mono" style="width:76px" data-ch="${j}" data-f="t" value="${fmt(c.t, 0)}" ${ro}><input type="text" class="grow" data-ch="${j}" data-f="title" value="${esc(c.title)}" maxlength="100" ${ro}>
          <button class="btn xs ghost danger" data-act="ch-del" data-i="${j}" ${ro}>✕</button></div>`).join("") || `<div class="dim">No chapters yet.</div>`}</div>
        <div class="row wrap" style="margin-top:10px"><button class="btn sm" data-act="chapter-here" ${ro}>＋ At playhead</button><button class="btn sm" data-act="ch-auto" ${ro}>✨ Auto chapters</button></div>
        <div class="sec"><h3>YouTube chapters</h3>${L.d.chapters_text ? `<pre class="mono lf-pre">${esc(L.d.chapters_text)}</pre>` : `<p class="dim" style="margin:0">Not valid for YouTube yet (needs 3+ chapters, 10 s+ each).</p>`}</div>`;
      el.onchange = (ev) => { const t = ev.target; if (t.dataset.ch == null) return; const next = ch.map((c) => ({ ...c }));
        next[+t.dataset.ch][t.dataset.f] = t.dataset.f === "t" ? Math.min(Math.max(parseTime(t.value) || 0, 0), total()) : t.value;
        L.d.chapters = next.sort((x, y) => x.t - y.t); save({ chapters: L.d.chapters }); paintTimeline(); };
    } else if (L.tab === "style") {
      el.innerHTML = `<div class="stack">
        <div><h3>Frame</h3>${seg("aspect", e.aspect, [["16:9", "16:9"], ["source", "Source"], ["9:16", "9:16"], ["1:1", "1:1"], ["4:5", "4:5"]])}</div>
        ${seg("fit", e.fit, [["crop", "Crop"], ["blur", "Blurred fit"]])}
        <details ${isMulti(e) ? "open" : ""}><summary>Layout</summary><div class="lay-gal" style="margin-top:8px">${LAYOUTS.map(([v, l]) => `<button class="lay-card${(e.layout || "single") === v ? " on" : ""}" data-act="layout" data-v="${v}" title="${l}" ${ro}>${layoutThumb(v)}<span>${l}</span></button>`).join("")}</div>
          <p class="dim" style="font-size:11.5px">Regions come from your saved default layout; fine-tune regions in a clip's Studio and “Save as default”.</p></details>
        <div class="sec"><h3>Captions</h3>${sw("captions", "Animated captions")}${seg("caption_style", e.caption_style, [["bold", "Bold"], ["clean", "Clean"], ["boxed", "Boxed"], ["neon", "Neon"]])}
          ${seg("caption_pos", e.caption_pos, [["low", "Low"], ["middle", "Middle"], ["high", "High"]])}
          <label class="field">Title card (first part)<input type="text" data-k="title_text" maxlength="120" value="${esc(e.title_text)}" ${ro}></label>
          <label class="field">Sticker (whole video)<input type="text" data-k="sticker_text" maxlength="120" value="${esc(e.sticker_text)}" ${ro}></label>
          <div class="seg">${[["caption", "Captions"], ["title", "Title card"], ["sticker", "Sticker"]].map(([v, l]) => `<button class="${L.tsk === v ? "on" : ""}" data-act="tsk" data-v="${v}">${l}</button>`).join("")}</div>
          <div id="lf-ts"></div></div>
        <div class="sec"><h3>Cuts &amp; sound</h3>${sw("remove_fillers", "Also cut “um” / “uh”")}${sw("remove_silence", "Jump cuts inside segments")}${sw("loudnorm", "Loudness normalise (−14 LUFS)")}${sw("denoise", "Reduce noise")}
          <label class="slider"><span>Transitions</span><input type="range" data-tr="1" min="0" max="1" step="0.05" value="${+L.d.transition || 0}" ${ro}><output>${+L.d.transition ? L.d.transition + " s" : "off"}</output></label>
          <div class="row wrap"><button class="btn sm" data-act="music" ${ro}>♫ ${e.music ? "Replace music" : "Add music"}</button>${e.music ? `<span class="dim grow">${esc(e.music.replace(/^music\//, ""))}</span><button class="btn xs ghost" data-act="nomusic" ${ro}>Remove</button>` : ""}
            <input type="file" id="lf-musicf" accept=".mp3,.wav,.m4a,.aac,.ogg,.flac" hidden></div>
          ${e.music ? rng("music_volume", "Music level", 0, 1, 0.01) : ""}</div>
        <div class="sec"><h3>Look</h3>${rng("brightness", "Brightness", -0.5, 0.5, 0.01)}${rng("contrast", "Contrast", 0.5, 2, 0.01)}${rng("saturation", "Saturation", 0, 2, 0.01)}${sw("sharpen", "Sharpen")}</div></div>`;
      const tk = L.tsk, OW = (SIZES[e.aspect] || [1920, 1080])[0], OH = (SIZES[e.aspect] || [1920, 1080])[1];
      if (!locked()) textStyleEditor($("#lf-ts"), { get: (k) => L.d.edit[`${tk}_${k}`], set: (k, v) => setEdit({ [`${tk}_${k}`]: v }, 400), def: tsDefaults(tk, e, OW, OH), sample: tk === "caption" ? "THIS IS THE CAPTION" : tk === "title" ? (e.title_text || "Title card") : (e.sticker_text || "@handle") });
      el.oninput = (ev) => { const t = ev.target;
        if (t.dataset.tr) { t.nextElementSibling.textContent = +t.value ? `${t.value} s` : "off"; L.d.transition = +t.value; return save({ transition: +t.value }, 400); }
        if (!t.dataset.k || t.type === "checkbox") return;
        if (t.type === "range") { t.nextElementSibling.textContent = (+t.value).toFixed(2); setEdit({ [t.dataset.k]: +t.value }, 300); }
        else setEdit({ [t.dataset.k]: t.value }, 600); };
      el.onchange = async (ev) => { const t = ev.target;
        if (t.type === "checkbox" && t.dataset.k) setEdit({ [t.dataset.k]: t.checked }, 0);
        if (t.id === "lf-musicf" && t.files[0]) { const fd = new FormData(); fd.append("file", t.files[0]);
          try { L.d = await api(`/api/longform/${lid}/music`, { body: fd }); toast("Music added.", "ok"); renderTab(); } catch (err) { fail(err); } } };
    } else if (L.tab === "overlays") {
      if (!L.assets) { L.assets = []; api("/api/assets").then((x) => { L.assets = x; if (L.tab === "overlays") renderTab(); }).catch(fail); }
      const ovs = e.overlays || [], lib = L.assets.filter((x) => ["image", "gif", "video", "sound"].includes(x.kind) && (!L.akind || x.kind === L.akind));
      el.innerHTML = `<p class="muted" style="font-size:12.5px;margin-top:0">Added at the playhead (${fmt(L.t)}). Times are in the long-form video.</p>
        <div class="row"><input type="text" id="lf-ovt" class="grow" maxlength="80" placeholder="Text pop-up" ${ro}><button class="btn sm" data-act="ov-text" ${ro}>＋ Text</button></div>
        <div class="row" style="margin-top:6px"><input type="text" id="lf-ove" class="grow" maxlength="8" placeholder="Emoji 😂" ${ro}><button class="btn sm" data-act="ov-emoji" ${ro}>＋ Emoji</button></div>
        <div class="sec"><h3>Memes, GIFs &amp; sound effects</h3><div class="seg">${[["", "All"], ["image", "Images"], ["gif", "GIFs"], ["video", "Video"], ["sound", "Sounds"]].map(([v, l]) => `<button class="${L.akind === v ? "on" : ""}" data-act="akind" data-v="${v}">${l}</button>`).join("")}</div>
          <div class="stack lf-lib" style="gap:4px">${lib.slice(0, 120).map((x) => `<button class="btn xs ghost lf-asset" data-act="ov-asset" data-id="${esc(x.id)}" ${ro}>${{ image: "🖼", gif: "✨", video: "🎞", sound: "🔊" }[x.kind] || ""} ${esc(x.name)}</button>`).join("") || `<span class="dim">Nothing in the library yet — add files in a clip's Studio.</span>`}</div></div>
        <div class="sec"><h3>On this video (${ovs.length})</h3>${ovs.map((o, j) => `<div class="lf-item"><button class="btn xs ghost" data-act="ov-go" data-i="${j}">${fmt(o.t)}</button>
          <span class="grow">${esc(o.kind)} · ${esc(o.text || (L.assets.find((x) => x.id === o.asset_id)?.name) || o.asset_id || "")}</span><span class="dim">${(+o.d).toFixed(1)}s</span>
          <button class="btn xs ghost danger" data-act="ov-del" data-i="${j}" ${ro}>✕</button></div>`).join("") || `<div class="dim">None yet.</div>`}</div>`;
    } else if (L.tab === "ai") {
      const p = L.plan;
      el.innerHTML = `<p class="muted" style="font-size:12.5px;margin-top:0">Type a request; the AI proposes a plan for this long form; approve the items you want.</p>
        <textarea id="lf-ask" rows="3" placeholder="e.g. add a sound effect on each big moment and pop the chapter names on screen" ${ro}></textarea>
        <div class="row" style="margin-top:8px"><button class="btn sm primary" data-act="plan" ${ro || (S.settings?.ai_ready?.plan ? "" : "disabled title='Add a Claude key or route Edit plans to another AI in Settings'")}>✨ Plan edits</button></div>
        ${p ? `<div class="sec"><b>${esc(p.summary)}</b>${p.items.map((it) => `<label class="st-item"><input type="checkbox" data-plan="${esc(it.id)}" ${L.picked.has(it.id) ? "checked" : ""}>
            <span><b>${esc(it.title)}</b><br><span class="dim">${esc(it.why)}</span></span></label>`).join("")}
          ${p.dropped ? `<p class="dim" style="font-size:11.5px">${p.dropped} suggestion${p.dropped > 1 ? "s" : ""} skipped (not applicable to a long form, or invalid).</p>` : ""}
          <button class="btn sm primary" data-act="plan-apply" ${L.picked.size ? "" : "disabled"}>Apply selected (${L.picked.size})</button></div>` : ""}`;
      el.onchange = (ev) => { const id = ev.target.dataset.plan; if (!id) return; ev.target.checked ? L.picked.add(id) : L.picked.delete(id); const b = $("[data-act=plan-apply]", el); b.disabled = !L.picked.size; b.textContent = `Apply selected (${L.picked.size})`; };
    } else {
      const d = L.d, ex = d.export || {};
      el.innerHTML = `<div class="stack">
        <div class="row">${pill(LF_STAGE, d.lf_stage)}<span class="dim grow">${fmt(total(), 0)} · ${esc(d.edit.aspect)}</span></div>
        ${d.current && ex.cover ? `<img src="/files/${esc(pid())}/${esc(ex.cover)}?r=${+d.rev}" alt="Cover" style="width:100%;border-radius:8px;border:1px solid var(--line-2)" title="Auto-picked cover (YouTube thumbnail)">` : ""}
        ${d.current ? `<p class="dim" style="margin:0;font-size:12px">Rendered ${esc(when(ex.at))} · ${fmt(ex.duration || 0, 0)}</p>` : `<p class="dim" style="margin:0;font-size:12px">${ex.status === "rendering" ? "Rendering…" : "Not rendered since the last change."} Publishing renders first when needed.</p>`}
        <div class="sec"><h3>Posts</h3>${(d.uploads || []).map((u) => `<div class="lf-item">${platTag(u.platform)}<span class="grow">${esc(u.account || "")}</span>${pill(UPLOAD_STATUS, u.status)}
            ${/^https:\/\//.test(u.url || "") ? `<a class="pl-link" href="${esc(u.url)}" target="_blank" rel="noopener">open ↗</a>` : ""}${u.error ? `<span class="dim" title="${esc(u.error)}">ⓘ</span>` : ""}</div>`).join("") || `<div class="dim">Not posted yet.</div>`}</div>
        ${d.locked ? `<p class="dim" style="font-size:12px">🔒 Posted: the video is final. A new version copies everything into an editable edit.</p><button class="btn sm" data-act="version">Make a new version</button>`
          : `<button class="btn sm primary" data-act="publish" ${d.status === "ready" ? "" : "disabled"}>⇪ Publish…</button>`}</div>`;
    }
  }
}

// ======================================================================
// Settings → Phone remote (remote.py): an opt-in Wi-Fi listener that serves the phone app. A phone pairs by scanning a
// QR code; each phone gets its own signing secret (never shown again after pairing) and can be revoked here.
// ======================================================================
function phoneRemoteCard() {
  const page = $(".page", view);
  if (!page || $("#phone-remote", page)) return;
  page.insertAdjacentHTML("beforeend", `<div class="card stack" style="margin-top:20px" id="phone-remote"><span class="dim">Loading…</span></div>`);
  const el = $("#phone-remote", page);
  let st = null, pending = null, ip = "";
  const agoS = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : new Date(t * 1000).toLocaleDateString(); };
  const draw = () => {
    ip = st.ips.includes(ip) ? ip : st.ips[0] || "";
    const paired = st.devices.filter((d) => d.paired);
    el.innerHTML = `<div class="row wrap"><h2 class="grow" style="margin:0">Phone remote</h2>${st.running ? '<span class="pill ok">on</span>' : st.enabled ? '<span class="pill err">not running</span>' : '<span class="pill">off</span>'}</div>
      <p class="muted" style="margin:0;font-size:12.5px">Control Reelwright from your phone over your Wi-Fi: keep or skip new clips, publish and schedule to approved accounts, pause Autopilot, import links, follow the pipeline and drive OBS. The phone never sees your keys, sign-ins, settings or files. It works on your own network only (away from home, use a VPN such as Tailscale).</p>
      <label class="row" style="gap:8px"><input type="checkbox" id="pr-on" ${st.enabled ? "checked" : ""}> <span>Let my phone control Reelwright over Wi-Fi</span></label>
      <div class="row wrap"><label class="field" style="width:130px">Port<input type="number" id="pr-port" min="1024" max="65535" value="${+st.port}"></label>
        ${st.ips.length > 1 ? `<label class="field grow">This PC's address for the phone<select id="pr-ip">${st.ips.map((a) => `<option ${a === ip ? "selected" : ""}>${esc(a)}</option>`).join("")}</select></label>` : ""}
        <button class="btn sm" id="pr-save" style="align-self:flex-end">Save port</button></div>
      ${st.error ? `<p style="color:var(--err);font-size:12.5px;margin:0">${esc(st.error)}</p>` : ""}
      ${st.running && ip ? `<div class="dim" style="font-size:12.5px">Phone address <span class="mono">http://${esc(ip)}:${+st.port}</span>. If Windows asks, allow Reelwright on <b>private</b> networks.</div>` : ""}
      ${!st.running ? "" : pending ? `<div class="row wrap" style="align-items:flex-start;gap:16px">
          <div style="background:#fff;padding:6px;border-radius:8px;line-height:0">${pending.svg}</div>
          <div class="grow stack" style="gap:6px;font-size:12.5px"><b>Scan this with your phone's camera</b>
            <span class="muted">Open the link on the phone (same Wi-Fi as this PC) and press Pair. The code is for one phone and expires at ${esc(when(new Date(pending.expires * 1000).toISOString()))} if unused: keep it off screen shares and streams.</span>
            <span class="muted">Then add it to the Home Screen: iPhone Safari → Share → Add to Home Screen; Android Chrome → ⋮ → Add to Home screen.</span>
            <div><button class="btn sm" id="pr-cancel">Cancel</button></div></div></div>`
        : `<div><button class="btn sm primary" id="pr-pair">＋ Pair a phone</button></div>`}
      ${paired.length ? `<table style="width:100%;font-size:12.5px"><tbody>${paired.map((d) => `<tr><td><b>${esc(d.name || "Phone")}</b></td>
          <td class="dim">${d.last_seen ? `seen ${esc(agoS(d.last_seen))}${d.last_ip ? ` from ${esc(d.last_ip)}` : ""}` : "not seen yet"}</td>
          <td style="text-align:right"><button class="btn xs danger" data-revoke="${esc(d.id)}" data-name="${esc(d.name || "this phone")}">Revoke</button></td></tr>`).join("")}</tbody></table>`
        : `<div class="dim" style="font-size:12.5px">No phones paired.</div>`}`;
  };
  const load = async () => {
    const was = pending;
    st = await api("/api/remote");
    if (pending && !st.devices.some((d) => d.id === pending.device_id && !d.paired)) {
      const d = st.devices.find((x) => x.id === pending.device_id);
      pending = null;
      if (d?.paired) toast(`Paired with ${d.name || "your phone"}.`, "ok");
    }
    if (el.isConnected && (was || !$("input:focus", el))) draw();
  };
  const tick = setInterval(() => (el.isConnected ? (pending || !document.hidden) && load().catch(() => {}) : clearInterval(tick)), 4000);
  const pairNew = async () => { pending = await api("/api/remote/pair", { body: { ip, device_id: pending?.device_id } }); draw(); };
  el.onchange = async (e) => {
    try {
      if (e.target.id === "pr-on") { st = await api("/api/remote", { method: "PUT", body: { enabled: e.target.checked } }); if (!st.enabled) pending = null; draw(); }
      if (e.target.id === "pr-ip") { ip = e.target.value; if (pending) await pairNew(); }
    } catch (err) { fail(err); load().catch(() => {}); }
  };
  el.onclick = async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    try {
      if (t.id === "pr-save") { st = await api("/api/remote", { method: "PUT", body: { port: +$("#pr-port", el).value } }); pending = null; draw(); toast("Saved.", "ok", 1200); }
      if (t.id === "pr-pair") await pairNew();
      if (t.id === "pr-cancel") { st = await api(`/api/remote/devices/${encodeURIComponent(pending.device_id)}`, { method: "DELETE" }); pending = null; draw(); }
      if (t.dataset.revoke && await confirmBox(`Revoke ${t.dataset.name}?`, "It stops working at once, including videos it has open. Pair it again to use it.", "Revoke")) {
        st = await api(`/api/remote/devices/${encodeURIComponent(t.dataset.revoke)}`, { method: "DELETE" });
        draw();
      }
    } catch (err) { fail(err); }
  };
  load().catch((err) => { el.innerHTML = `<h2 style="margin:0">Phone remote</h2><span class="dim">${esc(err.message)}</span>`; });
}

// ---------- signals (signals.py / routes_signals.py) ----------

// Editor timeline: the signals lane between the waveform and the clips — viewer clips (purple bars), detected sounds
// (ticks by kind) and markers ("clip that", OBS marks, game events: orange flags through the waveform).
const SOUND_COLOR = { laughter: "#37d39a", scream: "#ff5d5d", cheering: "#ffc247", applause: "#ffc247", gunshot: "#ff8a3d", explosion: "#ff8a3d", crying: "#6aa8ff", music: "#8b90a8" };
function drawSignalLane(ctx, sig, pps, top, y, h) {
  ctx.fillStyle = "#b07cff99";
  (sig.viewer_clips || []).forEach((c) => ctx.fillRect(c.t * pps, y, Math.max(2, c.d * pps), h));
  (sig.audio_events || []).forEach((e) => { ctx.fillStyle = SOUND_COLOR[e.kind] || "#cfd3e6"; ctx.fillRect(e.t * pps - 1.5, y, 3, h); });
  ctx.fillStyle = "#ff6a3d";
  (sig.markers || []).forEach((m) => {
    const x = m.t * pps;
    ctx.fillRect(x - 1, top, 2, y + h - top);
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x + 7, top + 4); ctx.lineTo(x, top + 8); ctx.fill();
  });
}

// Settings → Signals: its own card and Save (returns the placeholder; fills itself in once the page is on screen).
function signalsCard() {
  setTimeout(async () => {
    const el = $("#sigcard");
    if (!el) return;
    const LANGS = [["", "Detect for each video"], ["en", "English"], ["es", "Spanish"], ["pt", "Portuguese"], ["fr", "French"], ["de", "German"], ["it", "Italian"], ["nl", "Dutch"], ["pl", "Polish"], ["ru", "Russian"], ["tr", "Turkish"], ["ja", "Japanese"], ["ko", "Korean"], ["zh", "Chinese"]];
    const draw = (st) => {
      const s = st.settings, cl = st.chatlog, au = st.audio, tw = st.twitch;
      const chat = !s.twitch_chat_channel ? "Off." : cl.connected ? `Recording #${esc(cl.channel)}: ${+cl.count} messages since Reelwright started.` : `Connecting to #${esc(cl.channel)}…${cl.error ? " " + esc(cl.error) : ""}`;
      const lang = LANGS.some(([k]) => k === s.whisper_language) ? LANGS : [...LANGS, [s.whisper_language, s.whisper_language]];
      el.innerHTML = `<h2 style="margin:0">Signals</h2>
        <p class="muted" style="margin:0;font-size:12.5px">More evidence for finding clips: what you said, what chat and your viewers did, and what could be heard.</p>
        <div class="row wrap"><label class="field grow">Speech language<select id="sg-lang">${lang.map(([k, l]) => `<option value="${esc(k)}" ${k === s.whisper_language ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label></div>
        <label class="field">Brand vocabulary <span class="dim" style="font-size:12px">— names, games, emotes and slang Whisper should spell right, separated by commas</span><textarea id="sg-vocab" maxlength="2000" placeholder="e.g. Reelwright, Valorant, Jett, KEKW">${esc(s.brand_vocab)}</textarea></label>
        <label class="field">“Clip that” phrases <span class="dim" style="font-size:12px">— say one on stream and the 30–60 s before it becomes a clip</span><input type="text" id="sg-trig" maxlength="500" placeholder="${esc(st.default_triggers.join(", "))}" value="${esc(s.voice_triggers)}"></label>
        <label class="field">Record my Twitch chat live <span class="dim" style="font-size:12px">— your channel login; read-only and anonymous, while Reelwright runs. Only chat recorded live can be used: Twitch offers no chat replay.</span><input type="text" id="sg-chan" maxlength="60" placeholder="yourchannel (empty = off)" value="${esc(s.twitch_chat_channel)}"></label>
        <div class="dim" style="font-size:12px">${chat}</div>
        <label class="field">Extra chat reactions <span class="dim" style="font-size:12px">— one per line, e.g. <span class="mono">laugh: myLaughEmote</span> or <span class="mono">#channel hype: myHypeEmote</span>. Kinds: ${Object.keys(st.emotes).map(esc).join(", ")}</span><textarea id="sg-emo" maxlength="5000">${esc(s.chat_emotes)}</textarea></label>
        <label class="switch"><input type="checkbox" id="sg-audio" ${s.audio_events ? "checked" : ""} ${au.available ? "" : "disabled"}> Detect laughter, screams, cheering, applause, gunshots and explosions around the loud moments</label>
        <div class="dim" style="font-size:12px">${au.available ? (au.model_ready ? "Model ready." : `Downloads a ${+au.size_mb} MB model the first time.`) : "Not available in this build."} Model: <a href="${esc(au.page)}" target="_blank">${esc(au.model)}</a> — ${esc(au.licence)}.</div>
        <div><button type="button" class="btn sm primary" id="sg-save">Save signals</button></div>
        <h3 style="margin:10px 0 0">Twitch viewer clips</h3>
        <p class="muted" style="margin:0;font-size:12.5px">Clips your viewers made of a VOD show what they loved. Register an app at <a href="https://dev.twitch.tv/console/apps" target="_blank">dev.twitch.tv/console/apps</a> (category “Application Integration”, OAuth redirect <span class="mono">http://localhost</span>), then paste its Client ID and a new Client secret. Imported Twitch VODs are then checked when they are analyzed.</p>
        <div class="row wrap"><label class="field grow">Client ID<input type="text" id="sg-cid" value="${esc(tw.client_id)}" autocomplete="off"></label>
          <label class="field grow">Client secret ${tw.has_app ? '<span class="pill ok">saved</span>' : ""}<input type="password" id="sg-sec" placeholder="${tw.has_app ? "Paste again to replace" : ""}" autocomplete="off"></label></div>
        <div class="row wrap"><button type="button" class="btn sm" id="sg-tsave">Save Twitch app</button>${tw.has_app ? '<button type="button" class="btn sm" id="sg-ttest">Test</button><button type="button" class="btn sm danger" id="sg-trm">Remove</button>' : ""}<span class="dim" id="sg-tout" style="font-size:12px"></span></div>`;
    };
    const load = async () => draw(await api("/api/signals"));
    el.onclick = async (e) => {
      const id = e.target.id;
      try {
        if (id === "sg-save") {
          draw(await api("/api/signals", { method: "PUT", body: { whisper_language: $("#sg-lang").value, brand_vocab: $("#sg-vocab").value, voice_triggers: $("#sg-trig").value, twitch_chat_channel: $("#sg-chan").value, chat_emotes: $("#sg-emo").value, audio_events: $("#sg-audio").checked } }));
          toast("Signals saved.", "ok", 1500);
        }
        if (id === "sg-tsave") { await api("/api/signals/twitch", { method: "PUT", body: { client_id: $("#sg-cid").value, client_secret: $("#sg-sec").value } }); toast("Twitch app saved.", "ok"); await load(); }
        if (id === "sg-ttest") { $("#sg-tout").textContent = "Checking…"; await api("/api/signals/twitch/test", { method: "POST" }); $("#sg-tout").textContent = "✓ Twitch accepted the app."; }
        if (id === "sg-trm") { await api("/api/signals/twitch", { method: "DELETE" }); toast("Twitch app removed."); await load(); }
      } catch (err) { if (id === "sg-ttest") $("#sg-tout").textContent = ""; fail(err); }
    };
    load().catch((err) => { el.innerHTML = `<h2 style="margin:0">Signals</h2><span class="dim">${esc(err.message)}</span>`; });
  }, 0);
  return `<div class="card stack" style="margin-top:20px" id="sigcard"><span class="dim">Loading…</span></div>`;
}

// ---------- render & QA (WP2): caption animations, safe zones, the pre-publish check, Settings › Rendering ----------
const CAP_ANIMS = [["highlight", "Highlight"], ["pop", "Pop"], ["bounce", "Bounce"], ["box", "Box"], ["reveal", "Reveal"]];
const SAFE_ZONES = { // mirrors qa.SAFE_ZONES (share of a 9:16 frame each app's UI covers)
  tiktok: { top: 0.07, bottom: 0.2, right: 0.12 }, youtube: { top: 0.06, bottom: 0.18, right: 0.12 }, instagram: { top: 0.1, bottom: 0.2, right: 0.12 } };
const SAFE_NAMES = { tiktok: "TikTok", youtube: "Shorts", instagram: "Reels" };
const CAP_PLAIN = new Set(["because", "actually", "literally", "basically", "something", "anything", "everything", "nothing", "probably", "through", "without", "another", "whatever", "already", "anyway", "honestly", "seriously"]);
function capKeyword(w) { // mirrors media._keyword
  const t = String(w).replace(/[^\p{L}\p{N}_]/gu, "");
  return !!t && (/\d/.test(t) || (t.length >= 3 && t === t.toUpperCase() && t !== t.toLowerCase()) || (t.length >= 7 && !CAP_PLAIN.has(t.toLowerCase())));
}
function qaList(qa) { // clip.export.qa from qa.lint
  if (!qa.length) return `<div class="qa-list" style="flex-basis:100%;color:var(--ok);font-size:12px">✓ Pre-publish check: no issues</div>`;
  const err = qa.some((i) => i.level === "error");
  return `<details class="qa-list" style="flex-basis:100%;font-size:12px" ${err ? "open" : ""}><summary style="cursor:pointer">${err ? "⛔" : "⚠"} Pre-publish check: ${qa.length} issue${qa.length > 1 ? "s" : ""}</summary>
    <ul style="margin:6px 0 0;padding-left:18px">${qa.map((i) => `<li style="color:${i.level === "error" ? "var(--err)" : "inherit"}">${i.platform ? `<b>${esc(SAFE_NAMES[i.platform] || i.platform)}:</b> ` : ""}${esc(i.msg)}${i.fix ? ` <span class="dim">— ${esc(i.fix)}</span>` : ""}</li>`).join("")}</ul></details>`;
}
// Settings → Rendering: its own card and Save (returns the placeholder; fills itself in once the page is on screen).
function renderCard() {
  setTimeout(async () => {
    const el = $("#rendercard");
    if (!el) return;
    try {
      const [s, enc] = await Promise.all([api("/api/settings"), api("/api/render/encoders")]);
      const opt = [["auto", "Auto (with NVENC or 8+ CPU cores)"], ["on", "On"], ["off", "Off: renders wait for analysis"]].map(([k, l]) => `<option value="${k}" ${k === s.parallel_render ? "selected" : ""}>${l}</option>`).join("");
      el.innerHTML = `<h2>Rendering</h2>
        <p class="muted" style="margin:0;font-size:13px">Encoders that work on this PC (best first): <b>${esc(enc.encoders.join(", ") || "none")}</b> · ffmpeg ${esc(enc.licence)} build · renders ${enc.parallel ? "run beside analysis" : "wait for analysis"} right now.</p>
        <label class="field">Render while analysing<select id="rc-par">${opt}</select></label>
        <label class="field">Bleep list <span class="muted" style="font-size:12px">(clips with Bleep on beep or mute these; * = any ending; captions show f***)</span><textarea id="rc-bleep" rows="2">${esc(s.bleep_words)}</textarea></label>
        <div><button type="button" class="btn sm" id="rc-save">Save rendering settings</button></div>`;
      $("#rc-save").onclick = async () => {
        try { S.settings = await api("/api/settings", { method: "PUT", body: { parallel_render: $("#rc-par").value, bleep_words: $("#rc-bleep").value } }); toast("Rendering settings saved.", "ok"); } catch (err) { fail(err); }
      };
    } catch (err) { el.innerHTML = `<h2 style="margin:0">Rendering</h2><span class="dim">${esc(err.message)}</span>`; }
  }, 0);
  return `<div class="card stack" id="rendercard"><span class="dim">Loading…</span></div>`;
}

// ======================================================================
// Live & integrations (WP6): game / VTube Studio telemetry and the mark hotkey (telemetry.py), taught screen events
// (hud.py) and the Claude Desktop link (mcp_server.py). Settings cards plus the Editor's "Teach screen event" modal.
// ======================================================================
function liveCards() {
  const page = $(".page", view);
  if (!page || $("#live-card", page)) return;
  page.insertAdjacentHTML("beforeend", `<div class="card stack" style="margin-top:20px" id="live-card"><span class="dim">Loading…</span></div>
    <div class="card stack" style="margin-top:20px" id="mcp-card"><span class="dim">Loading…</span></div>`);
  const el = $("#live-card", page), mc = $("#mcp-card", page);
  let st = null, huds = [];
  const pill2 = (on, okTxt, offTxt) => `<span class="pill ${on ? "ok" : ""}">${on ? okTxt : offTxt}</span>`;
  const draw = () => {
    el.innerHTML = `<div class="row wrap"><h2 class="grow" style="margin:0">Live integrations</h2>${st.recording.length ? `<span class="pill err">● recording: ${st.recording.map(esc).join(", ")}</span>` : '<span class="pill">not recording</span>'}</div>
      <p class="muted" style="margin:0;font-size:12.5px">While an <b>OBS link</b> (OBS page) is recording, these mark moments in the recording (kills, deaths, round wins, objectives, VTube Studio reactions) so analysis looks there first. They use only the games' and VTube Studio's official local interfaces on this PC (no game memory is read, nothing is drawn over the game), run only while switched on <b>and</b> recording, and send nothing anywhere. Turn on “Import finished recordings” on the OBS page so the marks travel with the file.</p>
      <label class="field">Mark hotkey <span class="dim" style="font-size:11.5px">(marks the running recording from anywhere, even in a game; e.g. ctrl+alt+m; empty = off)</span>
        <div class="row"><input type="text" id="lv-hk" value="${esc(st.hotkey_spec)}" placeholder="ctrl+alt+m" style="width:180px"><button class="btn sm" id="lv-hk-save">Save</button>${st.hotkey.error ? `<span style="color:var(--err);font-size:12px">${esc(st.hotkey.error)}</span>` : st.hotkey.active ? '<span class="pill ok">active</span>' : ""}</div></label>
      <label class="row" style="gap:8px"><input type="checkbox" data-lv="lol_enabled" ${st.lol.enabled ? "checked" : ""}> <span><b>League of Legends</b>: kills, deaths, multikills, objectives, wins (Riot's Live Client Data on this PC) ${st.lol.enabled ? pill2(st.lol.active, "game found", "waiting for a game") : ""}</span></label>
      <div class="stack" style="gap:6px"><label class="row" style="gap:8px"><input type="checkbox" data-lv="gsi_enabled" ${st.gsi.enabled ? "checked" : ""}> <span><b>CS2 / Dota 2</b>: kills, multikills, deaths, round wins, Roshan (Valve's Game State Integration) ${st.gsi.enabled ? pill2(st.gsi.running, `listening on ${+st.gsi.port}`, "starts when recording") : ""}</span></label>
        ${st.gsi.error ? `<div style="color:var(--err);font-size:12px">${esc(st.gsi.error)}</div>` : ""}
        <div class="row wrap" style="font-size:12.5px"><label class="field" style="flex-direction:row;align-items:center;gap:6px">port <input type="number" id="lv-gport" value="${+st.gsi.port}" min="1024" max="65535" style="width:90px"></label>
          <a class="btn sm" href="/api/live/gsi/cs2" download="gamestate_integration_reelwright.cfg">Generate CS2 config file</a>
          <a class="btn sm" href="/api/live/gsi/dota2" download="gamestate_integration_reelwright.cfg">Generate Dota 2 config file</a>
          <button class="btn sm ghost" id="lv-gtok" title="Old config files stop working">New token</button></div>
        <ol class="list dim" style="font-size:12px;margin:0"><li><b>CS2:</b> put the file in <span class="mono">Steam\\steamapps\\common\\Counter-Strike Global Offensive\\game\\csgo\\cfg\\</span>, then restart CS2.</li>
          <li><b>Dota 2:</b> put it in <span class="mono">Steam\\steamapps\\common\\dota 2 beta\\game\\dota\\cfg\\gamestate_integration\\</span> (create the folder), add <span class="mono">-gamestateintegration</span> to Dota 2's launch options (Steam → Properties), then restart Dota 2.</li>
          <li>Changed the port or the token? Generate and replace the file again.</li></ol></div>
      <div class="stack" style="gap:6px"><label class="row" style="gap:8px"><input type="checkbox" data-lv="vts_enabled" ${st.vts.enabled ? "checked" : ""}> <span><b>VTube Studio</b>: hotkey expressions and animations, item drops, face tracking lost ${st.vts.enabled ? pill2(st.vts.connected, "connected", st.vts.authorized ? "connects when recording" : "not authorized") : ""}</span></label>
        ${st.vts.error ? `<div style="color:var(--err);font-size:12px">${esc(st.vts.error)}</div>` : ""}
        <div class="row wrap" style="font-size:12.5px"><label class="field" style="flex-direction:row;align-items:center;gap:6px">API port <input type="number" id="lv-vport" value="${+st.vts.port}" min="1024" max="65535" style="width:90px"></label>
          <button class="btn sm ${st.vts.authorized ? "" : "primary"}" id="lv-vauth">${st.vts.authorized ? "Authorize again" : "Authorize in VTube Studio"}</button>
          ${st.vts.authorized ? '<button class="btn sm ghost" id="lv-vforget">Forget</button>' : ""}</div>
        <div class="dim" style="font-size:12px">In VTube Studio: Settings → turn on <b>Start API</b> (port 8001). Press Authorize, then click <b>Allow</b> in the pop-up VTube Studio shows.</div></div>
      <h3 style="margin:6px 0 0">Taught screen events</h3>
      ${huds.length ? `<table style="width:100%;font-size:12.5px"><tbody>${huds.map((h) => `<tr><td style="width:120px"><img src="/api/live/hud/${esc(h.id)}.png" alt="${esc(h.name)}" style="max-width:110px;max-height:40px"></td>
          <td><b>${esc(h.name)}</b>${h.game ? ` <span class="dim">· ${esc(h.game)}</span>` : ""}</td>
          <td><label class="dim" title="How close a match must be (higher = stricter)">match ≥ <input type="number" data-hthr="${esc(h.id)}" value="${+h.threshold}" min="0.5" max="0.99" step="0.01" style="width:64px"></label></td>
          <td><label class="switch"><input type="checkbox" data-hen="${esc(h.id)}" ${h.enabled ? "checked" : ""}> on</label></td>
          <td style="text-align:right"><button class="btn xs danger" data-hrm="${esc(h.id)}">Delete</button></td></tr>`).join("")}</tbody></table>`
        : `<div class="dim" style="font-size:12.5px">None yet: open a video in the Editor and press <b>◎ Teach screen event</b>.</div>`}
      <div class="dim" style="font-size:12px">Switched-on events (up to 8) are looked for in every video Reelwright analyzes. Switch a game's events off when you play something else.</div>`;
  };
  const load = async () => { [st, huds] = await Promise.all([api("/api/live"), api("/api/live/hud")]); if (el.isConnected && !$("input:focus", el)) draw(); };
  const put = async (body) => { st = await api("/api/live", { method: "PUT", body }); draw(); };
  const tick = setInterval(() => (el.isConnected ? !document.hidden && load().catch(() => {}) : clearInterval(tick)), 5000);
  el.onchange = async (e) => {
    const t = e.target;
    try {
      if (t.dataset.lv) await put({ [t.dataset.lv]: t.checked });
      if (t.id === "lv-gport") await put({ gsi_port: +t.value });
      if (t.id === "lv-vport") await put({ vts_port: +t.value });
      if (t.dataset.hen) { await api(`/api/live/hud/${t.dataset.hen}`, { method: "PATCH", body: { enabled: t.checked } }); await load(); }
      if (t.dataset.hthr) { await api(`/api/live/hud/${t.dataset.hthr}`, { method: "PATCH", body: { threshold: +t.value } }); toast("Saved.", "ok", 1200); }
    } catch (err) { fail(err); load().catch(() => {}); }
  };
  el.onclick = async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    try {
      if (t.id === "lv-hk-save") { await put({ live_mark_hotkey: $("#lv-hk", el).value }); toast("Saved: the hotkey works while OBS records.", "ok"); }
      if (t.id === "lv-gtok" && await confirmBox("New GSI token?", "Config files you already put in CS2 / Dota 2 stop working until you generate and replace them.", "Replace")) {
        st = await api("/api/live/gsi/token", { method: "POST" }); draw();
      }
      if (t.id === "lv-vauth") {
        t.disabled = true; t.innerHTML = `<span class="spin"></span> Click Allow in VTube Studio…`;
        try { st = await api("/api/live/vts/authorize", { method: "POST" }); toast("VTube Studio connected to Reelwright.", "ok"); } finally { draw(); }
      }
      if (t.id === "lv-vforget") { st = await api("/api/live/vts/forget", { method: "POST" }); draw(); }
      if (t.dataset.hrm && await confirmBox("Delete this screen event?", "Markers it already found stay; new videos won't look for it.")) {
        await api(`/api/live/hud/${t.dataset.hrm}`, { method: "DELETE" }); await load();
      }
    } catch (err) { fail(err); }
  };
  load().catch((err) => { el.innerHTML = `<h2 style="margin:0">Live integrations</h2><span class="dim">${esc(err.message)}</span>`; });
  api("/api/live/mcp").then((cfg) => {
    const json = JSON.stringify(cfg, null, 2);
    mc.innerHTML = `<h2 style="margin:0">Claude Desktop (MCP)</h2>
      <p class="muted" style="margin:0;font-size:12.5px">Let Claude Desktop work with Reelwright while it runs: list projects and clips, find clips, render, keep or skip new picks, schedule posts and pause Autopilot. It can't see or change your settings, keys, sign-ins or account approvals. Its posts count as Autopilot posts: they only go to accounts with both “Approved for uploads” and “Allow Autopilot to post here” on (Publish page), use your default privacy, and wait while Autopilot is paused. What these tools return (titles, transcript snippets) becomes part of your Claude conversation.</p>
      <ol class="list" style="font-size:12.5px;margin:0"><li>In Claude Desktop: <b>Settings → Developer → Edit Config</b> (opens <span class="mono">%APPDATA%\\Claude\\claude_desktop_config.json</span>).</li>
        <li>Paste this in (if the file already has <span class="mono">"mcpServers"</span>, add just the <span class="mono">"reelwright"</span> entry), save, and restart Claude Desktop.</li>
        <li>Keep Reelwright open: Claude reaches it on this PC only.</li></ol>
      <textarea readonly class="mono" rows="${json.split("\n").length}" style="width:100%;font-size:12px">${esc(json)}</textarea>
      <div><button class="btn sm" id="mcp-copy">Copy</button></div>`;
    $("#mcp-copy", mc).onclick = async () => { await navigator.clipboard.writeText(json); toast("Copied.", "ok"); };
  }).catch((err) => { mc.innerHTML = `<h2 style="margin:0">Claude Desktop (MCP)</h2><span class="dim">${esc(err.message)}</span>`; });
}

function openHudTeach(pid, t0) { // Editor → ◎ Teach screen event: box something on one frame, name it, find it everywhere
  let box = null, t = Math.max(0, t0);
  modal(`<h2>Teach a screen event</h2>
    <p class="muted" style="margin:0 0 8px;font-size:12.5px">Find a frame where it shows (a “YOU DIED” screen, a kill-feed entry, a VICTORY banner), then drag a box tightly around it. Reelwright then finds it in this and every later video and marks each appearance.</p>
    <div class="row" style="gap:8px;margin-bottom:6px"><button class="btn xs" data-step="-5">−5 s</button><button class="btn xs" data-step="-1">−1 s</button><button class="btn xs" data-step="-0.2">−0.2</button>
      <span class="mono" id="ht-t"></span><button class="btn xs" data-step="0.2">+0.2</button><button class="btn xs" data-step="1">+1 s</button><button class="btn xs" data-step="5">+5 s</button></div>
    <div id="ht-wrap" style="position:relative;user-select:none;line-height:0;cursor:crosshair;background:#000;touch-action:none"><img id="ht-img" alt="Video frame" draggable="false" style="width:100%">
      <div id="ht-box" hidden style="position:absolute;border:2px solid #ffd400;box-shadow:0 0 0 9999px rgba(0,0,0,.35)"></div></div>
    <div class="row wrap" style="margin-top:10px"><label class="field grow">Name<input type="text" id="ht-name" maxlength="40" placeholder="You died"></label>
      <label class="field grow">Game <span class="dim" style="font-size:11px">(groups events)</span><input type="text" id="ht-game" maxlength="40" placeholder="Elden Ring"></label>
      <label class="field" style="width:110px" title="How close a match must be (higher = stricter)">Match ≥<input type="number" id="ht-thr" value="0.8" min="0.5" max="0.99" step="0.01"></label></div>
    <div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="ht-save">Save and scan this video</button></div>`, (m, close) => {
    const img = $("#ht-img", m), wrap = $("#ht-wrap", m), bx = $("#ht-box", m);
    const show = () => { $("#ht-t", m).textContent = fmt(t); img.src = `/api/live/hud/frame?pid=${encodeURIComponent(pid)}&t=${t.toFixed(2)}`; };
    img.onerror = () => toast("Couldn't read that frame.", "err");
    $$("[data-step]", m).forEach((b) => (b.onclick = () => { t = Math.max(0, t + +b.dataset.step); show(); }));
    const rel = (e) => { const r = img.getBoundingClientRect(); return [Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1), Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1)]; };
    const paint = () => { bx.hidden = !box; if (box) Object.assign(bx.style, { left: box[0] * 100 + "%", top: box[1] * 100 + "%", width: box[2] * 100 + "%", height: box[3] * 100 + "%" }); };
    wrap.onpointerdown = (e) => {
      const [x0, y0] = rel(e);
      wrap.setPointerCapture(e.pointerId);
      wrap.onpointermove = (ev) => { const [x1, y1] = rel(ev); box = [Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0)]; paint(); };
      wrap.onpointerup = () => { wrap.onpointermove = wrap.onpointerup = null; };
    };
    $("#ht-save", m).onclick = async (e) => {
      if (!box || box[2] < 0.01 || box[3] < 0.01) return toast("Drag a box around the thing to look for first.", "err");
      e.target.disabled = true;
      try {
        await api("/api/live/hud", { body: { pid, t, box, name: $("#ht-name", m).value, game: $("#ht-game", m).value, threshold: +$("#ht-thr", m).value } });
        await api("/api/live/hud/scan", { body: { pid } });
        close(); toast("Saved. Scanning this video; its matches become markers, and later videos are checked automatically.", "ok", 6000);
      } catch (err) { fail(err); e.target.disabled = false; }
    };
    show();
  }, true);
}

// ======================================================================
// Models — hardware-aware model marketplace (reelwright/models, routes_models.py): every exact variant (Ollama on this
// PC, Hugging Face search, curated picks, local GGUF) with its fit on this PC; compare, install, benchmark; per task a
// versioned selection with release gates, activation and rollback (ai.route follows the active release).
// ======================================================================
routes.models = Models;
const FIT_STATUS = { fits: ["ok", "Fits"], borderline: ["warn", "Borderline"], incompatible: ["err", "Too big"], unknown: ["", "Unknown"], inapplicable: ["info", "Cloud / CPU"] };
const GATE_STATUS = { pass: ["ok", "pass"], fail: ["err", "fail"], unmeasured: ["warn", "unmeasured"], inapplicable: ["", "n/a"], borderline: ["warn", "borderline"], unknown: ["", "unknown"] };
const SRC_LABEL = { publisher: "publisher-reported", tested: "locally tested", estimated: "estimated", unknown: "unknown" };
const MODEL_FIELDS = [["params_b", "Parameters", (x) => x + " B"], ["quant", "Quantization"], ["size_bytes", "Size", (x) => (x / 1073741824).toFixed(1) + " GiB"],
  ["context", "Context", (x) => num(x) + " tokens"], ["layers", "Layers"], ["kv_heads", "KV heads"], ["head_dim", "Head dim"], ["license", "License"]];
const fitLine = (f) => !f ? "" : `${pill(FIT_STATUS, f.status)} ${f.range_gib ? `<span class="dim" style="font-size:12px">${f.range_gib[0]}–${f.range_gib[1]} GiB`
  + `${f.pct_total ? ` · ${f.pct_total[0]}–${f.pct_total[1]}% of VRAM` : ""}${f.left_gib && f.status === "fits" ? ` · ${f.left_gib[0]}–${f.left_gib[1]} GiB left` : ""}`
  + `${f.short_gib && f.status === "incompatible" ? ` · short ${f.short_gib[0]}–${f.short_gib[1]} GiB` : ""}${f.source === "tested" ? ` · measured ${esc((f.measured_at || "").slice(0, 10))}` : ""}</span>` : ""}`;
const fieldVal = (v, [k, , f]) => (v[k] == null ? "—" : esc(f ? f(v[k]) : v[k]));
const srcTag = (v, k) => { const s = (v.src || {})[k] || (v[k] == null ? "unknown" : "publisher"); return `<span class="dim" style="font-size:11px">${esc(SRC_LABEL[s] || s)}</span>`; };
const checkList = (f) => f.checks.map((c) => `<li>${pill(GATE_STATUS, c.status)} <b>${esc(c.name)}</b> — ${esc(c.detail || "")}</li>`).join("");

function Models() {
  const load = (k, d) => { try { return JSON.parse(lsGet("models." + k) || "null") ?? d; } catch { return d; } };
  const M = { tab: load("tab", "catalog"), f: load("f", { view: "all", sort: "relevance" }), pins: [], hfNext: "", tasks: null };
  const save = () => { lsSet("models.tab", JSON.stringify(M.tab)); lsSet("models.f", JSON.stringify(M.f)); };
  const wl = () => ({ context: M.f.context || 8192, concurrency: M.f.concurrency || 1 });
  const qs = () => {
    const f = M.f, p = new URLSearchParams();
    for (const k of ["q", "task", "input", "output", "quant", "min_ctx", "max_params", "profile", "context", "concurrency", "sort"]) if (f[k]) p.set(k, f[k]);
    if (f.view === "fit") p.set("fit", "1");
    if (f.view === "installed") p.set("installed", "1");
    return p.toString();
  };

  this.mount = async () => {
    view.innerHTML = `<div class="page"><h1>Models</h1><p class="sub">Pick the exact model each AI task uses: sized to this PC, tested before it goes live, one click back.</p>
      <div class="card" style="padding:0"><div class="tabs">${[["catalog", "Choose model"], ["tasks", "Tasks & releases"], ["hardware", "This PC"]]
        .map(([k, l]) => `<button data-mtab="${k}" class="${M.tab === k ? "on" : ""}">${l}</button>`).join("")}</div>
      <div class="tab-body" id="mtab" style="padding:14px"></div></div><div id="mjobs" style="margin-top:10px"></div></div>`;
    view.onclick = (e) => { const t = e.target.closest("[data-mtab]"); if (t) { M.tab = t.dataset.mtab; save(); this.mount(); } };
    await ({ catalog: catalogTab, tasks: tasksTab, hardware: hardwareTab }[M.tab] || catalogTab)();
  };
  this.every = 1500;
  this.refresh = async () => { // pulls, benchmarks and measurements run in the background
    const j = await api("/api/models/jobs");
    if (!$("#mjobs")) return;
    const pulls = j.pulls.filter((p) => p.state === "running" || p.state === "failed"), jobs = j.jobs.filter((x) => x.state !== "done");
    $("#mjobs").innerHTML = pulls.map((p) => `<div class="card row wrap" style="gap:10px;margin-bottom:6px"><b>Installing ${esc(p.name)}</b><span class="dim">${esc(p.status)}</span>
        <div class="grow">${bar(p.total ? p.completed / p.total : 0)}</div>${p.error ? `<span style="color:var(--err)">${esc(p.error)}</span>` : `<button class="btn sm" data-cancel="${esc(p.name)}">Cancel</button>`}</div>`).join("")
      + jobs.map((x) => `<div class="card" style="margin-bottom:6px">${pill(JOB_STATUS, x.state)} ${esc(x.key.replace(/^bench:/, "Benchmark ").replace(/^measure:/, "Measure "))} ${x.error ? `<span style="color:var(--err)">${esc(x.error)}</span>` : ""}</div>`).join("");
    $$("[data-cancel]").forEach((b) => (b.onclick = () => api("/api/models/pull/cancel", { body: { name: b.dataset.cancel } }).then(() => toast("Canceling…")).catch(fail)));
  };
  const install = (name, size) => api("/api/models/pull", { body: { name, size_bytes: size } }).then(() => { toast(`Installing ${name} with Ollama…`); this.refresh(); }).catch(fail);

  // ---------- Choose model: catalog, filters, compare ----------
  async function catalogTab() {
    const f = M.f, hw = await api("/api/models/hardware");
    const opt = (pairs, cur) => pairs.map(([v, l]) => `<option value="${esc(v)}" ${String(cur ?? "") === v ? "selected" : ""}>${esc(l)}</option>`).join("");
    $("#mtab").innerHTML = `<div class="stack">
      <div class="row wrap" style="gap:8px"><input class="grow" id="m-q" placeholder="Search by name, tag, family or Hub repo (typos are fine)" value="${esc(f.q || "")}">
        <div class="seg">${[["all", "All models"], ["fit", "Fits my hardware"], ["installed", "Installed"]].map(([k, l]) => `<button type="button" data-view="${k}" class="${(f.view || "all") === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
      <div class="row wrap" style="gap:8px;font-size:12.5px">
        <label>Task <select data-f="task">${opt([["", "any"], ["text-generation", "text (clip finding)"], ["image-text-to-text", "vision (frames)"], ["feature-extraction", "embeddings"], ["automatic-speech-recognition", "speech to text"]], f.task)}</select></label>
        <label>Accepts <select data-f="input">${opt([["", "any"], ["image", "images"], ["audio", "audio"]], f.input)}</select></label>
        <label>Generates <select data-f="output">${opt([["", "any"], ["text", "text"], ["image", "images"], ["embedding", "embeddings"]], f.output)}</select></label>
        <label>Quant <input data-f="quant" size="7" value="${esc(f.quant || "")}" placeholder="Q4_K_M"></label>
        <label>Min context <input data-f="min_ctx" type="number" style="width:90px" value="${esc(f.min_ctx || "")}"></label>
        <label>Max size (B params) <input data-f="max_params" type="number" style="width:70px" value="${esc(f.max_params || "")}"></label>
        <label>Sort <select data-f="sort">${opt([["relevance", "relevance"], ["name", "name"], ["params", "parameters"], ["size", "size"], ["context", "context"], ["downloads", "downloads"]], f.sort)}</select></label></div>
      <div class="row wrap" style="gap:8px;font-size:12.5px"><span class="dim">Fit for</span>
        <label>Profile <select data-f="profile">${opt(hw.profiles.map((p) => [p.name, `${p.name} (−${p.reserve_gib} GiB)`]), f.profile || hw.active)}</select></label>
        <label>Context <input data-f="context" type="number" style="width:90px" value="${esc(f.context || 8192)}"></label>
        <label>Parallel requests <input data-f="concurrency" type="number" min="1" style="width:55px" value="${esc(f.concurrency || 1)}"></label>
        <span class="grow"></span>
        <button class="btn sm" id="m-ollama">Refresh Ollama</button><button class="btn sm" id="m-hf">Search Hugging Face</button>
        <button class="btn sm" id="m-hfnext" ${M.hfNext ? "" : "hidden"}>More from the Hub</button><button class="btn sm" id="m-gguf">Import GGUF…</button></div>
      <div id="m-cmp"></div><div id="m-res"><span class="dim">Loading…</span></div></div>`;
    let t;
    $("#m-q").oninput = (e) => { clearTimeout(t); t = setTimeout(() => { f.q = e.target.value; save(); results(); }, 250); };
    $$("[data-view]").forEach((b) => (b.onclick = () => { f.view = b.dataset.view; save(); catalogTab(); }));
    $$("[data-f]").forEach((el) => (el.onchange = () => { f[el.dataset.f] = el.value; save(); results(); compare(); }));
    $("#m-ollama").onclick = () => api("/api/models/sync", { body: { source: "ollama" } }).then((r) => { toast(`${r.count} installed models read.`, "ok"); results(); }).catch(fail);
    const hf = (next) => api("/api/models/sync", { body: { source: "hf", search: f.q || "", pipeline_tag: f.task || "", next: next || "", limit: 30 } })
      .then((r) => { M.hfNext = r.next; $("#m-hfnext").hidden = !r.next; toast(`${r.count} Hub variants · synced ${when(r.synced_at)}`, "ok"); results(); }).catch(fail);
    $("#m-hf").onclick = () => hf();
    $("#m-hfnext").onclick = () => hf(M.hfNext);
    $("#m-gguf").onclick = () => modal(`<h2>Import a local GGUF</h2><p class="muted">Reelwright reads the file's header (architecture, layers, context) to estimate its fit. Nothing is copied.</p>
      <label class="field">Path to the .gguf file<input id="gg-path" placeholder="D:\\models\\my-model.Q4_K_M.gguf"></label>
      <div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="gg-ok">Import</button></div>`,
      (m, close) => ($("#gg-ok", m).onclick = () => api("/api/models/import", { body: { path: $("#gg-path", m).value } })
        .then((r) => { close(); toast(`Imported ${r.variant.name}.`, "ok"); results(); }).catch(fail)));
    await Promise.all([results(), compare()]);
  }

  async function results() {
    const r = await api(`/api/models/catalog?${qs()}`), p = r.profile;
    const gpuLine = p.gpus?.length ? p.gpus.map((g) => `GPU ${g.index}: ${g.free_gib.toFixed(1)} of ${g.total_gib.toFixed(1)} GiB free, budget ${g.budget_gib.toFixed(1)} GiB`).join(" · ")
      : `GPU memory: ${p.telemetry === "denied" ? "telemetry denied" : "no NVIDIA reading"} (fit shows as unknown)`;
    const row = (v) => `<tr><td><b>${esc(v.name)}</b><div class="dim mono" style="font-size:11px">${esc(v.source)} · ${esc((v.digest || v.revision || "not pinned until installed").replace(/^sha256:/, "").slice(0, 16))}</div></td>
      <td>${esc(v.quant || "—")}</td><td>${v.params_b ? +v.params_b.toFixed(2) + " B" : "—"}</td><td>${v.size_bytes ? (v.size_bytes / 1073741824).toFixed(1) + " GiB" : "—"}</td><td>${v.context ? num(v.context) : "—"}</td>
      <td>${(v.inputs || []).includes("image") ? `<span class="chip" title="accepts images">images in</span> ` : ""}${(v.outputs || []).includes("image") ? `<span class="chip" title="generates images">images out</span> ` : ""}${v.installed ? `<span class="chip">installed</span>` : ""}</td>
      <td>${fitLine(v.fit)}</td><td style="white-space:nowrap"><button class="btn sm" data-open="${esc(v.id)}">Details</button> <button class="btn sm" data-pin="${esc(v.id)}">${M.pins.includes(v.id) ? "Unpin" : "Pin"}</button>`
      + `${v.pull && !v.installed ? ` <button class="btn sm" data-pull="${esc(v.pull)}" data-size="${+v.size_bytes || ""}">Install</button>` : ""}</td></tr>`;
    $("#m-res").innerHTML = `<div class="dim" style="font-size:12px;margin-bottom:8px">${r.count} variants · ${gpuLine}${p.stale ? " · stale reading" : ""}${r.hf ? ` · Hub last synced ${when(r.hf)}` : ""}</div>`
      + (r.groups.length ? r.groups.map((g) => `<div class="card" style="margin-bottom:10px;overflow-x:auto"><h3 style="margin:0 0 6px">${esc(g.family)} <span class="dim" style="font-size:12px;font-weight:400">${g.variants.length} variant${g.variants.length > 1 ? "s" : ""}</span></h3>
          <table><tbody>${g.variants.map(row).join("")}</tbody></table></div>`).join("")
        : `<div class="empty"><b>No models match</b>Clear a filter, switch to All models, or search the Hugging Face Hub.</div>`);
    $$("[data-open]").forEach((b) => (b.onclick = () => detail(b.dataset.open).catch(fail)));
    $$("[data-pin]").forEach((b) => (b.onclick = () => { const id = b.dataset.pin; M.pins = M.pins.includes(id) ? M.pins.filter((x) => x !== id) : [...M.pins, id].slice(-4); results(); compare(); }));
    $$("[data-pull]").forEach((b) => (b.onclick = () => install(b.dataset.pull, +b.dataset.size || null)));
  }

  async function compare() {
    if (M.pins.length < 2) return ($("#m-cmp").innerHTML = M.pins.length ? `<div class="dim" style="font-size:12px">Pin one more to compare side by side (2–4).</div>` : "");
    const r = await api("/api/models/compare", { body: { ids: M.pins, workload: wl(), profile: M.f.profile || "" } }).catch(fail);
    if (!r) return;
    $("#m-cmp").innerHTML = `<div class="card" style="overflow-x:auto"><h3 style="margin:0 0 6px">Compare</h3><table><thead><tr><th></th>${r.rows.map((x) => `<th>${esc(x.variant.name)}</th>`).join("")}</tr></thead><tbody>
      ${MODEL_FIELDS.map((fd) => `<tr><td class="dim">${fd[1]}</td>${r.rows.map((x) => `<td>${fieldVal(x.variant, fd)} ${srcTag(x.variant, fd[0])}</td>`).join("")}</tr>`).join("")}
      <tr><td class="dim">Fit (${esc(r.profile.name)})</td>${r.rows.map((x) => `<td>${fitLine(x.fit)}</td>`).join("")}</tr>
      <tr><td class="dim">Checks</td>${r.rows.map((x) => `<td style="font-size:12px"><ul style="margin:0;padding-left:16px">${checkList(x.fit)}</ul></td>`).join("")}</tr></tbody></table></div>`;
  }

  async function detail(id) {
    const r = await api(`/api/models/variant/${encodeURIComponent(id)}?${new URLSearchParams({ ...wl(), profile: M.f.profile || "" })}`);
    const v = r.variant, f = r.fit, tasks = (M.tasks || (M.tasks = await api("/api/models/tasks"))).tasks;
    modal(`<h2>${esc(v.name)}</h2><p class="dim mono" style="font-size:11.5px;word-break:break-all">${esc(v.id)}</p>${v.why ? `<p class="muted">${esc(v.why)}</p>` : ""}
      <table><tbody>${MODEL_FIELDS.map((fd) => `<tr><td class="dim">${fd[1]}</td><td>${fieldVal(v, fd)}</td><td>${srcTag(v, fd[0])}</td></tr>`).join("")}
        <tr><td class="dim">Accepts → generates</td><td>${esc((v.inputs || []).join(", "))} → ${esc((v.outputs || []).join(", "))}</td><td></td></tr>
        ${v.measured ? `<tr><td class="dim">Measured</td><td>${v.measured.vram_gib} GiB VRAM · ${v.measured.tokens_per_s ?? "—"} tok/s at ${num(v.measured.context)} ctx</td><td><span class="dim" style="font-size:11px">locally tested ${esc(v.measured.at.slice(0, 10))}</span></td></tr>` : ""}</tbody></table>
      <h3>Fit on this PC${f.workload ? ` (${esc(f.profile || "")}, ${num(f.workload.context)} ctx × ${f.workload.concurrency})` : ""}</h3><p>${fitLine(f)}</p>
      <ul style="margin:0;padding-left:18px;font-size:12.5px">${checkList(f)}</ul>
      <div class="row wrap" style="gap:8px;margin-top:12px">${v.provider === "ollama" && v.installed ? `<button class="btn" id="md-bench">Benchmark on this PC</button>` : ""}
        ${v.source === "hf" ? `<button class="btn" id="md-card">Load model card</button>` : ""}${v.pull && !v.installed ? `<button class="btn" id="md-pull">Install with Ollama</button>` : ""}</div>
      ${v.provider && (v.digest || v.revision) ? `<h3>Use it for a task</h3><div class="row wrap" style="gap:8px"><select id="md-task">${tasks.map((t) => `<option value="${t.task}">${esc(t.label)}</option>`).join("")}</select>
        <select id="md-mode"><option value="existing">as is (existing)</option><option value="scratch">as the base of a trained specialist (scratch)</option></select><button class="btn primary" id="md-use">Propose as candidate</button></div>`
        : `<p class="dim" style="font-size:12.5px">To select it for a task, install it first: selections pin the installed digest, never a name.</p>`}
      <div class="foot"><button class="btn" data-close>Close</button></div>`, (m, close) => {
      $("#md-bench", m)?.addEventListener("click", () => api("/api/models/benchmark", { body: { id: v.id, context: wl().context } })
        .then(() => { close(); toast("Benchmark running: loads the model, runs a prompt, reads the VRAM it took."); }).catch(fail));
      $("#md-card", m)?.addEventListener("click", () => api("/api/models/card", { body: { id: v.id } }).then(() => { close(); detail(id).catch(fail); }).catch(fail));
      $("#md-pull", m)?.addEventListener("click", () => { close(); install(v.pull, v.size_bytes || null); });
      $("#md-use", m)?.addEventListener("click", () => api("/api/models/propose", { body: { task: $("#md-task", m).value, id: v.id, mode: { kind: $("#md-mode", m).value }, workload: wl() } })
        .then((s) => { close(); M.tasks = null; toast(`Candidate v${s.version} for ${s.task} saved: review its gates under Tasks & releases.`, "ok", 6000); }).catch(fail));
    }, true);
  }

  // ---------- Tasks & releases ----------
  async function tasksTab() {
    const r = M.tasks = await api("/api/models/tasks");
    const short = (s) => esc(String(s || "").replace(/^sha256:/, "").slice(0, 12));
    const selLine = (s) => s ? `<b>${esc(s.provider || "—")}</b> · ${esc(s.model || "—")}${s.digest ? ` <span class="mono dim" style="font-size:11px">@${short(s.digest)}</span>` : ""}${s.origin === "settings" ? ` <span class="chip">follows Settings → AI</span>` : ""}` : "—";
    const selRow = (s, rel, task) => `<tr><td><input type="checkbox" data-pick="${s.id}"></td>
      <td>v${s.version}${s.id === rel.active ? ` <span class="pill ok">active</span>` : ""}${s.id === rel.desired && s.id !== rel.active ? ` <span class="chip">desired</span>` : ""}<div class="dim" style="font-size:11px">${esc(s.mode.kind)}</div></td>
      <td style="font-size:12.5px">${selLine(s)}${s.fit?.status ? `<div>${fitLine(s.fit)}</div>` : ""}${s.choice?.override_reason ? `<div class="dim" style="font-size:11px">override: ${esc(s.choice.override_reason)}</div>` : ""}</td>
      <td style="font-size:11.5px">${Object.entries(s.gates || {}).map(([g, st]) => `${pill(GATE_STATUS, st)} ${g}`).join("<br>") || "—"}</td>
      <td style="font-size:12px">${s.metrics?.at ? `quality ${s.metrics.quality ?? "—"} · support ${s.metrics.support ?? "—"} · ${s.metrics.latency} s<div class="dim" style="font-size:11px">${esc(s.metrics.at.slice(0, 10))}</div>` : "—"}</td>
      <td>${s.score ?? "—"}</td><td style="white-space:nowrap">${s.origin !== "settings" ? `<button class="btn sm" data-measure="${s.id}">Measure</button> ` : ""}${s.id !== rel.active ? `<button class="btn sm primary" data-activate="${s.id}" data-t="${task}" data-rev="${rel.rev}">Activate</button>` : ""}</td></tr>`;
    $("#mtab").innerHTML = `<p class="muted" style="margin-top:0">Each task keeps its desired selection, the candidate you last tried and the active release apart. A candidate that fails a gate leaves the active release running; Roll back returns to the one before.</p>`
      + r.tasks.map((t) => {
        const rel = t.release, byId = Object.fromEntries(t.selections.map((s) => [s.id, s]));
        return `<div class="card" style="margin-bottom:12px;overflow-x:auto" data-task="${t.task}"><div class="row wrap"><h3 class="grow" style="margin:0">${esc(t.label)} <span class="dim" style="font-size:12px;font-weight:400">rev ${rel.rev}</span></h3>
          ${rel.history.length > 1 ? `<button class="btn sm" data-rollback="${t.task}" data-rev="${rel.rev}">Roll back</button>` : ""}</div>
          <div style="font-size:13px;margin:6px 0">Active: ${selLine(byId[rel.active])}${rel.candidate && rel.candidate_status === "failed" ? ` · <span style="color:var(--err)">candidate v${byId[rel.candidate]?.version ?? "?"} failed its gates</span>` : ""}</div>
          <table><thead><tr><th></th><th>Version</th><th>Model</th><th>Gates</th><th>Measured</th><th>Score</th><th></th></tr></thead><tbody>${t.selections.map((s) => selRow(s, rel, t.task)).join("")}</tbody></table>
          <div class="row" style="gap:8px;margin-top:8px"><button class="btn sm" data-choose="${t.task}">Help me choose (ticked versions)</button></div><div data-chosen></div></div>`;
      }).join("");
    $$("[data-rollback]").forEach((b) => (b.onclick = () => api("/api/models/rollback", { body: { task: b.dataset.rollback, rev: +b.dataset.rev } }).then(() => { toast("Rolled back.", "ok"); tasksTab(); }).catch(fail)));
    $$("[data-activate]").forEach((b) => (b.onclick = () => activate(b.dataset.t, b.dataset.activate, +b.dataset.rev)));
    $$("[data-measure]").forEach((b) => (b.onclick = () => measure(b.dataset.measure)));
    $$("[data-choose]").forEach((b) => (b.onclick = async () => {
      const card = b.closest("[data-task]"), ids = $$("[data-pick]:checked", card).map((c) => c.dataset.pick);
      if (ids.length < 2) return toast("Tick 2 or more versions to compare.");
      const c = await api("/api/models/choose", { body: { task: card.dataset.task, ids } }).catch(fail);
      if (!c) return;
      const gateNames = Object.keys(c.rows.find((s) => Object.keys(s.gates || {}).length)?.gates || {});
      $("[data-chosen]", card).innerHTML = `<table style="margin-top:8px"><thead><tr><th>Rank</th><th>Version</th>${gateNames.map((g) => `<th>${g}</th>`).join("")}${c.metrics.map((m) => `<th title="worst ${m.worst} → best ${m.best}, weight ${m.weight}">${m.name}</th>`).join("")}<th>Score</th></tr></thead><tbody>
        ${c.rows.map((s) => `<tr><td>${s.rank ?? "—"}</td><td>v${s.version} · ${esc(s.model)}</td>${gateNames.map((g) => `<td>${pill(GATE_STATUS, (s.gates || {})[g] || "unmeasured")}</td>`).join("")}${c.metrics.map((m) => `<td>${s.metrics?.[m.name] ?? "unmeasured"}</td>`).join("")}<td><b>${s.score ?? "no rank"}</b></td></tr>`).join("")}</tbody></table>
        <div class="dim" style="font-size:11.5px">Score = 100 × Σ weight × (value − worst) / (best − worst). A version without measurements gets no rank: Measure it first.</div>`;
    }));
  }
  async function activate(task, id, rev, reason = "") {
    try {
      const r = await api("/api/models/activate", { body: { task, candidate: id, rev, override_reason: reason } });
      if (r.ok) { toast(`Activated: ${task} now uses this model.`, "ok"); return tasksTab(); }
      tasksTab();
      modal(`<h2>Gates failed</h2><p class="muted">The active release stays in place. Failing: <b>${esc(r.failed_gates.join(", "))}</b>. To activate anyway, say why (it's saved on the selection).</p>
        <label class="field">Override reason<input id="ov-r" placeholder="e.g. I close OBS before running this"></label>
        <div class="foot"><button class="btn" data-close>Keep the current release</button><button class="btn primary" id="ov-ok">Activate anyway</button></div>`,
        (m, close) => ($("#ov-ok", m).onclick = () => { const why = $("#ov-r", m).value.trim(); if (!why) return toast("Give a reason, or keep the current release."); close(); activate(task, id, r.release.rev, why); }));
    } catch (e) { fail(e); if (e.status === 409) tasksTab(); }
  }
  async function measure(id, confirm = false) {
    const r = await api("/api/models/measure", { body: { selection: id, confirm } }).catch(fail);
    if (!r) return;
    if (r.needs_confirm) return modal(`<h2>This benchmark costs money</h2><p class="muted">It runs the golden eval cases through a paid API. ${esc(r.cost.line)}</p>
      <div class="foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="ms-ok">Run it</button></div>`, (m, close) => ($("#ms-ok", m).onclick = () => { close(); measure(id, true); }));
    toast("Measuring through the eval cases: open Tasks & releases again when it's done.");
  }

  // ---------- This PC: hardware profile ----------
  async function hardwareTab() {
    const r = await api("/api/models/hardware"), p = r.profile;
    const prow = (x, on) => `<div class="row" style="gap:8px;margin-bottom:6px"><input type="radio" name="hw-act" ${on ? "checked" : ""}><input data-pn value="${esc(x.name)}"><label>reserve <input data-pr type="number" step="0.5" min="0" style="width:70px" value="${+x.reserve_gib}"> GiB</label><button class="btn sm" data-del>✕</button></div>`;
    $("#mtab").innerHTML = `<div class="stack">
      <div class="card"><h3 style="margin:0 0 6px">GPUs <span class="dim" style="font-size:12px;font-weight:400">read ${when(new Date(p.at * 1000).toISOString())}${p.stale ? " · stale" : ""} · driver ${esc(p.driver || "—")}</span></h3>
        ${p.gpus.length ? `<table><thead><tr><th>GPU</th><th>Total</th><th>Used</th><th>Free</th><th>Budget (${esc(p.name)}, −${p.reserve_gib} GiB)</th></tr></thead><tbody>${p.gpus.map((g) => `<tr><td>${g.index}: ${esc(g.name)}</td><td>${g.total_gib.toFixed(1)} GiB</td><td>${g.used_gib.toFixed(1)}</td><td>${g.free_gib.toFixed(1)}</td><td><b>${g.budget_gib.toFixed(1)} GiB</b></td></tr>`).join("")}</tbody></table>`
          : `<p class="dim">${p.telemetry === "denied" ? "GPU telemetry was denied (nvidia-smi refused): fits show as unknown." : "No NVIDIA GPU reading (nvidia-smi not found): fits show as unknown."}</p>`}
        <p style="font-size:12.5px;margin:8px 0 0">RAM ${p.ram_free_gib?.toFixed(1) ?? "—"} of ${p.ram_total_gib?.toFixed(1) ?? "—"} GiB free · disk for models ${p.disk_free_gib?.toFixed(0) ?? "—"} GiB free${p.loaded?.length ? ` · loaded in Ollama: ${p.loaded.map((l) => `${esc(l.name)} (${l.size_vram_gib.toFixed(1)} GiB)`).join(", ")}` : ""}</p></div>
      <div class="card"><h3 style="margin:0 0 6px">Profiles</h3><p class="muted" style="font-size:12.5px;margin:0 0 8px">A profile holds VRAM back for what else runs (a game, OBS). Budget = free − reserve. The selected one is the default for fit estimates.</p>
        <div id="hw-ps">${r.profiles.map((x) => prow(x, x.name === r.active)).join("")}</div>
        <div class="row" style="gap:8px"><button class="btn sm" id="hw-add">Add profile</button><button class="btn sm primary" id="hw-save">Save profiles</button></div></div></div>`;
    $("#hw-add").onclick = () => $("#hw-ps").insertAdjacentHTML("beforeend", prow({ name: "New", reserve_gib: 0 }, false));
    $("#hw-ps").onclick = (e) => e.target.closest("[data-del]")?.parentElement.remove();
    $("#hw-save").onclick = () => {
      const rows = $$("#hw-ps > .row"), profiles = rows.map((x) => ({ name: $("[data-pn]", x).value, reserve_gib: +$("[data-pr]", x).value || 0 }));
      const i = rows.findIndex((x) => $("input[type=radio]", x).checked);
      api("/api/models/hardware", { method: "PUT", body: { profiles, active: profiles[Math.max(0, i)]?.name } }).then(() => { toast("Profiles saved.", "ok"); hardwareTab(); }).catch(fail);
    };
  }
}

// ======================================================================
// Model Lab — a local "specialist" that learns the creator's clip judgment (training/, routes_training.py): the dataset
// from reviewed decisions, the external training env, the model designer, jobs, candidates with scorecards, promote/rollback.
// ======================================================================
routes.modellab = ModelLab;
function ModelLab() {
  this.every = 3000;
  let st = null, ds = null, checkT = 0;
  const num = (id) => Number($(id)?.value);
  const pct = (v) => (v == null ? "—" : (v * 100).toFixed(0) + "%");
  const spark = (m) => {
    const pts = m.filter((x) => x.loss != null).slice(-120);
    if (pts.length < 2) return "";
    const lo = Math.min(...pts.map((p) => p.loss)), hi = Math.max(...pts.map((p) => p.loss)) || 1;
    const xy = pts.map((p, i) => `${(i / (pts.length - 1)) * 160},${30 - ((p.loss - lo) / (hi - lo || 1)) * 28}`).join(" ");
    return `<svg width="160" height="32" aria-label="loss curve"><polyline points="${xy}" fill="none" stroke="var(--accent, #ff9a3c)" stroke-width="1.5"/></svg>`;
  };
  const design = () => {
    const mode = $("#ml-mode").value;
    const b = { mode, target: $("#ml-target").value.trim(), pilot: { tokens: num("#ml-tokens"), seq: num("#ml-seq"), micro: num("#ml-micro"), accum: num("#ml-accum"), lr: num("#ml-lr") } };
    if (mode === "scratch") b.arch = Object.fromEntries(["vocab", "layers", "width", "mlp", "heads", "kv_heads", "context", "seed"].map((k) => [k, num(`#ml-a-${k}`)]).concat([["tied", $("#ml-a-tied").checked], ["tokenizer", $("#ml-a-tok").value]]));
    else {
      const [src, ...rest] = $("#ml-base").value.split("|");
      b.base = src === "ollama" ? { source: "ollama", name: rest.join("|") } : { source: "hf", repo: $("#ml-repo").value.trim(), revision: $("#ml-rev").value.trim() || "main" };
      b.recipe = Object.fromEntries(["r", "alpha", "dropout", "lr", "epochs"].map((k) => [k, num(`#ml-r-${k}`)]));
    }
    return b;
  };
  const check = () => {
    clearTimeout(checkT);
    checkT = setTimeout(async () => {
      const out = $("#ml-check");
      if (!out) return;
      try {
        const d = await api("/api/training/design", { body: design() });
        const p = d.pilot, e = d.estimate;
        out.innerHTML = `${d.params ? `<div><b>${d.params.toLocaleString()}</b> parameters</div>` : d.params_b ? `<div>Base: ~${d.params_b}B parameters</div>` : ""}
          ${d.errors.length ? `<ul style="color:var(--err);margin:4px 0;padding-left:18px">${d.errors.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div style="color:var(--ok)">✓ Valid</div>`}
          ${p ? `<div class="muted" style="font-size:12.5px">${p.tokens_per_update.toLocaleString()} tokens/update · ${p.updates.toLocaleString()} updates · ${p.tok_s.toLocaleString()} tok/s · ${p.hours.toFixed(2)} h · ${p.vram_gb} GB VRAM · ${p.disk_gb} GB disk — <b>${esc(p.label)}</b>${p.label === "estimated" ? " (run a pilot to measure)" : ""}</div>` : ""}
          ${e ? `<div class="muted" style="font-size:12.5px">~${e.vram_gb} GB VRAM · ~${e.disk_gb} GB disk — <b>${esc(e.label)}</b></div>` : ""}
          <div style="font-size:12.5px">Fit on ${esc(d.profile.host)}${d.profile.gpu ? ` (${esc(d.profile.gpu)})` : ""}: ${d.fit.fits === true ? "✓" : d.fit.fits === false ? "✗" : "?"} ${esc(d.fit.notes.join(" "))}</div>
          ${d.profile.target ? `<div class="muted" style="font-size:12px">${esc(d.profile.target.warning)}</div>` : ""}
          ${d.license ? `<div style="font-size:12px">Licence: <b>${esc(d.license)}</b></div>` : d.mode !== "scratch" ? `<div style="font-size:12px">Licence: check the base model's card before training.</div>` : ""}`;
      } catch (err) { out.innerHTML = `<span style="color:var(--err)">${esc(err.message)}</span>`; }
    }, 300);
  };
  const envCard = (env) => `<h2>Training environment</h2>
    <p class="muted" style="font-size:12.5px;margin-top:-6px">PyTorch, transformers and PEFT live in their own folder (several GB), never inside Reelwright. Pinned: ${esc(env.pins.join(", "))}; llama.cpp ${esc(env.llama_cpp_tag)}.</p>
    <div>${env.ready ? `✓ Ready: torch ${esc(env.torch)}, CUDA ${env.cuda ? esc(env.cuda_version) + " on " + esc(env.gpu) : "<b>not available</b> (CPU only)"}${env.tools ? " · llama.cpp tools ✓" : " · llama.cpp tools missing"}` : "Not set up yet."}</div>
    <div class="dim" style="font-size:12px">${esc(env.wsl ? "WSL: " : "")}${esc(env.python.join(" "))}</div>
    <label class="field">Folder<input type="text" id="ml-envdir" placeholder="${esc(env.root)}"></label>
    <div><button class="btn primary" id="ml-setup">Set up training environment</button></div>
    <details><summary style="cursor:pointer;font-size:13px">Use an existing env or WSL2</summary>
      <label class="field">Python of the env, or a WSL command<input type="text" id="ml-py" placeholder="C:\\envs\\train\\Scripts\\python.exe  or  wsl -d Ubuntu -- /home/me/rw/bin/python"></label>
      <label style="font-size:13px"><input type="checkbox" id="ml-wsl"> It's a WSL command</label> <button class="btn sm" id="ml-use">Use it</button></details>`;
  const dataCard = () => `<h2>Dataset</h2>
    <p class="muted" style="font-size:12.5px;margin-top:-6px">Examples come from your reviewed decisions (👍, 👎, published, your trims and published titles), in the exact prompt shape the app uses. Nothing is used until you confirm it.</p>
    <label style="font-size:13px"><input type="checkbox" id="ml-consent" ${st.consent.all ? "checked" : ""}> Use my reviewed decisions from all projects</label>
    ${st.consent.all ? "" : `<details><summary style="cursor:pointer;font-size:13px">Or pick projects (${st.consent.projects.length})</summary>${st.projects.map((p) => `<label style="display:block;font-size:13px"><input type="checkbox" data-mlproj="${p.id}" ${st.consent.projects.includes(p.id) ? "checked" : ""}> ${esc(p.name)}</label>`).join("")}</details>`}
    <div><button class="btn" id="ml-build">Build dataset</button></div>
    <div id="ml-ds">${ds ? dsView(ds) : st.dataset ? `<span class="dim">Last build ${esc(st.dataset.id.slice(0, 12))}: ${st.dataset.counts.sft} examples</span>` : ""}</div>
    <h3 style="margin:10px 0 4px;font-size:14px">Claude as teacher</h3>
    <p class="muted" style="font-size:12.5px;margin:0">Claude labels confirmed projects through the app's own clip finder (paid; your own picks always win).</p>
    <select id="ml-tproj" multiple size="3" style="width:100%">${st.projects.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
    <div><button class="btn sm" id="ml-teach">Estimate cost…</button></div>`;
  const dsView = (b) => `<div style="font-size:13px">Dataset <b>${esc(b.hash.slice(0, 12))}</b>: ${b.counts.sft} examples (train ${b.counts.train} / dev ${b.counts.dev} / test ${b.counts.test}) · find ${b.counts.by_task.find}, judge ${b.counts.by_task.judge}, meta ${b.counts.by_task.meta} · ${b.counts.dpo} DPO pairs · ${b.counts.teacher} teacher${b.stats ? ` · ${b.stats.excluded_examples} excluded, ${b.stats.deleted_clips_skipped} deleted clips left out` : ""}</div>
    ${b.note ? `<div class="muted" style="font-size:12.5px">${esc(b.note)}</div>` : ""}
    ${b.counts.sft ? `<details><summary style="cursor:pointer;font-size:13px">Browse examples</summary><div id="ml-ex" class="dim">Loading…</div></details>` : ""}`;
  const designCard = () => `<h2>Design the model</h2>
    <p class="muted" style="font-size:12.5px;margin-top:-6px">Fit it to this PC (or a host on your network). Fine-tune an existing base with LoRA/QLoRA, or build a small Llama-style model from scratch.</p>
    <label class="field">Approach<select id="ml-mode"><option value="scratch">From scratch</option><option value="qlora">QLoRA (4-bit NF4) on a base</option><option value="lora">LoRA on a base</option></select></label>
    <div id="ml-scratch" class="cols" style="grid-template-columns:repeat(4,1fr);gap:6px">
      ${[["vocab", "Vocab"], ["layers", "Layers"], ["width", "Width"], ["mlp", "MLP width"], ["heads", "Heads"], ["kv_heads", "KV heads"], ["context", "Context"], ["seed", "Seed"]].map(([k, l]) => `<label class="field">${l}<input type="number" min="1" id="ml-a-${k}" value="${st.defaults.arch[k]}"></label>`).join("")}
      <label style="font-size:13px;align-self:end"><input type="checkbox" id="ml-a-tied" checked> Tied embeddings</label>
      <label class="field" style="grid-column:span 3">Tokenizer<select id="ml-a-tok"><option value="bpe">Train a new BPE on the dataset</option>${Object.entries(st.defaults.tokenizers).map(([k, t]) => `<option value="${esc(k)}">${esc(k)} (pinned, ${t.vocab} tokens, ${esc(t.license)})</option>`).join("")}</select></label></div>
    <div id="ml-lora" hidden>
      <label class="field">Base<select id="ml-base"><option value="hf|">Hugging Face repo (type below)</option></select></label>
      <div class="cols" style="grid-template-columns:2fr 1fr;gap:6px"><label class="field">Repo<input type="text" id="ml-repo" placeholder="Qwen/Qwen2.5-0.5B-Instruct"></label><label class="field">Revision<input type="text" id="ml-rev" value="main"></label></div>
      <div class="cols" style="grid-template-columns:repeat(5,1fr);gap:6px">${[["r", 16], ["alpha", 32], ["dropout", 0.05], ["lr", 0.0001], ["epochs", 1]].map(([k, v]) => `<label class="field">${k}<input type="number" step="any" id="ml-r-${k}" value="${v}"></label>`).join("")}</div></div>
    <div class="cols" style="grid-template-columns:repeat(5,1fr);gap:6px">${[["tokens", 50000000, "Tokens"], ["seq", 1024, "Sequence"], ["micro", 2, "Micro-batch"], ["accum", 8, "Grad. accum."], ["lr", 0.0006, "Learning rate"]].map(([k, v, l]) => `<label class="field">${l}<input type="number" step="any" id="ml-${k}" value="${v}"></label>`).join("")}</div>
    <label class="field">Target host <span class="muted" style="font-size:12px">(blank = this PC; or a remote Ollama / Reelwright URL)</span><input type="text" id="ml-target" placeholder="http://192.168.1.20:11434"></label>
    <div id="ml-check" class="stack" style="gap:2px"></div>
    <div style="display:flex;gap:8px"><button class="btn" id="ml-pilot">Run 20-step pilot</button><button class="btn primary" id="ml-train">Train</button></div>`;
  const jobsHtml = () => `<h2>Jobs</h2>${st.blocked ? `<div style="color:var(--warn, #e6b34a);font-size:13px">⏸ ${esc(st.blocked)}</div>` : ""}
    ${st.jobs.length ? st.jobs.map((j) => {
      const l = j.last || {}, r = j.result || {};
      return `<div class="card" style="padding:10px;margin-top:8px"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <b>${esc(j.kind)}</b> <span class="pill">${esc(j.status)}</span> <span class="dim" style="font-size:12.5px">${esc(j.message || "")}</span>
        ${l.step ? `<span style="font-size:12.5px">step ${esc(l.step)}/${esc(l.of)} · loss ${esc(l.loss)} · ${l.tok_s ? l.tok_s.toLocaleString() + " tok/s" : "…"}</span>` : ""}${spark(j.metrics || [])}
        <span style="margin-left:auto;display:flex;gap:6px">
        ${j.status === "running" ? `<button class="btn sm" data-mljob="pause" data-id="${j.id}">Pause</button>` : ""}
        ${j.status === "paused" ? `<button class="btn sm" data-mljob="resume" data-id="${j.id}">Resume</button>` : ""}
        ${["queued", "running", "paused"].includes(j.status) ? `<button class="btn sm" data-mljob="cancel" data-id="${j.id}">Cancel</button>` : ""}
        ${j.kind === "train" && j.status === "done" ? `<label style="font-size:12px"><input type="checkbox" data-mlpaid="${j.id}"> also score Claude (paid)</label><button class="btn sm primary" data-mljob="export" data-id="${j.id}">Export &amp; evaluate</button>` : ""}</span></div>
        ${r.tok_s && j.kind === "pilot" ? `<div style="font-size:12.5px">Measured: <b>${r.tok_s.toLocaleString()} tok/s</b>, peak VRAM <b>${esc(r.peak_vram_gb)} GB</b> on ${esc(r.device)}</div>` : ""}
        ${j.events?.tokenizer && j.events.tokenizer.vocab !== j.events.tokenizer.requested ? `<div class="muted" style="font-size:12px">The tokenizer learned ${j.events.tokenizer.vocab} of ${j.events.tokenizer.requested} tokens (small dataset).</div>` : ""}
        ${j.error ? `<div style="color:var(--err);font-size:12.5px">${esc(j.error)}</div>` : ""}
        ${(j.log || []).length ? `<details><summary style="cursor:pointer;font-size:12px">Log</summary><pre style="font-size:11px;max-height:160px;overflow:auto;white-space:pre-wrap">${esc(j.log.slice(-40).join("\n"))}</pre></details>` : ""}</div>`;
    }).join("") : `<div class="dim">No jobs yet.</div>`}`;
  const card = (c) => c ? `${pct(c.test_summary?.precision_at_k)} test · ${pct(c.evals_summary?.precision_at_k)} evals (IoU ${esc(c.evals_summary?.iou ?? "—")})` : "not scored";
  const relHtml = () => `<h2>Candidates</h2>
    <div style="font-size:13px">Active for finding clips: <b>${esc(st.active.find.provider)}:${esc(st.active.find.model)}</b> <button class="btn sm" id="ml-rollback">Roll back</button></div>
    ${st.releases.length ? st.releases.map((r) => `<div class="card" style="padding:10px;margin-top:8px"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <b>${esc(r.id)}</b> <code>${esc(r.tag || "")}</code> <span class="pill">${esc(r.status)}</span>
      ${r.status === "candidate" ? `<span style="margin-left:auto;display:flex;gap:6px"><select data-mltask="${r.id}"><option>find</option><option>judge</option><option>meta</option></select><button class="btn sm primary" data-mlpromote="${r.id}">Promote</button></span>` : ""}</div>
      <div style="font-size:12.5px">Candidate: ${card(r.card)} · vs ${esc(r.active_model || "active")}: ${card(r.active_card)}</div>
      ${r.reason ? `<div class="muted" style="font-size:12.5px">${esc(r.reason)}</div>` : ""}
      ${r.model?.license ? `<div style="font-size:12px">Licence: ${esc(r.model.license)}</div>` : ""}
      <details><summary style="cursor:pointer;font-size:12px">Hashes</summary><pre style="font-size:11px;white-space:pre-wrap">${esc(JSON.stringify(r.hashes, null, 1))}</pre></details></div>`).join("") : `<div class="dim">Export a finished training job to get a candidate.</div>`}`;
  const loadExamples = async (h) => {
    const ex = await api(`/api/training/dataset/${h.slice(0, 16)}/examples`);
    $("#ml-ex").innerHTML = `<table style="font-size:12px;width:100%"><tr><th>Task</th><th>Split</th><th>By</th><th>Target</th><th>Use</th></tr>${ex.map((e) => `<tr><td>${esc(e.task)}</td><td>${esc(e.split)}</td><td>${esc(e.reviewer)}</td><td><details><summary>${esc(e.target.slice(0, 80))}…</summary><pre style="white-space:pre-wrap;font-size:11px">${esc(e.prompt)}\n\n→ ${esc(e.target)}</pre></details></td><td><input type="checkbox" data-mlex="${e.id}" ${e.excluded ? "" : "checked"} title="Use this example"></td></tr>`).join("")}</table>`;
  };
  this.mount = async () => {
    st = await api("/api/training/status");
    view.innerHTML = `<div class="page"><h1>Model Lab</h1><p class="sub">Train a local specialist on your own clip judgment, with Claude as its teacher. It only replaces the current model after it beats it on the same tests.</p>
      <div class="cols"><div class="stack"><div class="card stack" id="ml-env">${envCard(st.env)}</div><div class="card stack" id="ml-data">${dataCard()}</div></div>
      <div class="stack"><div class="card stack" id="ml-design">${designCard()}</div><div class="card" id="ml-jobs">${jobsHtml()}</div><div class="card" id="ml-rel">${relHtml()}</div></div></div></div>`;
    api("/api/training/bases").then((bs) => { $("#ml-base").innerHTML += bs.map((b) => `<option value="${b.source}|${esc(b.source === "hf" ? b.repo + "@" + b.revision : b.name)}">${esc(b.name)} — ${b.source === "hf" ? "trainable" : "Ollama (inference only)"}${b.license ? " · " + esc(b.license) : ""}</option>`).join(""); }).catch(() => {});
    check();
    view.oninput = (e) => { if (e.target.closest("#ml-design")) check(); };
    view.onchange = async (e) => {
      const t = e.target;
      if (t.id === "ml-mode") { $("#ml-scratch").hidden = t.value !== "scratch"; $("#ml-lora").hidden = t.value === "scratch"; return check(); }
      if (t.id === "ml-base") { const [src, v] = t.value.split("|"); if (src === "hf" && v) { const [repo, rev] = v.split("@"); $("#ml-repo").value = repo; $("#ml-rev").value = rev || "main"; } return check(); }
      if (t.id === "ml-consent") { await api("/api/training/consent", { body: { all: t.checked } }).catch(fail); st = await api("/api/training/status"); $("#ml-data").innerHTML = dataCard(); return; }
      if (t.dataset.mlproj) return api("/api/training/consent", { body: { project: t.dataset.mlproj, on: t.checked } }).catch(fail);
      if (t.dataset.mlex) return api(`/api/training/examples/${t.dataset.mlex}/exclude`, { body: { on: !t.checked } }).then(() => toast(t.checked ? "Example used again." : "Example excluded from the next build.")).catch(fail);
    };
    view.onclick = async (e) => {
      const t = e.target.closest("button, summary");
      if (!t) return;
      try {
        if (t.id === "ml-setup") { await api("/api/training/env/setup", { body: { folder: $("#ml-envdir").value.trim() } }); toast("Setting up: this downloads several GB and takes a while.", "ok"); }
        else if (t.id === "ml-use") { await api("/api/training/env/existing", { body: { python: $("#ml-py").value, wsl: $("#ml-wsl").checked } }); toast("Checking that environment…", "ok"); }
        else if (t.id === "ml-build") { ds = await api("/api/training/dataset/build", { body: {} }); $("#ml-ds").innerHTML = dsView(ds); }
        else if (t.tagName === "SUMMARY" && t.parentElement.querySelector("#ml-ex")) { const h = ds?.hash || st.dataset?.id; if (h) loadExamples(h); return; }
        else if (t.id === "ml-teach") {
          const projects = [...$("#ml-tproj").selectedOptions].map((o) => o.value);
          if (!projects.length) return toast("Pick one or more projects first.");
          const est = await api("/api/training/teacher", { body: { projects } });
          if (!est.parts) return toast("None of those projects is confirmed for training.");
          if (await confirmBox("Label with Claude?", `${est.line}. Claude's labels are added as "teacher" examples.`, "Spend it")) { await api("/api/training/teacher", { body: { projects, confirm: true } }); toast("Labelling started.", "ok"); }
        }
        else if (t.id === "ml-pilot" || t.id === "ml-train") { await api("/api/training/jobs", { body: { ...design(), kind: t.id === "ml-pilot" ? "pilot" : "train" } }); toast("Queued.", "ok"); }
        else if (t.dataset.mljob) { await api(`/api/training/jobs/${t.dataset.id}/${t.dataset.mljob}`, { body: { eval_active: !!$(`[data-mlpaid="${t.dataset.id}"]`)?.checked } }); }
        else if (t.dataset.mlpromote) { const r = await api(`/api/training/releases/${t.dataset.mlpromote}/promote`, { body: { task: $(`[data-mltask="${t.dataset.mlpromote}"]`).value } }); toast(r.reason, r.promoted ? "ok" : "err", 8000); }
        else if (t.id === "ml-rollback") { if (await confirmBox("Roll back?", "The task goes back to the model that was active before the last promotion.", "Roll back")) { const r = await api("/api/training/rollback", { body: { task: "find" } }); toast(`Back to ${r.active}.`, "ok"); } }
        else return;
        this.refresh();
      } catch (err) { fail(err); }
    };
  };
  this.refresh = async () => {
    st = await api("/api/training/status");
    // a card being used (focused control, ticked box, open log or hashes) keeps its state until the user is done with it
    const busy = (el) => el && (el.contains(document.activeElement) || !!$("details[open], input:checked", el) || $$("select", el).some((s) => s.selectedIndex > 0));
    if ($("#ml-jobs") && !busy($("#ml-jobs"))) $("#ml-jobs").innerHTML = jobsHtml();
    if ($("#ml-rel") && !busy($("#ml-rel"))) $("#ml-rel").innerHTML = relHtml();
    if ($("#ml-env") && !$("#ml-env").contains(document.activeElement)) $("#ml-env").innerHTML = envCard(st.env);
  };
}
