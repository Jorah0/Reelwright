/* Undo/redo for clip edits (Editor, Studio, long form). Every edit is a PATCH body; history keeps, per step, the body
   that undoes it (the old values of the keys it changes) and the body that redoes it. Pure logic, no DOM: checked under
   node by tests/test_editor_ux.py. Ratings and Keep are feedback, not edits, so they never enter the history. */
(function (root) {
  "use strict";
  const META = new Set(["rating", "keep"]);
  const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
  const keyOf = (b) => [...Object.keys(b).filter((k) => k !== "edit"), ...Object.keys(b.edit || {}).map((k) => "edit." + k)].sort().join();

  // The PATCH body that puts back what `body` is about to change, read from `prev` (the clip as it was). Keys the old
  // clip never had (a newer edit key) are left out: there's no value to go back to.
  function inverse(body, prev) {
    const out = {}, ed = {};
    for (const k of Object.keys(body || {})) {
      if (k === "edit") {
        for (const ek of Object.keys(body.edit || {})) {
          let v = ek === "track_manual" ? (prev.edit?.track?.manual ?? []) : prev.edit?.[ek];
          if (v === undefined && ek === "cuts") v = null; // not in DEFAULT_EDIT: a clip's first cut undoes to "no cuts"
          if (ek === "framing" && v == null) v = { offset_y: 0, zoom: 1 }; // the server merges framing: null can't clear it
          if (v !== undefined) ed[ek] = clone(v);
        }
      } else if (!META.has(k) && prev[k] !== undefined) out[k] = clone(prev[k]);
    }
    if (Object.keys(ed).length) out.edit = ed;
    return Object.keys(out).length ? out : null;
  }

  // max steps kept; changes to the same keys within `gap` ms (a slider drag, typing) merge into one step.
  function History(max = 100, gap = 1000) {
    let id = null, done = [], undone = [];
    const use = (clipId) => { if (clipId !== id) { id = clipId; done = []; undone = []; } };
    return {
      push(clipId, undo, redo, now = Date.now()) {
        use(clipId);
        undone = []; // any new edit ends the redo chain
        if (!undo) return;
        const top = done[done.length - 1], k = keyOf(redo);
        if (top && top.k === k && now - top.t < gap) { top.redo = clone(redo); top.t = now; return; }
        done.push({ undo: clone(undo), redo: clone(redo), k, t: now });
        if (done.length > max) done.shift();
      },
      undo(clipId) { use(clipId); const e = done.pop(); if (!e) return null; undone.push(e); return clone(e.undo); },
      redo(clipId) { use(clipId); const e = undone.pop(); if (!e) return null; done.push({ ...e, t: -Infinity }); return clone(e.redo); },
      clear() { done = []; undone = []; },
      size() { return [done.length, undone.length]; },
    };
  }

  const api = { inverse, History };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RWUndo = api;
})(typeof self !== "undefined" ? self : this);
