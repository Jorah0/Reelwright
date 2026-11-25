/* Month-view date maths for the Schedule page, in local time. Days are built with new Date(y, m, d[, h, min]) so month
   lengths, leap years and DST (23 h / 25 h days) come from the platform, never from adding 24 h. Pure logic, no DOM:
   checked under node by tests/test_presets.py. */
(function (root) {
  "use strict";
  const pad = (n) => String(n).padStart(2, "0");
  const dayKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parseKey = (k) => { const [y, m, d] = String(k).split("-").map(Number); return { y, m: m - 1, d }; };
  // whole calendar days from key a to key b (UTC arithmetic on the date parts: no DST hour can shift the count)
  const daysBetween = (a, b) => { const p = parseKey(a), q = parseKey(b); return Math.round((Date.UTC(q.y, q.m, q.d) - Date.UTC(p.y, p.m, p.d)) / 864e5); };
  const addMonths = (y, m, n) => { const t = y * 12 + m + n; return { y: Math.floor(t / 12), m: ((t % 12) + 12) % 12 }; };
  const monthOffset = (y, m, today = new Date()) => (y - today.getFullYear()) * 12 + m - today.getMonth();

  // Weeks (rows of 7 days) covering month m (0-11) of year y, starting on weekStart (1 = Monday, 0 = Sunday).
  function monthGrid(y, m, weekStart = 1) {
    const lead = (new Date(y, m, 1).getDay() - weekStart + 7) % 7, last = new Date(y, m + 1, 0).getDate();
    const rows = Math.ceil((lead + last) / 7), out = [];
    for (let r = 0; r < rows; r++) {
      const row = [];
      for (let c = 0; c < 7; c++) {
        const dt = new Date(y, m, 1 - lead + r * 7 + c);
        row.push({ y: dt.getFullYear(), m: dt.getMonth(), d: dt.getDate(), key: dayKey(dt), wd: (dt.getDay() + 6) % 7, inMonth: dt.getMonth() === m });
      }
      out.push(row);
    }
    return out;
  }

  // The same local time of day as `iso` on the day `key` (ISO string). On a spring-forward day a time inside the
  // skipped hour moves forward by that hour, like any clock.
  function moveToDay(iso, key, fallback = [9, 0]) {
    const t = iso ? new Date(iso) : null, { y, m, d } = parseKey(key);
    const out = t ? new Date(y, m, d, t.getHours(), t.getMinutes(), t.getSeconds(), t.getMilliseconds()) : new Date(y, m, d, fallback[0], fallback[1]);
    return out.toISOString();
  }

  // The week view's offset (weeks from today, its first column) that shows the day `key`.
  const weekOffset = (key, today = new Date()) => Math.floor(daysBetween(dayKey(today), key) / 7);

  // {dayKey: [items sorted by time]} for items with a `when`.
  function byDay(items) {
    const out = {};
    for (const it of items) if (it.when) (out[dayKey(new Date(it.when))] ||= []).push(it);
    for (const k of Object.keys(out)) out[k].sort((a, b) => new Date(a.when) - new Date(b.when));
    return out;
  }

  const api = { dayKey, daysBetween, addMonths, monthOffset, monthGrid, moveToDay, weekOffset, byDay };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.RWCal = api;
})(typeof self !== "undefined" ? self : this);
