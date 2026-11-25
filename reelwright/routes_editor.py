"""WP5 Editor UX: the Review (triage) queue across projects and NLE export (FCPXML / EDL / SRT) of a clip or long form.
Triage decisions are not a route of their own: the page PATCHes /api/clips/{id} {rating}, the same path as 👍/👎 and
the phone remote, so pipeline.remember records every verdict."""
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from . import media, nle, pipeline, store

router = APIRouter()


@router.get("/api/triage")
def triage(project: str = "", new_only: bool = True, min_score: float | None = None, limit: int = 50):
    """AI picks to judge, best first: never posted, not montages or expired; new_only = not rated yet."""
    posted = {u.get("clip_id") for u in store.find("upload", status="done")}
    clips = [c for c in store.find("clip") if c.get("source") in ("ai", "auto") and not c.get("parts") and not c.get("expired")
             and c["id"] not in posted and (not new_only or not c.get("rating")) and (not project or c["project_id"] == project)
             and (min_score is None or (c.get("score") or 0) >= min_score)]
    clips.sort(key=lambda c: -(c.get("score") or 0))
    projects = {p["id"]: p for p in store.find("project")}
    cards = []
    for c in clips[:max(1, min(limit, 200))]:
        p = projects.get(c["project_id"]) or {}
        exp = c.get("export") or {}
        rendered = pipeline.clip_export_current(c)
        media = exp["file"] if rendered else (p.get("proxy") or p.get("source"))
        cards.append({"id": c["id"], "project_id": c["project_id"], "project": p.get("name"), "title": c.get("title") or "Clip",
                      "hook": c.get("hook") or "", "score": c.get("score"), "rating": c.get("rating") or 0,
                      "start": c["start"], "end": c["end"], "rendered": rendered,
                      "src": f"/files/{c['project_id']}/{media}" if media and not p.get("source_removed") else None,
                      "text": pipeline.clip_text(c)[:400]})
    return {"clips": cards, "total": len(clips)}


TYPES = {"fcpxml": ("application/xml", "fcpxml"), "edl": ("text/plain", "edl"), "srt": ("text/plain", "srt")}


def _download(tl: dict, fmt: str) -> Response:
    if fmt not in TYPES:
        raise HTTPException(404, "Export as fcpxml, edl or srt.")
    ctype, ext = TYPES[fmt]
    body = {"fcpxml": nle.fcpxml, "edl": nle.edl, "srt": nle.srt}[fmt](tl)
    name = re.sub(r"[^\w .-]+", "_", tl["name"]).strip()[:80] or "timeline"
    ascii_name = re.sub(r"[^\w .-]+", "_", name, flags=re.ASCII).strip() or "timeline"  # headers are latin-1: UTF-8 rides in filename*
    return Response(body, media_type=f"{ctype}; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}.{ext}\"; filename*=UTF-8''{quote(name)}.{ext}"})


@router.get("/api/clips/{cid}/keeps")
def clip_keep_spans(cid: str):
    """The source spans the render keeps (silence, filler and text cuts applied): Clip Studio's Cuts track."""
    clip = store.get("clip", cid)
    if not clip or clip.get("parts"):
        raise HTTPException(404, "Clip not found.")
    e = {**media.DEFAULT_EDIT, **clip["edit"]}
    return {"keeps": pipeline.clip_keeps(clip, e, nle._words(clip["project_id"])) or [[clip["start"], clip["end"]]]}


@router.get("/api/clips/{cid}/nle/{fmt}")
def clip_nle(cid: str, fmt: str):
    clip = store.get("clip", cid)
    if not clip:
        raise HTTPException(404, "Clip not found.")
    if clip.get("parts"):
        raise HTTPException(400, "Montages can't be exported as a timeline: export its clips instead.")
    return _download(nle.clip_timeline(clip), fmt)


@router.get("/api/longform/{lid}/nle/{fmt}")
def longform_nle(lid: str, fmt: str):
    d = store.get("longform", lid)
    if not d:
        raise HTTPException(404, "Long form not found.")
    return _download(nle.longform_timeline(d), fmt)
