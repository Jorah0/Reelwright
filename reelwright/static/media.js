/* Studio's "Online media & kits" section: search GIPHY / Freesound / Pexels / Pixabay with the creator's own keys
   (sent once to the server, sealed there, never read back), import a result into the library (licence kept for credits),
   and kits (named collections, zip export/import). Also the library's asset editor (trim, defaults, tags) and the
   recently-used list. Uses app.js's globals: api, esc, toast, fail. */
(function (root) {
  "use strict";
  const RECENT = "rw.media.recent";
  const LIC = { "cc0": "CC0", "cc-by": "CC BY", "cc-by-nc": "CC BY-NC", "sampling+": "Sampling+", "giphy": "GIPHY", "pixabay": "Pixabay", "pexels": "Pexels" };
  const KIND = { gif: "GIFs", sticker: "Stickers", sound: "Sounds", image: "Photos", video: "Videos" };
  const recentIds = () => { try { const v = JSON.parse(localStorage[RECENT] || "[]"); return Array.isArray(v) ? v : []; } catch { return []; } };
  function used(id) { try { localStorage[RECENT] = JSON.stringify([id, ...recentIds().filter((x) => x !== id)].slice(0, 12)); } catch { /* private mode */ } }

  // host: {assets() -> the library list, reload() -> refetch it and redraw the library, add(asset) -> overlay at the playhead}
  function mount(el, host) {
    const S = { tab: "kits", provs: null, kind: {}, q: {}, res: {}, busy: false, kits: [] };
    const prov = () => S.provs?.providers.find((p) => p.id === S.tab);
    async function load() {
      try { [S.provs, S.kits] = await Promise.all([api("/api/media/providers"), api("/api/media/kits")]); } catch (e) { fail(e); }
      draw();
    }
    function badge(lic) {
      const nc = lic === "cc-by-nc" || lic === "sampling+";
      return lic ? `<span class="md-lic${nc ? " nc" : ""}" title="${nc ? "Non-commercial: not for a monetized channel" : "Licence"}">${esc(LIC[lic] || lic)}</span>` : "";
    }
    function card(it) {
      const pv = it.kind === "sound" ? `<button class="btn xs md-play" data-md="play" data-src="${esc(it.preview_url)}" title="Hover or click to hear it">▶ ${(+it.duration || 0).toFixed(1)}s</button>`
        : `<img src="${esc(it.preview_url)}" alt="" loading="lazy">`;
      return `<div class="st-asset md-item">${pv}<div class="st-aname" title="${esc(it.title)}">${esc(it.title || "untitled")}</div>
        <div class="row" style="gap:4px;flex-wrap:wrap">${badge(it.licence)}<span class="muted" style="font-size:11px">${esc(it.author)}</span></div>
        <div class="row" style="gap:4px"><button class="btn xs" data-md="imp" data-id="${esc(it.id)}" title="Save into your media library">Import</button>
        <button class="btn xs" data-md="impadd" data-id="${esc(it.id)}" title="Import and add at the playhead">＋</button></div></div>`;
    }
    function provBody(p) {
      if (!p.connected) return `<div class="stack" style="gap:6px"><span class="muted" style="font-size:12px">Connect ${esc(p.name)} with your own free API key:
          <a href="${esc(p.signup_url)}" target="_blank" rel="noopener">get one here</a> · <a href="${esc(p.terms_url)}" target="_blank" rel="noopener">terms</a>.
          The key stays sealed on this PC.</span>
        <div class="row"><input type="password" id="md-key" placeholder="${esc(p.name)} API key" autocomplete="off"><button class="btn sm" data-md="key">Connect</button></div></div>`;
      const kind = S.kind[p.id] || p.kinds[0], r = S.res[p.id];
      return `${p.kinds.length > 1 ? `<div class="seg">${p.kinds.map((k) => `<button class="${k === kind ? "on" : ""}" data-md="kind" data-v="${k}">${KIND[k]}</button>`).join("")}</div>` : ""}
        <form class="row" id="md-sf"><input type="search" id="md-q" placeholder="Search ${esc(p.name)}" value="${esc(S.q[p.id] || "")}" maxlength="100"><button class="btn sm"${S.busy ? " disabled" : ""}>Search</button></form>
        <div class="st-lib">${r ? (r.items.length ? r.items.map(card).join("") : `<span class="muted" style="font-size:12px">Nothing found.</span>`) : ""}</div>
        ${r?.hidden_nc ? `<div class="muted" style="font-size:12px">${r.hidden_nc} non-commercial sound${r.hidden_nc > 1 ? "s" : ""} hidden (monetized channel).</div>` : ""}
        <div class="row" style="font-size:11.5px"><b class="grow">${esc(p.attribution)}</b><a href="${esc(p.terms_url)}" target="_blank" rel="noopener">terms</a>
          <button class="btn xs" data-md="unkey" title="Forget this key">Disconnect</button></div>
        ${p.id === "giphy" ? `<label class="muted" style="font-size:12px"><input type="checkbox" data-md="giphyok"${S.provs.giphy_storage_ok ? " checked" : ""}>
          I have GIPHY's written permission to save its GIFs (its API terms forbid storing them otherwise)</label>` : ""}`;
    }
    function kitsBody() {
      const all = host.assets() || [], byId = (id) => all.find((a) => a.id === id);
      return `<div class="row"><input type="text" id="md-kn" placeholder="New kit name, e.g. My hype sounds" maxlength="60"><button class="btn sm" data-md="kitnew" title="A kit of your ★ favourites">From ★</button></div>
        ${S.kits.length ? S.kits.map((k) => `<details class="md-kit"><summary><b>${esc(k.name)}</b> <span class="muted">${k.asset_ids.length}</span></summary>
          <div class="st-lib">${k.asset_ids.map(byId).filter(Boolean).map((a) => `<div class="st-asset" draggable="true" data-asset="${esc(a.id)}" title="Drag onto the timeline or preview">
            <span class="st-aname">${esc(a.name)}</span><button class="btn xs" data-md="add" data-id="${esc(a.id)}">＋ At playhead</button></div>`).join("")}</div>
          <div class="row"><a class="btn xs" href="/api/media/kits/${encodeURIComponent(k.id)}/export" download>Export .zip</a><button class="btn xs" data-md="kitdel" data-id="${esc(k.id)}">Delete kit</button></div></details>`).join("")
        : `<span class="muted" style="font-size:12px">No kits yet: star assets in the library, then make a kit of them.</span>`}
        <label class="btn xs" style="align-self:flex-start">Import a kit .zip<input type="file" id="md-kz" accept=".zip,application/zip" hidden></label>`;
    }
    function draw() {
      if (!el.isConnected) return;
      const tabs = [["kits", "Kits"], ...(S.provs?.providers || []).map((p) => [p.id, p.name])];
      el.innerHTML = `<div class="seg">${tabs.map(([v, l]) => `<button class="${S.tab === v ? "on" : ""}" data-md="tab" data-v="${v}">${esc(l)}</button>`).join("")}</div>
        <div class="stack" style="gap:8px">${S.tab === "kits" ? kitsBody() : prov() ? provBody(prov()) : ""}</div>
        ${S.provs ? `<label class="muted" style="font-size:12px"><input type="checkbox" data-md="mon"${S.provs.monetized ? " checked" : ""}> My channel is monetized: hide and flag non-commercial (CC BY-NC) media</label>` : ""}`;
      const f = el.querySelector("#md-sf");
      if (f) f.onsubmit = (e) => { e.preventDefault(); search(); };
      const kz = el.querySelector("#md-kz");
      if (kz) kz.onchange = async () => { const fd = new FormData(); fd.append("file", kz.files[0]);
        try { const k = await api("/api/media/kits/import", { body: fd }); toast(`Kit “${k.name}” imported.`, "ok"); await host.reload(); load(); } catch (e) { fail(e); } };
    }
    async function search() {
      const p = prov(), q = el.querySelector("#md-q")?.value.trim();
      if (!p || !q) return;
      S.q[p.id] = q; S.busy = true;
      try { S.res[p.id] = await api(`/api/media/search?${new URLSearchParams({ provider: p.id, q, kind: S.kind[p.id] || p.kinds[0] })}`); }
      catch (e) { if (e.status === 409) { p.connected = false; } fail(e); }
      finally { S.busy = false; draw(); }
    }
    async function importItem(id, add) {
      try {
        const a = await api("/api/media/import", { body: { provider: S.tab, id } });
        toast(a.duplicate ? "Already in your library." : "Imported into your media library.", "ok");
        await host.reload();
        if (add) { host.add((host.assets() || []).find((x) => x.id === a.id) || a); used(a.id); }
      } catch (e) { fail(e); }
    }
    let audio = null;
    const play = (src) => { audio?.pause(); audio = new Audio(src); audio.volume = 0.6; audio.play().catch(() => {}); };
    el.addEventListener("mouseover", (e) => { const b = e.target.closest?.("[data-md=play]"); if (b && b !== el._hov) { el._hov = b; play(b.dataset.src); } });
    el.addEventListener("mouseout", (e) => { if (e.target.closest?.("[data-md=play]") && !e.relatedTarget?.closest?.("[data-md=play]")) { el._hov = null; audio?.pause(); } });
    el.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-md]");
      if (!b) return;
      const md = b.dataset.md, p = prov();
      try {
        if (md === "tab") { S.tab = b.dataset.v; draw(); }
        else if (md === "kind") { S.kind[S.tab] = b.dataset.v; S.res[S.tab] = null; draw(); if (S.q[S.tab]) search(); }
        else if (md === "play") play(b.dataset.src);
        else if (md === "imp" || md === "impadd") importItem(b.dataset.id, md === "impadd");
        else if (md === "add") { const a = (host.assets() || []).find((x) => x.id === b.dataset.id); if (a) { host.add(a); used(a.id); } }
        else if (md === "key" || md === "unkey") {
          const key = md === "key" ? el.querySelector("#md-key").value.trim() : "";
          if (md === "key" && !key) return;
          await api("/api/media/keys", { method: "PUT", body: { provider: p.id, key } });
          p.connected = !!key; S.res[p.id] = null; draw(); toast(key ? `${p.name} connected.` : `${p.name} disconnected.`, "ok");
        } else if (md === "mon" || md === "giphyok") {
          const k = md === "mon" ? "media_monetized" : "giphy_storage_ok";
          await api("/api/settings", { method: "PUT", body: { [k]: b.checked } });
          S.provs[md === "mon" ? "monetized" : "giphy_storage_ok"] = b.checked; S.res = {};
        } else if (md === "kitnew") {
          const name = el.querySelector("#md-kn").value.trim(), favs = (host.assets() || []).filter((a) => a.fav).map((a) => a.id);
          if (!name) return toast("Name the kit first.");
          if (!favs.length) return toast("Star some library assets (★) first.");
          await api("/api/media/kits", { body: { name, asset_ids: favs } }); load();
        } else if (md === "kitdel") { await api(`/api/media/kits/${encodeURIComponent(b.dataset.id)}`, { method: "DELETE" }); load(); }
      } catch (err) { fail(err); }
    });
    load();
    return { reload: load };
  }

  // The library's per-asset editor: name, tags, trim (sounds, clips, GIFs: non-destructive) and defaults for new overlays.
  function editAsset(el, a, saved) {
    const tr = a.trim || {}, d = a.defaults || {}, timed = ["sound", "video", "gif"].includes(a.kind) && a.duration;
    const num = (id, v, step, min, max, ph) => `<input type="number" id="${id}" step="${step}" min="${min}" max="${max}" value="${v ?? ""}" placeholder="${ph}" style="width:72px">`;
    el.innerHTML = `<div class="stack md-ed" style="gap:6px"><b>${esc(a.name)}</b>
      ${a.source ? `<span class="muted" style="font-size:11.5px">${esc(LIC[a.source.licence] || a.source.licence || "")} · ${esc(a.source.attribution_text || a.source.author || "")}</span>` : ""}
      <input type="text" id="md-en" value="${esc(a.name)}" maxlength="80"><input type="text" id="md-et" value="${esc((a.tags || []).join(", "))}" placeholder="Tags, comma separated">
      ${timed ? `<div class="row" style="gap:6px">Trim ${num("md-ti", tr.in, 0.05, 0, a.duration, "in")} → ${num("md-to", tr.out, 0.05, 0, a.duration, "out")} <span class="muted">of ${(+a.duration).toFixed(2)} s</span></div>` : ""}
      <div class="row" style="gap:6px;flex-wrap:wrap">Defaults: ${a.kind === "sound" || a.kind === "video" ? `vol ${num("md-dv", d.volume, 0.05, 0, 2, "1")}` : ""}
        ${a.kind !== "sound" ? `size ${num("md-dw", d.w, 0.05, 0.02, 1, "0.35")}` : ""} length ${num("md-dd", d.d, 0.1, 0.1, 600, "auto")}</div>
      <div class="row"><button class="btn xs primary" data-ed="save">Save</button><button class="btn xs" data-ed="close">Close</button></div></div>`;
    const v = (id) => { const x = el.querySelector(`#${id}`); return x && x.value !== "" ? +x.value : null; };
    el.querySelector("[data-ed=close]").onclick = () => { el.innerHTML = ""; };
    el.querySelector("[data-ed=save]").onclick = async () => {
      const ti = v("md-ti"), to = v("md-to"), defaults = {};
      [["volume", "md-dv"], ["w", "md-dw"], ["d", "md-dd"]].forEach(([k, id]) => { if (v(id) !== null) defaults[k] = v(id); });
      const body = { name: el.querySelector("#md-en").value.trim(), tags: el.querySelector("#md-et").value.split(",").map((t) => t.trim()).filter(Boolean), defaults };
      if (timed) body.trim = ti === null && to === null ? null : { in: ti ?? 0, out: to ?? +a.duration };
      try { saved(await api(`/api/assets/${encodeURIComponent(a.id)}`, { method: "PATCH", body })); el.innerHTML = ""; toast("Saved.", "ok"); } catch (e) { fail(e); }
    };
  }

  root.RWMedia = { mount, editAsset, used, recentIds };
})(window);
