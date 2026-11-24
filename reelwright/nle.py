"""NLE export: a clip or a long form as an FCPXML 1.10 timeline (Final Cut Pro, DaVinci Resolve), a CMX3600 EDL
(Premiere, Resolve) and an SRT of its captions, so a pro editor can finish it elsewhere.

Every format is built from one timeline dict: the source file, its rate and size, the kept source spans in timeline
order (the render's cuts: silences, fillers, text cuts, long-form segments), markers at timeline seconds, and the
caption words in source seconds. Times are frame-exact: each span's in/out is rounded to a frame and the timeline
advances by whole frames, so the formats agree with each other. Speed changes are not carried (the NLE timeline is
1×). ponytail: no OpenTimelineIO JSON; FCPXML and EDL cover Resolve, Final Cut and Premiere. Add it when asked."""
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from . import config, longform, media, pipeline, store

NTSC = {23.976: Fraction(24000, 1001), 29.97: Fraction(30000, 1001), 59.94: Fraction(60000, 1001)}


def rate(fps: float) -> Fraction:
    """Frames per second as a fraction: 29.97 → 30000/1001 (what NLEs mean by it), else the nearest whole rate."""
    fps = float(fps or 30)
    return next((r for f, r in NTSC.items() if abs(fps - f) < 0.01), Fraction(max(1, round(fps))))


def _drop(r: Fraction) -> int:
    """Timecode labels dropped per minute: 2 at 29.97, 4 at 59.94, else 0 (23.976 is non-drop)."""
    return round(r) // 15 if r.denominator == 1001 and round(r) in (30, 60) else 0


def frames(t: float, r: Fraction) -> int:
    return round(Fraction(t).limit_denominator(100000) * r)


def _ft(n: int, r: Fraction) -> str:
    """n frames as an FCPXML rational time ("315/30s", "300300/30000s")."""
    return f"{n * r.denominator}/{r.numerator}s"


def _spans(tl: dict) -> list[tuple[int, int, int]]:
    """(source in frame, source out frame, timeline offset frame) for each kept span."""
    r, out, at = rate(tl["fps"]), [], 0
    for a, b in tl["keeps"]:
        i, o = frames(a, r), frames(b, r)
        if o > i:
            out.append((i, o, at))
            at += o - i
    return out


# ---------- FCPXML 1.10 ----------

def fcpxml(tl: dict) -> str:
    r = rate(tl["fps"])
    spans = _spans(tl)
    total = spans[-1][2] + spans[-1][1] - spans[-1][0] if spans else 0
    root = ET.Element("fcpxml", version="1.10")
    res = ET.SubElement(root, "resources")
    ET.SubElement(res, "format", id="r1", frameDuration=_ft(1, r), width=str(tl["width"]), height=str(tl["height"]))
    audio = {"hasAudio": "1", "audioSources": "1", "audioChannels": "2", "audioRate": "48000"} if tl.get("has_audio") else {}
    asset = ET.SubElement(res, "asset", id="r2", name=Path(tl["src"]).stem, start="0s", duration=_ft(frames(tl["duration"], r), r),
                          hasVideo="1", format="r1", **audio)
    ET.SubElement(asset, "media-rep", kind="original-media", src=Path(tl["src"]).resolve().as_uri())
    event = ET.SubElement(ET.SubElement(root, "library"), "event", name="Reelwright")
    seq = ET.SubElement(ET.SubElement(event, "project", name=tl["name"]), "sequence", format="r1", duration=_ft(total, r),
                        tcStart="0s", tcFormat="DF" if _drop(r) else "NDF")
    spine = ET.SubElement(seq, "spine")
    clips = [ET.SubElement(spine, "asset-clip", ref="r2", name=tl["name"], offset=_ft(at, r), start=_ft(i, r),
                           duration=_ft(o - i, r), format="r1") for i, o, at in spans]
    for t, label, kind in tl["markers"]:  # in the asset-clip it lands in, at that clip's own (source) time
        n = frames(t, r)
        k = next((k for k, (i, o, at) in enumerate(spans) if at <= n < at + o - i), None)
        if k is not None:
            i, _, at = spans[k]
            attrs = {"start": _ft(i + n - at, r), "duration": _ft(1, r), "value": str(label)[:100]}
            ET.SubElement(clips[k], "chapter-marker" if kind == "chapter" else "marker", **attrs,
                          **({"posterOffset": "0s"} if kind == "chapter" else {}))
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(root, encoding="unicode") + "\n"


# ---------- CMX3600 EDL ----------

def timecode(n: int, fps: float) -> str:
    """Frame n as HH:MM:SS:FF; 29.97 / 59.94 as drop frame (HH:MM:SS;FF: labels ;00 ;01 skipped each minute but every tenth)."""
    r = rate(fps)
    base = round(r)
    drop = _drop(r)
    if drop:
        per10 = base * 600 - drop * 9
        d, m = divmod(n, per10)
        n += drop * 9 * d + (drop * ((m - drop) // (base * 60 - drop)) if m >= drop else 0)
        sep = ";"
    else:
        sep = ":"
    return f"{n // (base * 3600):02d}:{n // (base * 60) % 60:02d}:{n // base % 60:02d}{sep}{n % base:02d}"


def edl(tl: dict) -> str:
    """One V and one A event per kept span (reel AX + FROM CLIP NAME, as Premiere and Resolve write them); markers
    as Resolve's * LOC comments. The record side starts at 01:00:00:00."""
    r, fps = rate(tl["fps"]), tl["fps"]
    df = _drop(r)
    rec0 = round(r) * 3600 - df * 54  # 01:00:00:00 as a frame count (an hour drops 54 labels per dropped one)
    name = Path(tl["src"]).name
    title = "".join(ch for ch in tl["name"] if ch.isprintable())[:70]
    lines, ev = [f"TITLE: {title}", f"FCM: {'DROP' if df else 'NON-DROP'} FRAME", ""], 0
    for i, o, at in _spans(tl):
        for track in ("V", "A"):
            ev += 1
            lines += [f"{ev:03d}  AX       {track:<5} C        {timecode(i, fps)} {timecode(o, fps)} "
                      f"{timecode(rec0 + at, fps)} {timecode(rec0 + at + o - i, fps)}", f"* FROM CLIP NAME: {name}", ""]
    for t, label, kind in tl["markers"]:
        text = " ".join(str(label).split())[:60]
        lines.append(f"* LOC: {timecode(rec0 + frames(t, r), fps)} {'BLUE' if kind == 'chapter' else 'RED':<7} {text}")
    return "\n".join(lines).rstrip() + "\n"


# ---------- SRT ----------

def srt_time(t: float) -> str:
    ms = max(0, round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def srt(tl: dict, max_words: int = 7, gap: float = 0.6, max_len: float = 3.5) -> str:
    """Caption cues at timeline time: words whose middle is in a kept span, grouped into short lines (a pause,
    7 words or 3.5 s starts a new one). Cut words are dropped, like the render's captions."""
    r, placed = rate(tl["fps"]), []
    for i, o, at in _spans(tl):  # the same frame-rounded spans as the EDL / FCPXML, so cues don't drift over many cuts
        a, b, at = float(i / r), float(o / r), float(at / r)
        for w in tl["words"]:
            if a <= (w["s"] + w["e"]) / 2 < b and str(w["w"]).strip():
                placed.append((at + max(w["s"], a) - a, at + min(w["e"], b) - a, str(w["w"]).strip()))
    cues: list[list] = []
    for s, e, w in sorted(placed):
        c = cues[-1] if cues else None
        if c and s - c[1] <= gap and len(c[2]) < max_words and e - c[0] <= max_len:
            c[1], c[2] = e, c[2] + [w]
        else:
            cues.append([s, e, [w]])
    return "".join(f"{n}\n{srt_time(s)} --> {srt_time(e)}\n{' '.join(ws)}\n\n" for n, (s, e, ws) in enumerate(cues, 1))


# ---------- timelines from the store ----------

def _words(pid: str) -> list[dict]:
    return [w for seg in pipeline.load_json(pid, "transcript.json", {"segments": []})["segments"] for w in seg.get("words", [])]


def _base(pid: str, name: str, e: dict, keeps: list, cap_words: list) -> dict:
    proj = store.get("project", pid)
    m = proj.get("meta") or {}
    bad = media.bleep_list(config.load()["bleep_words"]) if e.get("bleep") in ("beep", "mute") else []
    return {"name": name, "src": pipeline.pdir(pid) / (proj.get("source") or "source.mp4"), "fps": m.get("fps") or 30,
            "width": m.get("width") or 1920, "height": m.get("height") or 1080, "duration": m.get("duration") or 0,
            "has_audio": bool(m.get("has_audio")), "keeps": keeps, "markers": [],
            "words": media.censor(cap_words, bad) if bad else cap_words}


def clip_timeline(clip: dict) -> dict:
    """A clip as the render cuts it (pipeline.clip_keeps), with the signal markers inside it."""
    pid = clip["project_id"]
    e = {**media.DEFAULT_EDIT, **clip["edit"]}
    words = _words(pid)
    keeps = pipeline.clip_keeps(clip, e, words) or [(clip["start"], clip["end"])]
    tl = _base(pid, clip.get("title") or "Clip", e, keeps, e["words"] or words)
    for mk in pipeline.load_json(pid, "signals.json", {}).get("markers") or []:
        t = float(mk.get("t", -1))
        if any(a <= t < b for a, b in keeps):
            tl["markers"].append((round(media.remap(t, keeps), 3), str(mk.get("label") or mk.get("kind") or "marker"), "marker"))
    return tl


def longform_timeline(d: dict) -> dict:
    """A long form's segments (minus its silence / filler cuts) in order, with its chapters as chapter markers."""
    pid = d["project_id"]
    e = {**media.DEFAULT_EDIT, **d["edit"]}
    words = _words(pid)
    sil = pipeline.load_json(pid, "signals.json", {}).get("silences", []) if e["remove_silence"] else []
    keeps = [k for g in longform.groups(d["segments"]) for k in longform._cut_keeps(g, e, words, sil)]
    tl = _base(pid, d.get("title") or "Long form", e, keeps, words)
    f, _ = longform.timeline(d)  # sequence second -> output second (through the cuts), at 1× here
    speed = float(e["speed"] or 1)
    tl["markers"] = [(round(f(float(c["t"])) * speed, 3), str(c.get("title") or "Chapter"), "chapter") for c in d.get("chapters") or []]
    return tl
