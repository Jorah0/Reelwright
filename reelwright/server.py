"""HTTP API + static UI. Bound to localhost only (it holds your API keys and social tokens)."""
import json
import math
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import ai, assets, config, doctor, lan, learn, log, media, obs, pipeline, platforms, schedule, store, usage, youtube
from .config import PROJECTS, STATIC, YT_SECRET

VIDEO_EXT = media.VIDEO_EXT
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
PORT = int(os.environ.get("REELWRIGHT_PORT", 8765))

app = FastAPI(title="Reelwright")


@app.exception_handler(RuntimeError)
async def runtime_error(_: Request, e: RuntimeError):
    return JSONResponse({"detail": str(e)}, status_code=400)


LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@app.middleware("http")
async def no_stale_ui(request: Request, call_next):
    # Local-only app: refuse DNS-rebinding hosts and cross-site writes from pages open in the user's browser.
    origin = request.headers.get("origin")
    if request.headers.get("host") not in LOCAL_HOSTS or (
            origin and request.method != "GET" and origin.split("://", 1)[-1] not in LOCAL_HOSTS):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    resp = await call_next(request)
    if not request.url.path.startswith("/files/"):
        resp.headers["Cache-Control"] = "no-cache"  # revalidate UI assets so updates show up immediately
    return resp


@app.on_event("startup")
def _start():
    log.setup()
    log.event("app", "started")
    pipeline.start()
    obs.start()
    lan.apply()


def _404(doc, what="Not found"):
    if not doc:
        raise HTTPException(404, what)
    return doc


# ---------- settings ----------

@app.get("/api/settings")
def get_settings():
    return config.public()


@app.put("/api/settings")
def put_settings(patch: dict):
    if "anthropic_api_key" in patch and patch["anthropic_api_key"] is None:
        patch.pop("anthropic_api_key")
    if "ui_scale" in patch:  # per-component interface scale; a bad value must never make the UI unusable
        raw = patch["ui_scale"] if isinstance(patch["ui_scale"], dict) else {}
        patch["ui_scale"] = {k: round(min(max(float(v), 0.6), 2.0), 2) for k, v in raw.items()
                             if k in UI_SCALE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool) and v == v}
    if {"clip_min", "clip_target", "clip_max"} & patch.keys():  # lengths must make a usable range (target 0 = auto)
        cur = {**config.load(), **patch}
        try:
            lo, tg, hi = (float(cur[k]) for k in ("clip_min", "clip_target", "clip_max"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Clip lengths must be numbers.")
        if not (3 <= lo < hi <= 900) or (tg and not lo <= tg <= hi):
            raise HTTPException(400, "Clip lengths need min < max (3–900 s), with the target between them.")
    config.save(patch)
    return config.public()


UI_SCALE_KEYS = ("app", "sidebar", "text", "preview", "timeline", "panels")


# ---------- projects ----------

@app.post("/api/projects")
def upload_project(file: UploadFile = File(...), autopilot: str = Form("{}")):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VIDEO_EXT:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}.")
    p = pipeline.new_project(Path(file.filename).stem, json.loads(autopilot or "{}"))
    with open(pipeline.pdir(p["id"]) / f"source{ext}", "wb") as f:
        shutil.copyfileobj(file.file, f, 8 * 1024 * 1024)
    store.update("project", p["id"], {"source": f"source{ext}", "status": "queued", "stage": "Queued for analysis"})
    pipeline.submit("analyze", project_id=p["id"])
    return store.get("project", p["id"])


@app.post("/api/projects/url")
def import_url(body: dict):
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Enter a full http(s) link.")
    p = pipeline.new_project(url, body.get("autopilot"))
    pipeline.submit("import_url", project_id=p["id"], url=url)
    return p


@app.get("/api/projects")
def list_projects():
    projects = store.find("project")
    clips = store.find("clip")
    for p in projects:
        p["clip_count"] = sum(c["project_id"] == p["id"] for c in clips)
    return projects


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = _404(store.get("project", pid))
    p["clips"] = sorted(store.find("clip", project_id=pid), key=lambda c: (c["source"] == "manual", -c["score"]))
    return p


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, body: dict):
    return _404(store.update("project", pid, {k: v for k, v in body.items() if k in ("name", "autopilot")}))


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    try:
        pipeline.pdir(pid)
    except ValueError:
        raise HTTPException(404, "No such project")
    pipeline.delete_project(pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/analyze")
def reanalyze(pid: str):
    _404(store.get("project", pid))
    store.update("project", pid, {"status": "queued", "stage": "Queued for analysis", "progress": 0.0})
    return pipeline.submit("analyze", project_id=pid)


def _ready(pid: str) -> dict:
    p = _404(store.get("project", pid))
    if p["status"] != "ready":
        raise HTTPException(409, "Wait for analysis to finish.")
    return p


@app.post("/api/projects/{pid}/find")
def find(pid: str, body: dict):
    _ready(pid)
    num = lambda k: body[k] if isinstance(body.get(k), (int, float)) and not isinstance(body[k], bool) and 3 <= body[k] <= 900 else None  # noqa: E731
    return pipeline.submit("find", project_id=pid, query=(body.get("query") or "").strip(), count=body.get("count"),
                           target=num("target"), max_len=num("max_len"))


@app.post("/api/projects/{pid}/virality")
def project_virality(pid: str, body: dict):
    _ready(pid)
    return pipeline.submit("virality", project_id=pid, query=(body.get("query") or "").strip())


@app.post("/api/projects/{pid}/montage")
def montage(pid: str, body: dict):
    _ready(pid)
    return pipeline.make_montage(pid, body.get("clip_ids", []), body.get("title", ""))


@app.post("/api/projects/{pid}/autopilot/run")
def run_autopilot(pid: str, body: dict):
    p = _404(store.update("project", pid, {"autopilot": {**body, "enabled": True}}))
    if p["status"] == "ready":
        pipeline._autopilot(pid)
    return p


# ---------- clips ----------

@app.post("/api/projects/{pid}/clips")
def create_clip(pid: str, body: dict):
    p = _ready(pid)
    s, e = float(body["start"]), float(body["end"])
    if not 0 <= s < e <= p["meta"]["duration"] + 0.01:
        raise HTTPException(400, "Invalid clip range.")
    return pipeline.new_clip(pid, {"start": s, "end": e, "title": body.get("title") or "New clip", "score": 0})


EDIT_RANGES = {"crop_x": (0, 1), "crop_y": (0, 1), "speed": (0.25, 4), "brightness": (-1, 1), "contrast": (0, 3), "saturation": (0, 3),
               "gamma": (0.1, 3), "volume": (0, 5), "fade_in": (0, 10), "fade_out": (0, 10), "cam_x": (0, 1), "cam_y": (0, 1),
               "cam_w": (0.02, 1), "cam_h": (0.02, 1), "split": (0.15, 0.7), "zoom": (1, 4), "music_volume": (0, 2)}
EDIT_ENUMS = {"aspect": ("source", "9:16", "1:1", "4:5", "16:9"), "fit": ("crop", "blur"), "layout": ("single", "split"),
              "caption_style": ("bold", "clean", "boxed", "neon"), "caption_pos": ("low", "middle", "high"),
              "sticker_pos": ("top-left", "top-center", "top-right", "bottom-left", "bottom-right")}


def _keyframes(v, length: float) -> list:
    """Manual tracking keyframes: up to 500 [t (0..clip length), cx, cy (0..1)] or [t, cx, cy, zoom (1..4)]."""
    if not isinstance(v, list) or len(v) > 500:
        raise HTTPException(400, "track_manual must be a list of up to 500 [t, cx, cy] or [t, cx, cy, zoom] keyframes.")
    out = []
    for kf in v:
        try:
            nums = [float(n) for n in kf]
        except (TypeError, ValueError):
            raise HTTPException(400, "Each tracking keyframe must be [t, cx, cy] or [t, cx, cy, zoom] numbers.")
        if len(nums) not in (3, 4):
            raise HTTPException(400, "Each tracking keyframe must be [t, cx, cy] or [t, cx, cy, zoom] numbers.")
        t, x, y, *z = nums
        if not (0 <= t <= length + 0.01 and 0 <= x <= 1 and 0 <= y <= 1 and all(1 <= v <= 4 for v in z)):  # rejects NaN
            raise HTTPException(400, "Tracking keyframe out of range (t within the clip, cx/cy 0..1, zoom 1..4).")
        out.append([round(t, 3), round(x, 4), round(y, 4), *(round(v, 3) for v in z)])
    return sorted(out)


def _num(v, name: str, lo: float, hi: float) -> float:
    """A finite number clamped to [lo, hi] (like EDIT_RANGES), else 400."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be a number.")
    if not math.isfinite(f):
        raise HTTPException(400, f"{name} must be a number.")
    return min(max(f, lo), hi)


def _framing(v) -> dict:
    """Follow framing: offset_y (headroom, -0.4..0.4 of the crop height) and zoom (1..4); only the keys given."""
    if not isinstance(v, dict):
        raise HTTPException(400, "framing must be an object.")
    ranges = {"offset_y": (-0.4, 0.4), "zoom": (1.0, 4.0)}
    return {k: round(_num(v[k], f"framing.{k}", *r), 4) for k, r in ranges.items() if k in v and v[k] is not None}


TEXT_RANGES = {"size": (24, 200), "outline": (0, 20), "shadow": (0, 20), "spacing": (-5, 20)}  # size: px at 1080 wide


def _text_style(field: str, v, name: str):
    """One text-styling value (font, size, weight, color, outline, outline_color, shadow, spacing); None = the default."""
    if v is None:
        return None
    if field == "font":
        if not isinstance(v, str) or len(v) > 80:
            raise HTTPException(400, f"{name} must be a font family name.")
        return v.strip() or None
    if field == "weight":
        if v not in ("normal", "bold"):
            raise HTTPException(400, f"{name} must be normal or bold.")
        return v
    if field in ("color", "outline_color"):
        if not isinstance(v, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise HTTPException(400, f"{name} must be a #RRGGBB colour.")
        return v.upper()
    return round(_num(v, name, *TEXT_RANGES[field]), 2)


TEXT_FIELDS = ("font", "size", "weight", "color", "outline", "outline_color", "shadow", "spacing")
TEXT_KEYS = {f"{who}_{f}": f for who in ("caption", "title", "sticker") for f in TEXT_FIELDS}
OVERLAY_KINDS = ("image", "gif", "video", "sound", "text", "emoji")
OVERLAY_ANIMS = ("none", "pop", "fade", "shake", "slide")


def _overlays(v, clip: dict) -> list | None:
    """Clip overlays (≤ 60): media kinds must name a library asset of that kind; text/emoji carry ≤ 80 chars. Times are
    clip time (t inside the clip, d clamped to its end; sounds/videos default to the asset's length)."""
    if v is None or v == []:
        return None
    if not isinstance(v, list) or len(v) > 60:
        raise HTTPException(400, "overlays must be a list of up to 60 overlays.")
    length, out = clip["end"] - clip["start"], []
    for o in v:
        if not isinstance(o, dict) or o.get("kind") not in OVERLAY_KINDS:
            raise HTTPException(400, f"Each overlay needs a kind: {', '.join(OVERLAY_KINDS)}.")
        kind = o["kind"]
        t = _num(o.get("t", 0), "overlay t", -1e9, 1e9)
        if not 0 <= t < length:
            raise HTTPException(400, "Overlay starts outside the clip.")
        o2 = {"id": str(o.get("id") or store.new_id())[:40], "kind": kind, "t": round(t, 3)}
        if kind in ("text", "emoji"):
            text = o.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 80:
                raise HTTPException(400, "Text overlays need 1 to 80 characters.")
            o2["text"], default_d = text, 2.0
            o2.update({f: _text_style(f, o.get(f), f"overlay {f}") for f in TEXT_FIELDS if o.get(f) is not None})
        else:
            a = store.get("asset", str(o.get("asset_id") or ""))
            if not a or a["kind"] != kind:
                raise HTTPException(400, f"Overlay asset not found in the library (or not a {kind}).")
            o2["asset_id"], default_d = a["id"], (a.get("duration") or 3.0)
        d = default_d if o.get("d") is None else _num(o["d"], "overlay d", 0, 1e9)
        if d <= 0:
            raise HTTPException(400, "Overlay duration must be positive.")
        anim = o.get("anim") or "none"
        if anim not in OVERLAY_ANIMS:
            raise HTTPException(400, f"anim must be one of {', '.join(OVERLAY_ANIMS)}.")
        o2.update(d=round(min(d, length - t), 3), x=round(_num(o.get("x", 0.5), "overlay x", 0, 1), 4),
                  y=round(_num(o.get("y", 0.5), "overlay y", 0, 1), 4), w=round(_num(o.get("w", 0.3), "overlay w", 0.02, 1), 4),
                  anim=anim, volume=round(_num(o.get("volume", 1.0), "overlay volume", 0, 2), 3),
                  layer=int(_num(o.get("layer", 0), "overlay layer", -50, 50)), cutaway=bool(o.get("cutaway")) and kind == "video")
        out.append(o2)
    return out


def _words(v, clip: dict) -> list | None:
    """Caption words override: up to 2000 {s, e, w} with s ≤ e inside the clip (absolute source seconds), w ≤ 40 chars."""
    if v is None or v == []:
        return None
    if not isinstance(v, list) or len(v) > 2000:
        raise HTTPException(400, "words must be a list of up to 2000 {s, e, w}.")
    out = []
    for w in v:
        try:
            s, e, txt = float(w["s"]), float(w["e"]), w["w"]
        except (TypeError, KeyError, ValueError):
            raise HTTPException(400, "Each word must be {s, e, w}.")
        if not isinstance(txt, str) or len(txt) > 40 or not clip["start"] - 0.01 <= s <= e <= clip["end"] + 0.01:
            raise HTTPException(400, "Word out of range (inside the clip, s ≤ e, at most 40 characters).")
        out.append({"s": round(s, 3), "e": round(e, 3), "w": txt})
    return out


def clean_edit(edit: dict, clip: dict) -> dict:
    """Validate edit values at the API boundary (from the UI, the AI assistant, or anything else posting here).
    `clip` (start/end) bounds the list-valued keys; track_manual comes back as is for patch_clip to file under track."""
    out = {}
    for k, v in edit.items():
        if k == "track_manual":
            out[k] = _keyframes(v, clip["end"] - clip["start"])
            continue
        if k not in media.DEFAULT_EDIT:
            continue
        if k in TEXT_KEYS:
            out[k] = _text_style(TEXT_KEYS[k], v, k)
        elif k == "words":
            out[k] = _words(v, clip)
        elif k == "framing":
            out[k] = _framing(v)
        elif k == "overlays":
            out[k] = _overlays(v, clip)
        elif k in EDIT_RANGES:
            try:
                lo, hi = EDIT_RANGES[k]
                out[k] = min(max(float(v), lo), hi)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} must be a number.")
        elif k in EDIT_ENUMS:
            if v not in EDIT_ENUMS[k]:
                raise HTTPException(400, f"{k} must be one of {', '.join(EDIT_ENUMS[k])}.")
            out[k] = v
        elif isinstance(media.DEFAULT_EDIT[k], bool):
            out[k] = bool(v)
        elif k == "music":
            if v and not re.fullmatch(r"music/[\w.-]+", str(v)):
                raise HTTPException(400, "Invalid music file.")
            out[k] = str(v or "")
        else:  # free text: title_text, sticker_text
            out[k] = str(v or "")[:120]
    return out


POSTED = "Already posted — make a new version to edit it."
META_KEYS = {"title", "hook", "rating"}  # what stays editable after a clip is posted (doesn't change the video)


def posted(cid: str) -> bool:
    return bool(store.find("upload", clip_id=cid, status="done"))


def _editable(cid: str) -> dict:
    """The clip, unless it has been posted: the published render is final (pending uploads still use it)."""
    clip = _404(store.get("clip", cid))
    if posted(cid):
        raise HTTPException(409, POSTED)
    return clip


@app.patch("/api/clips/{cid}")
def patch_clip(cid: str, body: dict):
    clip = _editable(cid) if body.keys() - META_KEYS else _404(store.get("clip", cid))
    dur = store.get("project", clip["project_id"])["meta"]["duration"]

    def apply(c):
        changed = False
        if not c.get("parts"):
            for k in ("start", "end"):
                if k in body:
                    c[k] = round(min(max(float(body[k]), 0.0), dur), 3)
                    changed = True
        if "edit" in body:
            ed = clean_edit(body["edit"], c)
            if "track_manual" in ed:  # manual keyframes live beside the AI points, which stay
                c["edit"]["track"] = {"points": [], **(c["edit"].get("track") or {}), "manual": ed.pop("track_manual")}
            if "framing" in ed:  # partial framing updates keep the other key
                ed["framing"] = {**(c["edit"].get("framing") or {}), **ed["framing"]}
            c["edit"] = {**c["edit"], **ed}
            changed = True
        for k in ("title", "hook"):
            if k in body:
                c[k] = body[k]
        if "rating" in body:
            c["rating"] = max(-1, min(1, int(body["rating"])))
        if c["end"] - c["start"] < 0.5:
            raise HTTPException(400, "Clip must be at least 0.5 s long.")
        if changed:
            c["rev"] += 1

    clip = store.update("clip", cid, fn=apply)
    if clip and "rating" in body:
        pipeline.remember(clip, "rated")
    elif clip and body.keys() & {"start", "end", "edit", "title"}:
        pipeline.remember(clip, "refresh")  # keep an existing lesson in step with later trims and edit choices
    return clip


@app.post("/api/clips/{cid}/version")
def version_clip(cid: str):
    """A fresh, editable copy of a (usually posted) clip: same source, edit, overlays and track; no render or posts."""
    src = _404(store.get("clip", cid))
    root = src.get("version_of") or cid
    base = re.sub(r" \(v\d+\)$", "", src["title"])
    n = 2 + len(store.find("clip", version_of=root))
    copy = {k: json.loads(json.dumps(v)) for k, v in src.items() if k not in ("id", "created", "export", "posts", "rating",
                                                                               "track_error")}
    return store.put("clip", {**copy, "title": f"{base} (v{n})", "export": None, "posts": {}, "rating": 0, "version_of": root})


@app.delete("/api/clips/{cid}")
def delete_clip(cid: str):
    clip = store.get("clip", cid)
    if clip:
        pipeline.remember(clip, "dismissed")  # deleting an unrated AI pick is a (weak) "not this" lesson
    store.delete("clip", cid)
    return {"ok": True}


@app.post("/api/clips/{cid}/export")
def export_clip(cid: str):
    _editable(cid)
    return pipeline.request_export(cid)


def _face(cid: str):
    clip = _404(store.get("clip", cid))
    p = store.get("project", clip["project_id"])
    face = media.face_center(pipeline.pdir(p["id"]) / p["source"], clip["start"], clip["end"])
    if not face:
        raise HTTPException(422, "No face found in this clip — position the box by hand instead.")
    return clip, p, face


@app.post("/api/clips/{cid}/reframe")
def reframe(cid: str):
    _, _, face = _face(cid)
    return patch_clip(cid, {"edit": {"crop_x": round(face[0], 3), "crop_y": round(face[1], 3)}})


@app.post("/api/clips/{cid}/facecam")
def facecam(cid: str):
    clip, p, face = _face(cid)
    split = float(clip["edit"].get("split", 0.4))
    rect = media.facecam_rect(face, p["meta"]["width"], p["meta"]["height"], 1080 / max(round(1920 * split), 2))
    return patch_clip(cid, {"edit": {"layout": "split", **rect}})


def _track_jobs(cid: str) -> list[dict]:
    return store.find("job", clip_id=cid, kind="track")  # newest first


def track_request(body, clip: dict) -> tuple[str, dict | None]:
    """(mode, target) for a track job. No body repeats the clip's last choice; a target alone implies mode "target".
    target = {t (s from the clip start), x, y, w, h (fractions of the source frame)}."""
    if body is not None and not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object.")
    body, prev = body or {}, clip["edit"].get("track") or {}
    mode = body.get("mode") or ("target" if body.get("target") else prev.get("mode") or "auto")
    if mode not in ("auto", "face", "target"):
        raise HTTPException(400, "mode must be auto, face or target.")
    if mode != "target":
        return mode, None
    target = body["target"] if "target" in body else prev.get("target")
    try:
        t, x, y, w, h = (float(target[k]) for k in ("t", "x", "y", "w", "h"))
    except (TypeError, KeyError, ValueError):
        raise HTTPException(400, "Pick what to follow: target needs t, x, y, w, h numbers.")
    if not (0 <= t <= clip["end"] - clip["start"] + 0.01 and 0 <= x <= 1 and 0 <= y <= 1 and 0.01 < w <= 1 and 0.01 < h <= 1
            and x + w <= 1.001 and y + h <= 1.001):  # also rejects NaN
        raise HTTPException(400, "Target out of range (t within the clip, box inside the frame, w/h over 1%).")
    return mode, {"t": round(t, 3), "x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


@app.post("/api/clips/{cid}/track")
def track_clip(cid: str, body: dict | None = None):
    mode, target = track_request(body, _editable(cid))
    running = [j for j in _track_jobs(cid) if j["status"] in ("queued", "running")
               and j.get("mode", "auto") == mode and j.get("target") == target]
    return running[0] if running else pipeline.submit("track", clip_id=cid, mode=mode, target=target)


@app.get("/api/clips/{cid}/track")
def get_track(cid: str):
    clip = _404(store.get("clip", cid))
    tr, jobs = clip["edit"].get("track") or {}, _track_jobs(cid)
    return {"points": tr.get("points") or [], "manual": tr.get("manual") or [], "follow": bool(clip["edit"].get("follow")),
            "mode": tr.get("mode") or "auto", "target": tr.get("target"), "job": jobs[0] if jobs else None}


# ---------- pipeline board ----------

STAGES = [("harvested", "Harvested"), ("preclipped", "Pre-clipped"), ("longform", "Long form"), ("shortform", "Short form"),
          ("ready", "Ready to post"), ("uploaded", "Uploaded")]
PENDING = {"waiting_render", "scheduled", "held", "queued", "uploading", "needs_auth", "failed"}


def _iso(ts) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if isinstance(ts, (int, float)) else ts


def _clip_item(c: dict, ups: list[dict]) -> dict:
    """A clip card, in the furthest stage it has reached."""
    done = [u for u in ups if u["status"] == "done"]
    pending = [u for u in ups if u["status"] in PENDING]
    aspect = c["edit"].get("aspect")
    if done:
        stage = "uploaded"
    elif pipeline.clip_export_current(c) or pending:
        stage = "ready"
    elif c.get("rev", 1) <= 1 and not c.get("export") and not ups:
        stage = "preclipped"
    else:
        stage = "longform" if aspect in ("16:9", "source") else "shortform"
    exp, pid = c.get("export") or {}, c["project_id"]
    thumb = f"/files/{pid}/{exp['cover']}" if exp.get("cover") else         (f"/files/{pid}/poster.jpg" if (pipeline.pdir(pid) / "poster.jpg").exists() else None)
    stamps = [exp.get("at"), *(u.get("uploaded_at") for u in ups)]
    return {"type": "clip", "id": c["id"], "project_id": pid, "title": c.get("title"), "stage": stage, "thumb": thumb,
            "duration": round(c["end"] - c["start"], 3), "aspect": aspect,
            "updated": max([s for s in stamps if s] or [_iso(c.get("created"))]),
            "posts": [{"platform": u["platform"], "url": u.get("url"), "video_id": u.get("video_id"), "publish_at": u.get("publish_at")}
                      for u in done],
            "pending": [{"platform": u["platform"], "status": u["status"], "when": u.get("publish_at") or u.get("upload_at")}
                        for u in pending],
            "locked": bool(done), "edit_url": f"#/studio/{c['id']}"}


@app.get("/api/pipeline")
def pipeline_board():
    """Every project and clip on one board: each in exactly one stage, the furthest it has reached."""
    stages = {k: [] for k, _ in STAGES}
    clips, ups = store.find("clip"), store.find("upload")
    by_clip: dict[str, list] = {}
    for u in ups:
        by_clip.setdefault(u.get("clip_id"), []).append(u)
    with_clips = {c["project_id"] for c in clips}
    for p in store.find("project"):
        if p.get("status") in ("importing", "queued", "analyzing") or p.get("status") == "ready" and p["id"] not in with_clips:
            stages["harvested"].append({
                "type": "project", "id": p["id"], "project_id": p["id"], "title": p.get("name"), "stage": "harvested",
                "thumb": f"/files/{p['id']}/poster.jpg" if (pipeline.pdir(p["id"]) / "poster.jpg").exists() else None,
                "duration": (p.get("meta") or {}).get("duration"), "aspect": None, "updated": _iso(p.get("created")),
                "posts": [], "pending": [], "locked": False, "edit_url": f"#/editor/{p['id']}"})
    for c in clips:
        item = _clip_item(c, by_clip.get(c["id"], []))
        stages[item["stage"]].append(item)
    return {"stages": [{"key": k, "label": label, "items": stages[k]} for k, label in STAGES]}


# ---------- AI plan → approve → apply ----------

def _plan_context(clip: dict, proj: dict, library: list[dict]) -> str:
    """Everything Claude needs to plan one clip, with times relative to the clip start (what overlays use)."""
    s0, s1 = clip["start"], clip["end"]
    segs = pipeline.load_json(proj["id"], "transcript.json", {"segments": []})["segments"]
    sig = pipeline.load_json(proj["id"], "signals.json", {})
    lines = [f"[{max(g['start'] - s0, 0):.1f}-{g['end'] - s0:.1f}] {g['text'].strip()}" for g in segs if g["end"] > s0 and g["start"] < s1]
    loud = [f"{p['t'] - s0:.1f}s (energy {p['energy']})" for p in sig.get("loud", []) if s0 <= p["t"] <= s1]
    cuts = [f"{t - s0:.1f}s" for t in sig.get("scenes", []) if s0 < t < s1]
    edit = {k: v for k, v in clip["edit"].items() if k not in ("track", "words")}
    lib = [f"{a['id']} | {a['kind']} | {a['name']} | {', '.join(a.get('tags') or [])} | {a.get('duration') or 0:.1f}s" for a in library]
    taste, guide = pipeline.taste_examples(), pipeline.guidance()
    return "\n".join([
        f"Clip: {clip.get('title')!r}, {s1 - s0:.1f}s long, hook: {clip.get('hook') or '-'}",
        "Transcript (seconds from the clip start):", *(lines or ["(no speech)"]),
        f"Loud moments: {', '.join(loud) or 'none'}", f"Scene cuts: {', '.join(cuts) or 'none'}",
        f"Current edit settings: {json.dumps(edit)}",
        "Asset library (id | kind | name | tags | length) — built-in sound effects have ids starting sfx-:", *(lib or ["(empty)"]),
        *([f"Creator's style guidance:\n{guide}"] if guide else []), *([f"Creator's taste:\n{taste}"] if taste else [])])


def _plan_patch(item, clip: dict) -> dict:
    """One plan item -> its validated patch (HTTPException when Claude proposed something invalid, e.g. a made-up asset)."""
    sub = getattr(item, item.kind)
    raw = sub.model_dump(exclude_none=True) if sub else {}
    if item.kind == "overlay":
        return _overlays([raw], clip)[0] if raw else {}
    if item.kind == "track":
        out = {"mode": track_request({"mode": raw["mode"]}, clip)[0]} if raw.get("mode") else {}
        fr = _framing({k: raw[k] for k in ("offset_y", "zoom") if k in raw})
        return {**out, **({"framing": fr} if fr else {})}
    return clean_edit(raw, clip)  # edit / captions: exactly what PATCH would accept


@app.post("/api/clips/{cid}/plan")
def plan_clip(cid: str, body: dict | None = None):
    clip = _404(store.get("clip", cid))
    prompt = str((body or {}).get("prompt") or "")[:2000]
    plan = ai.plan(_plan_context(clip, store.get("project", clip["project_id"]), assets.listing()), prompt)
    items, dropped = [], 0
    for it in plan.items:
        try:
            patch = _plan_patch(it, clip)
        except HTTPException:
            patch = None
        if not patch:
            dropped += 1
            continue
        items.append({"id": store.new_id(), "kind": it.kind, "title": it.title[:120], "why": it.why[:400], "patch": patch})
    doc = store.put("plan", {"clip_id": cid, "prompt": prompt, "summary": plan.summary, "items": items, "dropped": dropped})
    return {"plan_id": doc["id"], "summary": plan.summary, "items": items, "dropped": dropped}


@app.post("/api/clips/{cid}/apply")
def apply_plan(cid: str, body: dict):
    clip = _editable(cid)
    plan = store.get("plan", str(body.get("plan_id") or ""))
    if not plan or plan["clip_id"] != cid:
        raise HTTPException(404, "Plan not found for this clip.")
    picked = set(body.get("item_ids") or [])
    edit, overlays, mode = {}, [], None
    for it in plan["items"]:
        if it["id"] not in picked:
            continue
        if it["kind"] == "overlay":
            overlays.append(it["patch"])
        elif it["kind"] == "track":
            mode = it["patch"].get("mode") or mode
            if "framing" in it["patch"]:
                edit["framing"] = {**edit.get("framing", {}), **it["patch"]["framing"]}
        else:
            edit.update(it["patch"])
    if overlays:  # added to the clip's overlays, never replacing them
        edit["overlays"] = (clip["edit"].get("overlays") or []) + overlays
    if edit:
        patch_clip(cid, {"edit": edit})  # the same validation as a manual edit
    if mode:
        track_clip(cid, {"mode": mode})
    return store.get("clip", cid)


# ---------- media library ----------

@app.get("/api/assets")
def list_assets():
    return assets.listing()


@app.post("/api/assets")
def upload_asset(file: UploadFile = File(...), name: str = Form(""), tags: str = Form("")):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in assets.KINDS:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'} — use {', '.join(sorted(assets.KINDS))}.")
    tmp = assets.ASSETS / f".upload-{store.new_id()}{ext}"
    try:
        size = 0
        with open(tmp, "wb") as f:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > assets.MAX_BYTES:
                    raise HTTPException(413, "That file is over 200 MB.")
                f.write(chunk)
        return assets.add(tmp, file.filename, name, tags.split(","))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp.unlink(missing_ok=True)


@app.get("/api/fonts")
def list_fonts():
    return assets.fonts()


@app.delete("/api/assets/{aid}")
def delete_asset(aid: str):
    try:
        assets.remove(aid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/clips/{cid}/music")
def music(cid: str, file: UploadFile = File(...)):
    clip = _404(store.get("clip", cid))
    ext = Path(file.filename or "").suffix.lower()
    if ext not in AUDIO_EXT:
        raise HTTPException(400, f"Unsupported audio type {ext or '(none)'}.")
    safe = re.sub(r"[^\w.-]+", "_", Path(file.filename).stem)[:60] + ext
    (pipeline.pdir(clip["project_id"]) / "music").mkdir(exist_ok=True)
    with open(pipeline.pdir(clip["project_id"]) / "music" / safe, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return patch_clip(cid, {"edit": {"music": f"music/{safe}"}})


@app.post("/api/clips/{cid}/assist")
def assist(cid: str, body: dict):
    clip = _editable(cid)
    p = store.get("project", clip["project_id"])
    segs = pipeline.load_json(p["id"], "transcript.json", {"segments": []})["segments"]
    edit = {k: v for k, v in clip["edit"].items() if k not in ("track", "words", "overlays")}  # bulky lists it can't edit
    patch = ai.assist(body.get("instruction", ""), clip, edit, segs, p["meta"]["duration"])
    explanation = patch.pop("explanation", "")
    update = {k: patch.pop(k) for k in ("start", "end") if k in patch}
    if patch:
        update["edit"] = patch
    return {"clip": patch_clip(cid, update) if update else clip, "explanation": explanation}


@app.post("/api/clips/{cid}/metadata")
def metadata(cid: str):
    clip = _404(store.get("clip", cid))
    segs = pipeline.load_json(clip["project_id"], "transcript.json", {"segments": []})["segments"]
    return ai.youtube_meta(clip, segs, clip["edit"]["aspect"] in ("9:16", "4:5"), pipeline.guidance())


@app.post("/api/clips/{cid}/virality")
def clip_virality(cid: str, body: dict):
    _404(store.get("clip", cid))
    return pipeline.submit("virality", clip_id=cid, query=(body.get("query") or "").strip())


@app.post("/api/clips/{cid}/upload")
def upload_clip(cid: str, body: dict):
    _404(store.get("clip", cid))
    targets = body.get("platforms") or [body.get("platform", "youtube")]
    return [pipeline.request_upload(cid, {**body, "platform": t}) for t in targets]


# ---------- uploads / jobs / reports ----------

@app.get("/api/uploads")
def uploads():
    ups = store.find("upload")
    clips = {c["id"]: c for c in store.find("clip")}
    for u in ups:
        u.setdefault("platform", "youtube")
        c = clips.get(u["clip_id"])
        u["clip"] = {"title": c["title"], "start": c["start"], "end": c["end"], "export": c.get("export")} if c else None
    return ups


@app.delete("/api/uploads/{uid}")
def cancel_upload(uid: str):
    u = _404(store.get("upload", uid))
    if u["status"] == "done":
        store.delete("upload", uid)  # removes it from the list; the published post stays online
    else:
        store.update("upload", uid, {"status": "canceled"})
    return {"ok": True}


@app.patch("/api/uploads/{uid}")
def move_upload(uid: str, body: dict):
    """Schedule tab: change a post's time (null = post as soon as possible) and/or platform."""
    _404(store.get("upload", uid))
    return pipeline.reschedule(uid, body["when"] if "when" in body else "keep", body.get("platform"))


# ---------- schedule ----------

@app.get("/api/schedule")
def schedule_view():
    clips = {c["id"]: c for c in store.find("clip")}
    items = []
    for u in store.find("upload"):
        if u.get("status") in ("canceled",) or (u.get("status") == "done" and not (u.get("publish_at") and not pipeline._due({"upload_at": u["publish_at"]}))):
            continue  # already out (a YouTube post still waiting for its publish time stays visible and movable)
        c = clips.get(u["clip_id"]) or {}
        exp = c.get("export") or {}
        items.append({"id": u["id"], "platform": u.get("platform", "youtube"), "status": u["status"], "auto": bool(u.get("auto")),
                      "when": u.get("publish_at") or u.get("upload_at"), "title": u.get("title") or c.get("title"),
                      "error": u.get("error", ""), "clip_id": u["clip_id"], "project_id": u.get("project_id"),
                      "cover": f"/files/{u.get('project_id')}/{exp['cover']}" if exp.get("cover") else None,
                      "category_id": u.get("category_id"), "privacy": u.get("privacy")})
    return {"items": items, "heat": schedule.heat(), "paused": pipeline.paused(), "connected": {p: platforms.connected(p) for p in platforms.PLATFORMS}}


@app.post("/api/schedule/suggest")
def suggest_time(body: dict):
    platform = body.get("platform", "youtube")
    if platform not in platforms.PLATFORMS:
        raise HTTPException(400, "Unknown platform.")
    taken = [t for t in schedule.scheduled_times(platform) if t != body.get("current")]
    times = schedule.suggest(platform, max(1, min(int(body.get("n", 1)), 20)), category=body.get("category_id"), taken=taken)
    return {"times": times, "source": schedule.slot_scores(platform)[1]}


@app.put("/api/autopilot")
def autopilot_switch(body: dict):
    """The one-click Autopilot pause: stops new scheduling everywhere and holds Autopilot's queued posts."""
    config.save({"autopilot_paused": bool(body.get("paused"))})
    log.event("autopilot", "paused" if body.get("paused") else "resumed")
    if not body.get("paused"):
        pipeline._scheduler_tick()  # release held posts now rather than within 15 s
    held = len(store.find("upload", status="held"))
    return {"paused": pipeline.paused(), "held": held}


@app.get("/api/tiktok/creator")
def tiktok_creator():
    try:
        return platforms.tiktok_creator()
    except platforms.AuthError as e:
        raise HTTPException(401, str(e))


@app.post("/api/uploads/{uid}/retry")
def retry(uid: str):
    _404(store.get("upload", uid))
    pipeline.retry_upload(uid)
    return store.get("upload", uid)


@app.get("/api/jobs")
def jobs():
    return store.find("job")[:80]


@app.get("/api/reports")
def reports():
    return store.find("report")[:50]


@app.get("/api/reports/{rid}")
def report(rid: str):
    return _404(store.get("report", rid))


# ---------- style profiles (custom training) ----------

@app.get("/api/profiles")
def profiles():
    return store.find("profile")


@app.post("/api/profiles")
def create_profile(body: dict):
    refs = [u.strip() for u in body.get("references", []) if u.strip().startswith(("http://", "https://"))]
    return store.put("profile", {"name": body.get("name") or "My style", "notes": body.get("notes", ""), "references": refs,
                                 "active": not store.find("profile"), "trained": None, "error": ""})


@app.patch("/api/profiles/{pid}")
def patch_profile(pid: str, body: dict):
    if body.get("active"):
        for p in store.find("profile"):
            store.update("profile", p["id"], {"active": p["id"] == pid})
    upd = {k: v for k, v in body.items() if k in ("name", "notes", "references", "active", "platforms", "auto_refresh")}
    if isinstance(body.get("answers"), dict):  # merge interview answers
        return _404(store.update("profile", pid, upd, fn=lambda p: p.setdefault("answers", {}).update(
            {str(k)[:60]: str(v)[:2000] for k, v in body["answers"].items()})))
    return _404(store.update("profile", pid, upd))


@app.post("/api/profiles/{pid}/questions")
def profile_questions(pid: str):
    p = _404(store.get("profile", pid))
    return ai.intake_questions(p["name"], p.get("notes", ""), p.get("references", []), p.get("answers", {}))


@app.post("/api/profiles/{pid}/research")
def profile_research(pid: str):
    _404(store.get("profile", pid))
    return pipeline.submit("research", profile_id=pid)


@app.delete("/api/profiles/{pid}")
def delete_profile(pid: str):
    store.delete("profile", pid)
    return {"ok": True}


@app.post("/api/profiles/{pid}/train")
def train(pid: str):
    _404(store.get("profile", pid))
    store.update("profile", pid, {"error": ""})
    return pipeline.submit("train", profile_id=pid)


@app.post("/api/profiles/{pid}/apply-theme")
def apply_theme(pid: str):
    p = _404(store.get("profile", pid))
    if not p.get("trained"):
        raise HTTPException(400, "Train this profile first.")
    t = p["trained"]["theme"]
    config.save({"caption_color": t["caption_color"], "default_aspect": t["aspect"]})
    return config.public()


# ---------- streaming sources ----------

@app.get("/api/sources")
def sources():
    return store.find("source")


@app.post("/api/sources")
def add_source(body: dict):
    platform, handle = body.get("platform"), (body.get("handle") or "").strip()
    if platform not in ("twitch", "kick", "youtube", "youtube_videos", "url") or not handle:
        raise HTTPException(400, "Pick a platform and enter a channel name (or a full URL for 'Other').")
    if platform == "url" and not handle.startswith(("http://", "https://")):
        raise HTTPException(400, "Enter a full link to a channel or playlist.")
    src = store.put("source", {"platform": platform, "handle": handle, "auto_import": bool(body.get("auto_import")),
                               "interval_hours": float(body.get("interval_hours", 2)), "max_per_scan": int(body.get("max_per_scan", 2)),
                               "autopilot": body.get("autopilot") or {"enabled": False}, "imported": [], "vods": [], "error": ""})
    pipeline.submit("scan_source", source_id=src["id"], auto=src["auto_import"])
    return src


@app.patch("/api/sources/{sid}")
def patch_source(sid: str, body: dict):
    upd = {k: v for k, v in body.items() if k in ("auto_import", "interval_hours", "max_per_scan", "autopilot")}
    return _404(store.update("source", sid, upd))


@app.delete("/api/sources/{sid}")
def delete_source(sid: str):
    store.delete("source", sid)
    return {"ok": True}


@app.post("/api/sources/{sid}/scan")
def scan(sid: str):
    _404(store.get("source", sid))
    return pipeline.submit("scan_source", source_id=sid, auto=False)


@app.post("/api/sources/{sid}/import")
def import_vods(sid: str, body: dict):
    src = _404(store.get("source", sid))
    vods = {v["id"]: v for v in src.get("vods", [])}
    out = [pipeline.import_vod(store.get("source", sid), vods[v]) for v in body.get("vod_ids", []) if v in vods]
    return [p for p in out if p]


# ---------- connections: YouTube / TikTok / Instagram ----------

@app.get("/api/connections")
def connections():
    st = platforms.status()
    st["tiktok"]["redirect_uri"] = platforms.tiktok_redirect_uri(PORT)
    return st


@app.get("/api/youtube")
def yt_status():
    return youtube.status()


@app.post("/api/youtube/secret")
async def yt_secret(file: UploadFile = File(...)):
    data = await file.read()
    try:
        cfg = json.loads(data)
        assert "installed" in cfg or "web" in cfg
    except (ValueError, AssertionError):
        raise HTTPException(400, "That isn't a Google OAuth client JSON (expected a Desktop app client).")
    YT_SECRET.write_bytes(data)
    return youtube.status()


@app.post("/api/youtube/connect")
def yt_connect():
    youtube.connect(on_connected=pipeline.requeue_needs_auth)
    return youtube.status()


@app.post("/api/connections/{platform}/disconnect")
def disconnect(platform: str):
    platforms.disconnect(platform)
    return connections()


@app.put("/api/connections/tiktok/app")
def tiktok_app(body: dict):
    if not body.get("client_key") or not body.get("client_secret"):
        raise HTTPException(400, "Enter both the Client key and Client secret from your TikTok developer app.")
    platforms.tiktok_set_app(body["client_key"], body["client_secret"])
    return connections()


@app.post("/api/connections/tiktok/connect")
def tiktok_connect():
    return {"url": platforms.tiktok_auth_url(PORT)}


@app.get("/oauth/tiktok/callback", response_class=HTMLResponse)
def tiktok_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        msg, ok = f"TikTok sign-in was not completed: {error_description or error}", False
    else:
        try:
            platforms.tiktok_callback(code, state, PORT)
            pipeline.requeue_needs_auth()
            msg, ok = "TikTok is connected to Reelwright. You can close this tab.", True
        except RuntimeError as e:
            msg, ok = str(e), False
    color = "#37d39a" if ok else "#ff5a6a"
    return (f"<html><body style='background:#0b0c10;color:#eceef6;font-family:system-ui;display:grid;place-items:center;height:100vh'>"
            f"<div style='text-align:center'><img src='/logo.svg' width=64><h2 style='color:{color}'>{'Connected' if ok else 'Not connected'}</h2>"
            f"<p>{msg.replace('<', '&lt;')}</p></div></body></html>")


@app.put("/api/connections/instagram")
def instagram(body: dict):
    if not body.get("access_token"):
        raise HTTPException(400, "Paste your Instagram access token.")
    platforms.instagram_set(body["access_token"], body.get("ig_user_id") or "")
    pipeline.requeue_needs_auth()
    return connections()


# ---------- learning & diagnostics ----------

@app.get("/api/learning")
def learning():
    """What Reelwright has learned from this creator, in a form the UI can show."""
    fb = pipeline.feedback(limit=10_000)
    model = learn.train(fb)
    counts: dict = {}
    for f in fb:
        counts[f["why"]] = counts.get(f["why"], 0) + 1
    return {"lessons": len(fb), "liked": sum(f["signal"] > 0 for f in fb), "disliked": sum(f["signal"] < 0 for f in fb),
            "by_kind": counts, "with_views": sum(f.get("views") is not None for f in fb),
            "model": {k: model[k] for k in ("n", "positive", "cv_acc", "baseline", "useful", "weights")} if model else None,
            "house_style": learn.house_style(fb), "trim_bias": learn.trim_bias(fb), "hints": learn.hints(fb, model),
            "results_at": config.load()["results_at"], "examples": pipeline.taste_examples()}


@app.delete("/api/learning")
def reset_learning():
    for f in store.find("feedback"):
        store.delete("feedback", f["id"])
    return learning()


@app.post("/api/learning/results")
def refresh_results():
    return pipeline.submit("results")


@app.get("/api/learning/export")
def export_learning():
    data = json.dumps(pipeline.export_learning(), indent=1)
    return Response(data, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="reelwright-learning.json"'})


@app.post("/api/learning/import")
def import_learning(file: UploadFile = File(...)):  # plain def: runs in the threadpool, never stalls the API
    raw = file.file.read(50 * 1024 * 1024 + 1)
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(400, "That file is too large for a learning export.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "That file isn't valid JSON.")
    return {**pipeline.import_learning(data), "learning": learning()}


@app.get("/api/doctor")
def run_doctor(deep: bool = False):
    return doctor.run(deep)


@app.get("/api/usage")
def ai_usage(days: int = 30):
    return usage.summary(max(1, min(days, 365)))


@app.get("/api/logs")
def logs(n: int = 300):
    return {"lines": log.tail(max(10, min(n, 2000)))}


# ---------- OBS: direct connections + network receiver ----------

def _obs_doc(body: dict, old: dict | None = None) -> dict:
    out: dict = {}
    if "name" in body:
        out["name"] = str(body["name"]).strip()[:60] or "OBS"
    if "host" in body:
        host = str(body["host"]).strip()
        if not host or any(c in host for c in " \"'<>"):
            raise HTTPException(400, "Enter the OBS PC's IP address or hostname (e.g. 192.168.1.20), or ws://host:port.")
        out["host"] = host
    if "port" in body:
        try:
            out["port"] = int(body["port"])
            assert 1 <= out["port"] <= 65535
        except (TypeError, ValueError, AssertionError):
            raise HTTPException(400, "Port must be 1-65535 (OBS default 4455).")
    if body.get("password") is not None and (body.get("password") or not old):  # blank on edit = keep the saved one
        out["password"] = str(body["password"])
    for k in ("enabled", "import_replays", "import_recordings"):
        if k in body:
            out[k] = bool(body[k])
    if "path_map" in body:
        rules = body["path_map"]
        if isinstance(rules, str):  # "D:/OBS => //GAMEPC/OBS" one per line
            rules = [[a.strip(), b.strip()] for a, _, b in (ln.partition("=>") for ln in rules.splitlines()) if a.strip() and b.strip()]
        out["path_map"] = [[str(a), str(b)] for a, b in rules][:20]
    if isinstance(body.get("autopilot"), dict):
        out["autopilot"] = body["autopilot"]
    return out


@app.get("/api/obs")
def obs_status():
    return obs.lan_status()


@app.post("/api/obs")
def add_obs(body: dict):
    doc = {"name": "OBS", "port": 4455, "password": "", "enabled": True, "import_replays": True, "import_recordings": False,
           "path_map": [], "autopilot": {"enabled": False}, "status": "connecting", "error": "", "imported": 0, "rev": 1,
           **_obs_doc({"host": "", **body})}
    return obs.public(store.put("obs", doc))


@app.patch("/api/obs/{oid}")
def patch_obs(oid: str, body: dict):
    old = _404(store.get("obs", oid))
    upd = _obs_doc(body, old)
    return obs.public(store.update("obs", oid, upd, fn=lambda c: c.update(rev=c.get("rev", 0) + 1)))


@app.delete("/api/obs/{oid}")
def delete_obs(oid: str):
    store.delete("obs", oid)
    return {"ok": True}


@app.post("/api/obs/{oid}/test")
def test_obs(oid: str):
    try:
        return obs.probe(_404(store.get("obs", oid)))
    except obs.OBSError as e:
        raise HTTPException(400, str(e))


@app.post("/api/obs/{oid}/action")
def obs_action(oid: str, body: dict):
    try:
        return obs.action(_404(store.get("obs", oid)), body.get("action", ""))
    except obs.OBSError as e:
        raise HTTPException(400, str(e))


@app.put("/api/lan")
def put_lan(body: dict):
    patch: dict = {}
    if "enabled" in body:
        patch["lan_receive"] = bool(body["enabled"])
    if "port" in body:
        try:
            port = int(body["port"])
            assert 1024 <= port <= 65535 and port != PORT
        except (TypeError, ValueError, AssertionError):
            raise HTTPException(400, f"Pick a port between 1024 and 65535 (not {PORT}, the app's own).")
        patch["lan_port"] = port
    if isinstance(body.get("autopilot"), dict):
        patch["obs_autopilot"] = body["autopilot"]
    config.save(patch)
    lan.apply()
    return obs.lan_status()


@app.post("/api/lan/token")
def lan_token():
    lan.new_token()
    return obs.lan_status()


@app.get("/api/obs/relay.py")
def relay_script():
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).with_name("relay.py"), filename="reelwright_relay.py", media_type="text/x-python")


app.mount("/files", StaticFiles(directory=PROJECTS), name="files")
app.mount("/assets", StaticFiles(directory=assets.ASSETS), name="assets")  # read-only files; no listing
app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
