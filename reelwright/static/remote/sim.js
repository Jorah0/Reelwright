/* Phone simulator (sim.html): the real phone app in a device-sized frame, same origin. Device presets, rotation, the
   notch / home-bar safe areas (set as the app's --top/--bot) and a network toggle that slows or fails the app's fetches. */
"use strict";
const DEVICES = {
  iphone15: { label: "iPhone 15", w: 393, h: 852, top: 47, bot: 34, cls: "notch home", ua: "iPhone" },
  iphonese: { label: "iPhone SE", w: 375, h: 667, top: 20, bot: 0, cls: "", ua: "iPhone" },
  pixel8: { label: "Pixel 8", w: 412, h: 915, top: 24, bot: 16, cls: "home", ua: "Android" },
  android_s: { label: "Small Android", w: 360, h: 640, top: 24, bot: 0, cls: "", ua: "Android" },
  tablet: { label: "Tablet", w: 820, h: 1180, top: 24, bot: 20, cls: "tablet home", ua: "iPad" },
};
const $ = (s) => document.querySelector(s);
const ls = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private */ } } };
const frame = $("#app"), phone = $("#phone");
let dev = DEVICES[ls.get("rw-sim-dev")] ? ls.get("rw-sim-dev") : "iphone15", land = ls.get("rw-sim-land") === "1", net = "fast";

for (const [k, d] of Object.entries(DEVICES)) $("#dev").add(new Option(`${d.label} (${d.w}×${d.h})`, k, false, k === dev));

function layout() {
  const d = DEVICES[dev], w = land ? d.h : d.w, h = land ? d.w : d.h;
  frame.style.width = `${w}px`;
  frame.style.height = `${h}px`;
  phone.className = `phone ${land ? d.cls.replace("notch", "") : d.cls}`;
  $("#rot").classList.toggle("on", land);
  insets();
}
function insets() { // env(safe-area-inset-*) can't be faked in a frame: the app reads --top / --bot, so set those
  const d = DEVICES[dev], s = frame.contentDocument?.documentElement?.style;
  if (!s) return;
  s.setProperty("--top", `${land ? 0 : d.top}px`);
  s.setProperty("--bot", `${d.bot}px`);
}
function network() { // wrap the app's fetch: slow adds latency, offline fails like a dropped connection
  const w = frame.contentWindow;
  if (!w || w.__rwSim) return;
  const real = w.fetch.bind(w);
  w.__rwSim = true;
  w.fetch = (...a) => net === "offline" ? Promise.reject(new TypeError("Failed to fetch (simulated offline)"))
    : net === "slow" ? new Promise((r) => setTimeout(r, 1500)).then(() => real(...a)) : real(...a);
}
frame.addEventListener("load", () => { insets(); network(); });

$("#dev").onchange = (e) => { dev = e.target.value; ls.set("rw-sim-dev", dev); layout(); };
$("#rot").onclick = () => { land = !land; ls.set("rw-sim-land", land ? "1" : "0"); layout(); };
$("#net").onchange = (e) => { net = e.target.value; frame.contentWindow?.dispatchEvent(new Event(net === "offline" ? "offline" : "online")); };
$("#reload").onclick = () => frame.contentWindow?.location.reload();

// the pairing link (#pair=<id>.<code>) goes to the app once; then off this page's address bar
const pair = location.hash.startsWith("#pair=") ? location.hash : "";
history.replaceState(null, "", location.pathname);
layout();
frame.src = `/${pair || "#home"}`;
