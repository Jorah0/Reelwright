"""HTTP API + static UI. Bound to localhost only (it holds your API keys and social tokens)."""
import json
import os
import re
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import ai, config, doctor, lan, learn, log, media, obs, pipeline, platforms, store, usage, youtube
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
    config.save(patch)
    return config.public()


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
    return pipeline.submit("find", project_id=pid, query=(body.get("query") or "").strip(), count=body.get("count"))


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


def clean_edit(edit: dict) -> dict:
    """Validate edit values at the API boundary (from the UI, the AI assistant, or anything else posting here)."""
    out = {}
    for k, v in edit.items():
        if k not in media.DEFAULT_EDIT:
            continue
        if k in EDIT_RANGES:
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


@app.patch("/api/clips/{cid}")
def patch_clip(cid: str, body: dict):
    clip = _404(store.get("clip", cid))
    dur = store.get("project", clip["project_id"])["meta"]["duration"]

    def apply(c):
        changed = False
        if not c.get("parts"):
            for k in ("start", "end"):
                if k in body:
                    c[k] = round(min(max(float(body[k]), 0.0), dur), 3)
                    changed = True
        if "edit" in body:
            c["edit"] = {**c["edit"], **clean_edit(body["edit"])}
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


@app.delete("/api/clips/{cid}")
def delete_clip(cid: str):
    clip = store.get("clip", cid)
    if clip:
        pipeline.remember(clip, "dismissed")  # deleting an unrated AI pick is a (weak) "not this" lesson
    store.delete("clip", cid)
    return {"ok": True}


@app.post("/api/clips/{cid}/export")
def export_clip(cid: str):
    _404(store.get("clip", cid))
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
    clip = _404(store.get("clip", cid))
    p = store.get("project", clip["project_id"])
    segs = pipeline.load_json(p["id"], "transcript.json", {"segments": []})["segments"]
    patch = ai.assist(body.get("instruction", ""), clip, clip["edit"], segs, p["meta"]["duration"])
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
    if not body.get("access_token") or not body.get("ig_user_id"):
        raise HTTPException(400, "Enter the access token and your Instagram professional account ID.")
    platforms.instagram_set(body["access_token"], body["ig_user_id"])
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
app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
