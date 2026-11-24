"""HTTP API + static UI. Bound to localhost only (it holds your API keys and social tokens)."""
import json
import math
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import accounts, ai, assets, config, doctor, lan, learn, log, media, multicam, obs, pipeline, platforms, providers, schedule, storage, store, usage, youtube
from .config import STATIC, YT_SECRET
from . import remote  # phone remote: its own opt-in Wi-Fi listener; here only the PC's Settings → Phone remote endpoints

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
    storage.start()  # finish a storage-folder move the app was closed in
    obs.start()
    lan.apply()
    remote.apply()  # the phone remote's listener, when turned on in Settings


def _404(doc, what="Not found"):
    if not doc:
        raise HTTPException(404, what)
    return doc


# ---------- settings ----------

# ---------- desktop app: close-to-taskbar, quit, start with Windows ----------

def _app_state() -> dict:
    from . import desktop
    return {"desktop": desktop.WINDOW is not None, "close_to_taskbar": bool(config.load().get("close_to_taskbar", True)),
            "start_on_boot": desktop.start_on_boot(), "boot_command": desktop.boot_command()}


@app.get("/api/app")
def app_state():
    return _app_state()


@app.put("/api/app")
def put_app(body: dict):
    from . import desktop
    for k in ("close_to_taskbar", "start_on_boot"):
        if k in body and not isinstance(body[k], bool):
            raise HTTPException(400, f"{k} must be true or false.")
    if "close_to_taskbar" in body:
        config.save({"close_to_taskbar": body["close_to_taskbar"]})
    if "start_on_boot" in body:
        try:
            desktop.set_start_on_boot(body["start_on_boot"])
        except (ImportError, OSError) as e:
            raise HTTPException(400, f"Couldn't change the Windows startup entry: {e}")
    return _app_state()


@app.post("/api/app/show")
def app_show():  # a second launch asks the running copy to come back instead of opening another window
    from . import desktop
    desktop.show_window()
    return {"ok": True}


@app.post("/api/app/quit")
def app_quit():
    from . import desktop
    if desktop.WINDOW is None:
        raise HTTPException(400, "Reelwright is running in a browser here; close the terminal that started it to quit.")
    threading.Timer(0.3, desktop.quit_app).start()  # answer the page first, then close
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return config.public()


@app.put("/api/settings")
def put_settings(patch: dict):
    if "anthropic_api_key" in patch and patch["anthropic_api_key"] is None:
        patch.pop("anthropic_api_key")
    for k in ("training_python", "training_wsl", "training_env_dir"):  # which program training runs: only via the
        patch.pop(k, None)                                            # validated /api/training/env routes

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
    _check_ai_settings(patch)
    try:
        storage.check_settings(patch)  # space saver + archive folders; switching it on starts every item's clock now
    except ValueError as e:
        raise HTTPException(400, str(e))
    config.save(patch)
    return config.public()


UI_SCALE_KEYS = ("app", "sidebar", "text", "preview", "timeline", "panels")
AI_PROVIDER_FIELDS = {"model": str, "base_url": str, "api_key": str, "num_ctx": int, "vision": bool}


def _check_ai_settings(patch: dict) -> None:
    """Validates the multi-provider keys in place; provider settings merge into the saved ones (a null key = keep it)."""
    names = providers.NAMES
    if "ai_routes" in patch:
        r = patch["ai_routes"]
        if not isinstance(r, dict) or any(k not in ai.TASKS or v not in names for k, v in r.items()):
            raise HTTPException(400, f"AI routes map a task ({', '.join(ai.TASKS)}) to a provider ({', '.join(names)}).")
        patch["ai_routes"] = {**config.load()["ai_routes"], **r}
    if "ai_consensus" in patch:
        c = patch["ai_consensus"]
        a, _, b = c.partition(":") if isinstance(c, str) else ("", "", "")
        if c not in ("off", "local_then_claude") and not (a in names and b in names and a != b):
            raise HTTPException(400, "Consensus is off, local_then_claude, or two different providers as scout:reviewer.")
    if "ai_providers" in patch:
        raw = patch["ai_providers"]
        if not isinstance(raw, dict) or not set(raw) <= set(config.PROVIDER_DEFAULTS):
            raise HTTPException(400, f"Unknown AI provider (known: {', '.join(config.PROVIDER_DEFAULTS)}).")
        saved = config.load()["ai_providers"]
        out = dict(saved)
        for name, p in raw.items():
            if not isinstance(p, dict):
                raise HTTPException(400, f"Bad value in the {name} settings.")
            p = {k: v for k, v in p.items() if k in AI_PROVIDER_FIELDS and v is not None}
            if isinstance(p.get("num_ctx"), float) and abs(p["num_ctx"]) < 1e9:
                p["num_ctx"] = int(p["num_ctx"])
            if any(not isinstance(v, AI_PROVIDER_FIELDS[k]) or (k == "num_ctx" and isinstance(v, bool)) for k, v in p.items()):
                raise HTTPException(400, f"Bad value in the {name} settings.")
            if "base_url" in p:
                try:
                    p["base_url"] = providers.check_url(p["base_url"].strip())
                except ValueError as e:
                    raise HTTPException(400, str(e))
            if "num_ctx" in p:
                p["num_ctx"] = min(max(p["num_ctx"], 2048), 262144)
            out[name] = {**(saved.get(name) or {}), **p}
        patch["ai_providers"] = out


@app.post("/api/ai/test")
def test_ai_provider(body: dict):
    """A tiny structured call to one provider with the saved settings: proves model, URL and key work."""
    name = body.get("provider")
    if name not in providers.NAMES:
        raise HTTPException(400, "Unknown provider.")
    try:
        r = ai._ask("Reply with JSON only.", "Write a 2-4 word search query about cats doing funny things.", ai.Query,
                    effort="low", provider=name)
        return {"ok": True, "result": r.query, "model": ai.model_name(name)}
    except Exception as e:  # shown in the card; the message never contains the key (providers scrub it)
        return {"ok": False, "error": str(e)[:400]}


@app.get("/api/ai/ollama/models")
def ollama_models():
    try:
        ms = providers.ollama_models(config.provider("ollama")["base_url"])
    except Exception as e:
        return {"models": [], "error": f"Ollama isn't reachable: {str(e)[:200]}"}
    return {"models": [{"name": m.get("name"), "size": (m.get("details") or {}).get("parameter_size"),
                        "vision": "vision" in (m.get("capabilities") or [])} for m in ms]}


# ---------- projects ----------

@app.post("/api/projects")
def upload_project(file: UploadFile = File(...), autopilot: str = Form("{}"), video_type: str = Form("auto"),
                   sub_genre: str = Form("")):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VIDEO_EXT:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}.")
    p = pipeline.new_project(Path(file.filename).stem, json.loads(autopilot or "{}"), video_type=_video_type(video_type, sub_genre))
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
    p = pipeline.new_project(url, body.get("autopilot"), video_type=_video_type(body.get("video_type"), body.get("sub_genre")))
    pipeline.submit("import_url", project_id=p["id"], url=url)
    return p


def _video_type(kind, sub=None) -> dict | None:
    """The creator's fixed type ("auto"/missing = detect it during analysis); 400 for anything off the list."""
    try:
        return ai.fixed_type(kind, sub)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/projects")
def list_projects():
    projects = store.find("project")
    clips = [c for c in store.find("clip") if not c.get("expired")]  # expired clips: history, behind "Show expired"
    dues = storage.due_map()
    for p in projects:
        mine = [c for c in clips if c["project_id"] == p["id"]]
        p["clip_count"] = len(mine)
        p["first_clip"] = mine[0]["id"] if mine else None  # an imported short opens straight in the Studio
        p.update(storage.project_view(p, dues))
    return projects


@app.get("/api/projects/{pid}")
def get_project(pid: str, expired: bool = False):
    p = _404(store.get("project", pid))
    every = store.find("clip", project_id=pid)
    dues = storage.due_map({pid})
    p["clips"] = sorted([{**c, "due": dues.get(("clip", c["id"]))} for c in every if expired or not c.get("expired")],
                        key=lambda c: (c["source"] == "manual", -c["score"]))
    p["expired_count"] = sum(bool(c.get("expired")) for c in every)
    p.update(storage.project_view(p, dues))
    return p


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, body: dict):
    _404(store.get("project", pid))
    patch = {k: v for k, v in body.items() if k in ("name", "autopilot")}
    if "keep" in body:  # "Keep": the space saver never touches this project's source, clips or long forms
        patch["keep"] = bool(body["keep"])
    if "video_type" in body:  # the creator's override; "auto" hands it back to detection on the next analysis
        patch["video_type"] = _video_type(body["video_type"], body.get("sub_genre"))
    p = _404(store.update("project", pid, patch))
    if body.get("refind") and p["status"] == "ready":  # re-find with the new type: untouched AI picks make way
        for c in store.find("clip", project_id=pid):
            if pipeline.replaceable(c):
                store.delete("clip", c["id"])
        pipeline.submit("find", project_id=pid, query="")
    return p


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    try:
        pipeline.pdir(pid)
    except ValueError:
        raise HTTPException(404, "No such project")
    if why := storage.hold_reason():  # mid-move or drive away: deleting now would orphan or miss its folder
        raise HTTPException(409, why)
    pipeline.delete_project(pid)
    return {"ok": True}


# ---------- camera angles (multicam.py) ----------

def _angle(fn, *args):
    try:
        return fn(*args)
    except multicam.AngleError as e:
        raise HTTPException(e.status, str(e)) from None


@app.post("/api/projects/{pid}/angles")
def add_angle(pid: str, file: UploadFile = File(...), name: str = Form("")):
    return _angle(multicam.add_upload, pid, file.filename or "", file.file, name)


@app.post("/api/projects/{pid}/angles/url")
def add_angle_url(pid: str, body: dict):
    return _angle(multicam.add_url, pid, body.get("url") or "", body.get("name") or "")


@app.get("/api/projects/{pid}/angles/obs")
def angle_obs_files(pid: str):
    _404(store.get("project", pid))
    return multicam.obs_folders()


@app.post("/api/projects/{pid}/angles/obs")
def add_angle_obs(pid: str, body: dict):
    return _angle(multicam.add_obs, pid, str(body.get("obs_id") or ""), body.get("file") or "", body.get("name") or "")


@app.patch("/api/projects/{pid}/angles/{aid}")
def patch_angle(pid: str, aid: str, body: dict):
    return _angle(multicam.update, pid, aid, body)


@app.post("/api/projects/{pid}/angles/{aid}/resync")
def resync_angle(pid: str, aid: str):
    return _angle(multicam.resync, pid, aid)


@app.delete("/api/projects/{pid}/angles/{aid}")
def delete_angle(pid: str, aid: str):
    return _angle(multicam.remove, pid, aid)


@app.post("/api/clips/{cid}/switches/suggest")
def suggest_switches(cid: str):
    """Auto-switch: proposed angle cuts for the clip (not saved: the creator approves or edits them)."""
    return _angle(multicam.suggest_for_clip, _404(store.get("clip", cid)))


@app.post("/api/projects/{pid}/analyze")
def reanalyze(pid: str):
    if _404(store.get("project", pid)).get("source_removed"):  # removed to save space: 400 with the fix, never a failed project
        pipeline.source_file(store.get("project", pid))
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
               "cam_w": (0.02, 1), "cam_h": (0.02, 1), "split": (0.15, 0.7), "zoom": (1, 4), "music_volume": (0, 2),
               "split2": (0.1, 0.5), "pip_size": (0.15, 0.6), "pip_radius": (0, 0.5), "pip_border": (0, 24)}
EDIT_ENUMS = {"aspect": ("source", "9:16", "1:1", "4:5", "16:9"), "fit": ("crop", "blur"), "layout": media.LAYOUTS,
              "caption_style": ("bold", "clean", "boxed", "neon"), "caption_pos": ("low", "middle", "high"),
              "sticker_pos": ("top-left", "top-center", "top-right", "bottom-left", "bottom-right"), "pip_corner": media.PIP_CORNERS,
              "caption_anim": media.CAPTION_ANIMS, "bleep": media.BLEEP_MODES}


def angle_ids(project: dict | None) -> set:
    """Camera-angle ids of a project (project["angles"]: [{id, ...}] or ids; absent until angles can be imported)."""
    return {a.get("id") if isinstance(a, dict) else a for a in (project or {}).get("angles") or []} - {None}


def _regions(v, angles: set = frozenset()) -> list | None:
    """Layout regions: up to media.MAX_REGIONS {id, role, x, y, w, h, follow, source?, out?}, boxes clamped inside the 0..1
    frame; source is "main" (the default, not stored) or one of the project's camera-angle ids."""
    if v is None:
        return None
    if not isinstance(v, list) or len(v) > media.MAX_REGIONS:
        raise HTTPException(400, f"regions must be a list of up to {media.MAX_REGIONS} boxes.")
    out, ids = [], set()
    for i, r in enumerate(v):
        if not isinstance(r, dict) or r.get("role") not in media.ROLES:
            raise HTTPException(400, f"Each region needs a role: {', '.join(media.ROLES)}.")
        try:
            box = media.clamp_box(r)
            dest = media.clamp_box(r["out"]) if r.get("out") is not None else None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(400, "Region boxes need numeric x, y, w, h.")
        rid = str(r.get("id") or "")[:24] or f"r{i}"
        rid = rid if rid not in ids else store.new_id()
        ids.add(rid)
        src = r.get("source") or "main"
        if src != "main" and src not in angles:
            raise HTTPException(400, f"Region source must be \"main\" or one of this project's camera angles, not {str(src)[:40]!r}.")
        out.append({"id": rid, "role": r["role"], **box, "follow": bool(r.get("follow")), **({"out": dest} if dest else {}),
                    **({"source": src} if src != "main" else {})})
    return out or None


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
        return media.safe_font(v) or None  # it is written into ASS tags: nothing that could inject one
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
TEXT_KEYS = {f"{who}_{f}": f for who in ("caption", "title", "sticker", "headline") for f in TEXT_FIELDS}
HEADLINE_ENUMS = {"theme": ("auto", "custom", *media.HEADLINE_THEMES), "position": media.HEADLINE_POSITIONS, "show": media.HEADLINE_SHOW}


def _headline(v) -> dict | None:
    """The headline banner: only the keys given (patch_clip merges them into the clip's), each checked. None clears it."""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise HTTPException(400, "headline must be an object.")
    out = {}
    for k, cap in (("text", media.HEADLINE_MAX), ("kicker", media.KICKER_MAX)):
        if k in v:
            if not isinstance(v[k], str) or len(v[k].strip()) > cap:
                raise HTTPException(400, f"Headline {k} must be text of at most {cap} characters.")
            out[k] = v[k].strip()
    for k, opts in HEADLINE_ENUMS.items():
        if k in v:
            if v[k] not in opts:
                raise HTTPException(400, f"Headline {k} must be one of {', '.join(opts)}.")
            out[k] = v[k]
    for k in ("on", "push"):
        if k in v:
            out[k] = bool(v[k])
    if "seconds" in v:
        out["seconds"] = round(_num(v["seconds"], "Headline seconds", 0.5, 30), 2)
    return out
OVERLAY_KINDS = ("image", "gif", "video", "sound", "text", "emoji")
OVERLAY_ANIMS = ("none", "pop", "fade", "shake", "slide")


def _switches(v, clip: dict, angles: set) -> list:
    """Angle cuts [{t, angle}]: t in clip seconds (clamped into the clip), angle "main" or one of the project's angles;
    sorted, one cut per moment."""
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > 500:
        raise HTTPException(400, "switches must be a list of up to 500 {t, angle}.")
    out, span = {}, clip["end"] - clip["start"]
    for sw in v:
        try:
            t, ang = float(sw["t"]), sw.get("angle") or "main"
        except (TypeError, KeyError, ValueError, AttributeError):
            raise HTTPException(400, "Each switch must be {t, angle}.") from None
        if not math.isfinite(t) or (ang != "main" and ang not in angles):
            raise HTTPException(400, f"A switch must cut to \"main\" or one of this project's camera angles, not {str(ang)[:40]!r}.")
        out[round(min(max(t, 0.0), span), 2)] = ang
    return [{"t": t, "angle": a} for t, a in sorted(out.items())]


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


def _cuts(v, clip: dict) -> list | None:
    """Cut spans: up to 500 [s, e] with s < e, clamped inside the clip, merged and sorted; None when nothing is cut.
    At least 0.5 s of the clip must stay (the render would otherwise fall back to keeping everything)."""
    if v is None or v == []:
        return None
    if not isinstance(v, list) or len(v) > 500:
        raise HTTPException(400, "cuts must be a list of up to 500 [start, end] spans.")
    spans = []
    for c in v:
        if not isinstance(c, (list, tuple)) or len(c) != 2:
            raise HTTPException(400, "Each cut must be [start, end].")
        s, e = _num(c[0], "cut start", -1e9, 1e9), _num(c[1], "cut end", -1e9, 1e9)
        if s >= e:
            raise HTTPException(400, "Each cut must end after it starts.")
        s, e = max(s, clip["start"]), min(e, clip["end"])
        if s < e:
            spans.append([round(s, 3), round(e, 3)])
    out: list = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    if clip["end"] - clip["start"] - sum(e - s for s, e in out) < 0.5:
        raise HTTPException(400, "Those cuts would remove the whole clip: keep at least 0.5 s.")
    return out or None


def clean_edit(edit: dict, clip: dict) -> dict:
    """Validate edit values at the API boundary (from the UI, the AI assistant, or anything else posting here).
    `clip` (start/end) bounds the list-valued keys; track_manual comes back as is for patch_clip to file under track."""
    out = {}
    for k, v in edit.items():
        if k == "track_manual":
            out[k] = _keyframes(v, clip["end"] - clip["start"])
            continue
        if k == "cuts":  # text-based cuts (WP5): [[s, e], …] source seconds the render removes (pipeline.clip_keeps)
            out[k] = _cuts(v, clip)
            continue
        if k not in media.DEFAULT_EDIT:
            continue
        if k in TEXT_KEYS:
            out[k] = _text_style(TEXT_KEYS[k], v, k)
        elif k == "words":
            out[k] = _words(v, clip)
        elif k == "framing":
            out[k] = _framing(v)
        elif k == "headline":
            out[k] = _headline(v)
        elif k == "regions":
            out[k] = _regions(v, angle_ids(store.get("project", clip.get("project_id") or "")))
        elif k == "overlays":
            out[k] = _overlays(v, clip)
        elif k == "switches":
            out[k] = _switches(v, clip, angle_ids(store.get("project", clip.get("project_id") or "")))
        elif k == "audio_source":
            if v != "main" and v not in angle_ids(store.get("project", clip.get("project_id") or "")):
                raise HTTPException(400, "audio_source must be \"main\" or one of this project's camera angles.")
            out[k] = v
        elif k == "switch_region":
            out[k] = str(v or "")[:24]
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
META_KEYS = {"title", "hook", "rating", "keep"}  # what stays editable after a clip is posted (doesn't change the video)
EXPIRED = "This clip expired (its render was removed to save space): press Keep to restore it, or make a new version."


def posted(cid: str) -> bool:
    return bool(store.find("upload", clip_id=cid, status="done"))


def _editable(cid: str) -> dict:
    """The clip, unless it has been posted (the published render is final; pending uploads still use it) or expired."""
    clip = _404(store.get("clip", cid))
    if posted(cid):
        raise HTTPException(409, POSTED)
    if clip.get("expired"):
        raise HTTPException(409, EXPIRED)
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
            if ed.get("headline"):  # partial headline updates (just the theme, just the text…) keep the rest
                ed["headline"] = {**(c["edit"].get("headline") or {}), **ed["headline"]}
            c["edit"] = {**c["edit"], **ed}
            changed = True
        for k in ("title", "hook"):
            if k in body:
                c[k] = body[k]
        if "rating" in body:
            c["rating"] = max(-1, min(1, int(body["rating"])))
        if "keep" in body:  # pinned: never expired by the space saver; Keep on an expired clip restores it
            c["keep"] = bool(body["keep"])
            if c["keep"]:
                c.pop("expired", None)
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
                                                                               "track_error", "expired", "keep")}
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
    face = media.face_center(pipeline.source_file(p), clip["start"], clip["end"])
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
    lay = clip["edit"].get("layout", "single")
    edit = {"layout": "split" if lay == "single" else lay, **rect}
    if clip["edit"].get("regions"):  # the regions drive the render: move (or add) the cam region too
        box = {k[4:]: v for k, v in rect.items()}
        rs = [dict(r) for r in clip["edit"]["regions"]]
        cam = next((r for r in rs if r["role"] == "cam"), None)
        edit["regions"] = [{**r, **box} if r is cam else r for r in rs] if cam else \
            [{"id": "cam", "role": "cam", **box}, *rs[:media.MAX_REGIONS - 1]]
    return patch_clip(cid, {"edit": edit})


@app.post("/api/clips/{cid}/regions/detect")
def detect_regions(cid: str):
    """Suggested regions: cam around the face, a chat column if one stands out, main = the rest."""
    clip = _editable(cid)
    p = store.get("project", clip["project_id"])
    regs = media.detect_regions(pipeline.source_file(p), clip["start"], clip["end"],
                                p["meta"]["width"], p["meta"]["height"])
    lay = clip["edit"].get("layout", "single")
    roles = {r["role"] for r in regs}
    if "chat" in roles:
        lay = "stack3"
    elif lay == "single" and "cam" in roles:
        lay = "split"
    return patch_clip(cid, {"edit": {"layout": lay, "regions": regs}})


def _layout_of(cid: str) -> dict:
    e = {**media.DEFAULT_EDIT, **_404(store.get("clip", cid))["edit"]}
    return {k: e[k] for k in media.LAYOUT_KEYS}


@app.post("/api/clips/{cid}/layout/apply-all")
def apply_layout_all(cid: str):
    """This clip's layout and regions onto every other editable clip of its project (posted clips and montages stay)."""
    lay, pid = _layout_of(cid), store.get("clip", cid)["project_id"]
    n = 0
    for c in store.find("clip", project_id=pid):
        if c["id"] != cid and not c.get("parts") and not posted(c["id"]):
            patch_clip(c["id"], {"edit": lay})
            n += 1
    return {"updated": n}


@app.post("/api/clips/{cid}/layout/default")
def save_layout_default(cid: str):
    """New clips start with this clip's layout and regions (pipeline.new_clip applies it over profile/learned guesses)."""
    lay = _layout_of(cid)  # camera-angle sources belong to this project only: the default keeps the boxes, not them
    lay["regions"] = [{k: v for k, v in r.items() if k != "source"} for r in lay.get("regions") or []] or lay.get("regions")
    store.put("pref", {"id": "layout", "edit": lay})
    return {"ok": True}


# ---------- headline banner ----------

@app.get("/api/headline/themes")
def headline_themes():
    """The preset themes, the "auto" map (video type -> theme), the creator's saved theme and whether new clips get one."""
    return {"themes": media.HEADLINE_THEMES, "auto": media.HEADLINE_AUTO, "fonts": media.HEADLINE_FONTS,
            "custom": (store.get("pref", "headline_theme") or {}).get("theme"), "default_on": bool(store.get("pref", "headline"))}


@app.post("/api/clips/{cid}/headline/write")
def headline_write(cid: str):
    """3 headline options + a kicker from the clip's transcript, title and hook; without AI, the hook and title."""
    clip = _404(store.get("clip", cid))
    plain = list(dict.fromkeys(t for t in (media.headline_text(clip.get("hook")), media.headline_text(clip.get("title"))) if t))
    segs = pipeline.load_json(clip["project_id"], "transcript.json", {"segments": []})["segments"]
    try:
        return {**ai.headline_options(clip, segs), "ai": True}
    except RuntimeError as e:  # no key, offline, refused: the honest defaults still work
        return {"options": plain, "kicker": "", "ai": False, "error": str(e)}


@app.post("/api/clips/{cid}/headline/theme")
def save_headline_theme(cid: str):
    """This clip's headline look (theme + its style overrides) saved as the creator's own theme ("custom")."""
    clip = _404(store.get("clip", cid))
    e = {**media.DEFAULT_EDIT, **clip["edit"]}
    vt = ((store.get("project", clip["project_id"]) or {}).get("video_type") or {}).get("type")
    spec = media.headline_spec({**e, "headline": {**(e["headline"] or {}), "on": True, "text": "x"}}, vt,
                               (store.get("pref", "headline_theme") or {}).get("theme"))
    theme = {k: spec[k] for k in media.HEADLINE_THEME_KEYS} | {"name": "My theme"}
    store.put("pref", {"id": "headline_theme", "theme": theme})
    return theme


@app.post("/api/clips/{cid}/headline/default")
def save_headline_default(cid: str, body: dict):
    """{"on": true}: new clips (manual, AI and autopilot) get this clip's headline look with their own hook as the text.
    {"on": false} stops that."""
    if not body.get("on", True):
        store.delete("pref", "headline")
        return {"ok": True, "on": False}
    e = {**media.DEFAULT_EDIT, **_404(store.get("clip", cid))["edit"]}
    look = {k: v for k, v in (e["headline"] or {}).items() if k not in ("on", "text", "kicker")}
    store.put("pref", {"id": "headline", "edit": {"headline": look, **{f"headline_{f}": e[f"headline_{f}"] for f in TEXT_FIELDS
                                                                       if e[f"headline_{f}"] is not None}}})
    return {"ok": True, "on": True}


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
            "posts": [{"upload_id": u["id"], "platform": u["platform"], "url": u.get("url"), "video_id": u.get("video_id"),
                       "publish_at": u.get("publish_at"), "account": (pipeline._account_of(u) or {}).get("name")} for u in done],
            "pending": [{"platform": u["platform"], "status": u["status"], "when": u.get("publish_at") or u.get("upload_at")}
                        for u in pending],
            "locked": bool(done), "edit_url": f"#/studio/{c['id']}"}


@app.get("/api/pipeline")
def pipeline_board(expired: bool = False):
    """Every project and clip on one board: each in exactly one stage, the furthest it has reached. Clips and long forms
    the space saver expired are left out unless ?expired=1; every card carries its Keep pin and due date."""
    stages = {k: [] for k, _ in STAGES}
    clips, ups, lfs = store.find("clip"), store.find("upload"), store.find("longform")
    dues = storage.due_map()
    by_clip: dict[str, list] = {}
    for u in ups:
        by_clip.setdefault(u.get("longform_id") or u.get("clip_id"), []).append(u)
    with_clips = {c["project_id"] for c in clips} | {d["project_id"] for d in lfs}
    for p in store.find("project"):
        if p.get("status") in ("importing", "queued", "analyzing") or p.get("status") == "ready" and p["id"] not in with_clips:
            stages["harvested"].append({
                "type": "project", "id": p["id"], "project_id": p["id"], "title": p.get("name"), "stage": "harvested",
                "thumb": f"/files/{p['id']}/poster.jpg" if (pipeline.pdir(p["id"]) / "poster.jpg").exists() else None,
                "duration": (p.get("meta") or {}).get("duration"), "aspect": None, "updated": _iso(p.get("created")),
                "posts": [], "pending": [], "locked": False, "edit_url": f"#/editor/{p['id']}",
                "keep": bool(p.get("keep")), "due": dues.get(("source", p["id"]))})
    for kind, docs, make in (("clip", clips, _clip_item), ("longform", lfs, _longform_item)):
        for d in docs:
            if d.get("expired") and not expired:
                continue
            item = make(d, by_clip.get(d["id"], []))
            item.update(keep=bool(d.get("keep")), expired=d.get("expired"), due=dues.get((kind, d["id"])))
            stages[item["stage"]].append(item)
    return {"stages": [{"key": k, "label": label, "items": stages[k]} for k, label in STAGES]}


LF_LANE = {"captured": "preclipped", "edited": "longform", "rendered": "ready", "uploaded": "uploaded"}


def _longform_item(d: dict, ups: list[dict]) -> dict:
    """A long-form card: captured → edited → rendered → uploaded, shown in the matching lane and marked Long form."""
    lf_stage = longform.stage(d, ups)
    done = [u for u in ups if u["status"] == "done"]
    pending = [u for u in ups if u["status"] in PENDING]
    exp, pid = d.get("export") or {}, d["project_id"]
    thumb = f"/files/{pid}/{exp['cover']}" if exp.get("cover") else \
        (f"/files/{pid}/poster.jpg" if (pipeline.pdir(pid) / "poster.jpg").exists() else None)
    stamps = [exp.get("at"), *(u.get("uploaded_at") for u in ups)]
    return {"type": "longform", "id": d["id"], "project_id": pid, "title": d.get("title"), "stage": LF_LANE[lf_stage],
            "lf_stage": lf_stage, "thumb": thumb, "duration": longform.length(d), "aspect": d["edit"].get("aspect"),
            "updated": max([s for s in stamps if s] or [_iso(d.get("created"))]),
            "posts": [{"upload_id": u["id"], "platform": u["platform"], "url": u.get("url"), "video_id": u.get("video_id"),
                       "publish_at": u.get("publish_at"), "account": (pipeline._account_of(u) or {}).get("name")} for u in done],
            "pending": [{"platform": u["platform"], "status": u["status"], "when": u.get("publish_at") or u.get("upload_at")}
                        for u in pending],
            "locked": bool(done), "edit_url": f"#/long/{d['id']}"}


@app.post("/api/pipeline/delete")
def pipeline_delete(body: dict):
    """Delete board items: {items: [{type: "project"|"clip", id}], options: {remote: {upload_id: true}}}. One result per
    item, so one failure never blocks the rest. A posted video is deleted on its platform only when its box was ticked."""
    items, remote = body.get("items"), (body.get("options") or {}).get("remote") or {}
    if not isinstance(items, list) or not isinstance(remote, dict):
        raise HTTPException(400, "Send items as a list of {type, id}.")
    ticked = {k for k, v in remote.items() if v is True}
    out = []
    for it in items:
        kind, iid = (it.get("type"), it.get("id")) if isinstance(it, dict) else (None, None)
        if kind not in ("project", "clip", "longform") or not isinstance(iid, str) or not iid:
            res = {"ok": False, "error": "Not a project, clip or long-form edit."}
        else:
            try:
                res = pipeline.delete_item(kind, iid, ticked)
            except Exception as e:
                log.error("pipeline", f"delete {kind} {iid}")
                res = {"ok": False, "error": str(e)[:300]}
        out.append({"type": kind if isinstance(kind, str) else None, "id": iid if isinstance(iid, str) else None,
                    "canceled": 0, "remote": [], "error": None, **res})
    return {"results": out}


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
    if body.get("accounts"):  # one upload per chosen account (two YouTube channels = two uploads)
        accs = []
        for aid in dict.fromkeys(body["accounts"]):  # each account once; all checked before anything is queued
            acc = accounts.get(aid)
            if not acc:
                raise HTTPException(400, "One of the chosen accounts is no longer linked; reopen Publish.")
            accs.append(pipeline._pick_account(acc["platform"], aid))  # approved, or 400
        return [pipeline.request_upload(cid, {**body, "platform": a["platform"], "account_id": a["id"],
                                              "tiktok": (body.get("tiktok_by_account") or {}).get(a["id"]) or body.get("tiktok")})
                for a in accs]
    targets = body.get("platforms") or [body.get("platform", "youtube")]
    return [pipeline.request_upload(cid, {**body, "platform": t}) for t in targets]


# ---------- uploads / jobs / reports ----------

@app.get("/api/uploads")
def uploads():
    ups = store.find("upload")
    clips = {c["id"]: c for c in store.find("clip")}
    for u in ups:
        u.setdefault("platform", "youtube")
        u["account"] = (pipeline._account_of(u) or {}).get("name")
        c = clips.get(u.get("clip_id")) or (store.get("longform", u["longform_id"]) if u.get("longform_id") else None)
        u["clip"] = {"title": c["title"], "start": c.get("start", 0), "end": c.get("end", longform.length(c) if c.get("segments") else 0),
                     "export": c.get("export"), "longform": "segments" in c} if c else None
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
    """Schedule tab: change a post's time (null = post as soon as possible) and/or account (or platform)."""
    _404(store.get("upload", uid))
    return pipeline.reschedule(uid, body["when"] if "when" in body else "keep", body.get("platform"), body.get("account_id"))


# ---------- schedule ----------

@app.get("/api/schedule")
def schedule_view():
    clips = {c["id"]: c for c in store.find("clip")}
    items = []
    for u in store.find("upload"):
        if u.get("status") in ("canceled",) or (u.get("status") == "done" and not (u.get("publish_at") and not pipeline._due({"upload_at": u["publish_at"]}))):
            continue  # already out (a YouTube post still waiting for its publish time stays visible and movable)
        c = clips.get(u.get("clip_id")) or (store.get("longform", u["longform_id"]) or {} if u.get("longform_id") else {})
        exp = c.get("export") or {}
        acc = pipeline._account_of(u) or {}
        items.append({"id": u["id"], "platform": u.get("platform", "youtube"), "status": u["status"], "auto": bool(u.get("auto")),
                      "account_id": acc.get("id"), "account": acc.get("name"),
                      "when": u.get("publish_at") or u.get("upload_at"), "title": u.get("title") or c.get("title"),
                      "error": u.get("error", ""), "clip_id": u["clip_id"], "longform_id": u.get("longform_id"), "project_id": u.get("project_id"),
                      "cover": f"/files/{u.get('project_id')}/{exp['cover']}" if exp.get("cover") else None,
                      "category_id": u.get("category_id"), "privacy": u.get("privacy")})
    return {"items": items, "heat": schedule.heat(), "paused": pipeline.paused(), "connected": {p: platforms.connected(p) for p in platforms.PLATFORMS},
            "accounts": [accounts.public(a) for a in accounts.usable()]}  # where a post can be moved to


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
def tiktok_creator(account_id: str = ""):
    try:
        return platforms.tiktok_creator(account_id or None)
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
def yt_connect(body: dict | None = None):
    """{"account_id": id} reconnects that channel; without it the sign-in becomes a new, unapproved account."""
    youtube.connect((body or {}).get("account_id"), on_connected=pipeline.requeue_needs_auth)
    return youtube.status()


@app.patch("/api/accounts/{aid}")
def patch_account(aid: str, body: dict):
    """The Publish page's two switches: Approved for uploads, and Allow Autopilot to post here."""
    patch = {k: bool(body[k]) for k in ("approved", "autopilot") if k in body}
    if not _404(accounts.update(aid, patch)).get("approved"):
        accounts.update(aid, {"autopilot": False})  # Autopilot needs the account approved first
    return connections()


@app.delete("/api/accounts/{aid}")
def remove_account(aid: str):
    _404(accounts.get(aid))
    held = pipeline.remove_account(aid)
    return {**connections(), "held": held}


@app.put("/api/connections/tiktok/app")
def tiktok_app(body: dict):
    if not body.get("client_key") or not body.get("client_secret"):
        raise HTTPException(400, "Enter both the Client key and Client secret from your TikTok developer app.")
    platforms.tiktok_set_app(body["client_key"], body["client_secret"])
    return connections()


@app.post("/api/connections/tiktok/connect")
def tiktok_connect(body: dict | None = None):
    return {"url": platforms.tiktok_auth_url(PORT, (body or {}).get("account_id"))}


@app.get("/oauth/tiktok/callback", response_class=HTMLResponse)
def tiktok_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        msg, ok = f"TikTok sign-in was not completed: {error_description or error}", False
    else:
        try:
            acc = platforms.tiktok_callback(code, state, PORT)
            pipeline.requeue_needs_auth()
            msg, ok = (f"TikTok account {acc.get('name') or ''} is linked to Reelwright. "
                       + ("You can close this tab." if acc.get("approved") else
                          "Before anything is posted to it, turn on “Approved for uploads” on the Publish page.")), True
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
    platforms.instagram_set(body["access_token"], body.get("ig_user_id") or "", body.get("account_id"))
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


# ---------- long-form edits (longform.py): capture, edit, render, publish ----------
from . import longform  # noqa: E402

LF_TITLES = {"condensed": "{} (condensed)", "highlights": "Best of {}", "clips": "{} (compilation)"}
LF_STRIP = ("words", "track_manual", "framing", "follow")  # per-clip things a sequence of segments has no use for


def _lf(lid: str) -> dict:
    return _404(store.get("longform", lid), "Long-form edit not found")


def _lf_view(d: dict) -> dict:
    ups = store.find("upload", longform_id=d["id"])
    jobs = [j for j in store.find("job", longform_id=d["id"]) if j["status"] in ("queued", "running")]
    return {**d, "length": longform.length(d), "current": longform.export_current(d), "lf_stage": longform.stage(d, ups),
            "locked": any(u["status"] == "done" for u in ups), "job": jobs[0] if jobs else None,
            "chapters_text": longform.chapters_text(d),  # at the exported video's times
            "uploads": [{"id": u["id"], "platform": u.get("platform"), "status": u["status"], "url": u.get("url"),
                         "error": u.get("error", ""), "when": u.get("publish_at") or u.get("upload_at"),
                         "account": (pipeline._account_of(u) or {}).get("name")} for u in ups]}


def _lf_segments(v, dur: float) -> list:
    if not isinstance(v, list) or not 0 < len(v) <= 5000:
        raise HTTPException(400, "segments must be a list of 1 to 5000 [start, end] pairs.")
    out = []
    for seg in v:
        try:
            a, b = (float(x) for x in seg)
        except (TypeError, ValueError):
            raise HTTPException(400, "Each segment must be [start, end] in seconds.")
        if not (0 <= a and b - a >= 0.1 and b <= dur + 0.01):  # also rejects NaN
            raise HTTPException(400, "Segment out of range (inside the video, at least 0.1 s long).")
        out.append([round(a, 3), round(min(b, dur), 3)])
    return out


def _lf_chapters(v, total: float) -> list:
    if not isinstance(v, list) or len(v) > 200:
        raise HTTPException(400, "chapters must be a list of up to 200 {t, title}.")
    out = []
    for c in v:
        if not isinstance(c, dict) or not isinstance(c.get("title", ""), str):
            raise HTTPException(400, "Each chapter needs t (seconds) and a title.")
        out.append({"t": round(_num(c.get("t"), "chapter t", 0, max(total, 0)), 3), "title": c.get("title", "").strip()[:100]})
    return sorted(out, key=lambda c: c["t"])


@app.get("/api/projects/{pid}/longform")
def list_longform(pid: str):
    _404(store.get("project", pid))
    return [_lf_view(d) for d in store.find("longform", project_id=pid)]


@app.get("/api/longform")
def all_longform():
    names = {p["id"]: p.get("name") for p in store.find("project")}
    return [{**_lf_view(d), "project_name": names.get(d["project_id"])} for d in store.find("longform")]


@app.post("/api/projects/{pid}/longform")
def capture_longform(pid: str, body: dict):
    """{mode: condensed|highlights|clips, title?, pad?, target?, clip_ids?, transition?}: a new long-form edit from the
    existing analysis. From clips is immediate; condensed and highlights run as a capture job."""
    p = _ready(pid)
    mode = body.get("mode")
    if mode not in longform.MODES:
        raise HTTPException(400, f"mode must be one of {', '.join(longform.MODES)}.")
    title = str(body.get("title") or "").strip()[:100] or LF_TITLES[mode].format(p["name"])[:100]
    tr = round(_num(body.get("transition", 0), "transition", 0, 1), 2)
    if mode == "clips":
        ids = body.get("clip_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 500:
            raise HTTPException(400, "Pick the clips to combine, in order.")
        clips = [store.get("clip", str(i)) for i in ids]
        if any(not c or c["project_id"] != pid or c.get("parts") for c in clips):
            raise HTTPException(400, "Every clip must be a (non-montage) clip of this project.")
        segs, chapters = longform.from_clips(clips)
        d = longform.create(pid, mode, title, segs, chapters, {"clip_ids": ids}, tr)
    else:
        st = {"pad": round(_num(body.get("pad", longform.DEFAULT_PAD), "pad", 0, 2), 2),
              "target": round(_num(body.get("target", longform.DEFAULT_TARGET), "target", 60, 4 * 3600))}
        d = longform.create(pid, mode, title, [], [], st, tr, status="capturing")
        pipeline.submit("longform_capture", longform_id=d["id"])
    return _lf_view(d)


@app.get("/api/longform/{lid}")
def get_longform(lid: str):
    return _lf_view(_lf(lid))


@app.patch("/api/longform/{lid}")
def patch_longform(lid: str, body: dict):
    """title and keep (always), and while it isn't posted: segments, chapters, transition, edit (the clip edit schema;
    overlays are timed in sequence seconds)."""
    d = _lf(lid)
    if "keep" in body:  # the space saver's pin; Keep on an expired long form restores it
        keep = bool(body["keep"])
        d = store.update("longform", lid, fn=lambda x: x.update(keep=keep, **({"expired": None} if keep else {})))
        body = {k: v for k, v in body.items() if k != "keep"}
    if body.keys() - {"title"} and d.get("expired"):
        raise HTTPException(409, "This long form expired (its render was removed to save space): press Keep to restore it.")
    if body.keys() - {"title"} and longform.posted(lid):
        raise HTTPException(409, POSTED)
    if d.get("status") == "capturing" and body.keys() - {"title"}:
        raise HTTPException(409, "Still capturing: wait for it to finish.")
    proj = store.get("project", d["project_id"])
    upd: dict = {}
    if "title" in body:
        upd["title"] = str(body["title"] or "").strip()[:100] or d["title"]
    if "segments" in body:
        upd["segments"] = _lf_segments(body["segments"], proj["meta"]["duration"])
    total = sum(b - a for a, b in upd.get("segments", d["segments"]))
    if "chapters" in body:
        upd["chapters"] = _lf_chapters(body["chapters"], total)
    if "transition" in body:
        upd["transition"] = round(_num(body["transition"], "transition", 0, 1), 2)
    if isinstance(body.get("edit"), dict):
        ed = {k: v for k, v in body["edit"].items() if k not in LF_STRIP}
        upd["edit"] = {**d["edit"], **clean_edit(ed, {"start": 0.0, "end": total, "project_id": d["project_id"]})}
    changed = bool(upd.keys() - {"title"})

    def apply(x):
        x.update(upd)
        if changed:
            x["rev"] += 1

    return _lf_view(store.update("longform", lid, fn=apply))


@app.post("/api/longform/{lid}/export")
def export_longform(lid: str):
    d = _lf(lid)
    if longform.posted(lid):
        raise HTTPException(409, POSTED)
    if d.get("expired"):
        raise HTTPException(409, "This long form expired: press Keep to restore it first.")
    if d.get("status") != "ready" or not d["segments"]:
        raise HTTPException(409, "Nothing to render yet.")
    return longform.request_export(lid)


@app.post("/api/longform/{lid}/version")
def version_longform(lid: str):
    _lf(lid)
    return _lf_view(longform.new_version(lid))


@app.post("/api/longform/{lid}/chapters/auto")
def auto_chapters(lid: str):
    """Chapters from what is said (Claude titles them when a key is set; offline: a boundary every ~3 minutes)."""
    d = _lf(lid)
    words = [w for s in pipeline.load_json(d["project_id"], "transcript.json", {"segments": []})["segments"] for w in s.get("words", [])]
    base = d.get("chapters") if len(d.get("chapters") or []) >= 3 else longform.auto_chapters(d["segments"], words)
    if ai.ready("other") and words:
        base = longform.titled_chapters(d["segments"], base, words) or base
    return patch_longform(lid, {"chapters": base})


@app.post("/api/longform/{lid}/metadata")
def longform_metadata(lid: str):
    """AI title, description (with the YouTube chapter block), tags and category for a regular YouTube video."""
    d = _lf(lid)
    words = [w for s in pipeline.load_json(d["project_id"], "transcript.json", {"segments": []})["segments"] for w in s.get("words", [])]
    text = " ".join(w["w"].strip() for w in longform.seq_words(words, d["segments"]))
    chapters = longform.chapters_text(d)
    md = ai.longform_meta(d["title"], chapters, text, pipeline.guidance())
    if chapters:
        md["description"] = f"{md['description'].rstrip()}\n\n{chapters}"
    return md


@app.post("/api/longform/{lid}/upload")
def upload_longform(lid: str, body: dict):
    """{accounts: [ids], title, description, tags, category_id, privacy, publish_at?, upload_at?, tiktok_by_account?}:
    one post per approved account (the same rules as a clip); Instagram only up to 15 minutes."""
    d = _lf(lid)
    if d.get("status") != "ready" or not d["segments"]:
        raise HTTPException(409, "Nothing to publish yet.")
    exp = d.get("export") or {}
    secs = (exp.get("duration") if longform.export_current(d) else None) or longform.output_length(d)
    accs = []
    for aid in dict.fromkeys(body.get("accounts") or []):  # each account once
        acc = accounts.get(aid)
        if not acc:
            raise HTTPException(400, "One of the chosen accounts is no longer linked; reopen Publish.")
        pipeline._pick_account(acc["platform"], aid)  # approved, or 400 before anything is queued
        if acc["platform"] == "instagram" and secs > 15 * 60:
            raise HTTPException(400, f"Instagram Reels can be at most 15 minutes; this is {secs / 60:.0f} minutes.")
        accs.append(acc)
    if not accs:
        raise HTTPException(400, "Pick at least one approved account.")
    return [pipeline.request_upload(None, {**body, "platform": a["platform"], "account_id": a["id"],
                                           "tiktok": (body.get("tiktok_by_account") or {}).get(a["id"]) or body.get("tiktok")},
                                    longform_id=lid) for a in accs]


@app.post("/api/longform/{lid}/music")
def longform_music(lid: str, file: UploadFile = File(...)):
    d = _lf(lid)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in AUDIO_EXT:
        raise HTTPException(400, f"Unsupported audio type {ext or '(none)'}.")
    safe = re.sub(r"[^\w.-]+", "_", Path(file.filename).stem)[:60] + ext
    (pipeline.pdir(d["project_id"]) / "music").mkdir(exist_ok=True)
    with open(pipeline.pdir(d["project_id"]) / "music" / safe, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return patch_longform(lid, {"edit": {"music": f"music/{safe}"}})


@app.post("/api/longform/{lid}/plan")
def plan_longform(lid: str, body: dict | None = None):
    """The Studio's "request -> AI plan" for a long form: edit, caption and overlay items (no subject tracking)."""
    d = _lf(lid)
    prompt = str((body or {}).get("prompt") or "")[:2000]
    plan = ai.plan(longform.plan_context(d, store.get("project", d["project_id"]), assets.listing()), prompt)
    seq = {"start": 0.0, "end": longform.length(d), "project_id": d["project_id"], "edit": d["edit"]}
    items, dropped = [], 0
    for it in plan.items:
        try:
            patch = _plan_patch(it, seq) if it.kind != "track" else None
        except HTTPException:
            patch = None
        if not patch:
            dropped += 1
            continue
        items.append({"id": store.new_id(), "kind": it.kind, "title": it.title[:120], "why": it.why[:400], "patch": patch})
    doc = store.put("plan", {"longform_id": lid, "prompt": prompt, "summary": plan.summary, "items": items, "dropped": dropped})
    return {"plan_id": doc["id"], "summary": plan.summary, "items": items, "dropped": dropped}


@app.post("/api/longform/{lid}/apply")
def apply_longform_plan(lid: str, body: dict):
    d = _lf(lid)
    plan = store.get("plan", str(body.get("plan_id") or ""))
    if not plan or plan.get("longform_id") != lid:
        raise HTTPException(404, "Plan not found for this long-form edit.")
    picked, edit, overlays = set(body.get("item_ids") or []), {}, []
    for it in plan["items"]:
        if it["id"] in picked:
            if it["kind"] == "overlay":
                overlays.append(it["patch"])
            else:
                edit.update(it["patch"])
    if overlays:  # added to the existing overlays, never replacing them
        edit["overlays"] = (d["edit"].get("overlays") or []) + overlays
    return patch_longform(lid, {"edit": edit}) if edit else _lf_view(d)


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


# ---------- storage folder, space saver, removed sources, imported shorts (storage.py) ----------

def _bad(fn, *args):
    try:
        return fn(*args)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get("/api/storage")
def storage_status(usage: bool = False):
    return storage.status(usage)


@app.post("/api/storage/check")
def storage_check(body: dict):
    """Validate a folder before moving there: {ok, path, free, needed} or {ok: false, error}."""
    try:
        p = storage.validate(body.get("path"), "archive" if body.get("purpose") == "archive" else "storage")
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(p), "free": storage._free(p), "needed": storage.usage()["total"], "missing": storage.missing()}


@app.post("/api/storage/move")
def storage_move(body: dict):
    _bad(storage.start_move, body.get("path"))
    return storage.status()


@app.post("/api/storage/pick")
def storage_pick(body: dict):
    """The desktop app's native picker: {kind: "folder"} → one folder, {kind: "files"} → video files. {paths: []} = canceled."""
    from . import desktop
    if desktop.WINDOW is None:
        raise HTTPException(400, "The native picker is part of the Reelwright desktop app: type the path instead.")
    import webview
    if body.get("kind") == "files":
        exts = ";".join(f"*{e}" for e in sorted(VIDEO_EXT))
        res = desktop.WINDOW.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=(f"Videos ({exts})",))
    else:
        res = desktop.WINDOW.create_file_dialog(webview.FileDialog.FOLDER)
    return {"paths": [str(p) for p in (res or [])]}


@app.get("/api/storage/cleanup")
def cleanup_preview():
    """What "Clean up now" would delete or archive right now, and what it skips (with why)."""
    return storage.preview()


@app.post("/api/storage/cleanup")
def cleanup_now():
    if not storage.policy()["mode"]:
        raise HTTPException(409, "Turn on the space saver (delete or archive) first.")
    if why := storage.hold_reason():
        raise HTTPException(409, why)
    if pipeline._busy("cleanup"):
        raise HTTPException(409, "A clean-up is already running (see Activity).")
    return pipeline.submit("cleanup")


@app.post("/api/projects/{pid}/redownload")
def redownload(pid: str):
    """Download a source the space saver removed again, from the link it came from (no re-analysis)."""
    p = _404(store.get("project", pid))
    url = storage.source_url(p)
    if not p.get("source_removed"):
        raise HTTPException(409, "This project still has its source video.")
    if not url:
        raise HTTPException(400, "This video wasn't downloaded from a link: re-import the file instead.")
    if pipeline._project_jobs(pid):
        raise HTTPException(409, "Wait for this project's running job to finish.")
    storage.check_writable()
    store.update("project", pid, {"status": "importing", "stage": "Re-downloading the source", "progress": 0.0})
    return pipeline.submit("import_url", project_id=pid, url=url, redownload=True)


@app.post("/api/projects/{pid}/source")
def reimport_source(pid: str, file: UploadFile = File(...)):
    """Put a removed source back from a file (it must be the same video)."""
    return _bad(storage.reimport, _404(store.get("project", pid)), file.file, file.filename or "")


@app.post("/api/shorts")
def import_short(file: UploadFile = File(...), video_type: str = Form("auto"), sub_genre: str = Form("")):
    """One old short from the browser: its own project, analysed, one whole clip. {project} or {duplicate}."""
    return _bad(storage.import_short_upload, file.file, file.filename or "", _video_type(video_type, sub_genre))


@app.post("/api/shorts/paths")
def import_short_paths(body: dict):
    """Old shorts already on this PC (files and/or folders, from the desktop picker): {added, duplicates, skipped}."""
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        raise HTTPException(400, "Send the files or folders to import as a list of paths.")
    return storage.import_short_paths(paths, _video_type(body.get("video_type"), body.get("sub_genre")))


class _ProjectFiles:
    """/files/<project id>/...: the project folders, served from whichever storage folder is chosen right now (re-created
    when it changes, so a move needs no restart). Only project-id folders: never anything else the folder holds."""

    def __init__(self):
        self.root, self.files = None, None

    async def __call__(self, scope, receive, send):
        from starlette.responses import PlainTextResponse
        root = config.projects_dir()
        if root != self.root:
            self.root, self.files = root, StaticFiles(directory=root, check_dir=False)
        first = Path(self.files.get_path(scope)).parts[:1]  # after StaticFiles' own normalisation ("a/../b" is "b")
        if not first or not storage.ID_RE.fullmatch(first[0]) or not root.is_dir():
            return await PlainTextResponse("Not Found", 404)(scope, receive, send)
        await self.files(scope, receive, send)


app.include_router(remote.router)  # /api/remote*: before the mounts below, which would swallow it
from .routes_live import router as _live_router  # noqa: E402  live & integrations (OBS marks, telemetry, HUD, MCP)
app.include_router(_live_router)
app.include_router(__import__(f"{__package__}.routes_signals", fromlist=["router"]).router)  # /api/signals* (WP1)
app.include_router(__import__(f"{__package__}.routes_learn", fromlist=["router"]).router)  # /api/learn/* (WP4: Lab insights, evals)
app.include_router(__import__(f"{__package__}.routes_render", fromlist=["router"]).router)  # proxies, encoders (WP2: render & QA)
app.include_router(__import__(f"{__package__}.routes_publish", fromlist=["router"]).router)  # /api/vault*, templates (WP3)
app.include_router(__import__(f"{__package__}.routes_editor", fromlist=["router"]).router)  # /api/triage, NLE export (WP5)
app.include_router(__import__(f"{__package__}.routes_models", fromlist=["router"]).router)  # /api/models/* (model marketplace)
from .routes_training import router as _training_router  # noqa: E402  /api/training/* (Model Lab; static import: packaged)
app.include_router(_training_router)
app.mount("/files", _ProjectFiles(), name="files")
app.mount("/assets", StaticFiles(directory=assets.ASSETS), name="assets")  # read-only files; no listing
app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
