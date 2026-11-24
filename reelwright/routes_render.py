"""Render & QA routes (WP2): preview proxies for the Editor/Studio players, and what this PC's ffmpeg can encode."""
from fastapi import APIRouter, HTTPException

from . import media, pipeline, store

router = APIRouter()


@router.post("/api/projects/{pid}/proxy")
def make_proxy(pid: str):
    """The project's preview copy: {"proxy": file or None, "needed": bool, "job"?}. Queues one (render queue) when the
    source scrubs badly in a browser (media.needs_proxy) and none exists yet."""
    p = store.get("project", pid)
    if not p:
        raise HTTPException(404, "Not found")
    if p.get("proxy") and (pipeline.pdir(pid) / p["proxy"]).is_file():
        return {"proxy": p["proxy"], "needed": True}
    if not p.get("source") or p.get("source_removed"):
        return {"proxy": None, "needed": False}
    try:
        src = pipeline.source_file(p)
        meta = p.get("meta") or {}
        if "vcodec" not in meta:  # projects imported before probe reported the codec: probe once, keep it
            meta = {**meta, **media.probe(src)}
            store.update("project", pid, {"meta": meta})
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    if not media.needs_proxy(meta):
        return {"proxy": None, "needed": False}
    jobs = store.find("job", project_id=pid, kind="proxy")
    running = [j for j in jobs if j["status"] in ("queued", "running")]
    if running:
        return {"proxy": None, "needed": True, "job": running[0]}
    if any(j["status"] == "failed" for j in jobs):  # it failed once: don't retry on every Editor open
        return {"proxy": None, "needed": True}
    busy = [j for j in store.find("job") if j["kind"] in ("export", "longform_export") and j["status"] in ("queued", "running")]
    if busy:  # never queue a long transcode ahead of the creator's own renders: the next Editor open asks again
        return {"proxy": None, "needed": True}
    return {"proxy": None, "needed": True, "job": pipeline.submit("proxy", project_id=pid)}


@router.get("/api/render/encoders")
def encoders():
    """Settings › Rendering: the usable H.264 encoders (best first), the ffmpeg licence, and whether renders run beside
    analysis right now."""
    return {"encoders": [n for n, _ in media.encoders()], "licence": media.licence(), "parallel": pipeline.parallel_render()}
