"""Background jobs: import → analyze → find clips → render → publish, plus the scheduler, Autopilot,
streaming-channel sources (Twitch / Kick / YouTube / any yt-dlp URL), virality analysis and style-profile training."""
import contextlib
import json
import os
import queue
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import (accounts, ai, assets, config, context, covers, experiments, learn, log, media, metrics, platforms, schedule, store,
               track, vision)
from . import signals as sig  # markers, chat reactions, viewer clips, audio events (signals.enrich after transcription)

_heavy = queue.Queue()    # GPU/CPU-bound analysis (imports, transcription, angles), one at a time
_render = queue.Queue()   # renders, long-form exports, preview proxies, subject tracking: beside analysis (parallel_render)
_ai = queue.Queue()       # Claude + metadata lookups (search, virality, training, channel scans)
_uploads = queue.Queue()  # network uploads, one at a time
_enqueued: set[str] = set()
_started = False
_serial = threading.Lock()  # analysis holds it per job; a render takes it too when it must not run alongside
QUEUE_OF = {"upload": _uploads, "find": _ai, "virality": _ai, "train": _ai, "research": _ai, "scan_source": _ai, "results": _ai,
            "cleanup": _ai, "export": _render, "longform_export": _render, "proxy": _render, "track": _render}
NO_MEDIA = {"results", "train", "research"}  # jobs that never touch project files: they run while the storage folder is away
STORAGE_POLL = 5.0


def pdir(pid: str):
    root = config.projects_dir()  # the chosen storage folder (Settings › Storage), followed live
    p = (root / pid).resolve()
    if not pid or p.parent != root.resolve():  # ids come from store.new_id; anything else is a traversal attempt
        raise ValueError(f"bad project id: {pid!r}")
    return p


def source_file(proj: dict):
    """The project's source video, or a clear RuntimeError instead of an ffmpeg crash: removed by the space saver
    (re-download / re-import it), or on a storage drive that isn't connected."""
    from . import storage
    if proj.get("source_removed"):
        fix = "re-download it" if storage.source_url(proj) else "re-import the file"
        raise RuntimeError(f"The source video was removed to save space: {fix} (Library or Editor) to edit or render from it again.")
    if storage.missing():
        raise RuntimeError(storage.hold_reason())
    if not proj.get("source"):
        raise RuntimeError("This project has no source video yet.")
    return pdir(proj["id"]) / proj["source"]


def load_json(pid: str, name: str, default=None):
    try:
        return json.loads((pdir(pid) / name).read_text("utf-8"))
    except FileNotFoundError:
        return default


def save_json(pid: str, name: str, data) -> None:
    (pdir(pid) / name).write_text(json.dumps(data), "utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def active_profile() -> dict | None:
    return next((p for p in store.find("profile") if p.get("active") and p.get("trained")), None)


def guidance() -> str:
    """The active profile's learned rules plus its live per-platform playbooks (empty when nothing is trained)."""
    p = next((p for p in store.find("profile") if p.get("active")), None)
    if not p:
        return ""
    parts = [p["trained"]["guidance"]] if p.get("trained") else []
    if p.get("playbooks"):
        parts.append("Current platform playbooks (researched from the web):\n" + ai.playbook_digest(p["playbooks"]))
    return "\n\n".join(parts)


# ---------- learning memory: what the creator thought of each pick ----------
# One "feedback" doc per clip (same id). It outlives the clip, so deleting or re-analysing never loses a lesson.
# why: "rated" (explicit 👍/👎, always wins) | "published" (implicit +1) | "dismissed" (deleted an AI pick unrated, -1)

def clip_text(clip: dict) -> str:
    segs = load_json(clip["project_id"], "transcript.json", {"segments": []})["segments"]
    return " ".join(s["text"] for s in segs if clip["start"] <= s["start"] < clip["end"])


def remember(clip: dict | None, why: str) -> None:
    """Best-effort: a learning-memory failure must never fail an upload, delete or rating."""
    if not clip or clip.get("parts"):  # gone, or a montage (a compilation, not a pick)
        return
    try:
        with store._lock:  # read-decide-write as one step so quick rating toggles can't interleave
            _remember(clip, why)
    except Exception:
        log.error("learning", f"remember {why} {clip.get('id')}")


def _snapshot(clip: dict) -> dict:
    """What a lesson records about the clip: its words, measurable traits, edit choices and how it was re-trimmed."""
    proj = store.get("project", clip["project_id"]) or {}
    feats = clip.get("features")
    if not feats:
        segs = load_json(clip["project_id"], "transcript.json", {"segments": []})["segments"]
        feats = learn.features(clip, segs, load_json(clip["project_id"], "signals.json", {}) or {},
                               (proj.get("meta") or {}).get("duration") or clip["end"])
    ab = clip.get("ai_bounds")
    edit = clip.get("edit") or {}
    return {"title": clip.get("title", ""), "hook": clip.get("hook", ""), "text": clip_text(clip)[:1500],
            "length": round(clip["end"] - clip["start"], 1), "ai_score": clip.get("ai_score", clip.get("score")),
            "source": clip.get("source"), "query": clip.get("query", ""), "features": feats,
            "edit": {k: edit[k] for k in (*learn.STYLE_KEYS, *learn.NUM_KEYS) if k in edit},
            "trim": [round(clip["start"] - ab[0], 2), round(clip["end"] - ab[1], 2)] if ab else None,
            "video_type": (proj.get("video_type") or {}).get("type")}


def _remember(clip: dict, why: str) -> None:
    old = store.get("feedback", clip["id"])
    if why == "refresh":  # the clip was edited after a lesson was recorded: keep the verdict, update what it says
        if old:
            store.put("feedback", {**old, **_snapshot(clip)})
        return
    if why == "rated":
        signal = clip.get("rating") or 0
        if not signal:  # un-rated: drop the explicit lesson, fall back to what the clip still implies
            if old and old["why"] == "rated":
                store.delete("feedback", clip["id"])
            if clip.get("posts"):
                _remember(clip, "published")
            return
    elif old or (why == "dismissed" and not replaceable(clip)):
        return  # implicit signals never overwrite a lesson; only deleting an untouched AI pick means "not this"
    else:
        signal = 1 if why == "published" else -1
    keep = {k: old[k] for k in RESULT_FIELDS if old and k in old}  # real results survive a re-rating
    store.put("feedback", {"id": clip["id"], "created": time.time(), "project_id": clip["project_id"], "signal": signal,
                           "why": why, **_snapshot(clip), **keep})


def feedback(limit: int = 200) -> list[dict]:
    """Lessons, explicit ratings first, newest first within each group."""
    return sorted(store.find("feedback"), key=lambda f: f.get("why") != "rated")[:limit]


def taste_examples(per_side: int = 6) -> str:
    """Few-shot memory for the clip finder: the creator's latest likes and rejections, applied on the very next scan
    (no retraining needed). ponytail: global across profiles; scope by profile id if creators share one install."""
    fb = feedback()
    out = []
    for label, sign, mark in (("LIKED picks", 1, "+"), ("REJECTED picks", -1, "-")):
        rows = [f for f in fb if f.get("signal") == sign][:per_side]
        if rows:
            out.append(f"{label}:")
            out += [f"{mark} [{f.get('why')}, {learn.num(f.get('length')) or 0:.0f}s] {f.get('title') or ''} — "
                    f"{str(f.get('text') or '')[:160]}" for f in rows]
    return "\n".join(out)


LEARN_FORMAT = "reelwright-learning"
LEARN_FIELDS = {"id", "created", "project_id", "signal", "why", "title", "hook", "text", "length", "ai_score", "source", "query",
                "features", "edit", "trim", "views", "likes", "results_at", "video_type", "account_id", "platform", "lift"}
RESULT_FIELDS = ("views", "likes", "results_at", "account_id", "platform", "lift", "retention")  # set by _results, not by a rating


def export_learning() -> dict:
    """Everything learned, as portable JSON: move it to another PC or keep a backup."""
    return {"format": LEARN_FORMAT, "version": 1, "exported_at": now_iso(),
            "feedback": [{k: v for k, v in f.items() if k in LEARN_FIELDS} for f in store.find("feedback")]}


def import_learning(data: dict) -> dict:
    """Merge lessons from an export. Untrusted input: only known fields, valid verdicts, safe ids; existing lessons win."""
    if not isinstance(data, dict) or data.get("format") != LEARN_FORMAT or not isinstance(data.get("feedback"), list):
        raise RuntimeError("That file isn't a Reelwright learning export.")
    added = skipped = 0
    for f in data["feedback"][:20000]:
        ok = (isinstance(f, dict) and isinstance(f.get("id"), str) and f["id"].isalnum() and len(f["id"]) <= 40
              and f.get("signal") in (1, -1) and f.get("why") in ("rated", "published", "dismissed")
              and isinstance(f.get("created"), (int, float)))
        if not ok or store.get("feedback", f["id"]):
            skipped += 1
            continue
        doc = {k: v for k, v in f.items() if k in LEARN_FIELDS}
        feats = doc.pop("features", None)
        if isinstance(feats, dict):
            doc["features"] = {k: learn.num(v) for k, v in feats.items() if k in learn.FEATURES and learn.num(v) is not None}
        for k in ("length", "ai_score", "views", "likes", "lift"):  # numbers, or gone
            v = learn.num(doc.pop(k, None))
            if v is not None:
                doc[k] = int(v) if k in ("views", "likes") else v
        trim = doc.pop("trim", None)
        if isinstance(trim, list) and len(trim) == 2 and all(learn.num(x) is not None for x in trim):
            doc["trim"] = [float(x) for x in trim]
        edit = doc.pop("edit", None)
        if isinstance(edit, dict):
            doc["edit"] = learn.clean_edit(edit)
        if not (isinstance(doc.get("video_type"), str) and doc["video_type"] in ai.VIDEO_TYPES):  # a known type key, or gone
            doc.pop("video_type", None)
        if doc.get("platform") not in platforms.PLATFORMS:
            doc.pop("platform", None)
        aid = doc.get("account_id")
        if not (isinstance(aid, str) and 0 < len(aid) <= 40 and aid.replace("_", "").replace("-", "").isalnum()):
            doc.pop("account_id", None)
        for k, cap in (("title", 300), ("hook", 500), ("text", 1500), ("query", 300), ("source", 20), ("project_id", 40),
                       ("results_at", 40)):
            if k in doc or k in ("title", "hook", "text", "query"):
                doc[k] = str(doc.get(k) or "")[:cap]
        store.put("feedback", doc)
        added += 1
    log.event("learning", f"imported {added} lessons, skipped {skipped}")
    return {"added": added, "skipped": skipped}


def replaceable(clip: dict) -> bool:
    """Re-analysis may replace an AI pick only if the creator never touched it."""
    return (clip.get("source") in ("ai", "auto") and not clip.get("rating") and clip.get("rev", 1) <= 1
            and not clip.get("posts") and not clip.get("export"))


def creator_context(prof: dict) -> str:
    t = prof.get("trained") or {}
    answers = "\n".join(f"- {k}: {v}" for k, v in (prof.get("answers") or {}).items() if str(v).strip())
    tz = datetime.now().astimezone()
    return (f"Profile: {prof['name']}\nNotes: {prof.get('notes') or '(none)'}\nInterview:\n{answers or '(none)'}\n"
            f"Learned style: {t.get('summary', '(not trained yet)')}\nWinning topics: {', '.join(t.get('winning_topics', []))}\n"
            f"Creator's local time zone: {tz.tzname()} (UTC{tz.strftime('%z')})")


# ---------- job plumbing ----------

def submit(kind: str, **fields) -> dict:
    job = store.put("job", {"kind": kind, "status": "queued", "progress": 0.0, "message": "Queued", "error": "", **fields})
    _enqueue(job)
    return job


def _enqueue(job: dict):
    if job["id"] not in _enqueued:
        _enqueued.add(job["id"])
        QUEUE_OF.get(job["kind"], _heavy).put(job["id"])


def _progress(job_id: str, frac: float, msg: str | None = None, project_id: str | None = None, lo=0.0, hi=1.0):
    p = round(lo + (hi - lo) * frac, 4)
    store.update("job", job_id, {"progress": p, **({"message": msg} if msg else {})})
    if project_id:
        store.update("project", project_id, {"progress": p, **({"stage": msg} if msg else {})})


def _worker(q: queue.Queue, gate=contextlib.nullcontext):
    """One queue's worker; gate() = what each job holds while it runs (see _analysis_gate / _render_gate)."""
    while True:
        jid = q.get()
        try:
            with gate():
                _run_job(jid)
        except Exception:  # a broken hook must never kill the queue
            log.error("queue", f"job {jid}")


def parallel_render() -> bool:
    """Settings › Rendering: renders run beside analysis. auto = when NVENC encodes (the GPU renders, the CPU stays free for
    transcription) or the CPU has 8+ cores; on a weaker CPU an x264 render would just slow both down."""
    v = config.load().get("parallel_render")
    if v in ("on", "off"):
        return v == "on"
    return media._has_nvenc() or (os.cpu_count() or 1) >= 8


def _analysis_gate():
    return _serial


def _render_gate():
    return contextlib.nullcontext() if parallel_render() else _serial


def _run_job(jid: str):
    job = store.get("job", jid)
    if not job or job["status"] == "canceled":
        return
    if job["kind"] not in NO_MEDIA:
        job = _wait_for_storage(job)
        if not job:
            return
    store.update("job", jid, {"status": "running", "started": time.time()})
    try:
        HANDLERS[job["kind"]](job)
        store.update("job", jid, {"status": "done", "progress": 1.0, "finished": time.time()},
                     fn=lambda j: j.update(message=j["message"] if j["message"] not in ("Queued", "") else "Done"))
    except Exception as e:
        log.error("job", f"{job['kind']} {jid} failed: {e}")
        store.update("job", jid, {"status": "failed", "error": str(e), "message": "Failed", "finished": time.time()})
        FAIL_HOOKS.get(job["kind"], lambda j, e: None)(job, str(e))


def _wait_for_storage(job: dict) -> dict | None:
    """Hold a job that needs project files while the storage folder is unplugged or being moved: it waits (queued, with
    the reason as its message) and continues by itself, never failing over it. None if it was canceled meanwhile.
    ponytail: holds its whole queue (one worker each), fine for a drive that's away or a move in progress."""
    from . import storage
    waited = False
    while why := storage.hold_reason():
        store.update("job", job["id"], {"message": f"Waiting: {why}"})
        waited = True
        time.sleep(STORAGE_POLL)
        job = store.get("job", job["id"])
        if not job or job["status"] == "canceled":
            return None
    if waited:
        store.update("job", job["id"], {"message": "Queued"})
    return job


# ---------- import & analysis ----------

def new_project(name: str, autopilot: dict | None = None, origin: dict | None = None, video_type: dict | None = None) -> dict:
    """video_type: a creator-fixed ai.fixed_type(...), or None to auto-detect during analysis."""
    from . import storage
    storage.check_writable()  # an unplugged storage drive or a move in progress: refuse, never write somewhere else
    p = store.put("project", {"name": name, "status": "importing", "stage": "Importing", "progress": 0.0, "source": None,
                              "meta": None, "autopilot": autopilot or {"enabled": False}, "error": "", "origin": origin,
                              "video_type": video_type})
    pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    return p


def ytdlp_download(url: str, outtmpl: str, hook=None) -> dict:
    """Download one video (≤1080p mp4) with yt-dlp to outtmpl; returns its info. Used for projects and camera angles."""
    import yt_dlp

    opts = {"outtmpl": outtmpl, "format": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "merge_output_format": "mp4", "noplaylist": True, "quiet": True, "no_warnings": True, "progress_hooks": [hook] if hook else []}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def _import_url(job):
    import yt_dlp

    pid = job["project_id"]
    store.update("project", pid, {"status": "importing", "stage": "Downloading"})

    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") == "downloading" and total:
            _progress(job["id"], (d.get("downloaded_bytes") or 0) / total, "Downloading", pid, 0, 1)

    info = ytdlp_download(job["url"], str(pdir(pid) / "source.%(ext)s"), hook)
    src = next(p for p in pdir(pid).iterdir() if p.stem == "source" and p.suffix in media.VIDEO_EXT)
    if job.get("redownload"):  # removed by the space saver: put it back; analysis, clips and the name stay as they are
        meta, want = media.probe(src), float((store.get("project", pid).get("meta") or {}).get("duration") or 0)
        if want and abs(float(meta["duration"]) - want) > 1.5:  # the link now serves another cut: clips would misalign
            src.unlink(missing_ok=True)
            raise RuntimeError(f"The link now gives a different video ({meta['duration']:.0f} s; the original was {want:.0f} s), "
                               "so your clips wouldn't line up. Re-import the original file instead.")
        store.update("project", pid, {"source": src.name, "source_removed": None, "source_at": now_iso(), "meta": meta,
                                      "status": "ready", "stage": "Ready", "progress": 1.0, "error": ""})
        log.event("storage", f"re-downloaded the source of project {pid}")
        return
    store.update("project", pid, {"source": src.name, "source_url": job["url"], "source_at": now_iso(), "name": info.get("title") or store.get("project", pid)["name"],
                                  "source_info": {  # what the video-type classifier reads (metadata only, kept short)
                                      "channel": info.get("channel") or info.get("uploader"), "site": info.get("extractor_key"),
                                      "categories": (info.get("categories") or [])[:5], "tags": (info.get("tags") or [])[:15],
                                      "description": (info.get("description") or "")[:500]}})
    if (info.get("subtitles") or {}).get("live_chat"):  # a past livestream with chat replay: chat bursts mark the moments
        _progress(job["id"], 1.0, "Downloading live-chat replay", pid)
        try:
            with yt_dlp.YoutubeDL({"outtmpl": str(pdir(pid) / "source.%(ext)s"), "skip_download": True, "writesubtitles": True,
                                   "subtitleslangs": ["live_chat"], "quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
                ydl.download([job["url"]])
        except Exception as e:  # optional signal: never fail the import over chat
            log.event("import", f"live chat skipped: {e}")
    if info.get("timestamp"):  # when the stream started: lines a live-captured Twitch chat log up with it (chatlog.py)
        store.update("project", pid, {"recorded_at": float(info["timestamp"])})
    submit("analyze", project_id=pid)


def chat_counts(path, duration: float, bin_s: int = 10) -> list[int]:
    """Messages per 10 s from a yt-dlp live_chat.json (one JSON action per line)."""
    counts = [0] * (int(duration // bin_s) + 1)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ms = json.loads(line)["replayChatItemAction"]["videoOffsetTimeMsec"]
                i = int(int(ms) / 1000 // bin_s)
            except (ValueError, KeyError, TypeError):
                continue
            if 0 <= i < len(counts):
                counts[i] += 1
    return counts


TYPE_FRAMES, TYPE_FRAME_W = 6, 256  # small stills spread across the video for the type classifier


def _type_about(proj: dict) -> str:
    """Cheap text evidence for the classifier: title, where it came from, the downloader's metadata."""
    o, i = proj.get("origin") or {}, proj.get("source_info") or {}
    lines = [f"Title / file name: {proj.get('name')}",
             o.get("platform") and f"Platform: {o['platform']}", o.get("url") and f"URL: {o['url']}",
             o.get("obs") and "Recorded with OBS (a replay-buffer or recording save)",
             i.get("channel") and f"Channel: {i['channel']}", i.get("site") and f"Site: {i['site']}",
             i.get("categories") and f"Categories: {', '.join(map(str, i['categories']))}",
             i.get("tags") and f"Tags: {', '.join(map(str, i['tags']))}", i.get("description") and f"Description: {i['description']}"]
    return "\n".join(str(x) for x in lines if x)


def _type_words(proj: dict) -> str:
    """What the offline keyword heuristic reads: title, tags and categories (descriptions/URLs match too loosely)."""
    i = proj.get("source_info") or {}
    return " | ".join(map(str, [proj.get("name") or "", *(i.get("tags") or []), *(i.get("categories") or [])]))


def _video_type(pid: str, src, dur: float, signals: dict | None = None, segs: list | None = None) -> dict:
    """Detect (or, with signals + transcript, refine) the video type unless the creator fixed it. With a key: one cheap
    Claude call on the title/metadata and a few small frames; refined after the transcript only when it was unsure.
    Without a key, or if the call fails: the keyword + signal heuristic. Never fails the analysis."""
    try:
        return _detect_type(pid, src, dur, signals, segs)
    except Exception:
        log.error("video-type", f"detection failed for {pid}")
        return (store.get("project", pid) or {}).get("video_type") or {}


def _detect_type(pid: str, src, dur: float, signals: dict | None, segs: list | None) -> dict:
    proj = store.get("project", pid) or {}
    old = vt = proj.get("video_type") or {}
    if vt.get("source") == "user":
        return vt
    if signals is not None and vt.get("source") == "ai" and vt.get("confidence", 0) >= 0.6:
        return vt  # confident early guess: no second call
    about = _type_about(proj)
    try:
        if not ai.ready("classify"):
            raise LookupError("no API key")
        t0 = time.time()
        shots = []
        for i in range(TYPE_FRAMES):  # one seek per frame: a full-length fps filter would decode the whole VOD
            t = dur * (i + 0.5) / TYPE_FRAMES
            try:
                shots += vision.frames(src, t, min(t + 1, dur), 1, TYPE_FRAME_W)
            except Exception as e:  # text-only evidence still classifies
                log.event("video-type", f"frame at {t:.0f}s skipped: {e}")
        excerpt = ai.transcript_lines(ai.even_sample(segs, 60)) if segs else ""
        vt = ai.classify_video(about, shots, excerpt)
        log.event("video-type", f"{pid}: {vt['type']}/{vt.get('sub_genre')} {vt['confidence']:.2f} in {time.time() - t0:.1f}s")
    except Exception as e:
        if not isinstance(e, LookupError):
            log.event("video-type", f"classifier skipped, using the heuristic: {e}")
        if old.get("source") == "ai":
            return old  # a failed refine keeps the earlier AI guess rather than a weaker keyword one
        vt = ai.heuristic_type(_type_words(proj), signals, segs, dur)

    def keep_user(doc):  # the creator may have picked a type while the call ran: theirs wins
        if (doc.get("video_type") or {}).get("source") != "user":
            doc["video_type"] = vt
    return (store.update("project", pid, fn=keep_user) or {}).get("video_type") or vt


def _analyze(job):
    pid = job["project_id"]
    proj = store.update("project", pid, {"status": "analyzing", "error": ""})
    src = source_file(proj)
    step = lambda frac, msg, lo, hi: _progress(job["id"], frac, msg, pid, lo, hi)  # noqa: E731

    step(0, "Reading video", 0, 0.02)
    meta = media.probe(src)
    store.update("project", pid, {"meta": meta})
    dur = meta["duration"]
    step(0, "Detecting the video type", 0.02, 0.02)  # before the expensive passes: it steers what they look for
    vt = _video_type(pid, src, dur)
    # no-one on camera (confidently): skip face detection, the costliest part of the on-screen pass
    faces = not (vt.get("people_on_cam") == 0 and not vt.get("facecam") and vt.get("confidence", 0) >= 0.7)

    step(0, "Building thumbnails", 0.02, 0.08)
    media.poster(src, min(dur * 0.1, 5), pdir(pid) / "poster.jpg")
    count = int(min(100, max(10, dur / 4)))
    try:
        media.sprite(src, dur, pdir(pid) / "sprite.jpg", count)
        store.update("project", pid, {"sprite_count": count})
    except RuntimeError:
        pass

    signals = {"waveform": [], "loud": [], "scenes": [], "silences": []}
    if meta["has_audio"]:
        step(0, "Analyzing audio energy", 0.08, 0.12)
        signals["waveform"], signals["loud"] = media.audio_signals(src)
    step(0, "Detecting scene cuts and on-screen action", 0.12, 0.25)
    from . import hud  # screen events the creator taught (hud.py): matched in the same decode, saved before markers merge
    tpl = hud.active()
    try:  # one decode for both; the on-screen part is best-effort and clip finding works without it
        signals["visual"] = vision.analyze(src, meta, lambda f: step(f, None, 0.12, 0.25), faces=faces, scenes=True,
                                           **({"hud": tpl} if tpl else {}))  # no templates: the call is exactly as before
        signals["scenes"] = signals["visual"].pop("scenes")
        hud.save_markers(pid, signals["visual"].pop("hud", None), tpl)
    except Exception as e:
        log.event("vision", f"on-screen analysis skipped: {e}")
        signals.pop("visual", None)
        signals["scenes"] = media.scenes(src)
    if meta["has_audio"]:
        step(0, "Detecting silences", 0.25, 0.3)
        signals["silences"] = media.silences(src)
    chat = next(pdir(pid).glob("*.live_chat.json"), None)
    if chat:
        signals["chat"] = chat_counts(chat, dur)
    save_json(pid, "signals.json", signals)

    transcript = {"segments": [], "language": None}
    if meta["has_audio"]:
        step(0, "Transcribing speech (Whisper)", 0.3, 0.3)
        transcript = ai.transcribe(src, dur, lambda f: step(f, None, 0.3, 0.8), vocab=sig.vocab(proj, config.load(), active_profile()))
    save_json(pid, "transcript.json", transcript)
    store.update("project", pid, {"has_transcript": bool(transcript["segments"]), "language": transcript.get("language")})
    _video_type(pid, src, dur, signals, transcript["segments"])  # refine an unsure guess now that the words are known
    step(0, "Reading markers, chat reactions, viewer clips and sounds", 0.8, 0.8)
    sig.enrich(pdir(pid), store.get("project", pid), src, dur, signals, transcript["segments"])  # each part best-effort
    save_json(pid, "signals.json", signals)

    step(0, "Finding the best clips", 0.8, 0.8)
    for old in store.find("clip", project_id=pid):  # re-analysis replaces untouched AI picks; rated/edited/posted stay
        if replaceable(old):
            store.delete("clip", old["id"])
    _find(pid, query="", job=job)
    store.update("project", pid, {"status": "ready", "stage": "Ready", "progress": 1.0})
    _autopilot(pid)


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v > 0 else None


def lengths(s: dict, target=None, max_len=None) -> tuple[float, float, float]:
    """(min, target, max) clip seconds from settings, with optional per-search overrides. max is a hard cap; target sits
    between min and max (0/unset = halfway); an override below the saved min pulls min down so the range stays usable."""
    mx = min(max(_num(max_len) or float(s["clip_max"]), 5.0), 900.0)
    tg = _num(target) or _num(s.get("clip_target")) or (float(s["clip_min"]) + mx) / 2
    tg = min(tg, mx)
    mn = max(3.0, min(float(s["clip_min"]), tg * 0.8))
    return round(mn, 2), round(max(tg, mn), 2), round(mx, 2)


VISUAL_KIND = {"action": "Action", "flash": "Flash", "color": "Colour shift", "face_reaction": "On-cam reaction"}


def visual_picks(spikes: list[dict], clip_max: float, dur: float) -> list[dict]:
    """Clips around the strongest visual spikes (a short lead-in first), best first, without overlaps."""
    out = []
    for m in spikes:
        start = max(0.0, m["t"] - min(5.0, clip_max / 3))
        name = VISUAL_KIND.get(m["kind"], m["kind"])
        out.append({"start": start, "end": min(start + clip_max, dur), "score": round(40 + 40 * m["strength"]),
                    "title": f"{name} at {int(m['t'] // 60)}:{int(m['t'] % 60):02d}", "hook": "", "hashtags": [],
                    "reason": f"On-screen {name.lower()} (no speech detected)."})
    return ai._dedupe(out)


def _find(pid: str, query: str, job: dict | None = None, count: int | None = None, target=None, max_len=None):
    s = config.load()
    proj = store.get("project", pid)
    segs = load_json(pid, "transcript.json", {"segments": []})["segments"]
    signals = load_json(pid, "signals.json", {})
    if proj.get("source") and not proj.get("source_removed"):  # lets the long-video judge look at frames (ai.rank_moments)
        signals["video"] = str(pdir(pid) / proj["source"])
    if proj.get("video_type"):  # its playbook goes into the finder's cached context and the judge's prompt
        signals["video_type"] = proj["video_type"]
    dur = proj["meta"]["duration"]
    count = count or s["clip_count"]
    clip_min, clip_target, clip_max = lengths(s, target, max_len)
    # short source (an OBS replay-buffer save, a short upload): the whole thing is the clip — tighten it to the speech.
    # An imported short (storage.import_short*) always is one whole clip, whatever its length
    whole = not query and (proj.get("origin") or {}).get("kind") == "imported_short"
    short = not query and (dur <= clip_max * 1.25 or whole)
    if short:
        count, clip_min, clip_max, clip_target = 1, max(3.0, min(clip_min, dur * 0.5)), dur, None
    # "find more" (no query): skip moments the creator already has as clips, asking for extras so the count still fills
    have = [] if query else [c for c in store.find("clip", project_id=pid) if not c.get("parts")]
    if short and have:
        return []
    want = count + len(have)
    fb = feedback()
    model = learn.train(fb)
    used_ai = (ai.ready("find") or bool(ai.consensus_pair())) and bool(segs)  # the provider routed for "find" (Settings → AI), Claude by default
    if used_ai:
        taste = "\n".join(x for x in (taste_examples(), learn.hints(fb, model, (proj.get("video_type") or {}).get("type"))) if x)
        aim = clip_target if (not query or target or max_len) else None  # a search obeys lengths only when picked for it
        prog = (lambda f: _progress(job["id"], f, "Scanning long video in parts", pid, 0.8, 0.98)) if job else None
        raw = ai.find_moments(segs, dur, signals, want, clip_min, clip_max, query, guidance(), taste,
                              avoid=[(c["start"], c["end"]) for c in have], progress=prog, target=aim)
        lo_len, hi_len = (3, clip_max if max_len else clip_max * 2) if query else (clip_min, clip_max)
        found = ai.refine(raw, segs, dur, lo_len, hi_len, target=aim)
        if not query and not short:  # creator's learned re-trim habit, re-snapped to sentence boundaries
            found = ai.refine(learn.apply_trim(found, learn.trim_bias(fb)), segs, dur, lo_len, hi_len, target=aim)
    elif query:
        q = query.lower()
        found = [{"start": x["start"], "end": x["end"], "score": 50, "title": x["text"][:60], "hook": x["text"],
                  "reason": "Transcript text match (add a Claude API key for semantic search).", "hashtags": []}
                 for x in segs if q in x["text"].lower()][:count]
    elif segs:
        found = ai.heuristic_moments(segs, signals.get("loud", []), dur, want if not short else 1, clip_min, clip_max,
                                     marks=signals.get("markers", []) + signals.get("viewer_clips", []))  # WP1 seeds
    elif (signals.get("visual") or {}).get("spikes"):  # no speech: cut around on-screen action, flashes, reactions
        found = visual_picks(signals["visual"]["spikes"], clip_target or clip_max, dur)[:want]  # aim at the target length
    else:  # no speech: cut around scene changes
        marks = sorted({0.0, *signals.get("scenes", [])})
        found = [{"start": t, "end": min(t + clip_max, dur), "score": 40, "title": f"Scene at {int(t // 60)}:{int(t % 60):02d}",
                  "hook": "", "reason": "Scene change (no speech detected).", "hashtags": []} for t in marks[:want]]
    if short and not found:
        found = [{"start": 0.0, "end": dur, "score": 50, "title": proj["name"][:70], "hook": "", "reason": "Whole clip.", "hashtags": []}]
    if whole:  # the AI's title/hook, but never trimmed: it's the creator's finished short
        found = [{**found[0], "start": 0.0, "end": dur}]
    if segs and not short and (used_ai or not query) and s.get("context_guard", True):  # a pick opening mid-story ("he…",
        found = ai._dedupe(context.guard(found, segs, hi_len if used_ai else clip_max, signals))  # "and then…") starts at
        # its setup; two picks that reached back to the same setup keep only the better one
    # a pick is a duplicate when most of it is already an existing clip (a tiny clip inside a big pick doesn't count)
    found = [f for f in found if not any(min(f["end"], c["end"]) - max(f["start"], c["start"]) > 0.5 * (f["end"] - f["start"])
                                         for c in have)]
    for c in found:  # measurable traits, then the creator's personal reranker (works offline too)
        c["features"] = learn.features(c, segs, signals, dur)
    found = learn.rerank(found, model)[:count]
    cards = s.get("context_cards", "auto") != "off" and ai.ready("meta")
    for c in found:
        clip = new_clip(pid, c, source="search" if query else ("ai" if used_ai else "auto"), query=query)
        if c.get("consensus"):  # which two AIs agreed and why (ai.review_moments), shown as a chip on the clip card
            store.update("clip", clip["id"], {"consensus": c["consensus"]})
        if cards and c.get("needs_context"):  # its setup didn't fit: an "Earlier: …" card instead
            _context_card(clip, segs)
    if job:
        store.update("job", job["id"], {"message": f"Found {len(found)} clips"})
    return found


def _context_card(clip: dict, segs: list[dict]) -> None:
    """Best-effort: a small AI call writes the card; it shows as the clip's headline for its first 2 s (unless the clip
    already has a headline). A failure only costs the card."""
    try:
        text = context.card(clip, segs)
    except Exception as e:
        log.event("context", f"no card for clip {clip['id']}: {e}")
        return
    if not text:
        return

    def apply(c):
        c["context_card"] = text
        c["edit"].update(context.card_edit(c["edit"], text) or {})
    store.update("clip", clip["id"], fn=apply)


def default_layout(proj: dict | None) -> dict:
    """The layout saved as default (server.save_layout_default), minus region sources this project has no camera
    angle for (an angle id belongs to one project; a stray one would 400 the clip's next edit)."""
    lay = dict((store.get("pref", "layout") or {}).get("edit") or {})
    if lay.get("regions"):
        have = {a.get("id") for a in (proj or {}).get("angles") or [] if isinstance(a, dict)}
        lay["regions"] = [{k: v for k, v in r.items() if not (k == "source" and v not in have)} for r in lay["regions"]]
    return lay


STREAM_HOSTS = {"twitch.tv": "twitch", "kick.com": "kick", "youtube.com": "youtube", "youtu.be": "youtube"}
_NOT_HANDLES = {"videos", "video", "clip", "clips", "watch", "live", "shorts", "embed", "channel", "c", "user", "directory", "popout"}


def streamer_guess(proj: dict) -> dict:
    """{platform, handle, name} for the streamer card, from what the project knows: the connected channel it came from,
    its link (twitch.tv/<h>, kick.com/<h>, youtube.com/@<h>), the downloader's channel name, else a linked account of
    that platform. Only what was found; {} for a plain local file."""
    from urllib.parse import urlparse

    o, info = proj.get("origin") or {}, proj.get("source_info") or {}
    src = store.get("source", o.get("source_id") or "") if o.get("source_id") else None
    out: dict = {}
    if src and src.get("platform") in media.CARD_PLATFORMS:
        out = {"platform": src["platform"], "handle": str(src.get("handle") or "").lstrip("@").strip()}
    url = proj.get("source_url") or o.get("url") or ""
    u = urlparse(url if "//" in url else f"https://{url}") if url else None
    host = (u.hostname or "").removeprefix("www.").removeprefix("m.") if u else ""
    plat = next((p for h, p in STREAM_HOSTS.items() if host == h or host.endswith("." + h)), None)
    if plat and not out:
        first = next((s for s in (u.path or "").split("/") if s), "")
        h = first[1:] if plat == "youtube" and first.startswith("@") else first if plat != "youtube" else ""
        out = {"platform": plat, **({"handle": h} if h and h.lower() not in _NOT_HANDLES else {})}
    name = str(info.get("channel") or "").strip()[:media.CARD_TEXT_MAX]
    if name:
        out["name"] = name
        if out.get("platform") in ("twitch", "kick") and not out.get("handle") and name.replace("_", "").isalnum():
            out["handle"] = name.lower()  # their channel names are the handle in another case
    if not out.get("handle") and out.get("platform") in (None, "youtube"):  # a linked YouTube account (the one card platform
        acc = next(iter(accounts.all_accounts("youtube")), None)              # accounts.py links)
        if acc and (acc.get("handle") or acc.get("name")):
            out = {"platform": "youtube", "handle": str(acc.get("handle") or "").lstrip("@"), "name": out.get("name") or acc.get("name")}
    return {k: v for k, v in out.items() if v}


def new_clip(pid: str, c: dict, source: str = "manual", query: str = "", parts: list | None = None) -> dict:
    s = config.load()
    proj = store.get("project", pid)
    edit = {**media.DEFAULT_EDIT, "aspect": s["default_aspect"]}
    prof = active_profile()
    if prof:  # the trained profile's theme becomes the starting look
        t = prof["trained"]["theme"]
        edit.update(aspect=t["aspect"], layout=t["layout"], caption_style=t["caption_style"], caption_pos=t["caption_pos"])
    edit.update(learn.house_style(feedback()))  # what the creator actually keeps choosing beats the profile's guess
    edit.update(default_layout(proj))  # a layout saved as default beats both
    m = proj.get("meta") or {}
    if m.get("height", 1) > m.get("width", 0) and edit["aspect"] != "source":
        edit.update(aspect="9:16", layout="single")  # already vertical
    hl = (store.get("pref", "headline") or {}).get("edit")  # "headline on new clips": the saved look, this clip's hook as text
    if hl and (text := media.headline_text(c.get("hook") or c.get("title"))):
        edit.update(hl, headline={**hl.get("headline", {}), "on": True, "text": text})
    sc = store.get("pref", "streamer_card") or {}
    if sc.get("auto") and sc.get("card"):  # "streamer card on new clips": the saved card, empty fields from this source
        guess = streamer_guess(proj or {})
        edit["streamer_card"] = {**sc["card"], "on": True, **{k: v for k, v in guess.items() if not sc["card"].get(k)}}
    ai_pick = source in ("ai", "auto", "search")
    return store.put("clip", {
        "project_id": pid, "start": c["start"], "end": c["end"], "title": c.get("title") or "Untitled clip",
        "hook": c.get("hook", ""), "reason": c.get("reason", ""), "score": int(c.get("score", 0)),
        "hashtags": c.get("hashtags", []), "source": source, "query": query, "edit": edit, "rev": 1,
        "export": None, "posts": {}, "rating": 0, "parts": parts,
        "features": c.get("features"), "fit": c.get("fit"), "ai_score": c.get("ai_score"),
        "ai_bounds": (c.get("ai_bounds") or [c["start"], c["end"]]) if ai_pick else None,
    })


def _find_job(job):
    _find(job["project_id"], job.get("query", ""), job, count=job.get("count"), target=job.get("target"), max_len=job.get("max_len"))


def _analyze_failed(job, err):
    store.update("project", job["project_id"], {"status": "failed", "error": err, "stage": "Failed"})


# ---------- rendering ----------

def parts_signature(clip: dict) -> list:
    """For a montage: which part revisions its render was built from (stale if any part changed or was deleted)."""
    return [[pid, (store.get("clip", pid) or {}).get("rev")] for pid in clip.get("parts") or []]


def clip_export_current(clip: dict) -> bool:
    exp = clip.get("export") or {}
    if clip.get("parts") and exp.get("parts") != parts_signature(clip):
        return False
    return exp.get("status") == "done" and exp.get("rev") == clip["rev"] and (pdir(clip["project_id"]) / exp["file"]).exists()


def clip_keeps(clip: dict, e: dict, words: list[dict]) -> list | None:
    """The source spans a clip's render keeps: minus silences, filler words and the creator's text cuts (edit "cuts");
    None = the whole clip. Shared by the render and the NLE export (nle.py) so both cut the same."""
    keeps = None
    if e["remove_silence"]:
        keeps = media.keep_segments(load_json(clip["project_id"], "signals.json", {}).get("silences", []), clip["start"], clip["end"])
    if e.get("remove_fillers") and words:  # "um"/"uh" cut on top of (or instead of) silence jump cuts
        keeps = media.cut_spans(keeps or [(clip["start"], clip["end"])], media.filler_spans(words, clip["start"], clip["end"]))
    if e.get("cuts"):  # deleted transcript words; bleeps and captions follow through media.render / build_ass
        keeps = media.cut_spans(keeps or [(clip["start"], clip["end"])], [(float(a), float(b)) for a, b in e["cuts"]])
    if keeps and len(keeps) == 1 and keeps[0] == (clip["start"], clip["end"]):
        keeps = None
    return keeps


def _render_clip(clip: dict, prog) -> str:
    from . import multicam

    pid = clip["project_id"]
    proj = store.get("project", pid)
    src = source_file(proj)  # removed to save space / drive away: a clear message, not an ffmpeg error
    s = config.load()
    e = assets.resolve_fonts({**media.DEFAULT_EDIT, **clip["edit"]})  # a missing font falls back instead of failing
    words = [w for seg in load_json(pid, "transcript.json", {"segments": []})["segments"] for w in seg.get("words", [])]
    cap_words = e["words"] or words  # the creator's caption edits; filler cuts still go by what was actually said
    keeps = clip_keeps(clip, e, words)
    size = media.output_size(e["aspect"], proj["meta"]["width"], proj["meta"]["height"])
    ass = None
    overlays = e["overlays"] or []
    texts = [o for o in overlays if o["kind"] in ("text", "emoji")]
    placed = []  # media overlays resolved to library files (a deleted asset just drops out of the render)
    for o in overlays:
        a = store.get("asset", o.get("asset_id") or "") if o["kind"] not in ("text", "emoji") else None
        if a and assets.path(a).exists():
            placed.append({**o, "path": str(assets.path(a))})
    headline = media.headline_spec(e, (proj.get("video_type") or {}).get("type"),
                                   (store.get("pref", "headline_theme") or {}).get("theme")) if e.get("headline") else None
    bad = media.bleep_list(s["bleep_words"]) if e["bleep"] in ("beep", "mute") else []
    bleeps = media.bleep_spans(words, clip["start"], clip["end"], bad) if bad else None  # what was said, like filler cuts
    if bad:
        cap_words = media.censor(cap_words, bad)
    card = card_of(e)
    if (e["captions"] and cap_words) or e["title_text"] or e["sticker_text"] or texts or headline or card:
        ass = media.build_ass(cap_words, clip["start"], clip["end"], float(e["speed"]), size, e["title_text"], s["caption_uppercase"],
                              s["caption_color"], bool(e["captions"]), e["caption_style"], e["caption_pos"], e["sticker_text"],
                              e["sticker_pos"], keeps, media.seam(e),
                              overlays=texts, text_style=e, headline=headline, anim=e["caption_anim"],
                              emphasis=bool(e["caption_emphasis"]), card=card)
    music = (pdir(pid) / e["music"]).resolve() if e["music"] else None
    if music and (not music.is_file() or (pdir(pid) / "music").resolve() not in music.parents):
        music = None
    (pdir(pid) / "exports").mkdir(exist_ok=True)
    rel = f"exports/{clip['id']}.mp4"
    media.render(src, pdir(pid) / rel, clip["start"], clip["end"], e, proj["meta"], ass, prog, keeps, music,
                 placed, assets.uploaded_font_files(), headline=headline,
                 angles=multicam.render_angles(pid, proj) if media.used_angles(e) else None, bleeps=bleeps, card=card)
    return rel


def card_of(e: dict) -> dict | None:
    """The edit's streamer card for rendering, its avatar resolved to the library file (a deleted one: the lettered circle)."""
    a = store.get("asset", str((e.get("streamer_card") or {}).get("avatar_asset_id") or "")) if e.get("streamer_card") else None
    return media.card_spec(e, str(assets.path(a)) if a and a.get("kind") == "image" and assets.path(a).exists() else None)


def _export(job):
    clip = store.get("clip", job["clip_id"])
    if not clip:
        raise RuntimeError("Clip was deleted.")
    rev = clip["rev"]
    store.update("job", job["id"], {"rev": rev})
    store.update("clip", clip["id"], {"export": {"status": "rendering", "rev": rev, "progress": 0}})

    def prog(f):
        _progress(job["id"], f, "Rendering")
        store.update("clip", clip["id"], fn=lambda c: (c.get("export") or {}).update(progress=round(f, 3)))

    if clip.get("parts"):  # montage: render each part (if stale), then join
        files = []
        for i, part_id in enumerate(clip["parts"]):
            part = store.get("clip", part_id)
            if not part:
                continue
            if not clip_export_current(part):
                rel_p = _render_clip(part, lambda f, i=i: prog((i + f) / (len(clip["parts"]) + 1)))
                store.update("clip", part_id, {"export": {"status": "done", "rev": part["rev"], "file": rel_p, "at": now_iso(), "progress": 1}})
                part = store.get("clip", part_id)
            files.append(pdir(part["project_id"]) / part["export"]["file"])
        if not files:
            raise RuntimeError("All clips in this montage were deleted.")
        rel = f"exports/{clip['id']}.mp4"
        first = store.get("project", clip["project_id"])["meta"]
        size = media.output_size(clip["edit"]["aspect"], first["width"], first["height"])
        media.concat(files, pdir(clip["project_id"]) / rel, size, lambda f: prog((len(clip["parts"]) + f) / (len(clip["parts"]) + 1)))
    else:
        rel = _render_clip(clip, prog)
    cover, cover_t = f"exports/{clip['id']}.jpg", None
    try:  # most clickable frame of the finished render: YouTube thumbnail, TikTok cover, editor preview
        cover_t = covers.make(pdir(clip["project_id"]) / rel, pdir(clip["project_id"]) / cover)
    except Exception:
        log.error("cover", f"clip {clip['id']}")
    issues = None
    try:  # the pre-publish check on the finished file (black/frozen frames, loudness, clipping, limits, safe zones)
        from . import qa
        issues = qa.lint(clip, pdir(clip["project_id"]) / rel)
    except Exception:
        log.error("qa", f"clip {clip['id']}")
    store.update("clip", clip["id"], {"export": {"status": "done", "rev": rev, "file": rel, "at": now_iso(), "progress": 1,
                                                 "parts": parts_signature(clip) if clip.get("parts") else None,
                                                 "cover": cover if cover_t is not None else None, "cover_t": cover_t, "qa": issues}})
    if store.get("clip", clip["id"])["rev"] != rev:  # edited while rendering: render the newer version too
        request_export(clip["id"])
    _release_uploads(clip["id"])


def _export_failed(job, err):
    store.update("clip", job["clip_id"], {"export": {"status": "failed", "error": err}})
    for u in store.find("upload", clip_id=job["clip_id"], status="waiting_render"):
        store.update("upload", u["id"], {"status": "failed", "error": f"Render failed: {err}"})


def _track(job):
    clip = store.get("clip", job["clip_id"])
    if not clip:
        raise RuntimeError("Clip was deleted.")
    pid, e = clip["project_id"], {**media.DEFAULT_EDIT, **clip["edit"]}
    proj = store.get("project", pid)
    meta = proj["meta"]
    W, H = media.output_size(e["aspect"], meta["width"], meta["height"])
    cuts = [t - clip["start"] for t in load_json(pid, "signals.json", {}).get("scenes", []) if clip["start"] < t < clip["end"]]
    mode, target = job.get("mode") or "auto", job.get("target")
    res = track.track(source_file(proj), clip["start"], clip["end"], meta, cuts, W / H, float(e["zoom"]),
                      lambda f: _progress(job["id"], f, "Tracking subject"), mode, target)

    def apply(c):  # keep the creator's manual keyframes; points made for older trims are simply replaced
        manual = (c["edit"].get("track") or {}).get("manual") or []
        c["edit"] = {**c["edit"], "follow": True, "track": {**res, "mode": mode, "target": target, "manual": manual}}
        c["rev"] += 1
        c["track_error"] = ""

    store.update("clip", clip["id"], fn=apply)


def _track_failed(job, err):
    store.update("clip", job["clip_id"], {"track_error": err})


def _proxy(job):
    """A light preview copy of the source for the Editor/Studio players (media.proxy); project["proxy"] names it."""
    pid = job["project_id"]
    src = source_file(store.get("project", pid))
    media.proxy(src, pdir(pid) / "proxy.mp4", lambda f: _progress(job["id"], f, "Making a preview copy"))
    store.update("project", pid, {"proxy": "proxy.mp4"})


EXPIRED = "This expired (the space saver removed its render): press Keep on it to restore it first."


def request_export(clip_id: str) -> dict:
    clip = store.get("clip", clip_id) or {}
    if clip.get("expired"):
        raise RuntimeError(EXPIRED)
    if clip and (proj := store.get("project", clip["project_id"])) and proj.get("source_removed"):
        source_file(proj)  # refuse up front (400 with the fix) rather than queue a render that can only fail
    rev = clip.get("rev")
    running = [j for j in store.find("job", clip_id=clip_id, kind="export")
               if j["status"] == "queued" or (j["status"] == "running" and j.get("rev") == rev)]
    return running[0] if running else submit("export", clip_id=clip_id)


def make_montage(pid: str, clip_ids: list[str], title: str) -> dict:
    parts = [store.get("clip", c) for c in clip_ids]
    parts = [c for c in parts if c]
    if len(parts) < 2:
        raise RuntimeError("Pick at least two clips for a montage.")
    total = sum((c["end"] - c["start"]) / float(c["edit"].get("speed", 1)) for c in parts)
    m = new_clip(pid, {"start": 0, "end": round(total, 2), "title": title or "Highlights montage", "score": 0,
                       "reason": f"Montage of {len(parts)} clips"}, source="montage", parts=[c["id"] for c in parts])
    store.update("clip", m["id"], fn=lambda c: c["edit"].update(aspect=parts[0]["edit"]["aspect"]))
    return store.get("clip", m["id"])


# ---------- publishing & scheduling ----------

def request_upload(clip_id: str | None, spec: dict, longform_id: str | None = None) -> dict:
    """Queue a post to one platform. Works before the render exists: it waits for it, then uploads (or waits for upload_at).
    With longform_id the post publishes that long-form edit instead of a clip (clip_id None)."""
    clip = store.get("longform", longform_id) if longform_id else store.get("clip", clip_id or "")
    if not clip:
        raise RuntimeError("That clip or long-form edit no longer exists.")
    if clip.get("expired"):
        raise RuntimeError(EXPIRED)
    platform = spec.get("platform", "youtube")
    if platform not in platforms.PLATFORMS:
        raise RuntimeError(f"Unknown platform {platform}")
    if not _current(clip) and (proj := store.get("project", clip["project_id"])) and proj.get("source_removed"):
        source_file(proj)  # it would need a render the source is gone for: refuse before creating the post
    acc = _pick_account(platform, spec.get("account_id"), bool(spec.get("auto")))
    publish_at, upload_at = spec.get("publish_at") or None, spec.get("upload_at") or None
    if platform != "youtube" and publish_at:  # TikTok/Instagram have no API scheduling: hold locally, post at that time
        upload_at, publish_at = publish_at, None
    tags = spec.get("tags", [])
    caption = spec.get("caption") or f"{spec.get('title') or clip['title']} " + " ".join(f"#{t.replace(' ', '')}" for t in tags[:5])
    description = spec.get("description", "")
    tv = platforms.template_vars(clip, store.get("project", clip["project_id"]), tags)  # incl. the source link at the clip start
    if spec.get("template", True):  # the account's description template, else the default one (Settings)
        tpl = acc["description_template"] if acc.get("description_template") is not None else config.load()["description_template"]
        if platform == "youtube":
            description = platforms.render_template(tpl, {**tv, "description": description})
        else:  # TikTok / Instagram: the caption is the post's text
            caption = platforms.render_template(tpl, {**tv, "description": caption.strip()})
    ai_content = bool(spec["ai_content"]) if "ai_content" in spec else (
        config.load()["ai_disclosure"] == "always" or bool(clip.get("ai_media")))  # ai_media: a generator put AI media in it
    up = store.put("upload", {
        "platform": platform, "account_id": acc["id"], "clip_id": clip_id, "project_id": clip["project_id"], "title": spec.get("title") or clip["title"],
        "description": description, "caption": caption.strip(), "tags": tags, "ai_content": ai_content,
        "source_link": tv["source_url"] or None,
        "category_id": spec.get("category_id", "22"), "privacy": spec.get("privacy") or config.load()["default_privacy"],
        "publish_at": publish_at, "upload_at": upload_at, "made_for_kids": bool(spec.get("made_for_kids")),
        "status": "waiting_render", "progress": 0.0, "error": "", "video_id": None, "url": None,
        "auto": bool(spec.get("auto")), "tiktok": _tiktok_opts(spec.get("tiktok")) if platform == "tiktok" else None,
        **({"clip_id": None, "longform_id": longform_id} if longform_id else {}),
    })
    _release_or_render(clip)
    return store.get("upload", up["id"])


# A post publishes a clip, or a long-form edit (upload["longform_id"], clip_id None; see longform.py).
def _target(u: dict) -> dict | None:
    return store.get("longform", u["longform_id"]) if u.get("longform_id") else store.get("clip", u.get("clip_id") or "")


def _is_lf(item: dict) -> bool:
    return "segments" in item


def _current(item: dict) -> bool:
    from . import longform  # imports this module
    return longform.export_current(item) if _is_lf(item) else clip_export_current(item)


def _release_or_render(item: dict):
    from . import longform
    if not _current(item):
        longform.request_export(item["id"]) if _is_lf(item) else request_export(item["id"])
    else:
        _release_uploads(item["id"], "longform_id" if _is_lf(item) else "clip_id")


def _pick_account(platform: str, aid: str | None, auto: bool = False) -> dict:
    """The account a new post goes to: the one named, which must be approved; unnamed only when exactly one account of
    the platform is usable (never a guess between several)."""
    if aid:
        acc = accounts.get(aid)
        if not acc or acc["platform"] != platform:
            raise RuntimeError(f"That {platform.title()} account is no longer linked.")
        if not acc.get("approved"):
            raise RuntimeError(f"{acc.get('name') or 'That account'} isn't approved for uploads yet: turn on "
                               "“Approved for uploads” for it on the Publish page.")
        return acc
    ok = accounts.usable(platform, auto)
    if len(ok) != 1:
        raise RuntimeError(f"Choose which {platform.title()} account to post to." if ok else
                           f"No {platform.title()} account is approved for uploads yet: link one and approve it on the Publish page.")
    return ok[0]


def _account_of(u: dict) -> dict | None:
    """The account a post goes to. Posts from before accounts existed (no account_id key at all) belong to the
    migrated account; a post whose account_id is None or was removed has none: it is never sent elsewhere."""
    return accounts.get(u["account_id"]) if "account_id" in u else accounts.legacy(u.get("platform", "youtube"))


def _refusal(u: dict, acc: dict | None) -> str:
    """Why this post must not go to its account (empty = it may)."""
    platform = u.get("platform", "youtube")
    if "account_id" in u and not u["account_id"]:
        return "No account was chosen for this post: move it to an approved account on the Schedule page."
    if not acc or acc["platform"] != platform:
        return (f"The {platform.title()} account for this post was removed, so it wasn't posted. Move it to another account "
                "on the Schedule page, or delete it.")
    name = acc.get("name") or f"This {platform.title()} account"
    if not acc.get("approved"):
        return f"{name} isn't approved for uploads: turn on “Approved for uploads” on the Publish page, then retry."
    if u.get("auto") and not acc.get("autopilot"):
        return f"Autopilot isn't allowed to post to {name}: turn on “Allow Autopilot to post here” on the Publish page, or move the post."
    return ""


def remove_account(aid: str) -> int:
    """Unlink an account. Its posts that haven't gone out are stopped (marked failed with the reason), never moved to
    another account; returns how many. One uploading right now can't be stopped and finishes."""
    acc = accounts.get(aid)
    if not acc:
        return 0
    held = 0
    for u in store.find("upload"):
        if u.get("status") in ("done", "canceled", "uploading") or (_account_of(u) or {}).get("id") != aid:
            continue
        store.update("upload", u["id"], {"status": "failed", "account_id": aid,
                                         "error": f"The account {acc.get('name') or ''} was removed, so this wasn't posted. Move it to "
                                                  "another account on the Schedule page, or delete it."})
        held += 1
    accounts.remove(aid)
    log.event("accounts", f"removed {acc['platform']} account {aid}; {held} post(s) held")
    return held


def _release_uploads(clip_id: str, key: str = "clip_id"):
    for u in store.find("upload", **{key: clip_id}, status="waiting_render"):
        _queue_or_schedule(u)


def _due(u: dict) -> bool:
    return not u.get("upload_at") or datetime.fromisoformat(u["upload_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)


def paused() -> bool:
    return bool(config.load()["autopilot_paused"])


def _tiktok_opts(o) -> dict | None:
    """The TikTok choices the user made in the publish dialog (TikTok requires an explicit pick, no defaults)."""
    if not isinstance(o, dict):
        return None
    levels = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")
    return {"privacy_level": o.get("privacy_level") if o.get("privacy_level") in levels else "SELF_ONLY",
            **{k: bool(o.get(k)) for k in ("allow_comment", "allow_duet", "allow_stitch", "brand_organic", "brand_content")}}


def _queue_or_schedule(u: dict):
    if u.get("auto") and paused():  # Autopilot is paused: keep it, post nothing, release on resume
        store.update("upload", u["id"], {"status": "held"})
        return
    if _due(u):
        store.update("upload", u["id"], {"status": "queued", "error": ""})
        submit("upload", upload_id=u["id"], clip_id=u["clip_id"], platform=u.get("platform", "youtube"))
    else:
        store.update("upload", u["id"], {"status": "scheduled"})


def _upload(job):
    u = store.get("upload", job["upload_id"])
    if not u or u["status"] not in ("queued", "scheduled", "waiting_render", "held"):
        return  # canceled, already uploading, or already posted: a duplicate job must never post twice
    if (u.get("auto") and paused()) or not _due(u):  # paused, or moved later after this job was queued
        _queue_or_schedule(u)
        return
    platform = u.get("platform", "youtube")
    clip = _target(u)  # a clip, or a long-form edit
    kind = "longform" if u.get("longform_id") else "clip"
    if not clip:  # deleted while this post waited: drop it, never post
        store.update("upload", u["id"], {"status": "canceled", "error": f"The {'long-form edit' if kind == 'longform' else 'clip'} for this post was deleted."})
        return
    if not _current(clip):  # edited after queueing → re-render first
        store.update("upload", u["id"], {"status": "waiting_render"})
        _release_or_render(clip)
        return
    acc = _account_of(u)
    refuse = _refusal(u, acc)  # the "okay" is enforced here, not only in the UI
    if refuse:
        store.update("upload", u["id"], {"status": "failed", "error": refuse})
        return
    if not platforms.account_connected(acc):
        store.update("upload", u["id"], {"status": "needs_auth", "error": f"Reconnect {acc.get('name') or platform.title()} on the Publish page to continue."})
        return
    with store._lock:  # claim it in one step: delete_clip (same lock) can't remove the clip between this check and the post
        if (store.get("upload", u["id"]) or {}).get("status") != u["status"] or not store.get(kind, clip["id"]):
            return  # canceled or deleted meanwhile
        # crash-safe: an earlier attempt's session (resume) is continued or checked, never posted again. A resume from
        # another account or platform (the post was moved) is dropped: its session belonged there and never finished.
        prev = u.get("resume") or {}
        if prev.get("account_id") != acc["id"] or prev.get("platform") != platform:
            prev = {}
        store.update("upload", u["id"], {"status": "uploading", "progress": 0,
                                         "resume": {**prev, "platform": platform, "account_id": acc["id"], "started": prev.get("started") or time.time()}})
    exp = clip["export"]
    cover = pdir(clip["project_id"]) / exp["cover"] if exp.get("cover") else None
    try:
        res = platforms.publish(platform, pdir(clip["project_id"]) / exp["file"], {**u, "account_id": acc["id"], "cover_t": exp.get("cover_t"), "resume": prev},
                                lambda f: store.update("upload", u["id"], {"progress": round(f, 3)}),
                                cover if cover and cover.exists() else None,
                                on_resume=lambda patch: store.update("upload", u["id"], fn=lambda d: d.setdefault("resume", {}).update(patch)))
    except platforms.AuthError as e:
        store.update("upload", u["id"], {"status": "needs_auth", "error": str(e)})
        return
    if (store.get("upload", u["id"]) or {}).get("status") == "canceled":
        res["note"] = "Canceled after it was already posted"
    store.update("upload", u["id"], {"status": "done", "progress": 1, "uploaded_at": now_iso(), "resume": None, **res})
    posted = store.update(kind, clip["id"], fn=lambda c: c.setdefault("posts", {}).update({platform: res}))
    if kind == "clip":  # a long form is not a pick: it teaches the clip finder nothing
        remember(posted, "published")


def _upload_failed(job, err):
    store.update("upload", job["upload_id"], {"status": "failed", "error": err})


def retry_upload(upload_id: str):
    if (store.get("upload", upload_id) or {}).get("status") in ("done", "uploading"):
        return
    u = store.update("upload", upload_id, {"status": "waiting_render", "error": ""})
    item = _target(u) if u else None
    if item:
        _release_or_render(item)


def requeue_needs_auth():
    for u in store.find("upload", status="needs_auth"):
        retry_upload(u["id"])


def reschedule(upload_id: str, when: str | None = "keep", platform: str | None = None, account_id: str | None = None) -> dict:
    """Move a post: new time (None = as soon as possible) and/or another approved account (which sets the platform) or
    platform. A YouTube video that is already uploaded and waiting for its publish time is moved on YouTube itself."""
    u = store.get("upload", upload_id)
    if not u:
        raise RuntimeError("That post no longer exists.")
    if u["status"] == "uploading":
        raise RuntimeError("This post is uploading right now; wait for it to finish.")
    if when not in ("keep", None):
        datetime.fromisoformat(when.replace("Z", "+00:00"))  # validates
    if u["status"] == "done":
        future = u.get("publish_at") and not _due({"upload_at": u["publish_at"]})
        if u.get("platform") == "youtube" and future and u.get("video_id") and when not in ("keep", None) and not platform and not account_id:
            from . import youtube
            return store.update("upload", upload_id, {"publish_at": youtube.set_publish_at(u["video_id"], when, (_account_of(u) or {}).get("id"),
                                                                                             bool(u.get("ai_content")))})
        raise RuntimeError("Already posted: it can't be moved (delete it on the platform and post again instead).")
    acc = accounts.get(account_id) if account_id else None
    if account_id and not acc:
        raise RuntimeError("That account is no longer linked.")
    if acc and platform and platform != acc["platform"]:
        raise RuntimeError("That account is on another platform.")
    if acc:
        acc = _pick_account(acc["platform"], account_id)  # must be approved
    plat = acc["platform"] if acc else platform or u.get("platform", "youtube")
    if plat not in platforms.PLATFORMS:
        raise RuntimeError(f"Unknown platform {plat}")
    t = (u.get("publish_at") or u.get("upload_at")) if when == "keep" else when
    patch = {"platform": plat, "publish_at": t if plat == "youtube" else None, "upload_at": None if plat == "youtube" else t,
             "error": "", "auto": False}  # a post the user moves by hand is theirs now: the Autopilot pause no longer holds it
    if plat == "tiktok" and not u.get("tiktok"):  # never choose a public audience for the user (TikTok guideline)
        patch["tiktok"] = _tiktok_opts({"privacy_level": "SELF_ONLY"})
    if acc:
        patch["account_id"] = acc["id"]
    elif plat != u.get("platform", "youtube"):  # platform only: its one approved account, else none (refused until chosen)
        ok = accounts.usable(plat)
        patch["account_id"] = ok[0]["id"] if len(ok) == 1 else None

    def apply(doc):
        if doc["status"] in ("uploading", "done"):  # the worker got there first
            raise RuntimeError("This post started uploading just now; it can't be moved.")
        doc.update(patch)

    u = store.update("upload", upload_id, fn=apply)
    log.event("schedule", f"upload {upload_id} -> {plat} at {t or 'asap'}")
    clip = _target(u)
    if u["status"] == "queued":
        return u  # its job is already waiting; the job re-checks the new time before posting
    if u["status"] in ("scheduled", "held", "failed", "needs_auth"):
        if clip and _current(clip):
            _queue_or_schedule(u)
        elif clip:
            store.update("upload", upload_id, {"status": "waiting_render"})
            _release_or_render(clip)
    return store.get("upload", upload_id)


def stagger(first_iso: str, interval_hours: float, n: int) -> list[str]:
    t = datetime.fromisoformat(first_iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return [(t + timedelta(hours=interval_hours * i)).astimezone(timezone.utc).isoformat() for i in range(n)]


def next_slot(first_iso: str | None, margin_min: int = 20) -> str:
    """Autopilot start time that is in the future: past times roll forward by whole days, keeping the time of day."""
    soon = datetime.now(timezone.utc) + timedelta(minutes=margin_min)
    if not first_iso:
        return soon.isoformat()
    t = datetime.fromisoformat(first_iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if t < soon:
        t += timedelta(days=(soon - t).days + 1)
    return t.isoformat()


def _autopilot(pid: str):
    """After a scan: each chosen account's top-N clips (ranked by that account's learned taste; experiments, when on,
    choose among the options the creator allows), with metadata, queued on a schedule."""
    ap = (store.get("project", pid) or {}).get("autopilot") or {}
    if not ap.get("enabled"):
        return
    if paused():
        log.event("autopilot", f"paused: nothing scheduled for project {pid}")
        return
    # only accounts the user approved AND allowed for Autopilot; the form names accounts, older saved forms name platforms
    ok = accounts.usable(auto=True)
    targets = ([a for a in ok if a["id"] in ap["accounts"]] if "accounts" in ap
               else [a for a in ok if a["platform"] in (ap.get("platforms") or ["youtube"])])
    if not targets:
        log.event("autopilot", f"no account is approved for Autopilot: nothing scheduled for project {pid}")
        return
    smart = ap.get("timing") == "best"  # best times per platform; "fixed" honours the user's own start + interval
    n = int(ap.get("top_n", 3))
    pool = [c for c in store.find("clip", project_id=pid) if not c.get("parts")]
    times = stagger(next_slot(ap.get("first_publish")), float(ap.get("interval_hours", 24)), n)
    segs = load_json(pid, "transcript.json", {"segments": []})["segments"]
    mode = ap.get("mode", "publish_at")  # publish_at: upload now, YouTube publishes later | upload_at: post at that time
    s, fb, ups = config.load(), feedback(), store.find("upload")
    glob, opts, target = learn.train(fb), experiments.allowed(s.get("experiments"), slot=smart), lengths(s)[1]
    prepared: dict = {}  # clip id -> (clip, meta): restyled and titled once, however many accounts post it
    for acc in targets:  # each account its own top N, by its own taste: its lessons shrunk to the global model
        model = learn.model_for(fb, glob, account_id=acc["id"])
        ranked = sorted(learn.rerank([{**c, "score": c["score"] if c.get("ai_score") is None else c["ai_score"]} for c in pool], model),
                        key=lambda c: -c["score"])
        picks = experiments.plan(ranked, n, opts, experiments.stats(ups, acc["id"]), experiments.recent_explore(ups, acc["id"]), target)
        for (pick, arm), when in zip(picks, times):
            if pick["id"] not in prepared:
                prepared[pick["id"]] = _autopilot_prepare(store.get("clip", pick["id"]), ap, segs)
            clip, meta = prepared[pick["id"]]
            platform, at = acc["platform"], when
            if smart:
                at = experiments.pick_time(schedule.suggest(platform, 6 if arm.get("slot") else 1, category=meta.get("category_id"))
                                           or [when], arm.get("slot"))
                if arm.get("slot"):
                    arm = {**arm, "slot": experiments.slot_of(at)}  # the slot it actually got
            up = request_upload(clip["id"], {**meta, "platform": platform, "account_id": acc["id"], "caption": meta.get("social_caption"),
                                             "privacy": ap.get("privacy", "public"), "auto": True,
                                             "publish_at": at if mode == "publish_at" else None,
                                             "upload_at": at if mode == "upload_at" else None})
            if arm:  # an experiment: what was posted, so its result (lift) is credited to it
                store.update("upload", up["id"], {"arm": arm})


def _autopilot_prepare(clip: dict, ap: dict, segs: list[dict]) -> tuple[dict, dict]:
    """Autopilot's look (aspect, captions, jump cuts) on the clip, and its title/description/tags."""
    edit = {**clip["edit"], "aspect": ap.get("aspect", clip["edit"]["aspect"]), "captions": ap.get("captions", True),
            "remove_silence": ap.get("jump_cuts", clip["edit"].get("remove_silence", False))}
    clip = store.update("clip", clip["id"], {"edit": edit, "rev": clip["rev"] + 1})
    meta = {"title": clip["title"], "description": clip.get("hook", ""), "tags": [h.lstrip("#") for h in clip.get("hashtags", [])],
            "category_id": "22"}
    if ai.ready("meta") and segs:
        try:
            meta = ai.youtube_meta(clip, segs, edit["aspect"] in ("9:16", "4:5"), guidance())
        except RuntimeError:
            pass
    return clip, meta


# ---------- streaming-channel sources (Twitch, Kick, YouTube, any yt-dlp URL) ----------

SOURCE_URLS = {
    "twitch": "https://www.twitch.tv/{h}/videos?filter=archives&sort=time",
    "youtube": "https://www.youtube.com/@{h}/streams",
    "youtube_videos": "https://www.youtube.com/@{h}/videos",
}


def source_url(src: dict) -> str:
    if src["platform"] == "url":
        return src["handle"]
    return SOURCE_URLS[src["platform"]].format(h=src["handle"].lstrip("@").strip())


def list_vods(src: dict, n: int = 15) -> list[dict]:
    """Recent VODs on a connected channel (metadata only)."""
    if src["platform"] == "kick":
        r = httpx.get(f"https://kick.com/api/v2/channels/{src['handle'].strip().lower()}/videos", timeout=20,
                      headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        if r.status_code >= 400:
            raise RuntimeError(f"Kick returned {r.status_code} — Kick sometimes blocks automated requests; paste VOD links instead.")
        out = []
        for v in r.json()[:n]:
            vid = v.get("video", {}) or {}
            out.append({"id": str(vid.get("uuid") or v.get("id")), "title": v.get("session_title") or "Kick VOD",
                        "url": f"https://kick.com/{src['handle']}/videos/{vid.get('uuid')}", "duration": (v.get("duration") or 0) / 1000,
                        "views": vid.get("views") or v.get("viewer_count"), "date": v.get("start_time") or v.get("created_at"),
                        "thumb": (v.get("thumbnail") or {}).get("src")})
        return out
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": n, "skip_download": True}) as y:
        info = y.extract_info(source_url(src), download=False)
    out = []
    for e in (info.get("entries") or [])[:n]:
        if not e:
            continue
        thumbs = e.get("thumbnails") or []
        out.append({"id": str(e.get("id")), "title": e.get("title"), "url": e.get("url") or e.get("webpage_url"),
                    "duration": e.get("duration"), "views": e.get("view_count"), "date": e.get("timestamp") or e.get("upload_date"),
                    "thumb": thumbs[-1]["url"] if thumbs else e.get("thumbnail")})
    return out


def import_vod(src: dict, vod: dict) -> dict | None:
    if vod["id"] in set(src.get("imported", [])):
        return None
    p = new_project(vod.get("title") or vod["url"], src.get("autopilot") if src.get("autopilot", {}).get("enabled") else None,
                    origin={"source_id": src["id"], "platform": src["platform"], "vod_id": vod["id"], "url": vod["url"]})
    submit("import_url", project_id=p["id"], url=vod["url"])
    store.update("source", src["id"], fn=lambda s: s.setdefault("imported", []).append(vod["id"]))
    return p


def _scan_source(job):
    src = store.get("source", job["source_id"])
    if not src:
        return
    vods = list_vods(src)
    store.update("source", src["id"], {"vods": vods, "last_scan": now_iso(), "error": ""})
    new = [v for v in vods if v["id"] not in set(src.get("imported", [])) and v["id"] not in set(src.get("baseline", []))]
    if job.get("auto") and src.get("auto_import"):
        if not src.get("baseline_set"):  # first scan only records what exists; afterwards new VODs are imported
            store.update("source", src["id"], {"baseline": [v["id"] for v in vods], "baseline_set": True})
            store.update("job", job["id"], {"message": f"Watching {len(vods)} existing VODs; new ones will import automatically"})
            return
        for v in new[: int(src.get("max_per_scan", 2))]:
            import_vod(store.get("source", src["id"]), v)
        store.update("job", job["id"], {"message": f"Imported {min(len(new), int(src.get('max_per_scan', 2)))} new VODs"})
    else:
        store.update("job", job["id"], {"message": f"Found {len(vods)} VODs"})


def _scan_failed(job, err):
    if job.get("source_id"):
        store.update("source", job["source_id"], {"error": err, "last_scan": now_iso()})


# ---------- virality & training ----------

def _virality(job):
    if job.get("clip_id"):
        clip = store.get("clip", job["clip_id"])
        pid, start, end, title = clip["project_id"], clip["start"], clip["end"], clip["title"]
    else:
        pid = job["project_id"]
        proj = store.get("project", pid)
        start, end, title = 0.0, proj["meta"]["duration"], proj["name"]
    segs = load_json(pid, "transcript.json", {"segments": []})["segments"]
    _progress(job["id"], 0.2, "Researching similar videos")
    report = ai.virality(segs, load_json(pid, "signals.json", {}), start, end, title, guidance(), job.get("query", ""))
    report.update(project_id=pid, clip_id=job.get("clip_id"), title=title, at=now_iso())
    r = store.put("report", report)
    if job.get("clip_id"):
        store.update("clip", job["clip_id"], {"viral": {"score": report["score"], "report_id": r["id"]}})
    else:
        store.update("project", pid, {"viral": {"score": report["score"], "report_id": r["id"]}})
    store.update("job", job["id"], {"message": f"Virality score {report['score']}", "report_id": r["id"]})


def _performance(notes: dict | None = None) -> list[dict]:
    """Latest counts of every post this app published (YouTube, plus TikTok and Instagram where the sign-in allows),
    each account asked with its own sign-in. An account that can't be read is skipped, with why in notes[account id]."""
    by_acc: dict[str, list[dict]] = {}
    for u in store.find("upload", status="done"):
        acc = _account_of(u)
        if u.get("video_id") and acc and acc["platform"] == u.get("platform", "youtube"):
            by_acc.setdefault(acc["id"], []).append(u)

    def predicted(cid):  # the score the finder gave, from the live clip or, if it was deleted, the feedback memory
        clip = store.get("clip", cid) if cid else None
        if clip:
            return None if clip.get("parts") else clip["score"]  # a montage's 0 is not a prediction
        return (store.get("feedback", cid) or {}).get("ai_score") if cid else None

    out = []
    for aid, ups in by_acc.items():
        acc = accounts.get(aid)
        if not platforms.account_connected(acc):
            continue  # signed out: its results wait until it is reconnected
        try:
            stats = metrics.fetch(acc, [str(u["video_id"]) for u in ups])
        except Exception as e:  # one account's missing permission or outage must not stop the others
            if not isinstance(e, metrics.MissingScope):
                log.error("results", f"{acc['platform']} account {aid}")
            if notes is not None:
                notes[aid] = str(e)[:400]
            continue
        for u in ups:
            st = stats.get(str(u["video_id"]))
            if st:
                out.append({"title": u.get("title", ""), "views": int(st.get("views") or 0), "likes": int(st.get("likes") or 0),
                            "ai_score": predicted(u.get("clip_id")), "clip_id": u.get("clip_id"), "upload_id": u["id"],
                            "account_id": aid, "platform": acc["platform"], "stats": st})
    return sorted(out, key=lambda p: -p["views"])


def _retention(u: dict, notes: dict) -> None:
    """A YouTube post's audience-retention curve, once (a missing permission is retried after reconnecting)."""
    clip = store.get("clip", u.get("clip_id") or "")
    aid = (_account_of(u) or {}).get("id")
    if not clip or not aid:
        return
    length = (clip["end"] - clip["start"]) / float((clip.get("edit") or {}).get("speed") or 1)
    exp = clip.get("export") or {}
    try:  # the rendered file's own length: jump cuts and filler cuts make it shorter than the clip's span
        length = float(media.probe(pdir(clip["project_id"]) / exp["file"])["duration"]) if exp.get("file") else length
    except Exception:
        pass
    try:  # YouTube Analytics days are Pacific time: start a day early so the first hours aren't cut off
        r = metrics.youtube_retention(aid, u["video_id"], (metrics.posted_at(u) - timedelta(days=1)).date().isoformat(), length)
    except Exception as e:
        notes[aid] = f"Retention: {str(e)[:300]}" if not isinstance(e, metrics.MissingScope) else str(e)
        return
    store.update("upload", u["id"], {"retention": r})


def _results(job=None) -> list[dict]:
    """Pull every post's latest counts and keep its 48 h and 7 d snapshots on the post (upload["metrics"]), turn them into
    lift against the same account's posts of the same age, read YouTube retention curves once a post is a week old, and
    attach it all to each published clip's lesson: the reranker learns from results relative to each account's own
    normal, not from raw view counts. No Claude call: cheap enough to run daily."""
    notes: dict = {}
    perf = _performance(notes)
    now = datetime.now(timezone.utc)
    for p in perf:
        u = store.get("upload", p["upload_id"]) or {}
        label = metrics.due(u, now)
        if label:
            snap = {**p["stats"], "age_h": round(metrics.age_h(u, now), 1), "at": now.isoformat()}
            u = store.update("upload", u["id"], fn=lambda d, k=label, v=snap: d.setdefault("metrics", {}).update({k: v}))
        if p["platform"] == "youtube" and "7d" in (u.get("metrics") or {}) and not u.get("retention"):
            _retention(u, notes)
    done = [{**u, "account_id": (_account_of(u) or {}).get("id"), "platform": u.get("platform", "youtube")}
            for u in store.find("upload", status="done")]
    lifts = metrics.lifts(done)
    for u in done:
        if u["id"] in lifts and (u.get("metrics") or {}).get("lift") != lifts[u["id"]]:
            store.update("upload", u["id"], fn=lambda d, lf=lifts[u["id"]]: d.setdefault("metrics", {}).update(lift=lf))
    for p in perf:  # best first
        cid = p.get("clip_id")
        if not cid:
            continue
        if not store.get("feedback", cid) and store.get("clip", cid):
            remember(store.get("clip", cid), "published")
        f = store.get("feedback", cid)
        if not f or f.get("account_id") not in (None, p["account_id"]):
            continue  # ponytail: one lesson per clip, so a clip posted to several accounts teaches through the first one
        patch = {"views": p["views"], "likes": p["likes"], "results_at": now_iso(), "account_id": p["account_id"],
                 "platform": p["platform"]}
        if p["upload_id"] in lifts:
            patch["lift"] = lifts[p["upload_id"]]["value"]
        ret = store.get("upload", p["upload_id"]) or {}
        if (ret.get("retention") or {}).get("available"):
            patch["retention"] = {k: ret["retention"][k] for k in ("available", "hold_3s", "drop_s", "trim")}
        store.update("feedback", cid, patch)
    store.put("pref", {"id": "metrics_notes", "notes": notes, "at": now.isoformat()})
    config.save({"results_at": now_iso()})
    if job:
        store.update("job", job["id"], {"message": f"Updated results for {len(perf)} posts"})
    return perf


def _train(job):
    prof = store.get("profile", job["profile_id"])
    refs = []
    for i, url in enumerate(prof.get("references", [])):
        _progress(job["id"], 0.6 * i / max(len(prof["references"]), 1), f"Reading {url[:60]}")
        try:
            refs += ai.reference_metadata(url)
        except Exception as e:
            store.update("job", job["id"], {"message": f"Skipped {url}: {str(e)[:120]}"})
    _progress(job["id"], 0.65, "Checking how your published clips performed")
    try:
        perf = _results()
    except Exception:
        perf = []
    rated = [{"rating": f["signal"], "why": f["why"], "title": f["title"], "text": f["text"]} for f in feedback()]
    _progress(job["id"], 0.7, "Learning your style")
    trained = ai.train_profile(prof["name"], prof.get("notes", ""), refs, rated[:60], perf[:60], prof.get("answers"))
    prof = store.update("profile", prof["id"], {"trained": trained, "trained_at": now_iso(), "sample_counts":
                                                {"references": len(refs), "rated": len(rated), "published": len(perf)}})
    if config.api_key():  # live platform research is Claude's web search tool: Anthropic only
        _research_platforms(job, prof, lo=0.75)


def _research_platforms(job: dict, prof: dict, lo: float = 0.0):
    """Per-platform training: research the open web for each platform being published to and store a fresh playbook."""
    targets = prof.get("platforms") or list(platforms.PLATFORMS)
    ctx = creator_context(prof)
    errors = []
    for i, platform in enumerate(targets):
        _progress(job["id"], lo + (1 - lo) * i / len(targets), f"Researching the latest on {ai.PLATFORM_NAMES.get(platform, platform)}")
        try:
            pb = {**ai.platform_playbook(platform, ctx), "at": now_iso()}
            store.update("profile", prof["id"], fn=lambda p, pl=platform, pb=pb: p.setdefault("playbooks", {}).update({pl: pb}))
        except RuntimeError as e:
            errors.append(f"{platform}: {e}")
    store.update("profile", prof["id"], {"researched_at": now_iso(), "error": "; ".join(errors)})
    if errors and len(errors) == len(targets):
        raise RuntimeError("; ".join(errors))


def _research(job):
    _research_platforms(job, store.get("profile", job["profile_id"]))


def _train_failed(job, err):
    store.update("profile", job["profile_id"], {"error": err})


# ---------- scheduler ----------

def _scheduler():
    while True:
        try:
            _scheduler_tick()
        except Exception:
            log.error("scheduler", "tick failed")
        time.sleep(15)


_last_source_check = 0.0


def _scheduler_tick():
    global _last_source_check
    for u in store.find("upload", status="scheduled"):
        if _due(u):
            _queue_or_schedule(u)
    if not paused():  # resumed: everything Autopilot held goes back on its schedule
        for u in store.find("upload", status="held"):
            _queue_or_schedule(u)
    if time.time() - _last_source_check > 60:
        _last_source_check = time.time()
        for src in store.find("source", auto_import=True):
            last = src.get("last_scan")
            every = float(src.get("interval_hours", 2)) * 3600
            if not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() > every:
                if not [j for j in store.find("job", source_id=src["id"]) if j["status"] in ("queued", "running")]:
                    submit("scan_source", source_id=src["id"], auto=True)
        # keep the active profile's platform intel current: re-research weekly
        prof = next((p for p in store.find("profile") if p.get("active") and p.get("auto_refresh", True) and p.get("playbooks")), None)
        if prof and config.api_key():
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(prof.get("researched_at") or "2000-01-01T00:00:00+00:00")).days
            if age >= 7 and not [j for j in store.find("job", profile_id=prof["id"]) if j["status"] in ("queued", "running")]:
                submit("research", profile_id=prof["id"])
        _learning_tick()
        from . import storage
        storage.tick()  # the space saver: once a few minutes after startup, then daily


def _busy(kind: str, **match) -> bool:
    return any(j["status"] in ("queued", "running") for j in store.find("job", kind=kind, **match))


def _learning_tick():
    """Keep learning without being asked: refresh post results daily (and within the hour once a post is due its 48 h
    or 7 d snapshot), retrain once enough new lessons pile up."""
    now = datetime.now(timezone.utc)
    day_ago, last = (now - timedelta(days=1)).isoformat(), config.load().get("results_at") or ""
    want = last < day_ago or (last < (now - timedelta(hours=1)).isoformat()
                              and any(metrics.due_soon(u, now) for u in store.find("upload", status="done")))
    if want and not _busy("results") and any(platforms.connected(p) for p in platforms.PLATFORMS):
        submit("results")
    prof = next((p for p in store.find("profile") if p.get("active") and p.get("trained") and p.get("auto_refresh", True)), None)
    if prof and ai.ready("other") and (prof.get("trained_at") or "") < day_ago and not _busy("train", profile_id=prof["id"]):
        since = datetime.fromisoformat(prof["trained_at"]).timestamp()
        if sum(f["created"] > since for f in store.find("feedback")) >= 10:
            submit("train", profile_id=prof["id"], auto=True)


def _delete_project_docs(pid: str):
    for kind in ("clip", "longform", "upload", "report"):
        for d in store.find(kind, project_id=pid):
            store.delete(kind, d["id"])
    store.delete("project", pid)


def delete_project(pid: str):
    _delete_project_docs(pid)
    shutil.rmtree(pdir(pid), ignore_errors=True)


def _project_jobs(pid: str) -> list[dict]:
    """Queued or running jobs that work on a project: its own, or its clips' and long forms'."""
    ids = {("clip_id", c["id"]) for c in store.find("clip", project_id=pid)} |           {("longform_id", d["id"]) for d in store.find("longform", project_id=pid)} | {("project_id", pid)}
    return [j for j in store.find("job") if j["status"] in ("queued", "running") and any(j.get(k) == v for k, v in ids)]


# ---------- delete from the Pipeline board ----------
BUSY = "This is uploading right now: wait for it to finish or let it fail, then delete it."
NO_API_DELETE = {"tiktok": "TikTok", "instagram": "Instagram"}  # neither posting API can delete a post


def _cancel_pending(ups: list[dict]) -> int:
    pending = [u for u in ups if u["status"] not in ("done", "canceled")]
    for u in pending:
        store.update("upload", u["id"], {"status": "canceled", "error": "Deleted from the Pipeline."})
    return len(pending)


def _delete_remote(u: dict) -> dict:
    """Delete one posted video on its platform with the channel it was posted from; never raises."""
    r = {"upload_id": u["id"], "platform": u.get("platform", "youtube"), "ok": False, "manual": False, "url": u.get("url"), "error": None}
    if r["platform"] in NO_API_DELETE:
        name = NO_API_DELETE[r["platform"]]
        return {**r, "manual": True, "error": f"{name} doesn't let apps delete posts: remove it in the {name} app."}
    acc = _account_of(u)
    if not acc or not u.get("video_id"):  # never fall back to another channel's sign-in
        return {**r, "error": "The channel this was posted to isn't linked any more: delete the video on YouTube."}
    try:
        from . import youtube
        youtube.delete_video(u["video_id"], acc["id"])
    except Exception as e:
        return {**r, "error": str(e)[:300] or type(e).__name__}
    store.delete("upload", u["id"])  # the video is gone: so is its history (a kept post still feeds the results learning)
    return {**r, "ok": True}


def delete_clip(cid: str, remote: set[str] = frozenset()) -> dict:
    """Remove a clip from Reelwright: pending posts are canceled first, an upload in flight refuses the delete, the render
    and cover go, and only the posted videos whose upload id is in `remote` are deleted on their platform."""
    with store._lock:  # _upload claims a post under this lock, so none can start between this check and the delete
        clip = store.get("clip", cid)
        if not clip:
            return {"ok": False, "error": "That clip no longer exists."}
        ups = store.find("upload", clip_id=cid)
        if any(u["status"] == "uploading" for u in ups):
            return {"ok": False, "error": BUSY}
        canceled = _cancel_pending(ups)
        remember(clip, "dismissed")  # same lesson as DELETE /api/clips (only an untouched AI pick counts)
        store.delete("clip", cid)
    exp, folder = clip.get("export") or {}, pdir(clip["project_id"])
    for rel in {exp.get("file"), exp.get("cover")} - {None}:
        f = (folder / rel).resolve()
        if folder in f.parents:  # a render path can never reach outside its project folder
            f.unlink(missing_ok=True)
    return {"ok": True, "canceled": canceled, "remote": [_delete_remote(u) for u in ups if u["status"] == "done" and u["id"] in remote]}


def delete_item(kind: str, iid: str, remote: set[str] = frozenset()) -> dict:
    if kind == "clip":
        return delete_clip(iid, remote)
    if kind == "longform":
        from . import longform
        return longform.delete(iid, remote)
    try:
        pdir(iid)
    except (ValueError, TypeError):
        return {"ok": False, "error": "Not a project id."}
    from . import storage
    if why := storage.hold_reason():  # mid-move or drive away: deleting now would orphan or miss its folder
        return {"ok": False, "error": why}
    with store._lock:  # the docs go under the lock; the (possibly huge) folder after it, so the UI never stalls
        if not store.get("project", iid):
            return {"ok": False, "error": "That project no longer exists."}
        ups = store.find("upload", project_id=iid)
        if any(u["status"] == "uploading" for u in ups):
            return {"ok": False, "error": BUSY}
        jobs = _project_jobs(iid)
        if any(j["status"] == "running" for j in jobs):
            return {"ok": False, "error": "A job is running for this project right now (analysis, render or import): "
                                          "wait for it to finish, then delete it."}
        for j in jobs:  # queued ones would recreate the folder after the delete
            store.update("job", j["id"], {"status": "canceled", "message": "Canceled: the project was deleted."})
        canceled = _cancel_pending(ups)
        _delete_project_docs(iid)
    shutil.rmtree(pdir(iid), ignore_errors=True)
    return {"ok": True, "canceled": canceled, "remote": []}


# ---------- files arriving from OBS (this PC, a mapped network folder, or the LAN receiver) ----------

def ingest_file(path, name: str, origin: dict, autopilot: dict | None = None, markers: list | None = None) -> dict:
    """Queue a video that already sits on a disk this PC can read: copied into a new project, then analyzed.
    markers: moments marked while OBS recorded it ([{t, kind, label, strength, d?}], file seconds), saved as markers.json
    for the analysis to merge into signals["markers"]."""
    p = new_project(name, autopilot, origin)  # refuses while the storage drive is away, so the folder below is writable
    if markers:
        save_json(p["id"], "markers.json", {"markers": markers})
    log.event("obs", f"{origin.get('obs')} saved {path} ({origin.get('kind')}) -> project {p['id']}")
    store.update("project", p["id"], {"stage": "Waiting for OBS to finish the file"})
    submit("ingest_file", project_id=p["id"], path=str(path))  # job "stable": True (a finished file, e.g. a short) skips the wait
    return p


def _ingest_file(job):
    from pathlib import Path

    from .relay import wait_stable

    src, pid = Path(job["path"]), job["project_id"]
    if src.suffix.lower() not in media.VIDEO_EXT:
        raise RuntimeError(f"Not a video file: {src.name}")
    if not job.get("stable") and not wait_stable(src, timeout=600):
        raise RuntimeError(f"{src} never finished writing (or isn't readable from this PC).")
    store.update("project", pid, {"stage": "Copying the video in"})
    dst = pdir(pid) / f"source{src.suffix.lower()}"
    shutil.copy2(src, dst)  # copy, never move: the creator's recordings stay where OBS put them
    store.update("project", pid, {"source": dst.name, "status": "queued", "stage": "Queued for analysis"})
    submit("analyze", project_id=pid)


def _import_failed(job, err):
    if job.get("redownload"):  # only the source is still missing: the project, its clips and analysis are fine
        store.update("project", job["project_id"], {"status": "ready", "stage": "Ready", "progress": 1.0},
                     fn=lambda p: p.update(source_removed={**(p.get("source_removed") or {}), "error": f"Re-download failed: {err}"}))
        return
    _analyze_failed(job, err)


def _cleanup(job):
    from . import storage
    storage.cleanup(job)


HANDLERS = {"import_url": _import_url, "analyze": _analyze, "find": _find_job, "export": _export, "upload": _upload,
            "scan_source": _scan_source, "virality": _virality, "train": _train, "research": _research,
            "results": _results, "ingest_file": _ingest_file, "track": _track, "cleanup": _cleanup, "proxy": _proxy}
FAIL_HOOKS = {"import_url": _import_failed, "analyze": _analyze_failed, "export": _export_failed, "upload": _upload_failed,
              "scan_source": _scan_failed, "train": _train_failed, "research": _train_failed, "ingest_file": _analyze_failed,
              "track": _track_failed}


def _lf(name: str):
    """A long-form job handler from longform.py (imported when it runs: longform imports this module)."""
    def run(*args):
        from . import longform
        return getattr(longform, name)(*args)
    return run


def _mc(name: str):
    """A camera-angle job handler from multicam.py (imported when it runs: multicam imports this module)."""
    def run(*args):
        from . import multicam
        return getattr(multicam, name)(*args)
    return run


HANDLERS.update(longform_capture=_lf("capture_job"), longform_export=_lf("export_job"))
HANDLERS["angle"], FAIL_HOOKS["angle"] = _mc("angle_job"), _mc("angle_failed")
FAIL_HOOKS.update(longform_capture=_lf("capture_failed"), longform_export=_lf("export_failed"))
QUEUE_OF["longform_capture"] = _ai  # highlights ask Claude; condensed is quick either way


def _recover_uploads():
    """Posts that were uploading when the app stopped go back in the queue with their resume info (upload["resume"]):
    the next attempt continues that session or asks the platform what became of it, instead of posting again."""
    for u in store.find("upload", status="uploading"):
        store.update("upload", u["id"], {"status": "queued"})
        log.event("upload", f"upload {u['id']} was interrupted; it resumes ({', '.join(sorted(u.get('resume') or {})) or 'nothing saved'})")


def start():
    global _started
    if _started:
        return
    _started = True
    for j in store.find("job"):  # recover after a restart: anything mid-flight runs again
        if j["status"] in ("queued", "running"):
            store.update("job", j["id"], {"status": "queued"})
            _enqueue(j)
    _recover_uploads()
    for c in store.find("clip"):  # one-time backfill: ratings/posts made before the feedback memory existed
        if (c.get("rating") or c.get("posts")) and not store.get("feedback", c["id"]):
            remember(c, "rated" if c.get("rating") else "published")
    for target, args in ((_worker, (_heavy, _analysis_gate)), (_worker, (_render, _render_gate)), (_worker, (_ai,)),
                         (_worker, (_uploads,)), (_scheduler, ())):
        threading.Thread(target=target, args=args, daemon=True).start()
