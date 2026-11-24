"""Routes for live & integrations: telemetry settings and status, GSI config files, VTube Studio authorization, the
teach-a-screen-event modal (hud.py), and the small read-only views the Claude Desktop tools (mcp_server.py) use."""
import math

import cv2
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from . import accounts, config, hud, mcp_server, pipeline, store, telemetry

router = APIRouter()
pipeline.HANDLERS["hud_scan"] = hud.scan_job  # heavy queue (it decodes the video); waits for an unplugged storage drive


def _404(doc, what="Not found"):
    if not doc:
        raise HTTPException(404, what)
    return doc


# ---------- telemetry settings ----------

@router.get("/api/live")
def live_status():
    return telemetry.status()


@router.put("/api/live")
def put_live(body: dict):
    patch: dict = {}
    for k in ("lol_enabled", "gsi_enabled", "vts_enabled"):
        if k in body:
            if not isinstance(body[k], bool):
                raise HTTPException(400, f"{k} must be true or false.")
            patch[k] = body[k]
    for k in ("gsi_port", "vts_port"):
        if k in body:
            v = body[k]
            if isinstance(v, bool) or not isinstance(v, int) or not 1024 <= v <= 65535:
                raise HTTPException(400, "Ports are numbers from 1024 to 65535.")
            patch[k] = v
    if "live_mark_hotkey" in body:
        spec = str(body["live_mark_hotkey"] or "").strip().lower().replace(" ", "")
        if spec:
            try:
                telemetry.parse_hotkey(spec)
            except ValueError as e:
                raise HTTPException(400, str(e))
        patch["live_mark_hotkey"] = spec
    config.save(patch)
    return telemetry.status()


@router.get("/api/live/gsi/{game}")
def gsi_file(game: str):
    try:
        text = telemetry.gsi_config(game)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return Response(text, media_type="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{telemetry.GSI_FILE}"'})


@router.post("/api/live/gsi/token")
def gsi_new_token():
    config.save({"gsi_token": ""})
    telemetry.gsi_token()
    return telemetry.status()


@router.post("/api/live/vts/authorize")
def vts_authorize():
    telemetry.vts_authorize()  # blocks until the creator clicks Allow in VTube Studio (VTSError -> 400)
    return telemetry.status()


@router.post("/api/live/vts/forget")
def vts_forget():
    config.save({"vts_token": ""})
    return telemetry.status()


# ---------- teach a screen event (HUD) ----------

def _hud_public(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "name", "kind", "game", "roi", "threshold", "enabled", "created")}


@router.get("/api/live/hud")
def hud_list():
    return [_hud_public(d) for d in store.find("hud")]


@router.get("/api/live/hud/frame")
def hud_frame(pid: str, t: float = 0.0):
    proj = _404(store.get("project", pid))
    if not math.isfinite(t):
        raise HTTPException(400, "Bad time.")
    img = hud.grab(pipeline.source_file(proj), t, 1280 if int((proj.get("meta") or {}).get("width") or 0) > 1280 else None)
    return Response(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes(), media_type="image/jpeg")


@router.get("/api/live/hud/{hid}.png")
def hud_image(hid: str):
    _404(store.get("hud", hid))
    return FileResponse(hud.DIR / f"{hid}.png", media_type="image/png")


@router.post("/api/live/hud")
def hud_teach(body: dict):
    proj = _404(store.get("project", str(body.get("pid") or "")), "That project no longer exists.")
    try:
        if not math.isfinite(float(body.get("t") or 0)):
            raise ValueError("Bad time.")
        doc = hud.teach(pipeline.source_file(proj), proj.get("meta") or {}, float(body.get("t") or 0), body.get("box"),
                        body.get("name", ""), body.get("game", ""), float(body.get("threshold") or 0.8))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return _hud_public(doc)


@router.patch("/api/live/hud/{hid}")
def hud_patch(hid: str, body: dict):
    _404(store.get("hud", hid))
    patch: dict = {}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise HTTPException(400, "enabled must be true or false.")
        patch["enabled"] = body["enabled"]
    if "threshold" in body:
        try:
            patch["threshold"] = min(max(float(body["threshold"]), 0.5), 0.99)
        except (TypeError, ValueError):
            raise HTTPException(400, "The threshold is a number from 0.5 to 0.99.")
    if "game" in body:
        patch["game"] = str(body["game"] or "").strip()[:40]
    return _hud_public(_404(store.update("hud", hid, patch)))


@router.delete("/api/live/hud/{hid}")
def hud_delete(hid: str):
    _404(store.get("hud", hid))
    hud.remove(hid)
    return {"ok": True}


@router.post("/api/live/hud/scan")
def hud_scan(body: dict):
    proj = _404(store.get("project", str(body.get("pid") or "")), "That project no longer exists.")
    if not hud.active():
        raise HTTPException(400, "No screen events are switched on: teach one first.")
    return pipeline.submit("hud_scan", project_id=proj["id"])


# ---------- what the Claude Desktop tools read (mcp_server.ALLOWED) ----------

@router.get("/api/live/mcp")
def mcp_setup():
    from .server import PORT
    return mcp_server.claude_config(PORT)


@router.get("/api/live/clips/{cid}")
def clip_view(cid: str):
    c = _404(store.get("clip", cid), "That clip no longer exists.")
    exp = c.get("export") or {}
    job = next((j for j in store.find("job", clip_id=cid, kind="export") if j["status"] in ("queued", "running")), None)
    return {"id": c["id"], "project_id": c["project_id"], "title": c.get("title"), "hook": c.get("hook"), "start": c.get("start"),
            "end": c.get("end"), "score": c.get("score"), "rating": c.get("rating"),
            "render": {"status": exp.get("status") or (job and job["status"]) or "not rendered", "progress": (job or exp).get("progress")},
            "uploads": [{"id": u["id"], "platform": u.get("platform", "youtube"), "account": (pipeline._account_of(u) or {}).get("name"),
                         "status": u.get("status"), "when": u.get("publish_at") or u.get("upload_at"), "url": u.get("url"),
                         "error": u.get("error") or ""} for u in store.find("upload", clip_id=cid)]}


@router.get("/api/live/review")
def review(limit: int = 20):
    from .remote import _review_clips  # the phone remote's queue: unrated, never-posted AI picks
    clips = _review_clips()
    names = {p["id"]: p.get("name") for p in store.find("project")}
    out = []
    for c in clips[:max(1, min(limit, 50))]:
        try:
            text = pipeline.clip_text(c)[:300]
        except (OSError, ValueError, KeyError):
            text = ""
        out.append({"id": c["id"], "project_id": c["project_id"], "project": names.get(c["project_id"]), "title": c.get("title"),
                    "hook": c.get("hook"), "score": c.get("score"), "start": c.get("start"), "end": c.get("end"), "snippet": text})
    return {"total": len(clips), "clips": out}


@router.get("/api/live/accounts")
def agent_accounts():
    """Accounts an assistant may post to: approved AND allowed for Autopilot (its posts are made as Autopilot's)."""
    return [{"id": a["id"], "platform": a["platform"], "name": a.get("name") or a.get("handle") or a["platform"].title()}
            for a in accounts.usable(auto=True)]
