"""Background jobs: import → analyze → find clips → render → publish, plus the scheduler, Autopilot,
streaming-channel sources (Twitch / Kick / YouTube / any yt-dlp URL), virality analysis and style-profile training."""
import json
import queue
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import ai, assets, config, covers, learn, log, media, platforms, schedule, store, track, vision
from .config import PROJECTS

_heavy = queue.Queue()    # GPU/CPU-bound work (analysis, renders), one at a time
_ai = queue.Queue()       # Claude + metadata lookups (search, virality, training, channel scans)
_uploads = queue.Queue()  # network uploads, one at a time
_enqueued: set[str] = set()
_started = False
QUEUE_OF = {"upload": _uploads, "find": _ai, "virality": _ai, "train": _ai, "research": _ai, "scan_source": _ai, "results": _ai}


def pdir(pid: str):
    p = (PROJECTS / pid).resolve()
    if not pid or p.parent != PROJECTS.resolve():  # ids come from store.new_id; anything else is a traversal attempt
        raise ValueError(f"bad project id: {pid!r}")
    return p


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
            "trim": [round(clip["start"] - ab[0], 2), round(clip["end"] - ab[1], 2)] if ab else None}


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
    keep = {k: old[k] for k in ("views", "likes") if old and k in old}  # real results survive a re-rating
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
                "features", "edit", "trim", "views", "likes", "results_at"}


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
        for k in ("length", "ai_score", "views", "likes"):  # numbers, or gone
            v = learn.num(doc.pop(k, None))
            if v is not None:
                doc[k] = int(v) if k in ("views", "likes") else v
        trim = doc.pop("trim", None)
        if isinstance(trim, list) and len(trim) == 2 and all(learn.num(x) is not None for x in trim):
            doc["trim"] = [float(x) for x in trim]
        edit = doc.pop("edit", None)
        if isinstance(edit, dict):
            doc["edit"] = learn.clean_edit(edit)
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


def _worker(q: queue.Queue):
    while True:
        jid = q.get()
        try:
            _run_job(jid)
        except Exception:  # a broken hook must never kill the queue
            log.error("queue", f"job {jid}")


def _run_job(jid: str):
    job = store.get("job", jid)
    if not job or job["status"] == "canceled":
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


# ---------- import & analysis ----------

def new_project(name: str, autopilot: dict | None = None, origin: dict | None = None) -> dict:
    p = store.put("project", {"name": name, "status": "importing", "stage": "Importing", "progress": 0.0, "source": None,
                              "meta": None, "autopilot": autopilot or {"enabled": False}, "error": "", "origin": origin})
    pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    return p


def _import_url(job):
    import yt_dlp

    pid = job["project_id"]
    store.update("project", pid, {"status": "importing", "stage": "Downloading"})

    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") == "downloading" and total:
            _progress(job["id"], (d.get("downloaded_bytes") or 0) / total, "Downloading", pid, 0, 1)

    opts = {"outtmpl": str(pdir(pid) / "source.%(ext)s"), "format": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "merge_output_format": "mp4", "noplaylist": True, "quiet": True, "no_warnings": True, "progress_hooks": [hook]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(job["url"], download=True)
    src = next(p for p in pdir(pid).iterdir() if p.stem == "source" and p.suffix in media.VIDEO_EXT)
    store.update("project", pid, {"source": src.name, "name": info.get("title") or store.get("project", pid)["name"]})
    if (info.get("subtitles") or {}).get("live_chat"):  # a past livestream with chat replay: chat bursts mark the moments
        _progress(job["id"], 1.0, "Downloading live-chat replay", pid)
        try:
            with yt_dlp.YoutubeDL({"outtmpl": str(pdir(pid) / "source.%(ext)s"), "skip_download": True, "writesubtitles": True,
                                   "subtitleslangs": ["live_chat"], "quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
                ydl.download([job["url"]])
        except Exception as e:  # optional signal: never fail the import over chat
            log.event("import", f"live chat skipped: {e}")
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


def _analyze(job):
    pid = job["project_id"]
    proj = store.update("project", pid, {"status": "analyzing", "error": ""})
    src = pdir(pid) / proj["source"]
    step = lambda frac, msg, lo, hi: _progress(job["id"], frac, msg, pid, lo, hi)  # noqa: E731

    step(0, "Reading video", 0, 0.02)
    meta = media.probe(src)
    store.update("project", pid, {"meta": meta})
    dur = meta["duration"]

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
    step(0, "Detecting scene cuts", 0.12, 0.25)
    signals["scenes"] = media.scenes(src)
    step(0, "Analyzing on-screen action", 0.25, 0.28)
    try:  # best-effort: clip finding works without it
        signals["visual"] = vision.analyze(src, meta, lambda f: step(f, None, 0.25, 0.28))
    except Exception as e:
        log.event("vision", f"on-screen analysis skipped: {e}")
    if meta["has_audio"]:
        step(0, "Detecting silences", 0.28, 0.3)
        signals["silences"] = media.silences(src)
    chat = next(pdir(pid).glob("*.live_chat.json"), None)
    if chat:
        signals["chat"] = chat_counts(chat, dur)
    save_json(pid, "signals.json", signals)

    transcript = {"segments": [], "language": None}
    if meta["has_audio"]:
        step(0, "Transcribing speech (Whisper)", 0.3, 0.3)
        transcript = ai.transcribe(src, dur, lambda f: step(f, None, 0.3, 0.8))
    save_json(pid, "transcript.json", transcript)
    store.update("project", pid, {"has_transcript": bool(transcript["segments"]), "language": transcript.get("language")})

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
    if proj.get("source"):  # lets the long-video judge look at frames (ai.rank_moments, setting "visual_judge")
        signals["video"] = str(pdir(pid) / proj["source"])
    dur = proj["meta"]["duration"]
    count = count or s["clip_count"]
    clip_min, clip_target, clip_max = lengths(s, target, max_len)
    # short source (an OBS replay-buffer save, a short upload): the whole thing is the clip — tighten it to the speech
    short = not query and dur <= clip_max * 1.25
    if short:
        count, clip_min, clip_max, clip_target = 1, max(3.0, min(clip_min, dur * 0.5)), dur, None
    # "find more" (no query): skip moments the creator already has as clips, asking for extras so the count still fills
    have = [] if query else [c for c in store.find("clip", project_id=pid) if not c.get("parts")]
    if short and have:
        return []
    want = count + len(have)
    fb = feedback()
    model = learn.train(fb)
    used_ai = bool(config.api_key()) and bool(segs)
    if used_ai:
        taste = "\n".join(x for x in (taste_examples(), learn.hints(fb, model)) if x)
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
        found = ai.heuristic_moments(segs, signals.get("loud", []), dur, want if not short else 1, clip_min, clip_max)
    elif (signals.get("visual") or {}).get("spikes"):  # no speech: cut around on-screen action, flashes, reactions
        found = visual_picks(signals["visual"]["spikes"], clip_max, dur)[:want]
    else:  # no speech: cut around scene changes
        marks = sorted({0.0, *signals.get("scenes", [])})
        found = [{"start": t, "end": min(t + clip_max, dur), "score": 40, "title": f"Scene at {int(t // 60)}:{int(t % 60):02d}",
                  "hook": "", "reason": "Scene change (no speech detected).", "hashtags": []} for t in marks[:want]]
    if short and not found:
        found = [{"start": 0.0, "end": dur, "score": 50, "title": proj["name"][:70], "hook": "", "reason": "Whole clip.", "hashtags": []}]
    # a pick is a duplicate when most of it is already an existing clip (a tiny clip inside a big pick doesn't count)
    found = [f for f in found if not any(min(f["end"], c["end"]) - max(f["start"], c["start"]) > 0.5 * (f["end"] - f["start"])
                                         for c in have)]
    for c in found:  # measurable traits, then the creator's personal reranker (works offline too)
        c["features"] = learn.features(c, segs, signals, dur)
    found = learn.rerank(found, model)[:count]
    for c in found:
        new_clip(pid, c, source="search" if query else ("ai" if used_ai else "auto"), query=query)
    if job:
        store.update("job", job["id"], {"message": f"Found {len(found)} clips"})
    return found


def new_clip(pid: str, c: dict, source: str = "manual", query: str = "", parts: list | None = None) -> dict:
    s = config.load()
    proj = store.get("project", pid)
    edit = {**media.DEFAULT_EDIT, "aspect": s["default_aspect"]}
    prof = active_profile()
    if prof:  # the trained profile's theme becomes the starting look
        t = prof["trained"]["theme"]
        edit.update(aspect=t["aspect"], layout=t["layout"], caption_style=t["caption_style"], caption_pos=t["caption_pos"])
    edit.update(learn.house_style(feedback()))  # what the creator actually keeps choosing beats the profile's guess
    m = proj.get("meta") or {}
    if m.get("height", 1) > m.get("width", 0) and edit["aspect"] != "source":
        edit.update(aspect="9:16", layout="single")  # already vertical
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


def _render_clip(clip: dict, prog) -> str:
    pid = clip["project_id"]
    proj = store.get("project", pid)
    s = config.load()
    e = assets.resolve_fonts({**media.DEFAULT_EDIT, **clip["edit"]})  # a missing font falls back instead of failing
    words = [w for seg in load_json(pid, "transcript.json", {"segments": []})["segments"] for w in seg.get("words", [])]
    cap_words = e["words"] or words  # the creator's caption edits; filler cuts still go by what was actually said
    keeps = None
    if e["remove_silence"]:
        keeps = media.keep_segments(load_json(pid, "signals.json", {}).get("silences", []), clip["start"], clip["end"])
    if e.get("remove_fillers") and words:  # "um"/"uh" cut on top of (or instead of) silence jump cuts
        keeps = media.cut_spans(keeps or [(clip["start"], clip["end"])], media.filler_spans(words, clip["start"], clip["end"]))
    if keeps and len(keeps) == 1 and keeps[0] == (clip["start"], clip["end"]):
        keeps = None
    size = media.output_size(e["aspect"], proj["meta"]["width"], proj["meta"]["height"])
    ass = None
    overlays = e["overlays"] or []
    texts = [o for o in overlays if o["kind"] in ("text", "emoji")]
    placed = []  # media overlays resolved to library files (a deleted asset just drops out of the render)
    for o in overlays:
        a = store.get("asset", o.get("asset_id") or "") if o["kind"] not in ("text", "emoji") else None
        if a and assets.path(a).exists():
            placed.append({**o, "path": str(assets.path(a))})
    if (e["captions"] and cap_words) or e["title_text"] or e["sticker_text"] or texts:
        ass = media.build_ass(cap_words, clip["start"], clip["end"], float(e["speed"]), size, e["title_text"], s["caption_uppercase"],
                              s["caption_color"], bool(e["captions"]), e["caption_style"], e["caption_pos"], e["sticker_text"],
                              e["sticker_pos"], keeps, float(e["split"]) if e["layout"] == "split" and e["aspect"] != "source" else None,
                              overlays=texts, text_style=e)
    music = (pdir(pid) / e["music"]).resolve() if e["music"] else None
    if music and (not music.is_file() or (pdir(pid) / "music").resolve() not in music.parents):
        music = None
    (pdir(pid) / "exports").mkdir(exist_ok=True)
    rel = f"exports/{clip['id']}.mp4"
    media.render(pdir(pid) / proj["source"], pdir(pid) / rel, clip["start"], clip["end"], e, proj["meta"], ass, prog, keeps, music,
                 placed, assets.uploaded_font_files())
    return rel


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
    store.update("clip", clip["id"], {"export": {"status": "done", "rev": rev, "file": rel, "at": now_iso(), "progress": 1,
                                                 "parts": parts_signature(clip) if clip.get("parts") else None,
                                                 "cover": cover if cover_t is not None else None, "cover_t": cover_t}})
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
    res = track.track(pdir(pid) / proj["source"], clip["start"], clip["end"], meta, cuts, W / H, float(e["zoom"]),
                      lambda f: _progress(job["id"], f, "Tracking subject"), mode, target)

    def apply(c):  # keep the creator's manual keyframes; points made for older trims are simply replaced
        manual = (c["edit"].get("track") or {}).get("manual") or []
        c["edit"] = {**c["edit"], "follow": True, "track": {**res, "mode": mode, "target": target, "manual": manual}}
        c["rev"] += 1
        c["track_error"] = ""

    store.update("clip", clip["id"], fn=apply)


def _track_failed(job, err):
    store.update("clip", job["clip_id"], {"track_error": err})


def request_export(clip_id: str) -> dict:
    rev = (store.get("clip", clip_id) or {}).get("rev")
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

def request_upload(clip_id: str, spec: dict) -> dict:
    """Queue a post to one platform. Works before the render exists: it waits for it, then uploads (or waits for upload_at)."""
    clip = store.get("clip", clip_id)
    platform = spec.get("platform", "youtube")
    if platform not in platforms.PLATFORMS:
        raise RuntimeError(f"Unknown platform {platform}")
    publish_at, upload_at = spec.get("publish_at") or None, spec.get("upload_at") or None
    if platform != "youtube" and publish_at:  # TikTok/Instagram have no API scheduling: hold locally, post at that time
        upload_at, publish_at = publish_at, None
    tags = spec.get("tags", [])
    caption = spec.get("caption") or f"{spec.get('title') or clip['title']} " + " ".join(f"#{t.replace(' ', '')}" for t in tags[:5])
    up = store.put("upload", {
        "platform": platform, "clip_id": clip_id, "project_id": clip["project_id"], "title": spec.get("title") or clip["title"],
        "description": spec.get("description", ""), "caption": caption.strip(), "tags": tags,
        "category_id": spec.get("category_id", "22"), "privacy": spec.get("privacy") or config.load()["default_privacy"],
        "publish_at": publish_at, "upload_at": upload_at, "made_for_kids": bool(spec.get("made_for_kids")),
        "status": "waiting_render", "progress": 0.0, "error": "", "video_id": None, "url": None,
        "auto": bool(spec.get("auto")), "tiktok": _tiktok_opts(spec.get("tiktok")) if platform == "tiktok" else None,
    })
    if clip_export_current(clip):
        _release_uploads(clip_id)
    else:
        request_export(clip_id)
    return store.get("upload", up["id"])


def _release_uploads(clip_id: str):
    for u in store.find("upload", clip_id=clip_id, status="waiting_render"):
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
    clip = store.get("clip", u["clip_id"])
    if not clip:
        raise RuntimeError("The clip for this post was deleted.")
    if not clip_export_current(clip):  # edited after queueing → re-render first
        store.update("upload", u["id"], {"status": "waiting_render"})
        request_export(clip["id"])
        return
    if not platforms.connected(platform):
        store.update("upload", u["id"], {"status": "needs_auth", "error": f"Connect {platform.title()} to continue."})
        return
    store.update("upload", u["id"], {"status": "uploading", "progress": 0})
    exp = clip["export"]
    cover = pdir(clip["project_id"]) / exp["cover"] if exp.get("cover") else None
    try:
        res = platforms.publish(platform, pdir(clip["project_id"]) / exp["file"], {**u, "cover_t": exp.get("cover_t")},
                                lambda f: store.update("upload", u["id"], {"progress": round(f, 3)}),
                                cover if cover and cover.exists() else None)
    except platforms.AuthError as e:
        store.update("upload", u["id"], {"status": "needs_auth", "error": str(e)})
        return
    if (store.get("upload", u["id"]) or {}).get("status") == "canceled":
        res["note"] = "Canceled after it was already posted"
    store.update("upload", u["id"], {"status": "done", "progress": 1, "uploaded_at": now_iso(), **res})
    remember(store.update("clip", clip["id"], fn=lambda c: c.setdefault("posts", {}).update({platform: res})), "published")


def _upload_failed(job, err):
    store.update("upload", job["upload_id"], {"status": "failed", "error": err})


def retry_upload(upload_id: str):
    if (store.get("upload", upload_id) or {}).get("status") in ("done", "uploading"):
        return
    u = store.update("upload", upload_id, {"status": "waiting_render", "error": ""})
    if u:
        clip = store.get("clip", u["clip_id"])
        if clip:
            _release_uploads(clip["id"]) if clip_export_current(clip) else request_export(clip["id"])


def requeue_needs_auth():
    for u in store.find("upload", status="needs_auth"):
        retry_upload(u["id"])


def reschedule(upload_id: str, when: str | None = "keep", platform: str | None = None) -> dict:
    """Move a post: new time (None = as soon as possible) and/or another platform. A YouTube video that is already
    uploaded and waiting for its publish time is moved on YouTube itself."""
    u = store.get("upload", upload_id)
    if not u:
        raise RuntimeError("That post no longer exists.")
    if u["status"] == "uploading":
        raise RuntimeError("This post is uploading right now; wait for it to finish.")
    if when not in ("keep", None):
        datetime.fromisoformat(when.replace("Z", "+00:00"))  # validates
    if u["status"] == "done":
        future = u.get("publish_at") and not _due({"upload_at": u["publish_at"]})
        if u.get("platform") == "youtube" and future and u.get("video_id") and when not in ("keep", None) and not platform:
            from . import youtube
            return store.update("upload", upload_id, {"publish_at": youtube.set_publish_at(u["video_id"], when)})
        raise RuntimeError("Already posted: it can't be moved (delete it on the platform and post again instead).")
    plat = platform or u.get("platform", "youtube")
    if plat not in platforms.PLATFORMS:
        raise RuntimeError(f"Unknown platform {plat}")
    t = (u.get("publish_at") or u.get("upload_at")) if when == "keep" else when
    patch = {"platform": plat, "publish_at": t if plat == "youtube" else None, "upload_at": None if plat == "youtube" else t,
             "error": "", "auto": False}  # a post the user moves by hand is theirs now: the Autopilot pause no longer holds it
    if plat == "tiktok" and not u.get("tiktok"):  # never choose a public audience for the user (TikTok guideline)
        patch["tiktok"] = _tiktok_opts({"privacy_level": "SELF_ONLY"})

    def apply(doc):
        if doc["status"] in ("uploading", "done"):  # the worker got there first
            raise RuntimeError("This post started uploading just now; it can't be moved.")
        doc.update(patch)

    u = store.update("upload", upload_id, fn=apply)
    log.event("schedule", f"upload {upload_id} -> {plat} at {t or 'asap'}")
    clip = store.get("clip", u["clip_id"])
    if u["status"] == "queued":
        return u  # its job is already waiting; the job re-checks the new time before posting
    if u["status"] in ("scheduled", "held", "failed", "needs_auth"):
        if clip and clip_export_current(clip):
            _queue_or_schedule(u)
        elif clip:
            store.update("upload", upload_id, {"status": "waiting_render"})
            request_export(clip["id"])
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
    """After a scan: take the top-N clips, write metadata, and queue them to each chosen platform on a schedule."""
    ap = (store.get("project", pid) or {}).get("autopilot") or {}
    if not ap.get("enabled"):
        return
    if paused():
        log.event("autopilot", f"paused: nothing scheduled for project {pid}")
        return
    smart = ap.get("timing") == "best"  # best times per platform; "fixed" honours the user's own start + interval
    clips = sorted([c for c in store.find("clip", project_id=pid) if not c.get("parts")], key=lambda c: -c["score"])[: int(ap.get("top_n", 3))]
    times = stagger(next_slot(ap.get("first_publish")), float(ap.get("interval_hours", 24)), len(clips))
    segs = load_json(pid, "transcript.json", {"segments": []})["segments"]
    targets = [p for p in ap.get("platforms") or ["youtube"] if p in platforms.PLATFORMS]
    for clip, when in zip(clips, times):
        edit = {**clip["edit"], "aspect": ap.get("aspect", clip["edit"]["aspect"]), "captions": ap.get("captions", True),
                "remove_silence": ap.get("jump_cuts", clip["edit"].get("remove_silence", False))}
        clip = store.update("clip", clip["id"], {"edit": edit, "rev": clip["rev"] + 1})
        meta = {"title": clip["title"], "description": clip.get("hook", ""), "tags": [h.lstrip("#") for h in clip.get("hashtags", [])],
                "category_id": "22"}
        if config.api_key() and segs:
            try:
                meta = ai.youtube_meta(clip, segs, edit["aspect"] in ("9:16", "4:5"), guidance())
            except RuntimeError:
                pass
        mode = ap.get("mode", "publish_at")  # publish_at: upload now, YouTube publishes later | upload_at: post at that time
        for platform in targets:
            at = (schedule.suggest(platform, 1, category=meta.get("category_id")) or [when])[0] if smart else when
            request_upload(clip["id"], {**meta, "platform": platform, "caption": meta.get("social_caption"),
                                        "privacy": ap.get("privacy", "public"), "auto": True,
                                        "publish_at": at if mode == "publish_at" else None,
                                        "upload_at": at if mode == "upload_at" else None})


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


def _performance() -> list[dict]:
    """View/like counts of clips this app published to YouTube (used as training signal)."""
    ids = [u["video_id"] for u in store.find("upload", platform="youtube", status="done") if u.get("video_id")]
    if not ids or not platforms.connected("youtube"):
        return []
    from .youtube import _service

    ups = {u["video_id"]: u for u in store.find("upload", platform="youtube") if u.get("video_id")}
    def predicted(cid):  # the score the finder gave, from the live clip or, if it was deleted, the feedback memory
        clip = store.get("clip", cid) if cid else None
        if clip:
            return None if clip.get("parts") else clip["score"]  # a montage's 0 is not a prediction
        return (store.get("feedback", cid) or {}).get("ai_score") if cid else None

    out = []
    for i in range(0, len(ids), 50):
        for it in _service().videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute().get("items", []):
            st, u = it.get("statistics", {}), ups.get(it["id"], {})
            out.append({"title": u.get("title", ""), "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                        "ai_score": predicted(u.get("clip_id")), "clip_id": u.get("clip_id")})
    return sorted(out, key=lambda p: -p["views"])


def _results(job=None) -> list[dict]:
    """Pull real view counts and attach them to each published clip's lesson, so the reranker learns from results,
    not just from what the creator chose to post. No Claude call: cheap enough to run daily."""
    perf = _performance()
    for p in perf:
        cid = p.get("clip_id")
        if cid and store.get("feedback", cid):
            store.update("feedback", cid, {"views": p["views"], "likes": p["likes"], "results_at": now_iso()})
        elif cid and store.get("clip", cid):
            remember(store.get("clip", cid), "published")
            store.update("feedback", cid, {"views": p["views"], "likes": p["likes"], "results_at": now_iso()})
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


def _busy(kind: str, **match) -> bool:
    return any(j["status"] in ("queued", "running") for j in store.find("job", kind=kind, **match))


def _learning_tick():
    """Keep learning without being asked: refresh post results daily, retrain once enough new lessons pile up."""
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    if platforms.connected("youtube") and (config.load().get("results_at") or "") < day_ago and not _busy("results"):
        submit("results")
    prof = next((p for p in store.find("profile") if p.get("active") and p.get("trained") and p.get("auto_refresh", True)), None)
    if prof and config.api_key() and (prof.get("trained_at") or "") < day_ago and not _busy("train", profile_id=prof["id"]):
        since = datetime.fromisoformat(prof["trained_at"]).timestamp()
        if sum(f["created"] > since for f in store.find("feedback")) >= 10:
            submit("train", profile_id=prof["id"], auto=True)


def delete_project(pid: str):
    for kind in ("clip", "upload", "report"):
        for d in store.find(kind, project_id=pid):
            store.delete(kind, d["id"])
    store.delete("project", pid)
    shutil.rmtree(pdir(pid), ignore_errors=True)


# ---------- files arriving from OBS (this PC, a mapped network folder, or the LAN receiver) ----------

def ingest_file(path, name: str, origin: dict, autopilot: dict | None = None) -> dict:
    """Queue a video that already sits on a disk this PC can read: copied into a new project, then analyzed."""
    p = new_project(name, autopilot, origin)
    log.event("obs", f"{origin.get('obs')} saved {path} ({origin.get('kind')}) -> project {p['id']}")
    store.update("project", p["id"], {"stage": "Waiting for OBS to finish the file"})
    submit("ingest_file", project_id=p["id"], path=str(path))
    return p


def _ingest_file(job):
    from pathlib import Path

    from .relay import wait_stable

    src, pid = Path(job["path"]), job["project_id"]
    if src.suffix.lower() not in media.VIDEO_EXT:
        raise RuntimeError(f"Not a video file: {src.name}")
    if not wait_stable(src, timeout=600):
        raise RuntimeError(f"{src} never finished writing (or isn't readable from this PC).")
    store.update("project", pid, {"stage": "Copying from OBS"})
    dst = pdir(pid) / f"source{src.suffix.lower()}"
    shutil.copy2(src, dst)  # copy, never move: the creator's recordings stay where OBS put them
    store.update("project", pid, {"source": dst.name, "status": "queued", "stage": "Queued for analysis"})
    submit("analyze", project_id=pid)


HANDLERS = {"import_url": _import_url, "analyze": _analyze, "find": _find_job, "export": _export, "upload": _upload,
            "scan_source": _scan_source, "virality": _virality, "train": _train, "research": _research,
            "results": _results, "ingest_file": _ingest_file, "track": _track}
FAIL_HOOKS = {"import_url": _analyze_failed, "analyze": _analyze_failed, "export": _export_failed, "upload": _upload_failed,
              "scan_source": _scan_failed, "train": _train_failed, "research": _train_failed, "ingest_file": _analyze_failed,
              "track": _track_failed}


def start():
    global _started
    if _started:
        return
    _started = True
    for j in store.find("job"):  # recover after a restart: anything mid-flight runs again
        if j["status"] in ("queued", "running"):
            store.update("job", j["id"], {"status": "queued"})
            _enqueue(j)
    for u in store.find("upload", status="uploading"):
        store.update("upload", u["id"], {"status": "queued"})
    for c in store.find("clip"):  # one-time backfill: ratings/posts made before the feedback memory existed
        if (c.get("rating") or c.get("posts")) and not store.get("feedback", c["id"]):
            remember(c, "rated" if c.get("rating") else "published")
    for target, args in ((_worker, (_heavy,)), (_worker, (_ai,)), (_worker, (_uploads,)), (_scheduler, ())):
        threading.Thread(target=target, args=args, daemon=True).start()
