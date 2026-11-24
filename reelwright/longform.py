"""Long-form edits: a separate set of actions next to short clips. One project can hold several "longform" docs, each
an ordered list of source segments played back to back, with chapters, the clip edit schema (16:9 by default) and its
own render, versions and posts.

Capture reuses the project's analysis (transcript.json words, signals.json silences / visual motion, the video type)
and never re-scans the video:
- condensed: the whole video minus dead air (silences, idle menu/AFK screens with no speech, filler words);
- highlights: the best AI moments (ai.find_moments with long lengths) up to a target total, in source order;
- clips: existing clips in the order picked.

Render: segments are grouped (ascending, close together, ≤ 10 min of source per group) and each group is one ffmpeg
pass with jump-cut keeps, exactly like a clip; the parts are joined by the concat demuxer (stream copy) and the audio is
loudness-normalised (and mixed with music) once over the whole programme."""
import bisect
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import ai, assets, config, covers, log, media, multicam, pipeline, store

MODES = ("condensed", "highlights", "clips")
DEFAULT_TARGET = 600.0   # highlights: total length aimed for (s)
DEFAULT_PAD = 0.25       # condensed: air kept around speech (s)


# ---------- the doc ----------

def create(pid: str, mode: str, title: str, segments: list, chapters: list, settings: dict | None = None,
           transition: float = 0.0, status: str = "ready") -> dict:
    edit = {**media.DEFAULT_EDIT, "layout": "single", "captions": True}
    edit.update(pipeline.default_layout(store.get("project", pid)))  # the creator's saved default layout, like new clips
    edit["aspect"] = "16:9"  # long form is landscape unless the creator changes it here
    return store.put("longform", {
        "project_id": pid, "title": title or "Long-form edit", "mode": mode, "status": status, "error": "",
        "segments": [[round(float(a), 3), round(float(b), 3)] for a, b in segments], "chapters": chapters,
        "edit": edit, "transition": float(transition), "settings": settings or {}, "rev": 1, "export": None, "posts": {}})


def length(d: dict) -> float:
    """Output seconds before speed and jump cuts (what chapters and overlays are timed against)."""
    return round(sum(b - a for a, b in d["segments"]), 3)


def export_current(d: dict) -> bool:
    exp = d.get("export") or {}
    return exp.get("status") == "done" and exp.get("rev") == d["rev"] and (pipeline.pdir(d["project_id"]) / exp["file"]).exists()


def posted(lid: str) -> bool:
    return bool(store.find("upload", longform_id=lid, status="done"))


def stage(d: dict, ups: list[dict]) -> str:
    """captured → edited → rendered → uploaded (pending posts count as rendered: they are on their way)."""
    if any(u["status"] == "done" for u in ups):
        return "uploaded"
    if export_current(d) or any(u["status"] in ("waiting_render", "scheduled", "held", "queued", "uploading", "needs_auth")
                                for u in ups):
        return "rendered"
    return "captured" if d.get("rev", 1) <= 1 and not d.get("export") else "edited"


# ---------- sequence time ----------

def offsets(segments) -> list[float]:
    out, acc = [], 0.0
    for a, b in segments:
        out.append(acc)
        acc += b - a
    return out


def to_seq(t: float, segments) -> float | None:
    """Source second -> sequence second (the first segment that contains it), None when it was cut."""
    for off, (a, b) in zip(offsets(segments), segments):
        if a <= t < b:
            return off + t - a
    return None


def seq_words(words: list[dict], segments) -> list[dict]:
    """Transcript words in sequence time and order (words in cut parts are dropped; a word in two segments repeats)."""
    words = sorted(words, key=lambda w: w["s"])
    starts = [w["s"] for w in words]
    out = []
    for off, (a, b) in zip(offsets(segments), segments):  # bisect: a 3-hour condensed cut has thousands of segments
        for w in words[bisect.bisect_left(starts, a - 30):bisect.bisect_left(starts, b)]:
            if a <= (w["s"] + w["e"]) / 2 < b:
                out.append({**w, "s": round(off + w["s"] - a, 3), "e": round(off + min(w["e"], b) - a, 3)})
    return out


# ---------- capture ----------

def idle_spans(visual: dict, words: list[dict], min_s: float = 15.0) -> list[tuple[float, float]]:
    """Stretches of a near-still picture (menus, loading, AFK) at least min_s long with no speech in them.
    ponytail: a fixed threshold (30% of the median motion, floor 0.003); expose it if real menus slip through."""
    motion = visual.get("motion") or []
    if not motion:
        return []
    med = sorted(motion)[len(motion) // 2]
    lo = max(0.3 * med, 0.003)
    spoken = sorted((w["s"], w["e"]) for w in words)
    out, start = [], None
    for i, m in enumerate([*motion, 1e9]):
        if m < lo and start is None:
            start = i
        elif m >= lo and start is not None:
            if i - start >= min_s:
                out.append((float(start), float(i)))
            start = None
    # speech inside an idle stretch splits it (a podcast's still camera is not dead air)
    cut = []
    for a, b in out:
        cur = a
        for s, e in spoken:
            if e <= cur or s >= b:
                continue
            if s - cur >= min_s:
                cut.append((cur, s))
            cur = max(cur, e)
        if b - cur >= min_s:
            cut.append((cur, b))
    return cut


def condensed(signals: dict, words: list[dict], dur: float, pad: float = DEFAULT_PAD, min_dead: float = 0.8,
              idle_s: float = 15.0) -> list[list[float]]:
    """The whole video minus silences, idle screens and filler words, keeping `pad` seconds of air around speech."""
    dead = [(s, e) for s, e in signals.get("silences") or [] if e - s >= min_dead]
    dead += idle_spans(signals.get("visual") or {}, words, idle_s)
    keeps = media.keep_segments(dead, 0.0, dur, pad=pad, min_gap=max(0.3, min_dead - 2 * pad))
    keeps = media.cut_spans(keeps, media.filler_spans(words, 0.0, dur))
    return [[round(a, 3), round(b, 3)] for a, b in keeps if b - a >= 0.15] or [[0.0, round(dur, 3)]]


def pick_highlights(moments: list[dict], target: float) -> tuple[list[list[float]], list[dict]]:
    """Best-scored moments until the target total length is filled, then played in source order, one chapter each."""
    chosen, total = [], 0.0
    for m in sorted(moments, key=lambda m: -m.get("score", 0)):
        n = m["end"] - m["start"]
        if total + n <= target and not any(min(m["end"], c["end"]) > max(m["start"], c["start"]) for c in chosen):
            chosen.append(m)
            total += n
    chosen.sort(key=lambda m: m["start"])
    segs = [[m["start"], m["end"]] for m in chosen]
    return segs, [{"t": round(off, 3), "title": (m.get("title") or f"Moment {i + 1}")[:100]}
                  for i, (off, m) in enumerate(zip(offsets(segs), chosen))]


def from_clips(clips: list[dict]) -> tuple[list[list[float]], list[dict]]:
    segs = [[c["start"], c["end"]] for c in clips]
    return segs, [{"t": round(off, 3), "title": (c.get("title") or "Clip")[:100]} for off, c in zip(offsets(segs), clips)]


def auto_chapters(segments, words: list[dict], every: float = 180.0) -> list[dict]:
    """Offline chapters for a condensed cut: a boundary roughly every `every` seconds, on a segment start, titled by
    the first words spoken after it (Claude retitles them with ai.longform_chapter_titles when a key is set)."""
    total, offs = sum(b - a for a, b in segments), offsets(segments)
    if total < 30:
        return [{"t": 0.0, "title": "Intro"}]
    marks, nxt = [0.0], every
    for off in offs:
        if off >= nxt and total - off >= 10:
            marks.append(round(off, 3))
            nxt = off + every
    seq = seq_words(words, segments)
    out = []
    for i, t in enumerate(marks):
        said = " ".join(w["w"].strip() for w in seq if t <= w["s"] < t + 20)[:60].strip()
        out.append({"t": t, "title": "Intro" if i == 0 else (said.capitalize() or f"Part {i + 1}")})
    return out


def _moments(pid: str, target: float, job_id: str | None = None) -> list[dict]:
    """Long candidate moments from the existing analysis: Claude with the video-type playbook, else the offline picks."""
    proj = store.get("project", pid)
    segs = pipeline.load_json(pid, "transcript.json", {"segments": []})["segments"]
    signals = pipeline.load_json(pid, "signals.json", {}) or {}
    dur = proj["meta"]["duration"]
    lo, hi = 30.0, 180.0  # long moments: a highlight reel, not shorts
    count = max(3, int(target // 60) + 2)
    if (ai.ready("find") or ai.consensus_pair()) and segs:  # the routed provider (Settings → AI), Claude by default
        if proj.get("video_type"):
            signals["video_type"] = proj["video_type"]
        if proj.get("source") and not proj.get("source_removed"):
            signals["video"] = str(pipeline.pdir(pid) / proj["source"])
        prog = (lambda f: pipeline._progress(job_id, f, "Finding the best moments", lo=0.05, hi=0.9)) if job_id else None
        raw = ai.find_moments(segs, dur, signals, count, lo, hi, guidance=pipeline.guidance(), progress=prog, target=90)
        return ai.refine(raw, segs, dur, lo, hi, target=90)
    if segs:
        return ai.heuristic_moments(segs, signals.get("loud", []), dur, count, min(lo, dur / 4), min(hi, dur / 2))
    spikes = (signals.get("visual") or {}).get("spikes") or []
    return pipeline.visual_picks(spikes, 45.0, dur)[:count]


def capture_job(job):
    """Fill a capturing doc's segments and chapters (highlights call Claude, so this runs as a job)."""
    d = store.get("longform", job["longform_id"])
    if not d:
        return
    pid, st = d["project_id"], d.get("settings") or {}
    proj = store.get("project", pid)
    words = [w for s in pipeline.load_json(pid, "transcript.json", {"segments": []})["segments"] for w in s.get("words", [])]
    dur = proj["meta"]["duration"]
    if d["mode"] == "highlights":
        segs, chapters = pick_highlights(_moments(pid, float(st.get("target") or DEFAULT_TARGET), job.get("id")),
                                         float(st.get("target") or DEFAULT_TARGET))
        if not segs:
            raise RuntimeError("No moments were found in this video.")
    else:
        signals = pipeline.load_json(pid, "signals.json", {}) or {}
        segs = condensed(signals, words, dur, float(st.get("pad", DEFAULT_PAD)))
        chapters = auto_chapters(segs, words)
    if ai.ready("other") and words and d["mode"] == "condensed":
        chapters = titled_chapters(segs, chapters, words) or chapters
    store.update("longform", d["id"], {"segments": segs, "chapters": chapters, "status": "ready", "error": ""})
    store.update("job", job["id"], {"message": f"{len(segs)} segments, {fmt_ts(sum(b - a for a, b in segs), 1e9)} long"})


def capture_failed(job, err):
    store.update("longform", job["longform_id"], {"status": "failed", "error": err})


def titled_chapters(segments, chapters: list[dict], words: list[dict]) -> list[dict] | None:
    """Claude titles for each chapter from what is said in it; None when the call fails (the caller keeps its titles)."""
    seq = seq_words(words, segments)
    ends = [c["t"] for c in chapters[1:]] + [float("inf")]
    texts = [" ".join(w["w"].strip() for w in seq if c["t"] <= w["s"] < e)[:1500] for c, e in zip(chapters, ends)]
    try:
        titles = ai.longform_chapter_titles(texts, pipeline.guidance())
    except Exception as e:
        log.event("longform", f"chapter titles skipped: {e}")
        return None
    return [{**c, "title": (t or c["title"])[:100]} for c, t in zip(chapters, [*titles, *[""] * len(chapters)])]


# ---------- chapters for YouTube ----------

def fmt_ts(t: float, total: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if total >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def youtube_chapters(chapters: list[dict], total: float) -> str:
    """YouTube's chapter block: first at 00:00, at least 3, each at least 10 s (shorter ones merge into the one before).
    Empty when the rules can't be met (YouTube would ignore the list anyway)."""
    out = []
    for c in sorted(chapters, key=lambda c: float(c["t"])):
        t = 0.0 if not out else float(c["t"])
        title = re.sub(r"\s+", " ", str(c.get("title") or "")).strip()[:100] or f"Part {len(out) + 1}"
        if out and (t - out[-1][0] < 10 or t >= total):
            continue
        out.append((t, title))
    while len(out) > 1 and total - out[-1][0] < 10:
        out.pop()
    return "\n".join(f"{fmt_ts(t, total)} {title}" for t, title in out) if len(out) >= 3 else ""


# ---------- render ----------

GROUP_GAP, GROUP_SPAN = 30.0, 600.0  # one ffmpeg pass per run of nearby ascending segments, ≤ 10 min of source


def groups(segments) -> list[list[tuple[float, float]]]:
    """Consecutive segments that can share one decode: ascending, under GROUP_GAP s apart, ≤ GROUP_SPAN of source.
    A far jump or a reordered segment starts a new group (seeking beats decoding the gap)."""
    out: list[list[tuple[float, float]]] = []
    for a, b in segments:
        g = out[-1] if out else None
        if g and a >= g[-1][1] and a - g[-1][1] <= GROUP_GAP and b - g[0][0] <= GROUP_SPAN:
            g.append((a, b))
        else:
            out.append([(a, b)])
    return out


def _source_at(t: float, keeps, speed: float) -> float:
    """Inverse of the jump-cut remap inside one group: output seconds -> source seconds."""
    t *= speed
    for a, b in keeps:
        if t <= b - a:
            return a + t
        t -= b - a
    return keeps[-1][1]


def _part_overlays(overlays: list[dict], g, seq0: float) -> list[dict]:
    """Sequence-timed overlays that start inside this group (its segments g, starting at sequence second seq0), re-timed
    to the part's clip time; media.render then takes them through the part's jump cuts and speed, as for a clip."""
    out, w, start = [], sum(b - a for a, b in g), g[0][0]
    for o in overlays or []:
        t = float(o["t"]) - seq0
        if not 0 <= t < w:
            continue
        a = _source_at(t, g, 1.0)
        b = _source_at(min(t + float(o["d"]), w), g, 1.0)
        out.append({**o, "t": round(a - start, 3), "d": round(max(b - a, 0.05), 3)})
    return out


def _part_switches(switches: list[dict], g, seq0: float) -> list[dict]:
    """Sequence-timed camera cuts re-timed to this group's clip time; the camera live at the part's start opens it."""
    out, live, w = [], None, sum(b - a for a, b in g)
    for s in sorted(switches or [], key=lambda s: float(s["t"])):
        t = float(s["t"]) - seq0
        if t <= 0:
            live = s["angle"]
        elif t < w:
            out.append({**s, "t": round(_source_at(t, g, 1.0) - g[0][0], 3)})
    return ([{"t": 0.0, "angle": live}] if live is not None else []) + out


def _cut_keeps(g, e: dict, words: list[dict], sil: list) -> list[tuple[float, float]]:
    """A group's segments minus silences / filler words when the edit removes them (what its render keeps)."""
    keeps = list(g)
    if e["remove_silence"]:
        keeps = [k for a, b in keeps for k in media.keep_segments(sil, a, b)]
    if e.get("remove_fillers") and words:
        keeps = media.cut_spans(keeps, media.filler_spans(words, g[0][0], g[-1][1]))
    return keeps


def timeline(d: dict):
    """(sequence second -> output second, output length): through the edit's silence / filler cuts and speed, so
    chapters match the exported video."""
    e = {**media.DEFAULT_EDIT, **d["edit"]}
    speed = float(e["speed"] or 1)
    gs = groups(d["segments"])
    if not (e["remove_silence"] or e.get("remove_fillers")):
        return (lambda t: t / speed), length(d) / speed
    pid = d["project_id"]
    sil = pipeline.load_json(pid, "signals.json", {}).get("silences", []) if e["remove_silence"] else []
    words = [w for seg in pipeline.load_json(pid, "transcript.json", {"segments": []})["segments"] for w in seg.get("words", [])]
    parts, seq0, out0 = [], 0.0, 0.0
    for g in gs:
        keeps = _cut_keeps(g, e, words, sil)
        parts.append((seq0, g, keeps, out0))
        seq0 += sum(b - a for a, b in g)
        out0 += sum(b - a for a, b in keeps)

    def f(t: float) -> float:
        s0, g, keeps, o0 = next((p for p in reversed(parts) if p[0] <= t), parts[0])
        return (o0 + media.remap(_source_at(t - s0, g, 1.0), keeps)) / speed

    return f, out0 / speed


def output_length(d: dict) -> float:
    return timeline(d)[1]


def chapters_text(d: dict) -> str:
    """The YouTube chapter block at the exported video's times."""
    f, total = timeline(d)
    return youtube_chapters([{**c, "t": f(float(c["t"]))} for c in d.get("chapters") or []], total)


def _render_part(d: dict, proj: dict, g: list[tuple[float, float]], e: dict, words: list[dict], out: Path,
                 seq0: float, prog) -> float:
    """One group (segments g, starting at sequence second seq0) through the clip renderer's pieces (captions, overlays,
    layouts, look). Returns its output length."""
    pid, s = d["project_id"], config.load()
    start, end = g[0][0], g[-1][1]
    sil = pipeline.load_json(pid, "signals.json", {}).get("silences", []) if e["remove_silence"] else []
    keeps = _cut_keeps(g, e, words, sil)
    jump = None if len(keeps) == 1 and keeps[0] == (start, end) else keeps
    speed = float(e["speed"])
    out_len = sum(b - a for a, b in keeps) / speed
    ovs = _part_overlays(e["overlays"], g, seq0)
    texts = [o for o in ovs if o["kind"] in ("text", "emoji")]
    placed = []
    for o in ovs:
        a = store.get("asset", o.get("asset_id") or "") if o["kind"] not in ("text", "emoji") else None
        if a and assets.path(a).exists():
            placed.append({**o, "path": str(assets.path(a))})
    if e.get("switches"):  # cuts between camera angles are in sequence time too
        e = {**e, "switches": _part_switches(e["switches"], g, seq0)}
    size = media.output_size(e["aspect"], proj["meta"]["width"], proj["meta"]["height"])
    headline = media.headline_spec(e, (proj.get("video_type") or {}).get("type"),
                                   (store.get("pref", "headline_theme") or {}).get("theme")) if e.get("headline") else None
    ass = None
    if (e["captions"] and words) or e["title_text"] or e["sticker_text"] or texts or headline:
        ass = media.build_ass(words, start, end, speed, size, e["title_text"], s["caption_uppercase"], s["caption_color"],
                              bool(e["captions"]), e["caption_style"], e["caption_pos"], e["sticker_text"], e["sticker_pos"],
                              jump, media.seam(e), overlays=texts, text_style=e, headline=headline)
    # each part seeks main and every angle to the same source start (an angle to start - its offset), as a clip does
    media.render(pipeline.source_file(proj), out, start, end, e, proj["meta"], ass, prog, jump, None, placed,
                 assets.uploaded_font_files(), headline=headline,
                 angles=multicam.render_angles(pid, proj) if media.used_angles(e) else None)
    return out_len


def _join(parts: list[Path], out: Path, e: dict, music: Path | None, has_audio: bool, total: float) -> None:
    """Concat demuxer (video stream-copied), then the music bed (ducked under the voice), one loudness pass and the
    programme's fades over the whole mix."""
    work = parts[0].parent
    (work / "parts.txt").write_text("".join(f"file '{p.name}'\n" for p in parts), "utf-8")
    args = [media.FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "parts.txt"]
    af = "loudnorm=I=-14:TP=-1.5:LRA=11" if e["loudnorm"] else "anull"
    if music:  # the parts fade the voice already; the music fades here, with the mix, after the loudness pass
        args += ["-stream_loop", "-1", "-i", str(music.resolve())]
        bed = f"[1:a]volume={float(e['music_volume']):.3f},atrim=0:{total:.3f},asetpts=PTS-STARTPTS"
        fi, fo = float(e["fade_in"]), float(e["fade_out"])
        tail = ([af] if e["loudnorm"] else []) + ([f"afade=t=in:st=0:d={fi:.2f}"] if fi > 0 else []) + \
            ([f"afade=t=out:st={max(total - fo, 0):.2f}:d={fo:.2f}"] if fo > 0 else [])
        g = media.mix_audio("[0:a]anull" if has_audio else None, bed, [], tail, has_audio and e.get("music_duck", True))
        args += ["-filter_complex", g, "-map", "0:v", "-map", "[a]"]
    elif has_audio:
        args += ["-map", "0:v", "-map", "0:a", "-af", af]
    else:
        args += ["-map", "0:v"]
    tmp = out.with_name(out.stem + ".rendering.mp4")
    args += ["-c:v", "copy", *(["-c:a", "aac", "-b:a", "160k", "-ar", "48000"] if has_audio or music else []),
             "-movflags", "+faststart", str(tmp.resolve())]
    r = subprocess.run(args, cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=media.NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"Joining the long-form parts failed: {r.stderr.strip()[-1500:]}")
    os.replace(tmp, out)


def render(d: dict, prog=lambda f: None) -> tuple[str, float]:
    """Render a long-form doc to exports/{id}.mp4; returns (relative path, output seconds)."""
    pid = d["project_id"]
    proj = store.get("project", pid)
    if not d["segments"]:
        raise RuntimeError("This long-form edit has no segments.")
    e = assets.resolve_fonts({**media.DEFAULT_EDIT, **d["edit"]})
    words = [w for seg in pipeline.load_json(pid, "transcript.json", {"segments": []})["segments"] for w in seg.get("words", [])]
    gs = groups(d["segments"])
    weights = [sum(b - a for a, b in g) for g in gs]
    total_w = sum(weights) or 1.0
    folder = pipeline.pdir(pid) / "exports"
    work = folder / f".lf_{d['id']}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    tr = min(max(float(d.get("transition") or 0), 0.0), 1.0)
    try:
        parts, out0, done = [], 0.0, 0.0
        for i, g in enumerate(gs):
            pe = {**e, "loudnorm": False, "music": "",  # both happen once over the whole programme in _join
                  "title_text": e["title_text"] if i == 0 else "",
                  "fade_in": e["fade_in"] if i == 0 else tr, "fade_out": e["fade_out"] if i == len(gs) - 1 else tr}
            if i and (e.get("headline") or {}).get("show", "whole") != "whole":  # "first N seconds" = of the programme
                pe["headline"] = {**e["headline"], "on": False}
            p = work / f"part_{i:04d}.mp4"
            w = weights[i]
            out0 += _render_part(d, proj, g, pe, words, p, done, lambda f, w=w, done=done: prog(0.92 * (done + f * w) / total_w))
            done += w
            parts.append(p)
        music = (pipeline.pdir(pid) / e["music"]).resolve() if e["music"] else None
        if music and (not music.is_file() or (pipeline.pdir(pid) / "music").resolve() not in music.parents):
            music = None
        rel = f"exports/{d['id']}.mp4"
        _join(parts, pipeline.pdir(pid) / rel, e, music, bool(proj["meta"].get("has_audio")), out0)
        prog(0.97)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return rel, out0


def request_export(lid: str) -> dict:
    d = store.get("longform", lid) or {}
    if d.get("expired"):
        raise RuntimeError(pipeline.EXPIRED)
    if d and (proj := store.get("project", d["project_id"])) and proj.get("source_removed"):
        pipeline.source_file(proj)  # removed to save space: refuse with the fix instead of queueing a render that fails
    rev = d.get("rev")
    running = [j for j in store.find("job", longform_id=lid, kind="longform_export")
               if j["status"] == "queued" or (j["status"] == "running" and j.get("rev") == rev)]
    return running[0] if running else pipeline.submit("longform_export", longform_id=lid)


def export_job(job):
    d = store.get("longform", job["longform_id"])
    if not d:
        raise RuntimeError("This long-form edit was deleted.")
    rev, pid = d["rev"], d["project_id"]
    store.update("job", job["id"], {"rev": rev})
    store.update("longform", d["id"], {"export": {"status": "rendering", "rev": rev, "progress": 0}})

    def prog(f):
        pipeline._progress(job["id"], f, "Rendering long form")
        store.update("longform", d["id"], fn=lambda x: (x.get("export") or {}).update(progress=round(f, 3)))

    rel, secs = render(d, prog)
    cover, cover_t = f"exports/{d['id']}.jpg", None
    try:
        cover_t = covers.make(pipeline.pdir(pid) / rel, pipeline.pdir(pid) / cover)
    except Exception:
        log.error("cover", f"longform {d['id']}")
    store.update("longform", d["id"], {"export": {"status": "done", "rev": rev, "file": rel, "at": pipeline.now_iso(), "progress": 1,
                                                   "duration": round(secs, 3), "cover": cover if cover_t is not None else None,
                                                   "cover_t": cover_t}})
    if (store.get("longform", d["id"]) or {}).get("rev") != rev:  # edited while rendering: render the newer version too
        request_export(d["id"])
    pipeline._release_uploads(d["id"], "longform_id")


def export_failed(job, err):
    store.update("longform", job["longform_id"], {"export": {"status": "failed", "error": err}})
    for u in store.find("upload", longform_id=job["longform_id"], status="waiting_render"):
        store.update("upload", u["id"], {"status": "failed", "error": f"Render failed: {err}"})


# ---------- versions & delete ----------

def new_version(lid: str) -> dict:
    src = store.get("longform", lid)
    if not src:
        raise RuntimeError("That long-form edit no longer exists.")
    root = src.get("version_of") or lid
    base = re.sub(r" \(v\d+\)$", "", src["title"])
    n = 2 + len(store.find("longform", version_of=root))
    copy = {k: json.loads(json.dumps(v)) for k, v in src.items() if k not in ("id", "created", "export", "posts", "expired", "keep")}
    return store.put("longform", {**copy, "title": f"{base} (v{n})", "export": None, "posts": {}, "rev": 1, "version_of": root})


def delete(lid: str, remote: set[str] = frozenset()) -> dict:
    """Same rules as a clip: pending posts canceled, refused while one is uploading, YouTube removal only when ticked."""
    with store._lock:  # _upload claims a post under this lock
        d = store.get("longform", lid)
        if not d:
            return {"ok": False, "error": "That long-form edit no longer exists."}
        ups = store.find("upload", longform_id=lid)
        if any(u["status"] == "uploading" for u in ups):
            return {"ok": False, "error": pipeline.BUSY}
        canceled = pipeline._cancel_pending(ups)
        store.delete("longform", lid)
    exp, folder = d.get("export") or {}, pipeline.pdir(d["project_id"])
    for rel in {exp.get("file"), exp.get("cover")} - {None}:
        f = (folder / rel).resolve()
        if folder in f.parents:
            f.unlink(missing_ok=True)
    return {"ok": True, "canceled": canceled,
            "remote": [pipeline._delete_remote(u) for u in ups if u["status"] == "done" and u["id"] in remote]}


# ---------- AI edit plan context ----------

def plan_context(d: dict, proj: dict, library: list[dict]) -> str:
    """What ai.plan needs to plan a long form, with times in sequence seconds (what its overlays use)."""
    pid = proj["id"]
    words = [w for s in pipeline.load_json(pid, "transcript.json", {"segments": []})["segments"] for w in s.get("words", [])]
    seq = seq_words(words, d["segments"])
    lines, cur = [], []
    for w in seq:  # ~12-word lines
        cur.append(w)
        if len(cur) >= 12:
            lines.append(f"[{cur[0]['s']:.1f}-{cur[-1]['e']:.1f}] {' '.join(x['w'].strip() for x in cur)}")
            cur = []
    if cur:
        lines.append(f"[{cur[0]['s']:.1f}-{cur[-1]['e']:.1f}] {' '.join(x['w'].strip() for x in cur)}")
    lines = ai.even_sample(lines, 400)
    edit = {k: v for k, v in d["edit"].items() if k not in ("track", "words")}
    lib = [f"{a['id']} | {a['kind']} | {a['name']} | {', '.join(a.get('tags') or [])} | {a.get('duration') or 0:.1f}s" for a in library]
    chapters = "\n".join(f"{c['t']:.0f}s {c['title']}" for c in d.get("chapters") or [])
    return "\n".join([
        f"LONG-FORM video (a landscape YouTube video, not a short): {d['title']!r}, {length(d):.0f}s long, "
        f"{len(d['segments'])} segments. Times below are seconds from the start of this long-form video.",
        f"Chapters:\n{chapters or '(none)'}", "Transcript:", *(lines or ["(no speech)"]),
        f"Current edit settings: {json.dumps(edit)}",
        "Asset library (id | kind | name | tags | length) — built-in sound effects have ids starting sfx-:", *(lib or ["(empty)"]),
        *([f"Creator's style guidance:\n{g}"] if (g := pipeline.guidance()) else [])])
