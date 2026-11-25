/* Timeline maths for Clip Studio's multi-layer timeline: frames and timecode, snapping, split, cuts (ripple delete),
   nudges, lane packing, keyframe interpolation and ruler ticks. Pure logic, no DOM: checked under node by
   tests/test_timeline.py. Times are seconds; items are {t, d} (clip time) unless a function says otherwise. */
(function (root) {
  "use strict";
  // NTSC rates are stored rounded by the probe (29.97): use the exact rational so frame n lands on n·1001/30000
  const NTSC = [[23.976, 24000 / 1001], [29.97, 30000 / 1001], [47.952, 48000 / 1001], [59.94, 60000 / 1001], [119.88, 120000 / 1001]];
  function fpsExact(fps) {
    fps = +fps || 30;
    const hit = NTSC.find(([r]) => Math.abs(r - fps) < 0.01);
    return hit ? hit[1] : fps;
  }
  const toFrame = (t, fps) => Math.round(t * fpsExact(fps) + 1e-9);
  const frameTime = (n, fps) => n / fpsExact(fps);
  const snapFrame = (t, fps) => frameTime(toFrame(t, fps), fps);

  // "H:MM:SS:FF · fN" (frames within the second, then the absolute frame number)
  function timecode(t, fps) {
    const n = toFrame(Math.max(0, t), fps), per = Math.round(fpsExact(fps)), s = Math.floor(n / fpsExact(fps) + 1e-9);
    const ff = n - Math.round(s * fpsExact(fps)), p2 = (v) => String(v).padStart(2, "0");
    return `${Math.floor(s / 3600)}:${p2(Math.floor((s % 3600) / 60))}:${p2(s % 60)}:${p2(Math.min(Math.max(ff, 0), per - 1))} · f${n}`;
  }

  // the nearest snap point within tol seconds (on its frame), else the nearest frame; on: false = frames only
  function snap(t, { fps = 30, points = [], tol = 0, on = true } = {}) {
    if (on) {
      let best = null, bd = tol;
      for (const p of points) { const d = Math.abs(p - t); if (d <= bd) { bd = d; best = p; } }
      if (best != null) return snapFrame(best, fps); // a word / marker edge, on its nearest frame
    }
    return snapFrame(t, fps);
  }

  // value of keyframes [[t, v…], …] (sorted) at time t, for value index i; flat before the first and after the last
  function interp(keys, t, i = 1) {
    if (!keys || !keys.length) return null;
    if (t <= keys[0][0]) return keys[0][i];
    for (let j = 1; j < keys.length; j++) {
      const [t0, v0, t1, v1] = [keys[j - 1][0], keys[j - 1][i], keys[j][0], keys[j][i]];
      if (t < t1) return t1 - t0 < 1e-9 ? v1 : v0 + ((v1 - v0) * (t - t0)) / (t1 - t0);
    }
    return keys[keys.length - 1][i];
  }

  // split an item at clip time `at` into two (keyframes, relative to the item's start, are cut and re-based); null outside
  function splitItem(item, at, newId) {
    const rel = at - item.t;
    if (!(rel > 1e-6 && rel < item.d - 1e-6)) return null;
    const left = { ...item, d: rel }, right = { ...item, id: newId, t: at, d: item.d - rel };
    if (Array.isArray(item.keys) && item.keys.length) {
      const n = item.keys[0].length, mid = [rel];
      for (let i = 1; i < n; i++) mid.push(interp(item.keys, rel, i));
      left.keys = [...item.keys.filter((k) => k[0] < rel), mid];
      right.keys = [[0, ...mid.slice(1)], ...item.keys.filter((k) => k[0] > rel).map((k) => [k[0] - rel, ...k.slice(1)])];
    }
    return [left, right];
  }

  // cuts = [[s, e], …] source seconds (edit.cuts). Add a range, merged and clamped into [lo, hi] (mirrors server._cuts)
  function addCut(cuts, a, b, lo, hi) {
    const spans = [...(cuts || []), [Math.min(a, b), Math.max(a, b)]].map(([s, e]) => [Math.max(s, lo), Math.min(e, hi)]).filter(([s, e]) => e > s)
      .sort((x, y) => x[0] - y[0]);
    const out = [];
    for (const [s, e] of spans) { const last = out[out.length - 1]; if (last && s <= last[1]) last[1] = Math.max(last[1], e); else out.push([s, e]); }
    return out;
  }
  // in this model every cut closes its gap in the render (later items keep their source time), so a delete is a ripple
  const rippleDelete = (cuts, a, b, lo, hi) => addCut(cuts, a, b, lo, hi);
  const removeCutAt = (cuts, t) => (cuts || []).filter(([s, e]) => !(t >= s && t < e));

  // the video track's pieces between [start, end]: cut spans and the split points, in order
  function segments(start, end, cuts, splits) {
    const pts = new Set([start, end]);
    for (const [s, e] of cuts || []) { if (s > start && s < end) pts.add(s); if (e > start && e < end) pts.add(e); }
    for (const p of splits || []) if (p > start && p < end) pts.add(p);
    const xs = [...pts].sort((a, b) => a - b), out = [];
    for (let i = 0; i + 1 < xs.length; i++) {
      const a = xs[i], b = xs[i + 1], m = (a + b) / 2;
      out.push({ a, b, cut: (cuts || []).some(([s, e]) => m >= s && m < e) });
    }
    return out;
  }

  // move items by df frames, each kept inside [0, len - d]
  const nudge = (items, df, fps, len) => items.map((it) => ({ ...it, t: Math.min(Math.max(it.t + df / fpsExact(fps), 0), Math.max(0, len - it.d)) }));

  // lane per item (input order) so overlapping items stack: greedy by start time
  function packLanes(items) {
    const order = items.map((it, i) => i).sort((a, b) => items[a].t - items[b].t), ends = [], lanes = [];
    for (const i of order) {
      const it = items[i];
      let l = ends.findIndex((e) => e <= it.t + 1e-9);
      if (l < 0) { l = ends.length; ends.push(0); }
      ends[l] = it.t + it.d; lanes[i] = l;
    }
    return lanes;
  }

  // ruler spacing for [t0, t1] over px: major (labelled) and minor seconds; frames = draw frame ticks too
  function ticks(t0, t1, px, fps) {
    const pps = px / Math.max(t1 - t0, 1e-6), steps = [1 / fpsExact(fps), 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600];
    const major = steps.find((s) => s * pps >= 70) || 7200, minor = steps.slice().reverse().find((s) => s < major && s * pps >= 8) || major;
    return { major, minor, frames: pps / fpsExact(fps) >= 6, pps };
  }

  // ids of items overlapping [a, b] (marquee selection)
  const inRange = (items, a, b) => items.filter((it) => it.t < Math.max(a, b) && it.t + it.d > Math.min(a, b)).map((it) => it.id);

  const api = { fpsExact, toFrame, frameTime, snapFrame, timecode, snap, interp, splitItem, addCut, rippleDelete, removeCutAt, segments,
    nudge, packLanes, ticks, inRange };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RWTimeline = api;
})(typeof self !== "undefined" ? self : this);
