/* Reelwright phone remote — vanilla JS, no build step. Talks only to the PC that served it, through the signed
   /remote/api (remote.py). The pairing secret stays in this phone's localStorage and is never sent: every request is
   signed with it (hmac.js), and every answer must carry the PC's proof before it is used. */
"use strict";

// ---------- utilities ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;
const view = $("#view");
const C = window.RWCrypto;
const PLATFORM = { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram" };
const UPLOAD = { held: ["warn", "Held"], waiting_render: ["info", "Rendering first"], scheduled: ["warn", "Scheduled"], queued: ["info", "Queued"],
  uploading: ["run", "Uploading"], done: ["ok", "Posted"], failed: ["err", "Failed"], canceled: ["", "Canceled"], needs_auth: ["warn", "Needs sign-in"] };
const PROJECT = { importing: ["run", "Importing"], queued: ["info", "Queued"], analyzing: ["run", "Analyzing"], ready: ["ok", "Ready"], failed: ["err", "Failed"] };
const platTag = (p) => `<span class="plat ${esc(p)}">${esc(PLATFORM[p] || p)}</span>`;
const pill = (map, k) => { const [c, l] = map[k] || ["", k || "—"]; return `<span class="pill ${c}">${esc(l)}</span>`; };
const bar = (p) => `<div class="bar"><div style="width:${Math.round(Math.max(0, Math.min(1, +p || 0)) * 100)}%"></div></div>`;
const dur = (s) => { s = Math.max(0, Math.round(+s || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, "0"); return h ? `${h}:${String(m).padStart(2, "0")}:${x}` : `${m}:${x}`; };
const when = (iso) => (iso ? new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "As soon as it's rendered");
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" }); };
const gb = (n) => `${(n / 1e9).toFixed(n >= 1e11 ? 0 : 1)} GB`;
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const toLocalInput = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 16);
const tomorrow9 = () => { const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); return toLocalInput(d); };
const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const ls = {
  get(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch { return false; } },
  del(k) { try { localStorage.removeItem(k); } catch { /* private mode */ } },
};

// ---------- signed API ----------
const KEY = "rw-remote";
let pair = ls.get(KEY); // {id, secret, name, pc}
let clockOffset = 0;    // seconds to add to this phone's clock (set from the PC's when they disagree)
let stale = false;      // the PC refused this phone's key: wait for the user (Try again / pair) instead of polling
class Unpaired extends Error {}
const nonce = () => { const b = new Uint8Array(16); crypto.getRandomValues(b); return C.hex(b); };

async function api(path, body, auth = pair, retried = false) {
  if (!auth) throw new Unpaired("This phone isn't paired yet.");
  const method = body === undefined ? "GET" : "POST", text = body === undefined ? "" : JSON.stringify(body);
  const url = `/remote/api/${path}`, ts = Math.floor(Date.now() / 1000 + clockOffset), n = nonce();
  const headers = { "X-Reelwright-Device": auth.id, "X-Reelwright-Time": String(ts), "X-Reelwright-Nonce": n,
    "X-Reelwright-Sig": C.signature(auth.secret, method, url, ts, n, text) };
  if (text) headers["Content-Type"] = "application/json";
  let r;
  try { r = await fetch(url, { method, headers, body: text || undefined, cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" }); }
  catch { throw new Error("Can't reach Reelwright. Is the PC on with Reelwright running, and this phone on the same Wi-Fi?"); }
  const buf = new Uint8Array(await r.arrayBuffer());
  let data = {};
  try { data = JSON.parse(new TextDecoder().decode(buf)); } catch { /* not JSON */ }
  if (r.status === 401) {
    const skew = typeof data.time === "number" ? data.time - Date.now() / 1000 : clockOffset;
    if (!retried && Math.abs(skew - clockOffset) > 30) { clockOffset = skew; return api(path, body, auth, true); } // this phone's clock is off
    throw new Unpaired(data.detail || "Reelwright doesn't recognise this phone any more.");
  }
  if ([413, 429, 503].includes(r.status)) throw new Error(data.detail || `Reelwright answered ${r.status}.`);
  if (!C.proofOk(auth.secret, n, r.status, buf, r.headers.get("X-Reelwright-Proof"))) {
    throw new Error("That answer didn't come from the Reelwright this phone is paired with, so it was ignored.");
  }
  if (!r.ok) throw new Error(data.detail || `Request failed (${r.status}).`);
  return data;
}

// ---------- UI helpers ----------
function toast(msg, kind = "", action = null, ms = 4500) {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  const s = document.createElement("span");
  s.textContent = msg;
  t.append(s);
  if (action) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = action.label;
    b.onclick = () => { t.remove(); action.run(); };
    t.append(b);
  }
  $("#toasts").append(t);
  setTimeout(() => t.remove(), action ? 7000 : ms);
}
const fail = (e) => (e instanceof Unpaired ? PairScreen("", e.message) : toast(e.message || String(e), "err", null, 7000));

function sheet(html, wire, onClose) {
  const s = document.createElement("div");
  s.className = "scrim";
  s.innerHTML = `<div class="sheet" role="dialog" aria-modal="true"><div class="grip"></div>${html}</div>`;
  const close = () => { if (s.isConnected) { s.remove(); onClose?.(); } };
  s.addEventListener("click", (e) => { if (e.target === s || e.target.closest("[data-close]")) close(); });
  $("#sheet-root").append(s);
  wire?.($(".sheet", s), close);
  return close;
}
const confirmSheet = (title, text, ok, danger = false) => new Promise((res) => {
  let yes = false;
  sheet(`<h2>${esc(title)}</h2><p class="muted small">${esc(text)}</p>
    <div class="foot"><button class="btn" type="button" data-close>Cancel</button><button class="btn ${danger ? "danger" : "primary"}" type="button" id="cy">${esc(ok)}</button></div>`,
  (el, close) => { $("#cy", el).onclick = () => { yes = true; close(); }; }, () => res(yes));
});

function copyText(text, input) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(() => true, () => false);
  try { input.focus(); input.select(); input.setSelectionRange(0, text.length); return Promise.resolve(document.execCommand("copy")); }
  catch { return Promise.resolve(false); }
}

let timers = [];
const poll = (ms, fn) => timers.push(setInterval(() => { if (!document.hidden) fn().catch((e) => e instanceof Unpaired && PairScreen("", e.message)); }, ms));
const setBadge = (el, n) => { el.hidden = !n; el.textContent = n > 99 ? "99+" : String(n); };
const back = (fallback) => (history.length > 1 ? history.back() : (location.hash = fallback));

function mediaBox(c, review) {
  const wide = ["16:9", "source"].includes(c.aspect) ? "wide" : "";
  if (c.video) {
    return `<div class="media ${wide}"><video src="${esc(c.video)}" ${c.poster ? `poster="${esc(c.poster)}"` : ""} playsinline muted loop autoplay preload="metadata" ${review ? "" : "controls"}></video>
      ${review ? `<button class="snd" type="button" aria-label="Sound">🔇</button>` : ""}</div>`;
  }
  return `<div class="media wide">${c.poster ? `<img src="${esc(c.poster)}" alt="">` : `<div class="ph">No preview yet</div>`}
    <span class="tag">Not rendered yet: poster and transcript</span></div>`;
}
function wireMedia(el) {
  $$(".media .snd", el).forEach((b) => (b.onclick = () => {
    const v = $("video", b.parentElement);
    v.muted = !v.muted;
    if (!v.muted) v.play().catch(() => {});
    b.textContent = v.muted ? "🔇" : "🔊";
  }));
}
const postRow = (p, posted) => `<div class="item" ${p.clip_id ? `data-go="#clip/${esc(enc(p.clip_id))}"` : ""}>
  ${p.cover ? `<img class="thumb" src="${esc(p.cover)}" alt="" loading="lazy">` : ""}
  <div class="txt"><div class="t">${esc(p.title || "Clip")}</div>
    <div class="s">${platTag(p.platform)} ${esc(p.account || "")} · ${esc(posted ? (p.at ? ago(Date.parse(p.at) / 1000) : "") : when(p.when))}</div>
    ${p.error && !posted && ["failed", "needs_auth"].includes(p.status) ? `<div class="s" style="color:var(--err)">${esc(p.error)}</div>` : ""}</div>
  ${posted ? (p.url ? `<a class="btn sm" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : "") : pill(UPLOAD, p.status)}</div>`;
const clipRow = (c) => `<a class="item" href="#clip/${esc(enc(c.id))}">${c.poster ? `<img class="thumb" src="${esc(c.poster)}" alt="" loading="lazy">` : `<span class="thumb"></span>`}
  <div class="txt"><div class="t">${esc(c.title)}</div><div class="s">${c.score != null ? `★ ${Math.round(c.score)} · ` : ""}${dur(c.duration)}${c.rendered ? " · rendered" : ""}${c.rating === 1 ? " · 👍" : c.rating === -1 ? " · 👎" : ""}</div></div>
  <span class="chev">›</span></a>`;

// ---------- pairing ----------
const parseCode = (s) => { const m = /([0-9a-f]{8})\.([A-Za-z0-9_-]{20,})/.exec(s || ""); return m ? { id: m[1], secret: m[2] } : null; }; // QR: id.code
const parseKey = (s) => { const m = /([0-9a-f]{8}):([0-9a-f]{64})/.exec(s || ""); return m ? { id: m[1], secret: m[2] } : null; }; // a paired phone's id:key
const guessName = () => { const ua = navigator.userAgent; return /iPhone/.test(ua) ? "iPhone" : isIOS ? "iPad" : /Android/.test(ua) ? "Android phone" : "Phone"; };

function PairScreen(code, msg = "") {
  timers.forEach(clearInterval);
  timers = [];
  stale = true; // no background polling while unpaired: a revoked key would only rack up failures
  document.body.classList.add("unpaired");
  const c = parseCode(code);
  if (c) {
    view.innerHTML = `<h1>Pair this phone</h1><div class="card stack">
      <p class="small muted" style="margin:0">This phone will control Reelwright on your PC: review and publish clips, pause Autopilot, import videos and drive OBS.
        It can't see your keys, account sign-ins, settings or files, and it only works on your own network.</p>
      <label class="field">Name for this phone<input type="text" id="pname" maxlength="40" value="${esc(guessName())}" autocomplete="off"></label>
      <button class="btn primary block" type="button" id="pgo">Pair</button></div>`;
    $("#pgo").onclick = async (e) => {
      e.currentTarget.disabled = true;
      try {
        const pn = nonce(), r = await api("pair", { name: $("#pname").value.trim(), pn }, c);
        pair = { id: c.id, secret: C.deriveKey(c.secret, pn, r.sn), name: r.name, pc: r.pc }; // the QR code is spent now
        stale = false;
        const kept = ls.set(KEY, pair);
        ls.set("rw-seen", Date.now() / 1000);
        history.replaceState(null, "", `${location.pathname}#home`);
        if (!kept) toast("This browser won't keep the pairing (private browsing?): you'll need to pair again next time.", "err");
        toast(`Paired with ${r.pc}.`, "ok");
        if (!standalone) installSheet(true, route);
        else route();
      } catch (err) {
        $("#pgo").disabled = false;
        toast(err instanceof Unpaired ? "That pairing code was already used, expired or was revoked: on the PC press “Pair a phone” again and scan the new code." : err.message, "err", null, 8000);
      }
    };
    return;
  }
  view.innerHTML = `<h1>Connect to your PC</h1><div class="card stack">
    ${msg ? `<div class="note">${esc(msg)}${pair ? " If you revoked this phone on the PC, pair it again." : ""}</div>` : ""}
    <ol class="small muted" style="margin:0;padding-left:20px">
      <li>On the PC, open Reelwright → <b>Settings → Phone remote</b>, turn it on and press <b>Pair a phone</b>.</li>
      <li>Scan the QR code with this phone's camera.</li></ol>
    <label class="field">Or paste the pairing link (or this phone's key, copied in Safari)<input type="text" id="pcode" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="http://192.168…/#pair=…"></label>
    <button class="btn primary block" type="button" id="pnext">Continue</button>
    ${pair ? `<button class="btn block" type="button" id="pretry">Try again</button>` : ""}
    ${isIOS && standalone ? `<div class="note">iPhone keeps Home Screen apps' storage separate from Safari. If you paired in Safari, open Reelwright there, tap ⋯ → <b>Copy this phone's key</b>, and paste it here.</div>` : ""}</div>`;
  $("#pnext").onclick = async () => {
    const v = $("#pcode").value, k = parseKey(v), p = parseCode(v);
    if (k) { // already paired in Safari: adopt the same key (same phone), after checking the PC accepts it
      try { await api("dashboard", undefined, k); } catch (err) { return toast(err instanceof Unpaired ? "Reelwright doesn't accept that key (revoked?). Pair again with a new QR code." : err.message, "err", null, 8000); }
      pair = { ...k, name: "", pc: "" };
      stale = false;
      ls.set(KEY, pair);
      return route();
    }
    if (!p) return toast("That isn't a Reelwright pairing link or key.", "err");
    PairScreen(`${p.id}.${p.secret}`);
  };
  $("#pretry")?.addEventListener("click", route);
}

function installSheet(justPaired, onClose) {
  const code = pair ? `${pair.id}:${pair.secret}` : "";
  sheet(`<h2>${justPaired ? "Paired ✓ Now add it to your Home Screen" : "Add to Home Screen"}</h2>
    ${isIOS ? `<ol class="small" style="padding-left:20px"><li>Tap <b>Share</b> in Safari's toolbar.</li><li>Choose <b>Add to Home Screen</b> → <b>Add</b>.</li>
        <li>Open <b>Reelwright</b> from your Home Screen. iPhone keeps Home Screen apps' storage separate from Safari, so the first time it asks
        for this phone's key: copy it here, then paste it there.</li></ol>
      <label class="field">This phone's key (keep it to yourself)<input type="text" readonly id="pcopy" class="qr-code" value="${esc(code)}"></label>
      <button class="btn block" type="button" id="cp">Copy this phone's key</button>`
    : `<ol class="small" style="padding-left:20px"><li>In Chrome, tap <b>⋮</b> → <b>Add to Home screen</b>.</li><li>Open <b>Reelwright</b> from your Home screen.</li></ol>`}
    <div class="note" style="margin-top:12px">Works on your home Wi-Fi. Away from home, connect the phone to your home network with a VPN such as Tailscale.</div>
    <div class="foot"><button class="btn primary" type="button" data-close style="grid-column:span 2">Done</button></div>`,
  (el) => {
    $("#cp", el)?.addEventListener("click", async () => toast(await copyText(code, $("#pcopy", el)) ? "Copied." : "Select the code and copy it.", "ok"));
  }, onClose);
}

function menuSheet() {
  sheet(`<h2>Reelwright Remote</h2>
    <p class="small muted">${pair ? `Paired with <b>${esc(pair.pc || "your PC")}</b> as “${esc(pair.name || "this phone")}”.` : "Not paired."}</p>
    <div class="stack"><button class="btn block" type="button" id="m-inst">Add to Home Screen…</button>
      ${pair && isIOS && !standalone ? `<button class="btn block" type="button" id="m-code">Copy this phone's key</button>` : ""}
      <a class="btn block" href="#activity" data-close>Activity</a>
      ${pair ? `<button class="btn danger block" type="button" id="m-forget">Forget this PC</button>` : ""}</div>
    <p class="tiny dim">Everything stays on your network: this app only talks to Reelwright on your PC, and every request is signed with this phone's
      own key, which is never sent. True push notifications need HTTPS, so new activity shows under 🔔 while the app is open.
      To remove this phone for good, press <b>Revoke</b> next to it in Settings → Phone remote on the PC.</p>
    <div class="foot"><button class="btn" type="button" data-close style="grid-column:span 2">Close</button></div>`,
  (el, close) => {
    $("#m-inst", el).onclick = () => { close(); installSheet(false); };
    $("#m-code", el)?.addEventListener("click", () => { close(); installSheet(false); });
    $("#m-forget", el)?.addEventListener("click", async () => {
      close();
      if (!(await confirmSheet("Forget this PC?", "This phone stops controlling Reelwright until you pair it again. The PC keeps listing it until you press Revoke there.", "Forget", true))) return;
      ls.del(KEY);
      pair = null;
      route();
    });
  });
}

// ---------- screens ----------
async function Home(_, alive) {
  const draw = async () => {
    const d = await api("dashboard");
    if (!alive()) return;
    setBadge($("#rev-n"), d.review);
    const hint = standalone || ls.get("rw-hint-off") ? "" : `<div class="card row" id="hint"><span class="grow small">📲 Add Reelwright to your Home Screen for a full-screen app.</span>
      <button class="btn sm" type="button" id="hint-how">How</button><button class="icon" type="button" id="hint-x" aria-label="Dismiss">✕</button></div>`;
    view.innerHTML = `${hint}
      <div class="card ap ${d.paused ? "paused" : ""}"><span class="dot"></span>
        <div class="grow"><b>Autopilot ${d.paused ? "paused" : "on"}</b><div class="tiny muted">${d.paused ? `${plural(d.held, "post")} held until you resume` : "Scheduling and posting as planned"}</div></div>
        <button class="btn ${d.paused ? "primary" : ""}" type="button" id="ap">${d.paused ? "▶ Resume" : "⏸ Pause"}</button></div>
      ${d.review ? `<a class="card cta" href="#review"><span class="big">${d.review}</span><div class="grow"><b>new ${d.review === 1 ? "clip" : "clips"} to review</b>
        <div class="tiny muted">Keep or skip: it teaches the clip finder your taste</div></div><span class="chev">›</span></a>` : ""}
      <h3>Running now</h3><div class="card">${d.jobs.length ? `<div class="list">${d.jobs.map((j) => `<div class="item"><div class="txt">
          <div class="t">${esc(j.kind)}${j.title ? ` · ${esc(j.title)}` : ""}</div><div class="s">${esc(j.status === "queued" ? "Waiting its turn" : j.message)}</div>
          ${j.status === "running" ? bar(j.progress) : ""}</div></div>`).join("")}</div>` : `<div class="muted small">Nothing running.</div>`}</div>
      <h3>Coming up</h3><div class="card">${d.upcoming.length ? `<div class="list">${d.upcoming.map((p) => postRow(p, false)).join("")}</div>` : `<div class="muted small">No posts waiting.</div>`}</div>
      <h3>Recently posted</h3><div class="card">${d.posted.length ? `<div class="list">${d.posted.map((p) => postRow(p, true)).join("")}</div>` : `<div class="muted small">Nothing posted yet.</div>`}</div>
      <h3>Storage</h3><div class="card">${d.disk ? `<div class="row" style="margin-bottom:8px"><div class="grow"><b>${gb(d.disk.free)}</b> <span class="muted small">free of ${gb(d.disk.total)}</span></div>
          <span class="tiny muted">${plural(d.projects, "project")}</span></div>${bar(1 - d.disk.free / d.disk.total)}
          ${d.disk.free < 20e9 ? `<div class="note" style="margin-top:10px">Space is running low on the PC. Delete old clips from the Board here, or projects on the PC.</div>` : ""}`
        : `<div class="muted small">Unknown.</div>`}</div>`;
    $("#ap").onclick = async (e) => {
      e.currentTarget.disabled = true;
      try {
        const r = await api("autopilot", { paused: !d.paused });
        toast(r.paused ? "Autopilot paused everywhere. Its queued posts are held until you resume." : "Autopilot resumed.", "ok");
      } catch (err) { fail(err); }
      draw().catch(fail);
    };
    $("#hint-how")?.addEventListener("click", () => installSheet(false));
    $("#hint-x")?.addEventListener("click", () => { ls.set("rw-hint-off", 1); $("#hint").remove(); });
  };
  await draw();
  poll(6000, draw);
}

async function Review(_, alive) {
  let d = await api("review"), loaded = Date.now();
  if (!alive()) return;
  let queue = d.clips, total = d.total;
  const refill = async () => { d = await api("review"); loaded = Date.now(); queue = d.clips; total = d.total; };
  const draw = async () => {
    if ((!queue.length && total > 0) || Date.now() - loaded > 20 * 60e3) await refill(); // media links last 30 min
    if (!alive()) return;
    setBadge($("#rev-n"), total);
    const c = queue[0];
    if (!c) {
      view.innerHTML = `<div class="empty"><b>All caught up 🎉</b>No new AI clips to review. They show up here after an analysis or “Find more clips”.
        <p><a class="btn" href="#projects">Find more clips</a></p></div>`;
      return;
    }
    view.innerHTML = `<div class="row" style="margin-bottom:10px"><h1 class="grow" style="margin:0">Review</h1><span class="muted small">${total} to go</span></div>
      <div class="deck"><div class="rcard" id="rc"><span class="stamp keep">KEEP</span><span class="stamp skip">SKIP</span>
        ${mediaBox(c, true)}
        <div class="rbody"><div class="t">${esc(c.title)}</div>
          <div class="row wrap tiny muted" style="margin-top:4px;gap:6px">${c.score != null ? `<span class="pill">★ ${Math.round(c.score)}</span>` : ""}<span>${dur(c.duration)}</span><span>· ${esc(c.project || "")}</span></div>
          ${c.hook ? `<div class="small" style="margin-top:8px">${esc(c.hook)}</div>` : ""}
          ${c.snippet ? `<div class="snippet">“${esc(c.snippet)}”</div>` : ""}</div></div>
        <div class="decide"><button class="btn danger" type="button" id="skip">✕ Skip</button>
          <a class="btn" href="#clip/${esc(enc(c.id))}" aria-label="Clip details and actions">⋯</a>
          <button class="btn ok" type="button" id="keep">✓ Keep</button></div>
        <p class="tiny dim" style="text-align:center">Swipe right to keep, left to skip. Same as 👍 / 👎 on the PC: it trains the clip finder.</p></div>`;
    wireMedia(view);
    swipe($("#rc"), decide);
    $("#keep").onclick = () => decide(1);
    $("#skip").onclick = () => decide(-1);
  };
  let busy = false;
  const decide = async (v) => {
    const c = queue[0], card = $("#rc");
    if (!c || busy) return;
    busy = true;
    card.style.transform = `translateX(${v * 130}%) rotate(${v * 14}deg)`;
    card.style.opacity = "0";
    try {
      await api(`clips/${enc(c.id)}/rate`, { rating: v });
      queue.shift();
      total = Math.max(0, total - 1);
      toast(v > 0 ? `Kept “${c.title}”` : `Skipped “${c.title}”`, v > 0 ? "ok" : "", { label: "Undo", run: async () => {
        try { await api(`clips/${enc(c.id)}/rate`, { rating: 0 }); queue.unshift(c); total++; draw().catch(fail); } catch (err) { fail(err); }
      } });
      setTimeout(() => draw().catch(fail), 200);
    } catch (err) {
      card.style.transform = "";
      card.style.opacity = "";
      fail(err);
    }
    busy = false;
  };
  await draw();
}

function swipe(card, decide) {
  let x0 = null, dx = 0, pid = null;
  const stamps = (k, s) => { $(".stamp.keep", card).style.opacity = k; $(".stamp.skip", card).style.opacity = s; };
  card.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button,a")) return;
    x0 = e.clientX; dx = 0; pid = e.pointerId;
    card.classList.add("drag");
  });
  card.addEventListener("pointermove", (e) => {
    if (x0 === null || e.pointerId !== pid) return;
    dx = e.clientX - x0;
    if (Math.abs(dx) > 10) { try { card.setPointerCapture(pid); } catch { /* already released */ } }
    card.style.transform = `translateX(${dx}px) rotate(${dx / 24}deg)`;
    stamps(Math.max(0, Math.min(1, dx / 90)), Math.max(0, Math.min(1, -dx / 90)));
  });
  const end = () => {
    if (x0 === null) return;
    card.classList.remove("drag");
    x0 = null;
    if (Math.abs(dx) > 90) return decide(dx > 0 ? 1 : -1);
    card.style.transform = "";
    stamps(0, 0);
  };
  card.addEventListener("pointerup", end);
  card.addEventListener("pointercancel", end); // the page started scrolling vertically: not a swipe
}

async function ClipView(id, alive) {
  let c = await api(`clips/${enc(id)}`);
  if (!alive()) return;
  const status = () => {
    if (c.rendered) return `<div class="row"><span class="pill ok">✓ Rendered</span><span class="small muted grow">Ready to post.</span></div>`;
    if (c.render.busy) return `<div class="row" style="margin-bottom:8px"><span class="spin"></span><span class="small grow">Rendering on the PC…</span><span class="tiny muted">${Math.round((c.render.progress || 0) * 100)}%</span></div>${bar(c.render.progress)}`;
    if (c.locked) return `<div class="small muted">Posted: the video is final. Make a new version on the PC to change it.</div>`;
    return `<div class="row"><span class="small muted grow">${c.render.status === "failed" ? "The last render failed." : "Not rendered since its last change."} Publishing renders it first.</span>
      <button class="btn sm" type="button" id="render">Render now</button></div>`;
  };
  const posts = () => (c.uploads.length ? `<div class="list">${c.uploads.map((u) => postRow({ ...u, clip_id: null }, u.status === "done")).join("")}</div>` : `<div class="muted small">Not posted yet.</div>`);
  const paint = () => {
    $("#c-status").innerHTML = status();
    $("#c-posts").innerHTML = posts();
    $("#r1").className = `btn ${c.rating === 1 ? "ok" : ""}`;
    $("#r-1").className = `btn ${c.rating === -1 ? "danger" : ""}`;
    $("#render")?.addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      try { await api(`clips/${enc(id)}/render`, {}); toast("Rendering on the PC.", "ok"); await refresh(); } catch (err) { fail(err); }
    });
  };
  const full = () => {
    view.innerHTML = `<div class="row" style="margin-bottom:10px"><button class="btn sm" type="button" id="back">‹ Back</button></div>
      <div class="rcard">${mediaBox(c, false)}<div class="rbody"><div class="t">${esc(c.title)}</div>
        <div class="tiny muted" style="margin-top:4px">${dur(c.duration)} · ${esc(c.project || "")}${c.score != null ? ` · ★ ${Math.round(c.score)}` : ""}</div>
        ${c.hook ? `<div class="small" style="margin-top:8px">${esc(c.hook)}</div>` : ""}${c.snippet && !c.video ? `<div class="snippet">“${esc(c.snippet)}”</div>` : ""}</div></div>
      <div class="grid2" style="margin-top:12px"><button class="btn" type="button" id="r-1">👎 Skip</button><button class="btn" type="button" id="r1">👍 Keep</button></div>
      <h3>Render</h3><div class="card" id="c-status"></div>
      <h3>Posts</h3><div class="card" id="c-posts"></div>
      <div class="grid2" style="margin-top:16px"><button class="btn primary" type="button" id="pub">⇪ Publish…</button><button class="btn danger" type="button" id="del">Delete…</button></div>`;
    $("#back").onclick = () => back("#review");
    for (const v of [1, -1]) {
      $(`#r${v}`).onclick = async () => {
        try { const r = await api(`clips/${enc(id)}/rate`, { rating: c.rating === v ? 0 : v }); c.rating = r.rating; paint(); } catch (err) { fail(err); }
      };
    }
    $("#pub").onclick = () => publishSheet(c, refresh);
    $("#del").onclick = () => deleteSheet(c);
    paint();
  };
  const refresh = async () => {
    const was = c.rendered;
    c = await api(`clips/${enc(id)}`);
    if (!alive()) return;
    if (was !== c.rendered) full(); else paint();
  };
  full();
  poll(5000, refresh);
}

function publishSheet(c, onDone) {
  const ok = c.accounts.filter((a) => a.platform !== "tiktok"), tt = c.accounts.filter((a) => a.platform === "tiktok");
  let mode = "now";
  sheet(`<h2>Publish</h2>
    ${ok.length ? ok.map((a, i) => `<label class="check"><input type="checkbox" class="acc" value="${esc(a.id)}" data-plat="${esc(a.platform)}" ${i === 0 ? "checked" : ""}> ${platTag(a.platform)} ${esc(a.name)}</label>`).join("")
      : `<div class="note">No account is approved for uploads yet. On the PC: Publish page → link an account and turn on “Approved for uploads”.</div>`}
    ${tt.length ? `<div class="note" style="margin:6px 0">TikTok (${tt.map((a) => esc(a.name)).join(", ")}): TikTok asks for its own posting choices, so publish there from the PC.</div>` : ""}
    <div class="stack" style="margin-top:8px">
      <label class="field">Title<input type="text" id="pt" maxlength="100" value="${esc(c.title)}"></label>
      <label class="field">Caption (Instagram)<textarea id="pc" rows="2" maxlength="2200">${esc(c.title)}</textarea></label>
      <label class="field">Visibility (YouTube)<select id="pv"><option value="">As in Settings</option><option value="public">Public</option><option value="unlisted">Unlisted</option><option value="private">Private</option></select></label>
      <label class="check"><input type="checkbox" id="pk"> Made for kids (YouTube)</label>
      <div class="seg" id="pw"><button type="button" class="on" data-v="now">Post now</button><button type="button" data-v="at">Schedule</button></div>
      <div id="ptl" class="stack" hidden><input type="datetime-local" id="ptime" value="${tomorrow9()}"><button class="btn sm" type="button" id="pbest">★ Best time</button></div>
      <p class="tiny muted" id="ph" style="margin:0"></p></div>
    <div class="foot"><button class="btn" type="button" data-close>Cancel</button><button class="btn primary" type="button" id="pgo" ${ok.length ? "" : "disabled"}>Queue post</button></div>`,
  (el, close) => {
    const help = { now: "Posts as soon as the render is ready.", at: "YouTube uploads now as private and goes public at this time; Instagram is posted at this time (the PC must be on)." };
    const setMode = (v) => { mode = v; $$("#pw button", el).forEach((b) => b.classList.toggle("on", b.dataset.v === v)); $("#ptl", el).hidden = v === "now"; $("#ph", el).textContent = help[v]; };
    setMode("now");
    $("#pw", el).onclick = (e) => e.target.dataset.v && setMode(e.target.dataset.v);
    $("#pbest", el).onclick = async () => {
      const plat = $$(".acc", el).find((x) => x.checked)?.dataset.plat || "youtube";
      try {
        const r = await api("suggest", { platform: plat });
        if (r.times?.[0]) { $("#ptime", el).value = toLocalInput(new Date(r.times[0])); toast(`Best time for ${PLATFORM[plat]} (${r.source === "yours" ? "from your results" : r.source === "research" ? "from live research" : "a default until there's data"}).`); }
      } catch (err) { fail(err); }
    };
    $("#pgo", el).onclick = async (e) => {
      const accs = $$(".acc", el).filter((x) => x.checked).map((x) => x.value);
      if (!accs.length) return toast("Pick at least one account.", "err");
      const t = mode === "at" ? new Date($("#ptime", el).value) : null;
      if (t && !(t > new Date())) return toast("Pick a time in the future.", "err");
      e.currentTarget.disabled = true;
      try {
        await api(`clips/${enc(c.id)}/publish`, { accounts: accs, title: $("#pt", el).value.trim(), caption: $("#pc", el).value.trim(),
          privacy: $("#pv", el).value || null, made_for_kids: $("#pk", el).checked, when: t ? t.toISOString() : null });
        close();
        toast(t ? `Scheduled for ${when(t.toISOString())}.` : "Queued: it posts when the render is ready.", "ok");
        onDone?.();
      } catch (err) { e.currentTarget.disabled = false; fail(err); }
    };
  });
}

function deleteSheet(c) {
  const posted = c.uploads.filter((u) => u.status === "done");
  sheet(`<h2>Delete this clip?</h2>
    <p class="small muted">Removes it from Reelwright: its render goes and posts that haven't gone out are canceled. Videos already posted stay online unless you tick them below.</p>
    ${posted.map((u) => (u.platform === "youtube" ? `<label class="check"><input type="checkbox" value="${esc(u.id)}"> Also delete it on YouTube${u.account ? ` (${esc(u.account)})` : ""}</label>`
      : `<div class="note" style="margin-bottom:8px">${esc(PLATFORM[u.platform] || u.platform)} doesn't let apps delete posts: remove it in the ${esc(PLATFORM[u.platform] || u.platform)} app.</div>`)).join("")}
    <div class="foot"><button class="btn" type="button" data-close>Cancel</button><button class="btn danger" type="button" id="dgo">Delete</button></div>`,
  (el, close) => {
    $("#dgo", el).onclick = async (e) => {
      e.currentTarget.disabled = true;
      try {
        const r = await api(`clips/${enc(c.id)}/delete`, { remote: $$("input:checked", el).map((x) => x.value) });
        close();
        if (!r.ok) return toast(r.error || "Couldn't delete it.", "err", null, 8000);
        const bad = (r.remote || []).filter((x) => !x.ok);
        toast(bad.length ? `Deleted here. ${bad.map((x) => x.error).join(" ")}` : "Deleted.", bad.length ? "err" : "ok", null, bad.length ? 9000 : 4000);
        back("#review");
      } catch (err) { e.currentTarget.disabled = false; fail(err); }
    };
  });
}

let pendingShare = "";
async function Projects(_, alive) {
  view.innerHTML = `<h1>Projects</h1>
    <form class="card stack" id="imp"><label class="field">Import a video by link
        <input type="text" id="iurl" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Paste a YouTube or Twitch link" value="${esc(pendingShare)}"></label>
      <button class="btn primary" type="submit">Import</button>
      <div class="tiny dim">In the YouTube or Twitch app: Share → Copy link, then paste it here.</div></form>
    <h3>Your projects</h3><div class="card"><div class="list" id="plist"><div class="muted small"><span class="spin"></span></div></div></div>`;
  pendingShare = "";
  const draw = async () => {
    const d = await api("projects");
    if (!alive()) return;
    $("#plist").innerHTML = d.projects.length ? d.projects.map((p) => `<a class="item" href="#project/${esc(enc(p.id))}">
        ${p.poster ? `<img class="thumb" src="${esc(p.poster)}" alt="" loading="lazy">` : `<span class="thumb"></span>`}
        <div class="txt"><div class="t">${esc(p.name)}</div><div class="s">${pill(PROJECT, p.status)} ${p.status === "ready" ? plural(p.clips, "clip") : esc(p.status === "failed" ? p.error : p.stage)}</div>
          ${["importing", "analyzing"].includes(p.status) ? bar(p.progress) : ""}</div><span class="chev">›</span></a>`).join("")
      : `<div class="muted small">No projects yet. Import a link above, or add a video in Reelwright on the PC.</div>`;
  };
  $("#imp").onsubmit = async (e) => {
    e.preventDefault();
    const b = $("#imp button");
    b.disabled = true;
    try {
      await api("import", { url: $("#iurl").value });
      $("#iurl").value = "";
      toast("Importing on the PC. After the analysis, its clips show up in Review.", "ok");
      await draw();
    } catch (err) { fail(err); }
    b.disabled = false;
  };
  await draw();
  poll(5000, draw);
}

async function ProjectView(id, alive) {
  let p = await api(`projects/${enc(id)}`);
  if (!alive()) return;
  view.innerHTML = `<div class="row"><button class="btn sm" type="button" id="back">‹ Projects</button></div>
    <h1 style="margin-top:12px" id="pn"></h1><div class="row tiny muted" id="pm"></div>
    <form class="card stack" id="find" style="margin-top:12px"><label class="field">Find more clips
      <input type="text" id="fq" maxlength="300" placeholder="Optional: describe a moment, e.g. “the boss fight”"></label>
      <button class="btn primary" type="submit" id="fgo">✨ Find more clips</button><div class="tiny dim" id="fnote"></div></form>
    <h3>Clips</h3><div class="card"><div class="list" id="clist"></div></div>`;
  const paint = () => {
    $("#pn").textContent = p.name;
    $("#pm").innerHTML = `${pill(PROJECT, p.status)} ${p.duration ? dur(p.duration) : ""} · ${plural(p.clips, "clip")}`;
    $("#fgo").disabled = p.status !== "ready";
    $("#fnote").textContent = p.status === "ready" ? "Runs on the PC; new clips appear here and in Review." : "Available once the analysis finishes.";
    $("#clist").innerHTML = p.items.length ? p.items.map(clipRow).join("") : `<div class="muted small">${p.status === "ready" ? "No clips yet: find some above." : esc(p.stage || "Waiting…")}</div>`;
  };
  $("#back").onclick = () => back("#projects");
  $("#find").onsubmit = async (e) => {
    e.preventDefault();
    $("#fgo").disabled = true;
    try { await api(`projects/${enc(id)}/find`, { query: $("#fq").value.trim() }); $("#fq").value = ""; toast("Searching on the PC…", "ok"); } catch (err) { fail(err); }
    $("#fgo").disabled = p.status !== "ready";
  };
  paint();
  poll(6000, async () => { p = await api(`projects/${enc(id)}`); if (alive()) paint(); });
}

async function Board(_, alive) {
  const d = await api("board");
  if (!alive()) return;
  const open = ls.get("rw-board-open") || { harvested: true, preclipped: true, shortform: true, ready: true };
  const item = (it) => {
    const href = it.type === "clip" ? `#clip/${enc(it.id)}` : it.type === "project" ? `#project/${enc(it.id)}` : "";
    const inner = `${it.thumb ? `<img class="thumb" src="${esc(it.thumb)}" alt="" loading="lazy">` : `<span class="thumb"></span>`}
      <div class="txt"><div class="t">${esc(it.title || "Untitled")}</div><div class="s">${it.type === "longform" ? "Long form · " : it.type === "project" ? "Video · " : ""}${it.duration ? dur(it.duration) : ""}
        ${it.posts.map((x) => ` ${platTag(x.platform)}`).join("")}${it.pending.length ? ` · ${it.pending.map((x) => esc((UPLOAD[x.status] || ["", x.status])[1])).join(", ")}` : ""}</div></div>`;
    return href ? `<a class="item" href="${esc(href)}">${inner}<span class="chev">›</span></a>` : `<div class="item">${inner}</div>`;
  };
  view.innerHTML = `<div class="row"><h1 class="grow">Pipeline</h1><button class="btn sm" type="button" id="rf">↻ Refresh</button></div>
    <p class="muted small" style="margin-top:-6px">Every video and clip, in the furthest stage it has reached. Tap one for its actions.</p>
    ${d.stages.map((s) => `<details class="stage" data-k="${esc(s.key)}" ${open[s.key] ? "open" : ""}><summary>${esc(s.label)}<span class="count">${s.items.length}</span></summary>
      <div class="list">${s.items.slice(0, 60).map(item).join("") || `<div class="muted small" style="padding:2px 2px 12px">Nothing here.</div>`}
      ${s.items.length > 60 ? `<div class="tiny dim" style="padding:8px 2px">…and ${s.items.length - 60} more on the PC.</div>` : ""}</div></details>`).join("")}`;
  $$("details.stage").forEach((x) => x.addEventListener("toggle", () => { open[x.dataset.k] = x.open; ls.set("rw-board-open", open); }));
  $("#rf").onclick = route;
}

async function Obs(_, alive) {
  let busy = false;
  const draw = async () => {
    if (busy) return;
    const d = await api("obs");
    if (!alive() || busy) return;
    view.innerHTML = `<h1>OBS</h1>${d.connections.length ? d.connections.map((c) => `<div class="card" data-id="${esc(c.id)}">
        <div class="row"><span class="dot ${c.status === "connected" ? "on" : c.status === "offline" ? "bad" : ""}"></span><b class="grow">${esc(c.name)}</b>
          <span class="tiny muted">${!c.enabled ? "Turned off" : c.status === "connected" ? "Connected" : c.status === "offline" ? "Offline" : "Connecting…"}</span></div>
        ${c.error && c.status !== "connected" ? `<div class="tiny muted" style="margin-top:6px">${esc(c.error)}</div>` : ""}
        ${c.status === "connected" ? `<div class="obs-grid">
          <button class="btn primary wide" type="button" data-a="clip_that"><i>✂</i>Clip that</button>
          <button class="btn" type="button" data-a="save_replay"><i>💾</i>Save replay</button><button class="btn" type="button" data-a="marker"><i>🔖</i>Marker</button>
          <button class="btn" type="button" data-a="start_record"><i>⏺</i>Start recording</button><button class="btn" type="button" data-a="stop_record"><i>⏹</i>Stop recording</button></div>` : ""}</div>`).join("")
      + `<p class="tiny dim">“Clip that” saves OBS's replay buffer and imports it into Reelwright. A marker adds a chapter to the recording (OBS 30.2+, Hybrid MP4).</p>`
      : `<div class="empty"><b>OBS isn't linked</b>On the PC, open the OBS link page and connect OBS (in OBS: Tools → WebSocket Server Settings).</div>`}`;
  };
  const LABEL = { clip_that: "Clipped", save_replay: "Replay saved", marker: "Marker added", start_record: "Recording started", stop_record: "Recording stopped" };
  view.onclick = async (e) => {
    const b = e.target.closest("[data-a]");
    if (!b) return;
    const a = b.dataset.a, oid = b.closest("[data-id]").dataset.id;
    if (a === "stop_record" && !(await confirmSheet("Stop recording?", "OBS stops recording now.", "Stop", true))) return;
    busy = true;
    b.disabled = true;
    const label = b.innerHTML;
    b.innerHTML = `<span class="spin"></span>`;
    try {
      const r = await api(`obs/${enc(oid)}/${a}`, {});
      toast(r.message || `${LABEL[a]}${r.recording != null ? ` · recording ${r.recording ? "on" : "off"}` : ""}.`, "ok");
    } catch (err) { fail(err); }
    b.disabled = false;
    b.innerHTML = label;
    busy = false;
  };
  await draw();
  poll(8000, draw);
}

async function Activity(_, alive) {
  const d = await api("activity");
  if (!alive()) return;
  const seen = +ls.get("rw-seen") || 0;
  view.innerHTML = `<h1>Activity</h1>
    <div class="note">Push notifications need HTTPS, which a home-network address doesn't have, so Reelwright can't buzz your phone.
      New activity shows here, and as a badge on 🔔, while the app is open.</div>
    <div class="card" style="margin-top:12px">${d.events.length ? d.events.map((e) => `<div class="ev ${e.ok ? "" : "bad"}" ${e.clip_id ? `data-go="#clip/${esc(enc(e.clip_id))}"` : e.project_id ? `data-go="#project/${esc(enc(e.project_id))}"` : ""}>
        <span class="i">${e.ok ? "✓" : "!"}</span><div class="grow"><div class="small"><b>${esc(e.title)}</b></div><div class="tiny muted">${esc(e.detail)}</div>
        <div class="tiny dim">${ago(e.at)}${e.url ? ` · <a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">open ↗</a>` : ""}</div></div>
        ${e.at > seen ? `<span class="new"></span>` : ""}</div>`).join("") : `<div class="muted small">Nothing yet.</div>`}</div>`;
  ls.set("rw-seen", d.now);
  setBadge($("#bell-n"), 0);
}

// ---------- activity badge (polled while the app is open: no push without HTTPS) ----------
let newest = null;
async function checkActivity() {
  if (!pair || stale) return;
  const d = await api("activity");
  const seen = +ls.get("rw-seen") || 0;
  setBadge($("#bell-n"), location.hash.startsWith("#activity") ? 0 : d.events.filter((e) => e.at > seen).length);
  if (newest !== null) d.events.filter((e) => e.at > newest).slice(0, 3).forEach((e) => toast(`${e.title}: ${e.detail}`, e.ok ? "ok" : "err", null, 6000));
  newest = d.events[0]?.at ?? newest ?? d.now;
}

// ---------- routing ----------
const routes = { home: Home, review: Review, projects: Projects, project: ProjectView, board: Board, obs: Obs, activity: Activity, clip: ClipView };
const TAB = { project: "projects", clip: "review", activity: "" };
let seq = 0;
async function route() {
  timers.forEach(clearInterval);
  timers = [];
  view.onclick = null;
  $$(".scrim").forEach((s) => s.remove());
  const h = location.hash.slice(1);
  if (h.startsWith("pair=")) return PairScreen(decodeURIComponent(h.slice(5)));
  if (!pair) return PairScreen("");
  document.body.classList.remove("unpaired");
  const [name, arg] = h.split("/");
  const r = routes[name] ? name : "home";
  $$("#tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === (r in TAB ? TAB[r] : r)));
  const mine = ++seq;
  view.innerHTML = `<div class="empty"><span class="spin"></span></div>`;
  window.scrollTo(0, 0);
  try {
    await routes[r](arg && decodeURIComponent(arg), () => mine === seq);
    stale = false;
  } catch (e) {
    if (mine !== seq) return;
    if (e instanceof Unpaired) return PairScreen("", e.message);
    view.innerHTML = `<div class="empty"><b>Couldn't load this</b>${esc(e.message)}<p><button class="btn" type="button" id="retry">Try again</button></p></div>`;
    $("#retry").onclick = route;
  }
}

view.addEventListener("click", (e) => { // rows that open something, without nesting links inside links
  const g = e.target.closest("[data-go]");
  if (g && !e.target.closest("a,button,input,label,video")) location.hash = g.dataset.go;
});
$("#bell").onclick = () => (location.hash = "#activity");
$("#menu").onclick = menuSheet;
window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { if (!document.hidden) checkActivity().catch(() => {}); });
setInterval(() => { if (!document.hidden) checkActivity().catch(() => {}); }, 20000);
if ("serviceWorker" in navigator && window.isSecureContext) navigator.serviceWorker.register("sw.js").catch(() => {}); // http:// Wi-Fi: not available, and not needed

{ // shared from another app (Web Share Target: where the platform supports it): prefill the import form
  const q = new URLSearchParams(location.search), m = /https?:\/\/\S+/.exec([q.get("url"), q.get("text"), q.get("title")].filter(Boolean).join(" "));
  if (m) { pendingShare = m[0]; history.replaceState(null, "", "/#projects"); }
}
route();
checkActivity().catch(() => {});
