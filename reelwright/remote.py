"""Phone remote: an optional Wi-Fi listener that serves Reelwright's phone web app (static/remote/) and a narrow, signed
API for it. The main app stays bound to 127.0.0.1 (it holds API keys and social tokens); this is a separate listener.

Security model (lan.py + relay.py's, plus body integrity and per-device secrets):
- Off by default. Binds 0.0.0.0 on its own port (38810; never the main app's 38765-38799 fallback range or the LAN
  receiver's) and drops every client that isn't on a private, link-local or loopback address (or 100.64/10, the range
  Tailscale-style VPNs hand out), before reading a byte.
- Pairing: Settings → Phone remote shows a QR of http://<lan-ip>:<port>/#pair=<device id>.<secret>. The fragment never
  leaves the phone. The phone keeps the secret in localStorage and signs every request with HMAC-SHA256 over method,
  path, time, a single-use nonce and the body's hash; the secret itself never travels. Every verified response carries a
  proof (HMAC over the nonce, status and response body) that the phone checks, so a look-alike on the network can't feed
  it fake data. One secret per phone; Revoke deletes it, and with it every media link it signed.
- The API calls the same server/pipeline functions as the desktop UI (account approval, delete rules, the learning
  memory). It never returns settings, keys, tokens, addresses or file paths and has no file browsing: media is served
  only by doc id through URLs signed with the phone's secret that expire after 30 minutes (Range requests supported).
- Rate-limited per address, request bodies capped at 64 KB, no CORS headers.
- The phone simulator (Settings → Phone remote → Developer) pairs a "sim" device through a shortcut link that only exists
  while that toggle is on, and a sim device is refused from any address but this PC's loopback. Its pages (sim.html,
  sim.js) are served only then, to loopback only.
"""
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi import APIRouter, HTTPException

from . import accounts, config, lan, log, obs, pipeline, store
from .config import STATIC
from .relay import OBS, OBSError

APP_DIR = STATIC / "remote"
MAIN_PORTS = range(config.DEFAULT_PORT, config.DEFAULT_PORT + 35)  # desktop.pick_port's fallback range (config.app_ports)
CLOCK_SKEW_S = 90               # signed requests are valid ±90 s (a phone whose clock is off adopts the PC's from the 401)
PAIR_TTL_S = 15 * 60            # a pairing code nobody used within 15 minutes is dead
MAX_CONNS, MAX_CONNS_PER_IP = 64, 16  # open connections: a LAN host can't take every thread
MEDIA_TTL_S = 30 * 60
MAX_BODY = 64 * 1024
MAX_PER_MIN = 600               # per address: video playback makes many small range requests
MAX_FAILS = 30                  # bad signatures per address per FAIL_WINDOW_S before it is shut out
FAIL_WINDOW_S = 600
SEEN_EVERY_S = 60
ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8", "100.64.0.0/10",
    "fc00::/7", "fe80::/10", "::1/128")]
PENDING = ("waiting_render", "scheduled", "held", "queued", "uploading", "needs_auth")
JOB_LABEL = {"analyze": "Analysis", "import_url": "Import", "find": "AI clip search", "export": "Render", "scan_source": "Channel scan",
             "virality": "Virality analysis", "train": "Style training", "research": "Platform research", "results": "Post results",
             "ingest_file": "Import from OBS", "track": "Subject tracking", "longform_capture": "Long form", "longform_export": "Long-form render",
             "angle": "Camera angle", "hud_scan": "Screen events", "cleanup": "Space saver", "proxy": "Preview copy",
             "upload": "Publish"}  # every pipeline.HANDLERS kind (tests/test_housekeeping.py)
OBS_ACTIONS = ("start_record", "stop_record", "save_replay", "clip_that", "marker")

_state: dict = {"http": None, "port": None, "error": ""}
_lock = threading.Lock()
_dev_lock = threading.Lock()  # every read-modify-write of remote_devices
_nonces: dict[tuple[str, str], float] = {}
_nonce_lock = threading.Lock()
_rl_lock = threading.Lock()
_hits: dict[str, deque] = {}
_fails: dict[str, deque] = {}


# ---------- rules ----------

def client_allowed(ip: str) -> bool:
    """Private, link-local or loopback (or a VPN's 100.64/10): the only addresses the listener talks to."""
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in ALLOWED_NETS)


def check_port(port) -> int:
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError("The phone remote's port must be a number.") from None
    taken = {config.DEFAULT_LAN_PORT, int(config.load()["lan_port"]), int(os.environ.get("REELWRIGHT_PORT", config.DEFAULT_PORT))}
    if not 1024 <= p <= 65535 or p in MAIN_PORTS or p in taken:
        raise ValueError(f"Pick a port from 1024 to 65535 outside {MAIN_PORTS.start}-{MAIN_PORTS.stop - 1} (the app's own ports) "
                         f"and not the OBS receiver's, e.g. {config.DEFAULT_REMOTE_PORT}.")
    return p


def _limited(ip: str) -> bool:
    now = time.monotonic()
    with _rl_lock:
        if len(_hits) > 4096:  # many addresses: forget them rather than grow without bound
            _hits.clear()
        f = _fails.get(ip)
        while f and now - f[0] > FAIL_WINDOW_S:
            f.popleft()
        h = _hits.setdefault(ip, deque())
        while h and now - h[0] > 60:
            h.popleft()
        if (f and len(f) >= MAX_FAILS) or len(h) >= MAX_PER_MIN:
            return True
        h.append(now)
    return False


AI_PER_HOUR = 20  # paid AI calls (titles, clip search, re-analysis) one paired phone may start per hour
_ai_calls: dict[str, deque] = {}


def _ai_budget(dev: dict, limit: int = AI_PER_HOUR) -> None:
    """A paired phone spends the creator's AI credit: a lost or misused one can't run up the bill (429 past the budget)."""
    now = time.monotonic()
    with _rl_lock:
        h = _ai_calls.setdefault(str(dev.get("id")), deque())
        while h and now - h[0] > 3600:
            h.popleft()
        if len(h) >= limit:
            raise HTTPException(429, f"This phone has used its {limit} AI requests for this hour; try again later or use the PC.")
        h.append(now)


def _failed(ip: str) -> None:
    with _rl_lock:
        if len(_fails) > 4096:
            _fails.clear()
        _fails.setdefault(ip, deque()).append(time.monotonic())


# ---------- devices and signatures ----------

def _devices() -> list[dict]:
    return [d for d in config.load()["remote_devices"] if isinstance(d, dict) and d.get("id") and d.get("secret")]


def _live(d: dict, now: float) -> bool:
    return bool(d.get("paired_at")) or now - float(d.get("created") or 0) < PAIR_TTL_S


def _edit_devices(fn) -> None:
    """Read-modify-write the device list as one step; never-used pairing codes past their 15 minutes are dropped too."""
    with _dev_lock:
        now = time.time()
        config.save({"remote_devices": fn([d for d in _devices() if _live(d, now)])})


def add_device(sim: bool = False) -> dict:
    """A new pending phone. Its secret is only the pairing code: pairing swaps it for a key derived with the phone.
    sim: the phone simulator's device (one at a time: an earlier simulator device is revoked)."""
    dev = {"id": secrets.token_hex(4), "secret": secrets.token_urlsafe(24), "name": "", "created": time.time(), "paired_at": None,
           **({"sim": True} if sim else {})}
    _edit_devices(lambda devs: [d for d in devs if not (sim and d.get("sim"))] + [dev])
    return dev


def _loopback(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    return (getattr(a, "ipv4_mapped", None) or a).is_loopback


def device_allowed(dev: dict, ip: str) -> bool:
    """A phone works from any address the listener accepts; the simulator's device only from this PC, and only while the
    developer toggle is on (turning it off also revokes it)."""
    return not dev.get("sim") or (_loopback(ip) and bool(config.load()["remote_dev_sim"]))


def derive_key(code: str, phone_nonce: str, pc_nonce: str) -> str:
    """The paired phone's key (hmac.js deriveKey makes the same): once paired, the QR code itself unlocks nothing."""
    return _mac(code, f"key\n{phone_nonce}\n{pc_nonce}")


def verify_device(did) -> dict | None:
    """The device if it is paired, or still within its pairing window; else None."""
    d = next((d for d in _devices() if d["id"] == did), None) if did else None
    return d if d and _live(d, time.time()) else None


def revoke(did: str) -> None:
    _edit_devices(lambda devs: [d for d in devs if d["id"] != did])
    store.delete("remote_seen", did)
    log.event("remote", f"phone {did} revoked")


def _mac(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _canon(method: str, path: str, ts: int, nonce: str, body: bytes) -> str:
    return f"{method}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}"


def sign(did: str, secret: str, method: str, path: str, body: bytes = b"", ts: int | None = None, nonce: str | None = None) -> dict:
    """The headers the phone sends (static/remote/hmac.js builds the same signature)."""
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or secrets.token_hex(16)
    return {"X-Reelwright-Device": did, "X-Reelwright-Time": str(ts), "X-Reelwright-Nonce": nonce,
            "X-Reelwright-Sig": _mac(secret, _canon(method, path, ts, nonce, body))}


def proof(secret: str, nonce: str, status: int, body: bytes) -> str:
    """What the phone checks on every answer: only the PC that holds its secret can make it."""
    return _mac(secret, f"server\n{nonce}\n{status}\n{hashlib.sha256(body).hexdigest()}")


def verify(headers, method: str, path: str, body: bytes) -> tuple[dict, str] | None:
    """(device, nonce) for a fresh, unused, valid signature over this exact request; else None."""
    dev = verify_device(headers.get("X-Reelwright-Device", ""))
    nonce, sig = headers.get("X-Reelwright-Nonce", ""), headers.get("X-Reelwright-Sig", "")
    try:
        ts = int(headers.get("X-Reelwright-Time", ""))
    except ValueError:
        return None
    now = time.time()
    if not dev or not nonce or len(nonce) > 64 or abs(now - ts) > CLOCK_SKEW_S:
        return None
    if not hmac.compare_digest(sig.encode(), _mac(dev["secret"], _canon(method, path, ts, nonce, body)).encode()):
        return None
    with _nonce_lock:  # single use: a captured request can't be replayed
        for k, t in list(_nonces.items()):
            if now - t > 2 * CLOCK_SKEW_S:
                del _nonces[k]
        if (dev["id"], nonce) in _nonces:
            return None
        _nonces[(dev["id"], nonce)] = now
    return dev, nonce


def _seen(did: str, ip: str) -> None:
    """Last-seen time for the PC's device list; in the store, not settings.json (no race with the user's saves)."""
    old = store.get("remote_seen", did)
    now = time.time()
    if not old or now - old.get("at", 0) > SEEN_EVERY_S or old.get("ip") != ip:
        store.put("remote_seen", {"id": did, "created": (old or {}).get("created", now), "at": now, "ip": ip})


# ---------- media: by doc id, through signed short-lived links ----------

def _media_sig(dev: dict, what: str, doc_id: str, exp: int) -> str:
    return _mac(dev["secret"], f"media\n{what}\n{doc_id}\n{exp}")


def _url(dev: dict, what: str, doc_id: str, ttl: int = MEDIA_TTL_S) -> str:
    exp = int(time.time()) + ttl
    return f"/remote/media/{what}/{doc_id}?d={dev['id']}&e={exp}&s={_media_sig(dev, what, doc_id, exp)}"


def media_url(did: str, what: str, doc_id: str, ttl: int = MEDIA_TTL_S) -> str:
    dev = verify_device(did)
    return _url(dev, what, doc_id, ttl) if dev else ""


def _inside(folder, rel):
    f = (folder / rel).resolve()
    return f if folder.resolve() in f.parents and f.is_file() else None


def _media_path(what: str, doc_id: str):
    """video / cover: a clip's render and its cover; poster: a project's poster. Nothing else is reachable."""
    try:
        if what == "poster":
            return _inside(pipeline.pdir(doc_id), "poster.jpg") if store.get("project", doc_id) else None
        clip = store.get("clip", doc_id)
        exp = (clip or {}).get("export") or {}
        rel = exp.get("file") if what == "video" and exp.get("status") == "done" else exp.get("cover") if what == "cover" else None
        return _inside(pipeline.pdir(clip["project_id"]), rel) if rel else None
    except (ValueError, TypeError, OSError):
        return None


def _has_poster(pid: str) -> bool:
    try:
        return (pipeline.pdir(pid) / "poster.jpg").is_file()
    except ValueError:
        return False


def _thumb(dev: dict, clip: dict | None, pid: str) -> str | None:
    if clip and ((clip.get("export") or {}).get("cover")):
        return _url(dev, "cover", clip["id"])
    return _url(dev, "poster", pid) if _has_poster(pid) else None


# ---------- what the phone sees ----------

def _404(doc, what="That no longer exists."):
    if not doc:
        raise HTTPException(404, what)
    return doc


# C:\… or C:/… or \\server\share\…: whole folder names (they may hold spaces), then the file name
_PATHS = re.compile(r"""(?:(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\)(?:[^\\/\n"'<>|:]*[\\/])*[^\\/\s"'<>|:]*""")


def _safe(v, n: int) -> str:
    """Text for the phone minus this PC's file paths (ffmpeg, yt-dlp and OBS errors quote them)."""
    return _PATHS.sub("…", str(v or "")[:2000])[:n]


def _https(u) -> str | None:
    return u if isinstance(u, str) and u.startswith("https://") else None


def _epoch(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _clip_card(dev: dict, c: dict, names: dict | None = None) -> dict:
    rendered = pipeline.clip_export_current(c)
    proj = (names or {}).get(c["project_id"]) if names is not None else (store.get("project", c["project_id"]) or {}).get("name")
    return {"id": c["id"], "project_id": c["project_id"], "project": proj, "title": c.get("title") or "Clip", "hook": c.get("hook") or "",
            "score": c.get("score"), "duration": round(c["end"] - c["start"], 1), "aspect": (c.get("edit") or {}).get("aspect"),
            "rating": c.get("rating") or 0, "rendered": rendered, "video": _url(dev, "video", c["id"]) if rendered else None,
            "poster": _thumb(dev, c, c["project_id"])}


def _post(dev: dict, u: dict, clip: dict | None = None) -> dict:
    clip = clip if clip is not None else store.get("clip", u.get("clip_id") or "")
    return {"id": u["id"], "platform": u.get("platform", "youtube"), "status": u.get("status"), "title": u.get("title") or (clip or {}).get("title"),
            "account": (pipeline._account_of(u) or {}).get("name"), "account_id": u.get("account_id"),
            "when": u.get("publish_at") or u.get("upload_at"), "at": u.get("uploaded_at"), "url": _https(u.get("url")),
            "clip_id": u.get("clip_id"), "error": _safe(u.get("error"), 200),
            "cover": _url(dev, "cover", clip["id"]) if clip and (clip.get("export") or {}).get("cover") else None}


def _review_clips() -> list[str]:
    """Ids of new AI picks the creator hasn't judged (newest first): unrated, never posted, not montages. Ids, from a few
    fields: callers read whole docs only for the cards they show."""
    posted = {u.get("clip_id") for u in store.find("upload", status="done")}
    return [cid for cid, source, rating, parts in store.fields("clip", "source", "rating", "parts")
            if source in ("ai", "auto") and not rating and not parts and cid not in posted]


def _pair(dev, data):
    """Single use: the phone's nonce and ours turn the code into its key, so a photo of the QR taken later is worthless."""
    pn = str(data.get("pn") or "")
    if not re.fullmatch(r"[0-9a-f]{16,64}", pn):
        raise ValueError("Update the phone app (reload the page) and pair again.")
    name = re.sub(r"[^\w .'()-]", "", str(data.get("name") or "")).strip()[:40] or "Phone"
    sn, done = secrets.token_hex(16), []

    def claim(devs):
        for d in devs:  # under the lock: of two phones racing with one code, only the first gets it
            if d["id"] == dev["id"] and not d.get("paired_at") and d["secret"] == dev["secret"]:
                d.update(name=name, paired_at=time.time(), secret=derive_key(dev["secret"], pn, sn))
                done.append(d)
        return devs

    _edit_devices(claim)
    if not done:
        raise HTTPException(409, "This phone is already paired. To pair it again, revoke it on the PC first.")
    log.event("remote", f"phone paired: {name} ({dev['id']})")
    return {"ok": True, "name": name, "pc": socket.gethostname(), "sn": sn}


def _dashboard(dev, data):
    jobs = sorted(store.find("job", status="queued") + store.find("job", status="running"), key=lambda j: -j["created"])[:12]
    ups = store.find("upload")
    upcoming = sorted((u for u in ups if u.get("status") in PENDING), key=lambda u: u.get("publish_at") or u.get("upload_at") or "")[:10]
    posted = sorted((u for u in ups if u.get("status") == "done"), key=lambda u: u.get("uploaded_at") or "", reverse=True)[:10]
    try:
        du = shutil.disk_usage(config.projects_dir())  # the drive the project folders live on (Settings › Storage)
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = None
    titles = {}
    for j in jobs:
        key = ("clip", j["clip_id"]) if j.get("clip_id") else ("project", j.get("project_id") or "")
        titles[j["id"]] = (store.get(*key) or {}).get("title" if key[0] == "clip" else "name")
    return {"paused": pipeline.paused(), "held": sum(u.get("status") == "held" for u in ups),
            "jobs": [{"id": j["id"], "kind": JOB_LABEL.get(j["kind"], j["kind"]), "status": j["status"], "progress": j.get("progress") or 0,
                      "message": j.get("message") or "", "title": titles.get(j["id"])} for j in jobs],
            "upcoming": [_post(dev, u) for u in upcoming], "posted": [_post(dev, u) for u in posted], "disk": disk,
            "review": len(_review_clips()), "projects": len(store.fields("project")),
            "obs": sum(c.get("status") == "connected" for c in store.find("obs"))}


def _autopilot(dev, data):
    from . import server
    if not isinstance(data.get("paused"), bool):
        raise ValueError("paused must be true or false.")
    return server.autopilot_switch({"paused": data["paused"]})


def _review(dev, data):
    clips = _review_clips()
    names = {p["id"]: p.get("name") for p in store.find("project")}
    segs: dict[str, list] = {}
    cards = []
    for c in filter(None, map(lambda cid: store.get("clip", cid), clips[:30])):
        pid = c["project_id"]
        if pid not in segs:
            try:
                segs[pid] = pipeline.transcript_segments(pid)
            except ValueError:
                segs[pid] = []
        text = " ".join(s.get("text", "") for s in segs[pid] if c["start"] <= s.get("start", -1) < c["end"])
        cards.append({**_clip_card(dev, c, names), "snippet": text.strip()[:400]})
    return {"clips": cards, "total": len(clips)}


def _clip(dev, data, cid):
    c = _404(store.get("clip", cid), "That clip no longer exists.")
    ups = store.find("upload", clip_id=cid)
    job = next((j for j in store.find("job", clip_id=cid, kind="export") if j["status"] in ("queued", "running")), None)
    exp = c.get("export") or {}
    return {**_clip_card(dev, c), "snippet": "" if c.get("parts") else pipeline.clip_text(c)[:600],
            "render": {"status": exp.get("status"), "progress": exp.get("progress") if job else None, "busy": bool(job)},
            "locked": any(u.get("status") == "done" for u in ups), "uploads": [_post(dev, u, c) for u in ups[:20]],
            "accounts": [{"id": a["id"], "platform": a["platform"], "name": a.get("name") or a.get("handle") or a["platform"].title()}
                         for a in accounts.usable()]}


def _rate(dev, data, cid):
    from . import server
    r = data.get("rating")
    if isinstance(r, bool) or r not in (-1, 0, 1):
        raise ValueError("rating must be 1 (keep), -1 (skip) or 0 (undo).")
    c = server.patch_clip(cid, {"rating": r})  # same path as the desktop's 👍/👎: the learning memory records it
    return {"ok": True, "rating": c["rating"]}


def _render(dev, data, cid):
    from . import server
    job = server.export_clip(cid)  # refuses a posted clip, like the desktop
    return {"ok": True, "job": job.get("id")}


def _future(v) -> str:
    t = _epoch(v) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else None
    if t is None or not math.isfinite(t) or t < time.time() - 60:
        raise ValueError("Pick a time in the future.")
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _default_description(c: dict) -> str:
    return (c.get("hook") or "") + ("\n\n#Shorts" if (c.get("edit") or {}).get("aspect") in ("9:16", "4:5") else "")


def _post_body(data: dict, title: str, description: str) -> dict:
    """The desktop Publish dialog's body from the phone's choices (the server checks every account is approved)."""
    ids = data.get("accounts")
    if not isinstance(ids, list) or not ids or not all(isinstance(a, str) for a in ids):
        raise ValueError("Pick at least one account.")
    for aid in ids:
        if (accounts.get(aid) or {}).get("platform") == "tiktok":
            raise ValueError("TikTok asks for its own posting choices (who can view it, comments, disclosures): publish to TikTok "
                             "from the Publish dialog on the PC.")
    when = _future(data["when"]) if data.get("when") else None
    privacy = data.get("privacy") if data.get("privacy") in ("public", "unlisted", "private") else None
    desc = data.get("description")
    body = {"accounts": ids, "title": str(data.get("title") or title or "").strip()[:100],
            "description": desc.strip()[:5000] if isinstance(desc, str) and desc.strip() else description,
            "caption": str(data.get("caption") or "").strip()[:2200] or None, "privacy": privacy,
            "made_for_kids": bool(data.get("made_for_kids")), "publish_at": when, "upload_at": None}
    if not body["title"]:
        raise ValueError("Add a title.")
    return body


def _publish(dev, data, cid):
    from . import server
    c = _404(store.get("clip", cid), "That clip no longer exists.")
    body = {**_post_body(data, c.get("title"), _default_description(c)), "tags": [str(h).lstrip("#") for h in (c.get("hashtags") or [])][:15]}
    ups = server.upload_clip(cid, body)  # every account checked (approved, or 400) before anything is queued
    return {"uploads": [{"id": u["id"], "platform": u["platform"], "account_id": u.get("account_id"), "status": u["status"]} for u in ups]}


def _delete(dev, data, cid):
    from . import server
    tick = data.get("remote") or []
    if not isinstance(tick, list):
        raise ValueError("remote must be a list of upload ids.")
    res = server.pipeline_delete({"items": [{"type": "clip", "id": cid}], "options": {"remote": {str(u): True for u in tick}}})
    return res["results"][0]


def _suggest(dev, data):
    from . import server
    return server.suggest_time({"platform": str(data.get("platform") or "youtube"), "n": 1})


def _project_card(dev, p: dict, n: int) -> dict:
    return {"id": p["id"], "name": p.get("name") or "Video", "status": p.get("status"), "stage": p.get("stage") or "",
            "progress": p.get("progress") or 0, "error": _safe(p.get("error"), 200), "created": p.get("created"), "clips": n,
            "duration": (p.get("meta") or {}).get("duration"), "poster": _url(dev, "poster", p["id"]) if _has_poster(p["id"]) else None}


def _projects(dev, data):
    counts = store.live_clips(expired=True)  # every clip, counted from an index
    return {"projects": [_project_card(dev, p, counts.get(p["id"], (0,))[0]) for p in store.newest("project", 80)]}


def _project(dev, data, pid):
    p = _404(store.get("project", pid), "That project no longer exists.")
    clips = sorted(store.find("clip", project_id=pid), key=lambda c: (c.get("source") == "manual", -(c.get("score") or 0)))
    names = {pid: p.get("name")}
    return {**_project_card(dev, p, len(clips)), "items": [_clip_card(dev, c, names) for c in clips[:80]]}


def _find(dev, data, pid):
    from . import server
    _ai_budget(dev)
    job = server.find(pid, {"query": str(data.get("query") or "").strip()[:300]})
    return {"ok": True, "job": job.get("id")}


def _import(dev, data):
    from . import server
    m = re.search(r"https?://[^\s<>\"']+", str(data.get("url") or ""))  # a share sheet often sends "Title https://…"
    if not m:
        raise ValueError("Paste a full video link (https://…).")
    host = (urllib.parse.urlsplit(m[0]).hostname or "").lower()
    try:  # every address the name resolves to (127.1, 0x7f000001, a DNS name pointing home…), not just how it is spelled
        addrs = {i[4][0] for i in socket.getaddrinfo(host, None)} if host else set()
    except OSError:
        addrs = set()  # unresolvable: the import itself fails with its own message
    if not host or host.rstrip(".") == "localhost" or any(client_allowed(a) or ipaddress.ip_address(a.split("%")[0]).is_unspecified for a in addrs):
        # ponytail: checked once here; a site redirecting to a home address isn't caught (same as the PC's own import)
        raise ValueError("Import a link to a video site, not to a computer on this network.")
    p = server.import_url({"url": m[0][:2000]})
    return {"ok": True, "project_id": p["id"]}


def _board(dev, data):
    from . import server
    clips = {c["id"]: c for c in store.slim("clip", "export.cover")}  # _thumb reads only the cover
    out = []
    for st in server.pipeline_board()["stages"]:
        items = [{"type": it["type"], "id": it["id"], "project_id": it["project_id"], "title": it.get("title"), "duration": it.get("duration"),
                  "locked": it.get("locked"), "pending": it.get("pending") or [], "updated": it.get("updated"),
                  "posts": [{"platform": p.get("platform"), "url": _https(p.get("url")), "account": p.get("account")} for p in it.get("posts") or []],
                  "thumb": _thumb(dev, clips.get(it["id"]) if it["type"] == "clip" else None, it["project_id"]) if it.get("thumb") else None}
                 for it in st["items"]]
        out.append({"key": st["key"], "label": st["label"], "items": items})
    return {"stages": out}


def _obs_list(dev, data):
    return {"connections": [{"id": c["id"], "name": c.get("name") or "OBS", "status": c.get("status") or "off", "enabled": bool(c.get("enabled")),
                             "error": _safe(c.get("error"), 200)} for c in store.find("obs")]}


def _clip_that(conn: dict) -> dict:
    """Save the replay buffer and import it. When the connection's watcher is live and imports replays, it does the import."""
    with OBS(obs.url(conn), obs.password(conn), events=False) as o:
        try:
            before = o.request("GetLastReplayBufferReplay").get("savedReplayPath")
        except OBSError:
            before = None
        o.request("SaveReplayBuffer")
        if conn.get("status") == "connected" and conn.get("import_replays", True):
            return {"ok": True, "message": "Replay saved; Reelwright is importing it."}
        for _ in range(30):  # OBS writes the file asynchronously and reports it when done
            time.sleep(0.5)
            try:
                path = o.request("GetLastReplayBufferReplay").get("savedReplayPath")
            except OBSError:
                path = None
            if path and path != before:
                p = obs.handle_file(conn, path, "replay")
                if not p:
                    raise RuntimeError("OBS saved the replay, but this PC can't read OBS's folder: add a folder mapping on the OBS link page.")
                return {"ok": True, "message": "Replay saved and imported.", "project_id": p["id"]}
    raise RuntimeError("OBS didn't report the saved replay in time.")


def _obs_action(dev, data, oid, what):
    conn = _404(store.get("obs", oid), "That OBS connection was removed.")
    if what not in OBS_ACTIONS:
        raise ValueError(f"Unknown OBS action {what}.")
    if not conn.get("enabled"):
        raise ValueError("That OBS connection is turned off on the PC.")
    if what == "clip_that":
        return _clip_that(conn)
    if what == "marker":  # a chapter in the recording (OBS 30.2+, Hybrid MP4); OBS has no stream-marker request
        try:
            with OBS(obs.url(conn), obs.password(conn), events=False) as o:
                o.request("CreateRecordChapter", {"chapterName": f"Reelwright {time.strftime('%H:%M:%S')}"})
        except OBSError as e:
            raise RuntimeError(f"OBS couldn't add a marker ({e}). Markers need OBS 30.2 or newer, recording in Hybrid MP4.") from None
        return {"ok": True, "message": "Marker added to the recording."}
    st = obs.action(conn, what)
    return {"ok": True, "recording": st.get("recording"), "replay_buffer": st.get("replay_buffer")}


def _activity(dev, data):
    """What happened lately, newest first: renders, analyses and imports, posts (with links) and failures."""
    names = {p["id"]: p.get("name") for p in store.find("project")}
    titles = dict(store.fields("clip", "title"))
    up_done = {}
    ev = []
    for j in store.newest("job", 300):
        if j["status"] not in ("done", "failed") or not j.get("finished"):
            continue
        if j["kind"] == "upload":
            up_done[j.get("upload_id")] = j["finished"]
            continue
        ok = j["status"] == "done"
        ev.append({"at": j["finished"], "ok": ok, "title": f"{JOB_LABEL.get(j['kind'], j['kind'])} {'finished' if ok else 'failed'}",
                   "detail": (titles.get(j.get("clip_id")) or names.get(j.get("project_id")) or "") + ("" if ok else f" — {_safe(j.get('error'), 160)}"),
                   "clip_id": j.get("clip_id"), "project_id": j.get("project_id"), "url": None})
    for u in store.find("upload"):
        if u.get("status") not in ("done", "failed", "needs_auth"):
            continue
        ok = u["status"] == "done"
        at = _epoch(u.get("uploaded_at")) if ok else up_done.get(u["id"]) or u.get("created")
        plat = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}.get(u.get("platform"), u.get("platform"))
        ev.append({"at": at, "ok": ok, "title": f"Posted to {plat}" if ok else f"{plat} post needs attention",
                   "detail": (u.get("title") or titles.get(u.get("clip_id")) or "") + ("" if ok else f" — {_safe(u.get('error'), 160)}"),
                   "clip_id": u.get("clip_id"), "project_id": u.get("project_id"), "url": _https(u.get("url"))})
    ev = sorted((e for e in ev if e["at"]), key=lambda e: e["at"], reverse=True)[:60]
    return {"events": ev, "now": time.time()}


# ---------- the full mobile app: editing, publish, schedule, long form, analysis ----------
# Every action goes through the desktop's own server functions (patch_clip's validation and the posted-clip lock,
# presets.edit_for, upload_clip's approval and QA gates, pipeline.reschedule), so the phone can't do what the PC refuses.

PHONE_EDIT = {"layout", "captions", "caption_style", "headline", "streamer_card", "cuts"}  # what the phone may change
PHONE_SUB = {"headline": {"on", "text", "theme"}, "streamer_card": {"on"}}
MEDIA_KINDS = ("sound", "sticker")


def _secs(v, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{name} must be a number of seconds.")
    return float(v)


def _editor(dev, data, cid):
    from . import media, routes_presets, server
    c = _404(store.get("clip", cid), "That clip no longer exists.")
    meta = (store.get("project", c["project_id"]) or {}).get("meta") or {}
    e = c.get("edit") or {}
    hl, card = e.get("headline") or {}, e.get("streamer_card") or {}
    job = next((j for j in store.find("job", clip_id=cid, kind="export") if j["status"] in ("queued", "running")), None)
    rendered = pipeline.clip_export_current(c)
    return {"id": cid, "title": c.get("title") or "Clip", "start": c["start"], "end": c["end"], "montage": bool(c.get("parts")),
            "source_duration": meta.get("duration"), "fps": float(meta.get("fps") or 30) or 30.0,
            "locked": server.posted(cid) or bool(c.get("expired")), "rev": c.get("rev"),
            "edit": {"layout": e.get("layout"), "captions": bool(e.get("captions")), "caption_style": e.get("caption_style"),
                     "headline": {k: hl.get(k) for k in ("on", "text", "theme")}, "streamer_card": {"on": bool(card.get("on"))},
                     "cuts": e.get("cuts") or [],
                     "overlays": [{k: o.get(k) for k in ("id", "kind", "asset_id", "t", "d", "text")} for o in e.get("overlays") or []]},
            "options": {"layouts": list(media.LAYOUTS), "caption_styles": list(server.EDIT_ENUMS["caption_style"]),
                        "themes": list(server.HEADLINE_ENUMS["theme"])},
            "presets": [{"id": p["id"], "name": _safe(p.get("name"), 60), "fav": bool(p.get("fav"))}
                        for p in routes_presets.list_presets(c["project_id"])["presets"]][:60],
            "assets": [{"id": a["id"], "kind": a["kind"], "name": _safe(a.get("name"), 60), "duration": a.get("duration")}
                       for a in store.find("asset") if a.get("kind") in MEDIA_KINDS][:200],
            "kits": None,  # hook: the media library's kits (routes_media.py) once that API lands; the assets above work today
            "rendered": rendered, "video": _url(dev, "video", cid) if rendered else None, "poster": _thumb(dev, c, c["project_id"]),
            "render": {"status": (c.get("export") or {}).get("status"), "busy": bool(job), "progress": (job or {}).get("progress")}}


def _edit(dev, data, cid):
    from . import server
    server._editable(cid)  # 409 once posted (or expired), before anything else
    if not data or data.keys() - {"start", "end", "edit"}:
        raise ValueError("Send start, end and/or edit.")
    body = {k: _secs(data[k], k) for k in ("start", "end") if k in data}
    if "start" in body and "end" in body and body["end"] - body["start"] < 0.5:
        raise ValueError("The clip must end at least 0.5 s after it starts.")
    if "edit" in data:
        ed = data["edit"]
        if not isinstance(ed, dict) or not ed or ed.keys() - PHONE_EDIT or any(
                isinstance(ed.get(k), dict) and ed[k].keys() - sub for k, sub in PHONE_SUB.items()):
            raise ValueError("From the phone you can change the layout, captions, headline (text, theme, on/off), streamer card "
                             "and cuts; the rest is on the PC.")
        body["edit"] = ed
    c = server.patch_clip(cid, body)  # clean_edit validates every value, like any desktop edit
    return {"ok": True, "rev": c["rev"], "start": c["start"], "end": c["end"]}


def _preset(dev, data, cid):
    from . import routes_presets, server
    c = server._editable(cid)
    if c.get("parts"):
        raise ValueError("Montages take their parts' edits: apply the preset to those clips.")
    pr = _404(store.get("preset", str(data.get("preset_id") or "")[:40]), "That preset no longer exists.")
    one = {**pr, "parts": {k: v for k, v in pr["parts"].items() if k in routes_presets.PARTS}}  # clip parts only: never app settings
    if one["parts"]:
        server.patch_clip(cid, {"edit": routes_presets.edit_for(one, c)})
    return {"ok": True}


def _media_add(dev, data, cid):
    """A library sound or sticker at the playhead (t, clip seconds), or {remove: overlay id}."""
    from . import server
    c = server._editable(cid)
    ovs = list((c.get("edit") or {}).get("overlays") or [])
    if "remove" in data:
        keep = [o for o in ovs if o.get("id") != str(data["remove"])]
        if len(keep) == len(ovs):
            raise HTTPException(404, "That sound or sticker is no longer on the clip.")
        ovs = keep
    else:
        a = store.get("asset", str(data.get("asset_id") or "")[:40])
        if not a or a.get("kind") not in MEDIA_KINDS:
            raise ValueError("Pick a sound or a sticker from your library.")
        ovs.append({"kind": a["kind"], "asset_id": a["id"], "t": _secs(data.get("t", 0), "t")})
    server.patch_clip(cid, {"edit": {"overlays": ovs}})
    return {"ok": True}


def _meta(dev, data, cid):
    """AI title/description/tags through the desktop's route, with what Claude charged for it."""
    from . import ai, server
    _ai_budget(dev)
    before = {r["id"] for r in store.find("usage")}
    md = server.metadata(cid)
    costs = [r.get("cost") for r in store.find("usage") if r["id"] not in before]  # ponytail: a concurrent AI call counts too
    return {"title": _safe(md.get("title"), 100), "description": _safe(md.get("description"), 5000),
            "tags": [_safe(t, 60) for t in md.get("tags") or []][:30], "provider": ai.route("meta"),
            "cost": round(sum(x or 0 for x in costs), 6) if costs else None}


def _publish_info(dev, data, cid):
    from . import ai, platforms, server
    c = _404(store.get("clip", cid), "That clip no longer exists.")
    desc = _default_description(c)
    tv = platforms.template_vars(c, store.get("project", c["project_id"]), [str(h).lstrip("#") for h in c.get("hashtags") or []])
    default_tpl = config.load()["description_template"]
    accs, best = [], {}
    for a in accounts.all_accounts():
        tpl = a["description_template"] if a.get("description_template") is not None else default_tpl
        accs.append({"id": a["id"], "platform": a["platform"], "name": a.get("name") or a.get("handle") or a["platform"].title(),
                     "approved": bool(a.get("approved")), "template": _safe(platforms.render_template(tpl, {**tv, "description": desc}), 5000)})
    for plat in {a["platform"] for a in accs if a["approved"]}:
        try:
            best[plat] = (server.suggest_time({"platform": plat, "n": 1}).get("times") or [None])[0]
        except Exception:  # a best-time guess must never stop the dialog
            best[plat] = None
    return {"title": c.get("title") or "", "description": desc, "accounts": accs, "best": best, "locked": server.posted(cid),
            "ai": {"provider": ai.route("meta"), "ready": ai.ready("meta")}}


def _schedule(dev, data):
    from . import server
    v = server.schedule_view()
    clips = {r[0] for r in store.fields("clip")}  # ids only: is the cover's clip still there
    keys = ("id", "platform", "status", "auto", "account_id", "account", "when", "title", "clip_id", "longform_id", "project_id")
    items = [{**{k: i.get(k) for k in keys}, "error": _safe(i.get("error"), 200),
              "cover": _url(dev, "cover", i["clip_id"]) if i.get("cover") and i.get("clip_id") in clips else None} for i in v["items"]]
    return {"items": items, "paused": v["paused"],
            "accounts": [{"id": a["id"], "platform": a["platform"], "name": a.get("name") or a.get("handle") or a["platform"].title()}
                         for a in v["accounts"]]}


def _move(dev, data, uid):
    """Move a post (time and/or account) by the Schedule tab's rules: pipeline.reschedule, plus never into the past."""
    when = "keep"
    if "when" in data:
        when = None if data["when"] is None else _future(data["when"])
    aid = data.get("account_id")
    if aid is not None and not isinstance(aid, str):
        raise ValueError("account_id must be an account id.")
    if when == "keep" and not aid:
        raise ValueError("Pick a new time or account.")
    u = pipeline.reschedule(uid, when, None, aid or None)
    return {"ok": True, "when": u.get("publish_at") or u.get("upload_at"), "account_id": u.get("account_id"), "status": u.get("status")}


def _longforms(dev, data):
    from . import longform
    jobs = {j.get("longform_id") for j in store.find("job", kind="longform_export") if j["status"] in ("queued", "running")}
    return {"items": [{"id": d["id"], "project_id": d["project_id"], "title": d.get("title") or "Long form", "status": d.get("status"),
                       "duration": longform.length(d) if d.get("segments") else 0, "rendered": longform.export_current(d),
                       "busy": d["id"] in jobs, "locked": longform.posted(d["id"]), "error": _safe(d.get("error"), 200)}
                      for d in store.find("longform")[:60]]}


def _lf_render(dev, data, lid):
    from . import server
    job = server.export_longform(lid)  # refuses a posted, expired or empty long form, like the desktop
    return {"ok": True, "job": job.get("id")}


def _lf_publish(dev, data, lid):
    from . import server
    d = _404(store.get("longform", lid), "That long form no longer exists.")
    ups = server.upload_longform(lid, _post_body(data, d.get("title"), ""))  # approval per account, like a clip
    return {"uploads": [{"id": u["id"], "platform": u["platform"], "account_id": u.get("account_id"), "status": u["status"]} for u in ups]}


def _analyze(dev, data, pid):
    from . import server
    if config.DEMO:
        raise ValueError("Demo mode: analysis is off (the sample projects are already analysed).")
    _ai_budget(dev)
    p = _404(store.get("project", pid), "That project no longer exists.")
    if p.get("status") in ("importing", "queued", "analyzing"):
        raise ValueError("This video is already being imported or analysed.")
    server.reanalyze(pid)
    return {"ok": True}


API = [  # (method, path after /remote/api/, handler(dev, body, *groups))
    ("POST", r"pair", _pair), ("GET", r"dashboard", _dashboard), ("POST", r"autopilot", _autopilot), ("GET", r"review", _review),
    ("GET", r"clips/(\w{1,40})", _clip), ("POST", r"clips/(\w{1,40})/rate", _rate), ("POST", r"clips/(\w{1,40})/render", _render),
    ("POST", r"clips/(\w{1,40})/publish", _publish), ("POST", r"clips/(\w{1,40})/delete", _delete), ("POST", r"suggest", _suggest),
    ("GET", r"projects", _projects), ("GET", r"projects/(\w{1,40})", _project), ("POST", r"projects/(\w{1,40})/find", _find),
    ("POST", r"import", _import), ("GET", r"board", _board), ("GET", r"obs", _obs_list),
    ("POST", r"obs/(\w{1,40})/(\w{1,20})", _obs_action), ("GET", r"activity", _activity),
    ("GET", r"clips/(\w{1,40})/editor", _editor), ("POST", r"clips/(\w{1,40})/edit", _edit), ("POST", r"clips/(\w{1,40})/preset", _preset),
    ("POST", r"clips/(\w{1,40})/media", _media_add), ("POST", r"clips/(\w{1,40})/meta", _meta),
    ("GET", r"clips/(\w{1,40})/publish", _publish_info), ("GET", r"schedule", _schedule), ("POST", r"uploads/(\w{1,40})/move", _move),
    ("GET", r"longform", _longforms), ("POST", r"longform/(\w{1,40})/render", _lf_render),
    ("POST", r"longform/(\w{1,40})/publish", _lf_publish), ("POST", r"projects/(\w{1,40})/analyze", _analyze),
]


def _call(fn, dev, data, *args) -> tuple[int, dict]:
    try:
        return 200, fn(dev, data, *args)
    except HTTPException as e:
        return e.status_code, {"detail": e.detail if isinstance(e.detail, str) else "Refused."}
    except (RuntimeError, ValueError, TypeError) as e:  # RuntimeError covers OBSError and the pipeline's refusals
        return 400, {"detail": _safe(e, 300) or "Refused."}
    except Exception:
        log.error("remote", f"{fn.__name__} failed")
        return 500, {"detail": "Something went wrong on the PC (details in its Activity → Event log)."}


# ---------- the listener ----------

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".webmanifest": "application/manifest+json", ".png": "image/png", ".svg": "image/svg+xml"}
MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".png": "image/png"}
CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SIM_FILES = ("sim.html", "sim.js")  # the phone simulator's frame: only while the developer toggle is on, only to loopback


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"  # on Windows SO_REUSEADDR would let it share a port another program holds

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._open: dict = {}  # live connection -> client address
        self._open_lock = threading.Lock()

    def verify_request(self, request, client_address) -> bool:
        """Before a byte is read: strangers are dropped, and no address holds more than its share of connections.
        ponytail: caps connections, not their duration (a slow sender keeps its slot up to the 30 s idle timeout per read)."""
        ip = client_address[0]
        if not client_allowed(ip):
            return False
        with self._open_lock:
            if len(self._open) >= MAX_CONNS or sum(v == ip for v in self._open.values()) >= MAX_CONNS_PER_IP:
                return False
            self._open[request] = ip
        return True

    def shutdown_request(self, request):  # every connection ends here, served or refused
        with self._open_lock:
            self._open.pop(request, None)
        super().shutdown_request(request)

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], OSError):  # a phone dropping a video stream midway is normal; anything else isn't
            log.error("remote", "request failed")


class Handler(BaseHTTPRequestHandler):
    server_version = "Reelwright"
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, *a):  # quiet: the GUI app has no console
        pass

    def _out(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        for k, v in {"Content-Type": ctype, "Content-Length": str(len(body)), "X-Content-Type-Options": "nosniff",
                     "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", **(extra or {})}.items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj, dev: dict | None = None, nonce: str | None = None):
        body = json.dumps(obj).encode()
        self._out(code, body, "application/json", {"X-Reelwright-Proof": proof(dev["secret"], nonce, code, body)} if dev else None)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _not_allowed(self):  # every other verb: a plain 405 (unread body: the connection closes)
        self.close_connection = True
        self._json(405, {"detail": "Method not allowed."})

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _not_allowed

    def _route(self, method: str):
        if _state["http"] is not self.server:  # turned off or moved to another port: kept-alive connections end too
            self.close_connection = True
            return self._json(503, {"detail": "The phone remote is turned off on the PC."})
        if _limited(self.client_address[0]):
            return self._json(429, {"detail": "Too many requests; wait a minute."})
        path = self.path.split("?", 1)[0]
        if path.startswith("/remote/api/"):
            return self._api(method, path[len("/remote/api/"):])
        if path.startswith("/remote/media/") and method == "GET":
            return self._media(path)
        if method == "GET":
            return self._static(path)
        self._json(405, {"detail": "Method not allowed."})

    def _api(self, method: str, name: str):
        try:
            n = -1 if self.headers.get("Transfer-Encoding") else int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            if MAX_BODY < n <= 1024 * 1024:
                self.rfile.read(n)  # drain a modestly oversized body so the client reads the answer, not a reset
            self.close_connection = True
            return self._json(413, {"detail": "Request too large."})
        body = self.rfile.read(n) if n else b""
        ok = verify(self.headers, method, self.path, body)
        if not ok:
            _failed(self.client_address[0])
            return self._json(401, {"detail": "This phone isn't paired, or its clock is off.", "time": int(time.time())})
        dev, nonce = ok
        if not device_allowed(dev, self.client_address[0]):  # the simulator's device, from anywhere but this PC
            _failed(self.client_address[0])
            return self._json(401, {"detail": "This phone isn't paired, or its clock is off.", "time": int(time.time())})
        _seen(dev["id"], self.client_address[0])
        for m, rx, fn in API:
            g = re.fullmatch(rx, name)
            if g and m == method:
                break
        else:
            return self._json(404, {"detail": "Not found."}, dev, nonce)
        if not dev.get("paired_at") and fn is not _pair:
            return self._json(401, {"detail": "Finish pairing first.", "time": int(time.time())})
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return self._json(400, {"detail": "Send a JSON object."}, dev, nonce)
        code, out = _call(fn, dev, data, *g.groups())
        self._json(code, out, dev, nonce)

    def _static(self, path: str):
        name = "index.html" if path in ("/", "/share") else path[1:]
        f = APP_DIR / name
        sim = bool(config.load()["remote_dev_sim"]) and _loopback(self.client_address[0])  # the simulator, on this PC only
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,60}", name) or f.suffix not in TYPES or not f.is_file() or (name in SIM_FILES and not sim):
            return self._json(404, {"detail": "Not found."})
        frame = {"Content-Security-Policy": CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'"), "X-Frame-Options": "SAMEORIGIN"} \
            if sim else {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"}  # sim.html frames the real app (same origin only)
        self._out(200, f.read_bytes(), TYPES[f.suffix], {"Cache-Control": "no-cache", **frame})

    def _media(self, path: str):
        m = re.fullmatch(r"/remote/media/(video|cover|poster)/(\w{1,40})", path)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
        dev = verify_device(one("d"))
        try:
            exp = int(one("e"))
        except ValueError:
            exp = 0
        genuine = bool(m and dev and dev.get("paired_at") and device_allowed(dev, self.client_address[0]) and hmac.compare_digest(one("s").encode(), _media_sig(dev, m[1], m[2], exp).encode()))
        if not genuine or exp < time.time():
            if not genuine:  # a real link that merely expired (a page left open) is no attack: don't count it
                _failed(self.client_address[0])
            return self._json(403, {"detail": "This link expired or wasn't made for this phone."})
        f = _media_path(m[1], m[2])
        if not f:
            return self._json(404, {"detail": "Not found."})
        self._file(f, dev["id"])

    def _file(self, f, did: str):
        size = f.stat().st_size
        start, end, code = 0, size - 1, 200
        r = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if r and (r[1] or r[2]):  # one range (all a video player asks for); anything else gets the whole file
            if r[1]:
                start, end = int(r[1]), min(int(r[2]), size - 1) if r[2] else size - 1
            else:
                start = max(0, size - int(r[2])) if int(r[2]) else size
            if start >= size or start > end:
                return self._out(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            code = 206
        self.send_response(code)
        hdrs = {"Content-Type": MEDIA_TYPES.get(f.suffix.lower(), "application/octet-stream"), "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes", "Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff",
                **({"Content-Range": f"bytes {start}-{end}/{size}"} if code == 206 else {})}
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            left, n = end - start + 1, 0
            while left > 0:
                n += 1
                if n % 8 == 0 and (_state["http"] is not self.server or not verify_device(did)):  # revoked or turned off mid-stream
                    self.close_connection = True
                    break
                chunk = fh.read(min(left, 256 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def apply() -> dict:
    """Start, stop or move the listener to match settings. Safe to call any time."""
    s = config.load()
    with _lock:
        want = int(s["remote_port"]) if s["remote_enabled"] else None
        if _state["http"] and _state["port"] != want:
            srv = _state["http"]
            _state.update(http=None, port=None)
            srv.shutdown()
            srv.server_close()
        if want and not _state["http"]:
            try:
                host = "127.0.0.1" if config.DEMO else "0.0.0.0"  # the demo sandbox is for this PC's simulator only
                srv = Server((host, check_port(want)), Handler)
            except (OSError, ValueError) as e:
                _state["error"] = f"The phone remote can't listen on port {want}: {e}"
                return local_status()
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            _state.update(http=srv, port=want, error="")
        if not want:
            _state["error"] = ""
    return local_status()


def local_status() -> dict:
    """For Settings → Phone remote on the PC: never a device's secret."""
    s = config.load()
    seen = {d["id"]: d for d in store.find("remote_seen")}
    now = time.time()
    devs = [{"id": d["id"], "name": d.get("name") or "", "paired": bool(d.get("paired_at")), "paired_at": d.get("paired_at"),
             "last_seen": (seen.get(d["id"]) or {}).get("at"), "last_ip": (seen.get(d["id"]) or {}).get("ip"),
             "expires": None if d.get("paired_at") else float(d.get("created") or 0) + PAIR_TTL_S, "sim": bool(d.get("sim"))}
            for d in _devices() if _live(d, now)]
    return {"enabled": s["remote_enabled"], "running": bool(_state["http"]), "port": s["remote_port"], "error": _state["error"],
            "ips": lan.local_ips(), "hostname": socket.gethostname(), "devices": devs, "dev_sim": bool(s["remote_dev_sim"]), "demo": config.DEMO,
            "rescan_notice": bool(s["notice_remote_rescan"])}  # the port moved (config.migrate_ports): pair phones again


def qr_svg(text: str) -> str:
    import segno  # pure-Python QR encoder, only needed while pairing
    return segno.make(text, error="m", micro=False).svg_inline(scale=5, border=3, dark="#000", light="#fff")


def pair_link(device_id: str | None, ip: str | None) -> dict:
    """The QR for pairing a phone: a pending device (the one given, while it is still pending) at this PC's address."""
    ips = lan.local_ips()
    ip = (ip or (ips[0] if ips else "")).strip()
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        raise ValueError("Pick one of this PC's network addresses.") from None
    if not client_allowed(str(addr)):
        raise ValueError("That isn't a local network address: the phone remote only works on your own network.")
    dev = verify_device(device_id) if device_id else None
    if not dev or dev.get("paired_at"):
        dev = add_device()
    url = f"http://{addr}:{config.load()['remote_port']}/#pair={dev['id']}.{dev['secret']}"
    return {"device_id": dev["id"], "url": url, "svg": qr_svg(url), "expires": float(dev["created"]) + PAIR_TTL_S}


# ---------- the PC's own endpoints (main app, localhost only): Settings → Phone remote ----------

router = APIRouter()


@router.get("/api/remote")
def get_remote():
    return local_status()


@router.put("/api/remote")
def put_remote(body: dict):
    patch: dict = {}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise HTTPException(400, "enabled must be true or false.")
        patch["remote_enabled"] = body["enabled"]
    if "port" in body:
        try:
            patch["remote_port"] = check_port(body["port"])
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
    if "dev_sim" in body:
        if not isinstance(body["dev_sim"], bool):
            raise HTTPException(400, "dev_sim must be true or false.")
        patch["remote_dev_sim"] = body["dev_sim"]
    config.save(patch)
    if patch.get("remote_dev_sim") is False:  # off: the simulator's device goes too
        _edit_devices(lambda devs: [d for d in devs if not d.get("sim")])
    return apply()


def sim_link() -> dict:
    """The phone simulator's link: http://127.0.0.1:<port>/sim.html#pair=<sim device>.<code>. Loopback only; only while the
    developer toggle is on and the listener runs (the simulator drives the real phone app through the real signed API)."""
    if not config.load()["remote_dev_sim"]:
        raise ValueError("Turn on Settings → Phone remote → Developer: phone simulator first.")
    if not _state["http"]:
        raise ValueError("Turn on the phone remote first: the simulator loads the real phone app from it.")
    dev = add_device(sim=True)
    return {"device_id": dev["id"], "url": f"http://127.0.0.1:{_state['port']}/sim.html#pair={dev['id']}.{dev['secret']}"}


def _sim_window(url: str) -> bool:
    """A second desktop window at phone size, when the desktop app (pywebview) is running; else the caller opens a tab."""
    desk = sys.modules.get("reelwright.desktop")
    if not getattr(desk, "WINDOW", None):
        return False
    try:
        import webview
        webview.create_window("Reelwright: phone simulator", url, width=980, height=980, min_size=(420, 600))
        return True
    except Exception:
        log.error("remote", "simulator window failed")
        return False


@router.post("/api/remote/simulator")
def post_simulator(body: dict | None = None):
    try:
        link = sim_link()
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {**link, "window": _sim_window(link["url"])}


@router.post("/api/remote/demo")
def post_demo(body: dict | None = None):
    """One click: a separate demo sandbox (demo.py: its own temporary library and ports, nothing ever posted) in its own
    console window (close it to stop), opening its phone simulator in the browser."""
    if config.FROZEN:  # ponytail: the packaged .exe has no `demo` entry; add one to desktop.main when the exe needs it
        raise HTTPException(400, "The demo sandbox runs from the source folder: python -m reelwright demo --open")
    import subprocess
    subprocess.Popen([sys.executable.replace("pythonw.exe", "python.exe"), "-m", "reelwright", "demo", "--open", "--small"], cwd=str(config.ROOT),
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), start_new_session=sys.platform != "win32")
    return {"ok": True}


@router.post("/api/remote/pair")
def post_pair(body: dict | None = None):
    body = body or {}
    try:
        return pair_link(body.get("device_id"), body.get("ip"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.delete("/api/remote/devices/{did}")
def delete_device(did: str):
    revoke(did)
    return local_status()
