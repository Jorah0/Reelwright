/* The editors' right-hand tool panel: collapses to a slim icon rail (Ctrl+\), each tool section folds under its heading,
   and tools can be hidden or reordered ("Customize tools"). A section is any `.sec[data-tool="id"]` inside the panel body;
   its first <h3> is its heading. Remembered per editor in localStorage, and through `persist` in the app's settings so it
   survives a reinstall. Layout is CSS only (flex `order`, a grid column change): re-rendered panels keep the state. */
(function (root) {
  "use strict";
  const lsKey = (ed) => `rw.panel.${ed}`;
  const read = (ed) => { try { const v = JSON.parse(localStorage[lsKey(ed)] || "null"); return v && typeof v === "object" ? v : null; } catch { return null; } };
  const write = (ed, v) => { try { localStorage[lsKey(ed)] = JSON.stringify(v); } catch { /* private mode: this session only */ } };
  const H = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  // opts: {editor, root (the grid that gives the panel its column), panel, body (scrolling list of .sec), tools: [{id, icon, label}],
  //        saved (server copy, used when localStorage has none), persist(editor, state), beforeJump(tool) (e.g. open its tab),
  //        custom: false (rail only: no folding/hiding menu, for panels whose tools are tabs)}
  function attach(opts) {
    const { editor, root: grid, panel, body, tools } = opts;
    const st = { min: false, col: [], hide: [], order: [], ...(read(editor) || opts.saved || {}) };
    const peek = new Set(); // hidden tools opened from the overflow menu: shown until the panel is set up again
    const known = (id) => tools.some((t) => t.id === id);
    const order = () => [...st.order.filter(known), ...tools.map((t) => t.id).filter((id) => !st.order.includes(id))];
    const shown = () => order().filter((id) => !st.hide.includes(id) || peek.has(id));
    const tool = (id) => tools.find((t) => t.id === id);
    const secOf = (id) => body.querySelector(`.sec[data-tool="${id}"]`);
    let saveT = 0, dirty = false;
    const flush = () => { clearTimeout(saveT); if (dirty) { dirty = false; opts.persist?.(editor, st); } };
    const save = () => { write(editor, st); dirty = true; clearTimeout(saveT); saveT = setTimeout(flush, 600); };

    const rail = document.createElement("div");
    rail.className = "tp-rail";
    rail.setAttribute("role", "toolbar");
    rail.setAttribute("aria-label", "Tools");
    panel.prepend(rail);
    if (opts.custom !== false) body.classList.add("tp-body"); // sections ordered with flex `order`

    function drawRail() {
      const hid = order().filter((id) => st.hide.includes(id));
      rail.innerHTML = `<button class="tp-btn tp-toggle" data-tp="min" title="${st.min ? "Expand" : "Collapse"} the tool panel (Ctrl+\\)" aria-label="${st.min ? "Expand" : "Collapse"} the tool panel" aria-expanded="${!st.min}">${st.min ? "⇤" : "⇥"}</button>
        ${shown().map((id) => { const t = tool(id); return `<button class="tp-btn${st.col.includes(id) ? " folded" : ""}" data-tp-go="${H(id)}" title="${H(t.label)}" aria-label="Go to ${H(t.label)}">${H(t.icon)}</button>`; }).join("")}
        <span class="tp-grow"></span>
        ${opts.custom === false ? "" : `<button class="tp-btn" data-tp="menu" title="Collapse all, expand all, customize tools${hid.length ? ` · ${hid.length} hidden` : ""}" aria-haspopup="menu" aria-label="Tool panel menu">⋯${hid.length ? `<i class="tp-n">${hid.length}</i>` : ""}</button>`}`;
    }
    function apply() {
      grid.classList.toggle("tp-min", !!st.min);
      const ord = order();
      body.querySelectorAll(".sec[data-tool]").forEach((el) => {
        const id = el.dataset.tool;
        el.style.order = String(ord.indexOf(id) + 1);
        el.classList.toggle("tp-col", st.col.includes(id));
        el.classList.toggle("tp-hide", st.hide.includes(id) && !peek.has(id));
        const h = el.querySelector(":scope > h3:first-child");
        if (h) { h.classList.add("tp-h"); h.tabIndex = 0; h.setAttribute("role", "button"); h.setAttribute("aria-expanded", String(!st.col.includes(id))); }
      });
      drawRail();
    }
    const fold = (id, on = !st.col.includes(id)) => { st.col = on ? [...new Set([...st.col, id])] : st.col.filter((x) => x !== id); save(); apply(); };
    function setMin(on) { st.min = on; save(); apply(); dispatchEvent(new Event("resize")); } // previews and timelines size from their boxes
    function jump(id) {
      if (st.hide.includes(id)) peek.add(id);
      st.min = false; st.col = st.col.filter((x) => x !== id); save(); apply(); dispatchEvent(new Event("resize"));
      opts.beforeJump?.(tool(id)); // e.g. switch to the tab that holds it
      const el = secOf(id);
      if (!el) return;
      el.scrollIntoView({ block: "start", behavior: "smooth" });
      el.classList.remove("tp-flash"); void el.offsetWidth; el.classList.add("tp-flash");
      el.querySelector(":scope > h3")?.focus({ preventScroll: true });
    }
    function menu(btn) {
      closeMenu();
      const hid = order().filter((id) => st.hide.includes(id));
      const m = document.createElement("div");
      m.className = "tp-menu"; m.setAttribute("role", "menu");
      m.innerHTML = `<button role="menuitem" data-tp="fold-all">Collapse all</button><button role="menuitem" data-tp="open-all">Expand all</button>
        <button role="menuitem" data-tp="custom">Customize tools…</button>
        ${hid.length ? `<div class="tp-sep">Hidden tools</div>${hid.map((id) => `<button role="menuitem" data-tp-go="${H(id)}">${H(tool(id).icon)} ${H(tool(id).label)}</button>`).join("")}` : ""}`;
      document.body.append(m);
      const r = btn.getBoundingClientRect(); // the menu lives on <body> (not zoomed): viewport px
      m.style.left = Math.max(4, Math.min(r.right - m.offsetWidth, innerWidth - m.offsetWidth - 4)) + "px";
      m.style.top = Math.max(4, Math.min(r.bottom + 4, innerHeight - m.offsetHeight - 4)) + "px";
      m.querySelector("button")?.focus();
      m.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { closeMenu(); act(b); } });
      m.addEventListener("keydown", (e) => {
        const bs = [...m.querySelectorAll("button")], i = bs.indexOf(document.activeElement);
        if (e.key === "Escape") { closeMenu(); btn.focus(); }
        else if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); bs[(i + (e.key === "ArrowDown" ? 1 : bs.length - 1)) % bs.length]?.focus(); }
      });
      setTimeout(() => document.addEventListener("pointerdown", outside, true));
    }
    const outside = (e) => { if (!e.target.closest(".tp-menu")) closeMenu(); };
    function closeMenu() { document.querySelectorAll(".tp-menu").forEach((m) => m.remove()); document.removeEventListener("pointerdown", outside, true); }
    function customize() {
      closeMenu();
      const wrap = document.createElement("div");
      wrap.className = "modal-bg tp-modal-back";
      const rows = () => order().map((id) => `<li class="tp-row" draggable="true" data-id="${H(id)}"><span class="tp-drag" aria-hidden="true">⠿</span>
          <label class="switch grow"><input type="checkbox" ${st.hide.includes(id) ? "" : "checked"} data-show="${H(id)}"> ${H(tool(id).icon)} ${H(tool(id).label)}</label>
          <button class="btn xs ghost" data-mv="-1" aria-label="Move ${H(tool(id).label)} up">↑</button><button class="btn xs ghost" data-mv="1" aria-label="Move ${H(tool(id).label)} down">↓</button></li>`).join("");
      wrap.innerHTML = `<div class="modal tp-cust" role="dialog" aria-modal="true" aria-label="Customize tools"><h2>Customize tools</h2>
        <p class="muted" style="font-size:12.5px;margin-top:-6px">Untick a tool to hide it (it stays in the rail's ⋯ menu). Drag, or use ↑ ↓, to reorder.</p>
        <ul class="tp-list">${rows()}</ul><div class="foot"><button class="btn ghost" data-reset>Reset</button><span class="grow"></span><button class="btn primary" data-done>Done</button></div></div>`;
      document.body.append(wrap);
      const list = wrap.querySelector(".tp-list");
      const redo = (focusId, sel) => { list.innerHTML = rows(); save(); apply(); if (focusId) list.querySelector(`[data-id="${focusId}"] ${sel}`)?.focus(); };
      const move = (id, to) => { const o = order().filter((x) => x !== id); o.splice(Math.max(0, Math.min(to, o.length)), 0, id); st.order = o; };
      const done = () => { wrap.remove(); rail.querySelector('[data-tp="menu"]')?.focus(); };
      wrap.addEventListener("change", (e) => { const id = e.target.dataset.show; if (!id) return; st.hide = e.target.checked ? st.hide.filter((x) => x !== id) : [...st.hide, id]; peek.delete(id); redo(id, "input"); });
      wrap.addEventListener("click", (e) => {
        if (e.target === wrap || e.target.closest("[data-done]")) return done();
        if (e.target.closest("[data-reset]")) { Object.assign(st, { col: [], hide: [], order: [] }); peek.clear(); return redo(); }
        const mv = e.target.closest("[data-mv]");
        if (mv) { const id = mv.closest("[data-id]").dataset.id, d = +mv.dataset.mv; move(id, order().indexOf(id) + d); redo(id, `[data-mv="${d}"]`); }
      });
      wrap.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); done(); } });
      let dragId = null;
      list.addEventListener("dragstart", (e) => { dragId = e.target.closest("[data-id]")?.dataset.id; e.dataTransfer.effectAllowed = "move"; });
      list.addEventListener("dragover", (e) => { if (dragId) e.preventDefault(); });
      list.addEventListener("drop", (e) => {
        e.preventDefault();
        const over = e.target.closest("[data-id]");
        if (!dragId || !over || over.dataset.id === dragId) return;
        const r = over.getBoundingClientRect(), after = e.clientY > r.top + r.height / 2, o = order().filter((x) => x !== dragId);
        move(dragId, o.indexOf(over.dataset.id) + (after ? 1 : 0)); dragId = null; redo();
      });
      wrap.querySelector("input")?.focus();
    }
    function act(b) {
      if (b.dataset.tpGo) return jump(b.dataset.tpGo);
      const a = b.dataset.tp;
      if (a === "min") setMin(!st.min);
      else if (a === "menu") menu(b);
      else if (a === "fold-all") { st.col = order(); save(); apply(); }
      else if (a === "open-all") { st.col = []; save(); apply(); }
      else if (a === "custom") customize();
    }
    rail.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) act(b); });
    const head = (e) => e.target.closest?.(".sec[data-tool] > h3.tp-h");
    body.addEventListener("click", (e) => { const h = head(e); if (h && !e.target.closest("button, a, input, select")) fold(h.parentElement.dataset.tool); });
    body.addEventListener("keydown", (e) => { const h = head(e); if (h && e.target === h && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); fold(h.parentElement.dataset.tool); } });
    const mo = new MutationObserver(() => { mo.disconnect(); apply(); mo.observe(body, { childList: true, subtree: true }); }); // re-rendered sections
    mo.observe(body, { childList: true, subtree: true });
    apply();
    return {
      apply, jump, toggle: () => setMin(!st.min), state: () => ({ ...st }),
      key(e) { // Ctrl+\ toggles the panel; true when handled
        if (e.key === "\\" && (e.ctrlKey || e.metaKey) && !e.altKey) { e.preventDefault(); setMin(!st.min); return true; }
        return false;
      },
      destroy() { mo.disconnect(); closeMenu(); document.querySelector(".tp-modal-back")?.remove(); flush(); },
    };
  }

  root.RWPanel = { attach, read };
})(typeof self !== "undefined" ? self : this);
