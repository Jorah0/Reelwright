"""AI layer: Whisper transcription, Claude-powered clip finding / search / edit assistant / YouTube metadata."""
from __future__ import annotations

import math
import threading
from typing import Literal, Optional

import anthropic
import numpy as np
from pydantic import BaseModel, Field

from . import config, usage

# ---------- transcription (faster-whisper, GPU when available) ----------

_whisper = {}
_whisper_lock = threading.Lock()
_cuda_broken = False


def _enable_cuda_dlls():
    """Windows: make the pip-installed NVIDIA wheels (cuBLAS/cuDNN) loadable by CTranslate2."""
    import os
    import site
    from pathlib import Path

    import sys

    roots = [*site.getsitepackages(), site.getusersitepackages(), getattr(sys, "_MEIPASS", "")]  # _MEIPASS: packaged .exe
    for sp in filter(None, roots):
        for bin_dir in Path(sp).glob("nvidia/*/bin"):
            if str(bin_dir) not in os.environ["PATH"]:
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(str(bin_dir))


def _model(size: str, device: str):
    from faster_whisper import WhisperModel

    if device == "cuda":
        _enable_cuda_dlls()
    key = (size, device)
    if key not in _whisper:
        _whisper.clear()  # keep one model resident
        _whisper[key] = WhisperModel(size, device=device, compute_type="float16" if device == "cuda" else "int8")
    return _whisper[key]


def transcribe(path, duration: float, progress=None) -> dict:
    global _cuda_broken
    s = config.load()
    devices = ["cuda", "cpu"] if s["whisper_device"] == "auto" else [s["whisper_device"]]
    if _cuda_broken and len(devices) > 1:
        devices = ["cpu"]  # a failed CUDA context can hang on retry; stay on CPU for this session
    last = None
    with _whisper_lock:
        for device in devices:
            try:
                segs, info = _model(s["whisper_model"], device).transcribe(str(path), word_timestamps=True, vad_filter=True)
                out = []
                for seg in segs:
                    out.append({
                        "start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip(),
                        "words": [{"s": round(w.start, 2), "e": round(w.end, 2), "w": w.word} for w in (seg.words or [])],
                    })
                    if progress and duration:
                        progress(min(seg.end / duration, 1.0))
                return {"language": info.language, "device": device, "segments": out}
            except Exception as e:  # CUDA libs missing etc. → fall back to CPU
                last = e
                _whisper.clear()
                _cuda_broken = _cuda_broken or device == "cuda"
    raise RuntimeError(f"Transcription failed: {last}")


# ---------- Claude ----------

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}


def _ask(system: str, user: str | tuple[str, str], schema: type[BaseModel], effort: str = "high") -> BaseModel:
    """One structured Claude call. Pass user as (context, task) to cache the context: repeated searches or analyses
    of the same video then re-read the transcript from cache (~10% of the price, faster). Keep volatile text in task."""
    key = config.api_key()
    if not key:
        raise RuntimeError("Add your Anthropic API key in Settings to use Claude-powered features.")
    model = config.load()["model"]
    client = anthropic.Anthropic(api_key=key, max_retries=4)  # rides out short 429/529 overload bursts
    if isinstance(user, tuple):
        context, task = user
        user = [{"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": task}]
    kw = dict(model=model, max_tokens=16000, system=system, messages=[{"role": "user", "content": user}],
              output_format=schema, output_config={"effort": effort})
    if model in FALLBACK_MODELS:  # server-side refusal fallback (routes a declined request to another model)
        kw.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        r = client.beta.messages.parse(**kw)
    except anthropic.AuthenticationError:
        raise RuntimeError("Anthropic rejected the API key — check it in Settings.") from None
    except anthropic.RateLimitError:
        raise RuntimeError("Claude is rate-limiting requests right now — try again in a minute.") from None
    except anthropic.BadRequestError as e:
        raise RuntimeError(f"Claude rejected the request: {e.message}") from None
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from None
    except anthropic.APIConnectionError:
        raise RuntimeError("Couldn't reach the Claude API — check your internet connection.") from None
    usage.record(schema.__name__, getattr(r, "model", None) or model, getattr(r, "usage", None) or {})
    if r.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request.")
    if r.parsed_output is None:
        raise RuntimeError("Claude returned no structured result (response may have been cut off).")
    return r.parsed_output


def transcript_lines(segments: list[dict], start: float = 0, end: float = float("inf")) -> str:
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments if s["end"] > start and s["start"] < end)


class Moment(BaseModel):
    start: float = Field(description="start time in seconds")
    end: float = Field(description="end time in seconds")
    title: str = Field(description="punchy clip title, max 70 chars")
    hook: str = Field(description="the opening line/idea that grabs attention")
    reason: str = Field(description="why this works as a standalone clip")
    score: int = Field(description="virality / quality score 0-100")
    hashtags: list[str]


class MomentList(BaseModel):
    clips: list[Moment]


FIND_SYSTEM = """You are a world-class short-form video editor and content strategist (YouTube Shorts, TikTok, Reels).
You find the moments in long videos that work as standalone clips. A great clip:
- opens with a hook in the first 3 seconds (bold claim, question, conflict, surprise, emotion) — never mid-sentence;
- is self-contained: a viewer with no context understands it;
- has a payoff (punchline, answer, reveal, insight) and ends right after it, on a complete sentence;
- prefers energy spikes (laughter, raised voices), strong opinions, stories, practical value and quotable lines.
Use the timestamps given; start/end must align with transcript line boundaries. Never overlap clips. Score honestly.
Transcript lines may carry measured tags: LOUD / VERY LOUD (audio well above this video's normal level: laughter,
shouting, hype), fast (rapid speech), CHAT SPIKE (live chat exploded) — strong evidence of a reaction-worthy moment,
but the words still have to stand alone."""


def even_sample(xs: list, k: int) -> list:
    """k items spread evenly across xs, first and last included (so long videos aren't judged by their opening)."""
    if len(xs) <= k:
        return list(xs)
    return [xs[round(i * (len(xs) - 1) / (k - 1))] for i in range(k)]


def tagged_lines(segments: list[dict], signals: dict) -> str:
    """Transcript lines annotated with what the audio/chat was doing: (LOUD/VERY LOUD, fast, CHAT SPIKE).
    Gives the model per-line prosody instead of a separate list of timestamps it has to line up itself."""
    wave = np.asarray(signals.get("waveform") or [], dtype=np.float32)
    wmed = float(np.median(wave)) if wave.size else 0.0
    chat = np.asarray(signals.get("chat") or [], dtype=np.float32)  # messages per 10 s
    chat_hi = max(float(np.percentile(chat, 95)), 2 * float(np.median(chat)), 3.0) if chat.size else None
    out = []
    for s in segments:
        tags = []
        if wmed > 0:
            part = wave[int(s["start"] * 10):int(s["end"] * 10) + 1]
            lvl = float(part.mean()) / wmed if part.size else 0
            if lvl >= 1.6:
                tags.append("VERY LOUD" if lvl >= 2.2 else "LOUD")
        if len(s["text"].split()) / max(s["end"] - s["start"], 0.3) >= 3.8:
            tags.append("fast")
        if chat_hi is not None:
            part = chat[int(s["start"] // 10):int(s["end"] // 10) + 1]
            if part.size and float(part.max()) >= chat_hi:
                tags.append("CHAT SPIKE")
        out.append(f"[{s['start']:.1f}-{s['end']:.1f}]{' (' + ', '.join(tags) + ')' if tags else ''} {s['text']}")
    return "\n".join(out)


def chat_spikes(chat: list, n: int = 40) -> list[float]:
    """Start times (s) of the busiest 10-second chat windows."""
    if not chat:
        return []
    x = np.asarray(chat, dtype=np.float32)
    top = [int(i) for i in np.argsort(x)[::-1][:n] if x[i] > 2 * max(float(np.median(x)), 1.0)]
    return sorted(i * 10.0 for i in top)


CHUNK_S, OVERLAP_S = 40 * 60, 120  # long VODs are scanned in 40-minute windows so late moments get equal attention


def find_moments(segments, duration, signals: dict, count: int, min_len: int, max_len: int, query: str = "",
                 guidance: str = "", taste: str = "", avoid=(), progress=None) -> list[dict]:
    """Short videos: one pass. Long ones: scan each window in parallel, then a judge pass ranks all candidates
    on one scale (a single pass over a 6-hour transcript under-samples its later hours)."""
    kw = dict(query=query, guidance=guidance, taste=taste, avoid=avoid)
    if duration <= CHUNK_S * 1.5:
        return _find_pass(segments, duration, signals, count, min_len, max_len, **kw)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    windows = [(t, min(t + CHUNK_S + OVERLAP_S, duration)) for t in range(0, int(duration), CHUNK_S)]
    per = max(2, math.ceil(count * 2 * CHUNK_S / duration))
    jobs = []
    for a, b in windows:
        segs = [s for s in segments if a <= s["start"] < b]
        if segs:
            sig = {**signals, "scenes": [t for t in signals.get("scenes", []) if a <= t < b],
                   "loud": [m for m in signals.get("loud", []) if a <= m["t"] < b]}
            jobs.append((segs, sig, [r for r in avoid if a <= r[0] < b]))
    cands, done = [], 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_find_pass, segs, duration, sig, per, min_len, max_len, query=query, guidance=guidance, taste=taste,
                            avoid=av, window=(segs[0]["start"], segs[-1]["end"])) for segs, sig, av in jobs]
        for f in as_completed(futs):
            cands += f.result()
            done += 1
            if progress:
                progress(done / (len(futs) + 1))
    cands = _dedupe(cands)
    if len(cands) <= count:
        return cands
    return rank_moments(cands, segments, count, guidance=guidance, taste=taste, query=query)


def _dedupe(clips: list[dict]) -> list[dict]:
    out = []
    for c in sorted(clips, key=lambda c: -c.get("score", 0)):  # overlapping windows can find the same moment twice
        if not any(min(c["end"], o["end"]) - max(c["start"], o["start"]) > 0.5 * min(c["end"] - c["start"], o["end"] - o["start"])
                   for o in out):
            out.append(c)
    return out


class Ranked(BaseModel):
    index: int = Field(description="candidate number")
    score: int = Field(description="0-100 on one calibrated scale across all candidates")
    reason: str


class Ranking(BaseModel):
    ranked: list[Ranked] = Field(description="the best candidates, best first")


def rank_moments(cands: list[dict], segments: list[dict], count: int, guidance: str = "", taste: str = "",
                 query: str = "") -> list[dict]:
    """Judge pass: compare candidates from different parts of a long video side by side and keep the best."""
    listing = "\n\n".join(
        f"#{i} [{c['start']:.0f}-{c['end']:.0f}s] {c.get('title', '')} — hook: {c.get('hook', '')}\n"
        f"{' '.join(s['text'] for s in segments if c['start'] <= s['start'] < c['end'])[:700]}"
        for i, c in enumerate(cands))
    goal = f'matching the request "{query}"' if query else "for short-form (Shorts/TikTok/Reels)"
    system = ("You are the final editor choosing which candidate clips to publish. Candidates were found in separate "
              "parts of one long video by different passes, so their original scores are not comparable: rescore them "
              "on one honest scale." + (f"\n\nCreator style profile:\n{guidance}" if guidance else ""))
    user = (f"CANDIDATES:\n{listing}\n\n" + (f"The creator's reactions to earlier picks:\n{taste}\n\n" if taste else "")
            + f"Pick the {count} strongest {goal}, best first.")
    res = _ask(system, user, Ranking, effort="medium")
    out, seen = [], set()
    for r in res.ranked:
        if 0 <= r.index < len(cands) and r.index not in seen:
            seen.add(r.index)
            out.append({**cands[r.index], "score": max(0, min(100, r.score)), "reason": r.reason or cands[r.index].get("reason", "")})
    out += [c for i, c in sorted(enumerate(cands), key=lambda ic: -ic[1].get("score", 0)) if i not in seen]  # judge skipped some
    return out[:count]


def _find_pass(segments, duration, signals: dict, count: int, min_len: int, max_len: int, query: str = "",
               guidance: str = "", taste: str = "", avoid=(), window=None) -> list[dict]:
    task = (f'Find up to {count} moments matching this request: "{query}". Length may be {max(5, min_len // 2)}-{max_len * 2}s; '
            "relevance to the request matters most." if query else
            f"Find the {count} best clips, each {min_len}-{max_len} seconds long, sorted best first.")
    if window:
        task += f" This is the part of the video from {window[0]:.0f}s to {window[1]:.0f}s; only pick moments inside it."
    chat = chat_spikes(signals.get("chat") or [])
    # stable per video → cached; taste and task change between calls so they come after the cache breakpoint
    context = (f"Video duration: {duration:.1f}s\n"
               f"Scene cuts (s): {even_sample(signals.get('scenes', []), 400)}\n"
               f"Audio energy spikes (t, relative energy): {[(m['t'], m['energy']) for m in signals.get('loud', [])]}\n"
               + (f"Live-chat activity spikes (s): {[t for t in chat if not window or window[0] <= t < window[1]]}\n" if chat else "")
               + f"\nTRANSCRIPT (tags show audio/chat at that line):\n{tagged_lines(segments, signals)}")
    if avoid:
        task += f" These ranges are already clips — pick other moments: {[(round(a, 1), round(b, 1)) for a, b in avoid]}."
    if taste:
        task = ("The creator's recent reactions to earlier picks (learn the pattern; don't copy topics):\n"
                f"{taste}\n\nTASK: {task}")
    else:
        task = f"TASK: {task}"
    system = FIND_SYSTEM + (f"\n\nThis creator's trained style profile (follow it):\n{guidance}" if guidance else "")
    res = _ask(system, (context, task), MomentList, effort="high")
    return [m.model_dump() for m in res.clips]


def refine(clips: list[dict], segments: list[dict], duration: float, min_len: float, max_len: float) -> list[dict]:
    """Snap AI/heuristic clip bounds to sentence boundaries, enforce length, drop overlaps (keeps higher score)."""
    starts = [s["start"] for s in segments] or [0.0]
    ends = [s["end"] for s in segments] or [duration]
    out = []
    for c in sorted(clips, key=lambda c: -c.get("score", 0)):
        s_near = min(starts, key=lambda t: abs(t - c["start"]))
        s = s_near if abs(s_near - c["start"]) <= 2 else c["start"]
        e_near = min(ends, key=lambda t: abs(t - c["end"]))
        e = e_near if abs(e_near - c["end"]) <= 2 else c["end"]
        s, e = max(0.0, s), min(duration, e)
        if e - s > max_len:
            fit = [t for t in ends if s + min_len <= t <= s + max_len]
            e = max(fit) if fit else s + max_len
        if e - s < min_len:
            fit = [t for t in ends if s + min_len <= t <= s + max_len]
            e = min(fit) if fit else min(duration, s + min_len)
        if e - s < 1:
            continue
        if any(min(e, o["end"]) - max(s, o["start"]) > 0.5 * min(e - s, o["end"] - o["start"]) for o in out):
            continue
        out.append({**c, "start": round(s, 2), "end": round(e, 2)})
    return out


def heuristic_moments(segments, loud: list[dict], duration: float, count: int, min_len: float, max_len: float) -> list[dict]:
    """No-API-key fallback: windows of whole sentences scored by speech density and audio energy."""
    cands = []
    for i, seg in enumerate(segments):
        j, words = i, 0
        while j < len(segments) and segments[j]["end"] - seg["start"] < min_len:
            words += len(segments[j]["text"].split())
            j += 1
        if j >= len(segments):
            break
        end = segments[j]["end"]
        if end - seg["start"] > max_len:
            continue
        words += len(segments[j]["text"].split())
        energy = sum(m["energy"] for m in loud if seg["start"] <= m["t"] <= end)
        score = words / (end - seg["start"]) * 20 + energy * 40
        text = " ".join(s["text"] for s in segments[i:j + 1])
        cands.append({"start": seg["start"], "end": end, "score": round(min(score, 99)), "title": text[:60].strip() + "…",
                      "hook": seg["text"][:120], "reason": "Dense speech and high audio energy (offline heuristic).", "hashtags": []})
    return refine(cands, segments, duration, min_len, max_len)[:count]


class EditPatch(BaseModel):
    explanation: str = Field(description="one sentence describing what you changed")
    start: Optional[float] = None
    end: Optional[float] = None
    aspect: Optional[Literal["source", "9:16", "1:1", "4:5", "16:9"]] = None
    fit: Optional[Literal["crop", "blur"]] = None
    crop_x: Optional[float] = None
    speed: Optional[float] = None
    brightness: Optional[float] = None
    contrast: Optional[float] = None
    saturation: Optional[float] = None
    gamma: Optional[float] = None
    sharpen: Optional[bool] = None
    volume: Optional[float] = None
    loudnorm: Optional[bool] = None
    denoise: Optional[bool] = None
    fade_in: Optional[float] = None
    fade_out: Optional[float] = None
    captions: Optional[bool] = None
    remove_silence: Optional[bool] = Field(default=None, description="jump cuts: remove silences / dead air")
    remove_fillers: Optional[bool] = Field(default=None, description="cut filler words like um / uh")
    title_text: Optional[str] = None


ASSIST_SYSTEM = """You are the editing assistant inside Reelwright, a video editor. Turn the user's instruction into changes
to ONE clip. Only set fields you want to change. Ranges: speed 0.5-2, brightness -0.3..0.3 (0 = none), contrast 0.7-1.5,
saturation 0-2, gamma 0.7-1.5, volume 0-3, fades 0-3 s, crop_x 0..1 (horizontal framing centre). start/end are absolute
seconds in the source; to trim, move them onto transcript line boundaries. aspect "9:16" is vertical for Shorts/TikTok/Reels."""


def assist(instruction: str, clip: dict, edit: dict, segments: list[dict], duration: float) -> dict:
    user = (f"Source duration: {duration:.1f}s\nClip: start={clip['start']} end={clip['end']} title={clip.get('title')!r}\n"
            f"Current edit settings: {edit}\nTranscript around the clip:\n"
            f"{transcript_lines(segments, clip['start'] - 20, clip['end'] + 20)}\n\nInstruction: {instruction}")
    patch = _ask(ASSIST_SYSTEM, user, EditPatch, effort="low")
    return patch.model_dump(exclude_none=True)


class YouTubeMeta(BaseModel):
    title: str = Field(description="max 95 characters, compelling, no clickbait lies")
    description: str = Field(description="2-4 short paragraphs + relevant hashtags at the end")
    tags: list[str] = Field(description="8-15 search tags")
    category_id: str = Field(description="YouTube category id, e.g. 22 People & Blogs, 24 Entertainment, 27 Education, 28 Science & Tech, 20 Gaming, 17 Sports, 10 Music")
    social_caption: str = Field(description="TikTok/Instagram Reels caption: one hooky line + 3-6 hashtags, under 300 characters")


def youtube_meta(clip: dict, segments: list[dict], vertical: bool, guidance: str = "") -> dict:
    user = (f"Write YouTube metadata for this {'YouTube Short (vertical)' if vertical else 'video'}.\n"
            f"Working title: {clip.get('title')}\nHook: {clip.get('hook')}\n"
            f"Transcript:\n{transcript_lines(segments, clip['start'], clip['end'])}")
    system = "You are a short-form growth strategist writing searchable, honest, high-CTR titles and captions for YouTube, TikTok and Reels."
    if guidance:
        system += f"\nThis creator's trained style profile:\n{guidance}"
    m = _ask(system, user, YouTubeMeta, effort="low")
    d = m.model_dump()
    d["title"] = d["title"][:100]
    if vertical and "#shorts" not in d["description"].lower():
        d["description"] += "\n\n#Shorts"
    tags, total = [], 0
    for t in d["tags"]:  # YouTube caps tags at ~500 chars total
        if total + len(t) + 1 > 480:
            break
        tags.append(t)
        total += len(t) + 1
    d["tags"] = tags
    return d


# ---------- virality analysis ----------

class Dimension(BaseModel):
    name: str
    score: int = Field(description="0-100")
    note: str


class HotMoment(BaseModel):
    start: float
    end: float
    why: str


class ViralityReport(BaseModel):
    score: int = Field(description="overall virality potential 0-100")
    verdict: str = Field(description="one-sentence verdict")
    dimensions: list[Dimension] = Field(description="Hook, Retention/pacing, Emotion, Clarity, Trend fit, Shareability, Platform fit")
    strengths: list[str]
    risks: list[str]
    improvements: list[str] = Field(description="concrete edits that would raise performance")
    title_ideas: list[str]
    best_platform: str = Field(description="YouTube Shorts, TikTok, Instagram Reels or long-form YouTube, with the reason")
    comparable_patterns: str = Field(description="what the most-viewed similar videos do that this should copy or avoid")
    hot_moments: list[HotMoment] = Field(description="up to 5 strongest moments worth clipping")


VIRAL_SYSTEM = """You are a data-driven short-form video analyst. You predict how well a video or clip will perform and say
exactly why, using the transcript, pacing signals and the view counts of similar videos currently on YouTube.
Be calibrated: most content scores 30-60; reserve 80+ for content with a proven viral pattern. Be specific and actionable."""


def pacing_summary(segments: list[dict], signals: dict, start: float, end: float) -> str:
    dur = max(end - start, 0.1)
    words = sum(len(s["text"].split()) for s in segments if start <= s["start"] < end)
    cuts = sum(1 for t in signals.get("scenes", []) if start <= t < end)
    silent = sum(max(0.0, min(b, end) - max(a, start)) for a, b in signals.get("silences", []))
    spikes = [m["t"] for m in signals.get("loud", []) if start <= m["t"] < end]
    first = next((round(s["start"] - start, 1) for s in segments if s["start"] >= start), None)
    return (f"length {dur:.1f}s; {words / dur * 60:.0f} words/min; {cuts} scene cuts ({cuts / dur * 60:.1f}/min); "
            f"{silent / dur * 100:.0f}% silence; first words at +{first}s; audio energy spikes at {spikes[:15]}")


def similar_videos(query: str, n: int = 12) -> list[dict]:
    """Metadata (no downloads) of the top YouTube results for a query, most viewed first."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as y:
            info = y.extract_info(f"ytsearch{n}:{query}", download=False)
    except Exception:
        return []
    out = [{"title": e.get("title"), "views": e.get("view_count"), "duration": e.get("duration"),
            "channel": e.get("channel") or e.get("uploader"), "url": e.get("url")}
           for e in info.get("entries", []) if e]
    return sorted(out, key=lambda v: -(v["views"] or 0))


class Query(BaseModel):
    query: str = Field(description="a 3-6 word YouTube search query for videos on the same topic and format")


def virality(segments, signals, start, end, title: str, guidance: str = "", query: str = "") -> dict:
    text = transcript_lines(segments, start, end)[:60000]
    if not query:
        query = _ask("Write a search query.", f"Title: {title}\nTranscript excerpt:\n{text[:4000]}", Query, effort="low").query
    sims = similar_videos(query)
    user = (f"Title: {title}\nPacing: {pacing_summary(segments, signals, start, end)}\n\n"
            f"Similar videos on YouTube right now (query: {query}), most viewed first:\n"
            + "\n".join(f"- {v['views'] or 0:,} views | {v['duration'] or '?'}s | {v['title']} ({v['channel']})" for v in sims)
            + f"\n\nTRANSCRIPT:\n{text or '(no speech)'}")
    system = VIRAL_SYSTEM + (f"\n\nThe creator's trained style profile:\n{guidance}" if guidance else "")
    report = _ask(system, user, ViralityReport, effort="high").model_dump()
    report["query"], report["similar"] = query, sims
    return report


# ---------- custom training: a creator style profile ----------

class Theme(BaseModel):
    caption_style: Literal["bold", "clean", "boxed", "neon"]
    caption_color: str = Field(description="hex colour for the highlighted caption word, e.g. #FFD400")
    caption_pos: Literal["low", "middle", "high"]
    aspect: Literal["9:16", "1:1", "4:5", "16:9"]
    layout: Literal["single", "split"] = Field(description="split = facecam on top (streamers/gaming), single otherwise")


class StyleProfile(BaseModel):
    summary: str = Field(description="2-3 sentences on what makes this creator's best content work")
    guidance: str = Field(description="8-15 bullet rules an editor should follow when picking and editing clips for this creator")
    hook_patterns: list[str]
    winning_topics: list[str]
    avoid: list[str]
    ideal_length_s: list[int] = Field(description="[min, max] seconds")
    title_formula: str
    theme: Theme


def reference_metadata(url: str, n: int = 30) -> list[dict]:
    """Titles/views/durations for a video, channel or playlist URL (metadata only, no download)."""
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
                           "playlistend": n}) as y:
        info = y.extract_info(url, download=False)
    entries = info.get("entries") or [info]
    return [{"title": e.get("title"), "views": e.get("view_count"), "duration": e.get("duration")} for e in entries if e][:n]


def train_profile(name: str, notes: str, references: list[dict], rated: list[dict], performance: list[dict],
                  answers: dict | None = None) -> dict:
    user = f"Profile name: {name}\nCreator notes: {notes or '(none)'}\n\n"
    if answers:
        user += "CREATOR INTERVIEW:\n" + "\n".join(f"- {k}: {v}" for k, v in answers.items() if str(v).strip()) + "\n\n"
    if references:
        user += "REFERENCE VIDEOS (views | seconds | title):\n" + "\n".join(
            f"- {r['views'] or 0:,} | {r['duration'] or '?'} | {r['title']}" for r in references) + "\n\n"
    if performance:
        user += ("THIS CREATOR'S PUBLISHED CLIPS AND THEIR RESULTS (ai = the score the clip finder predicted; where it "
                 "disagrees with real views, learn what the predictions over- or under-rate):\n") + "\n".join(
            f"- {p['views']:,} views, {p.get('likes', 0)} likes | ai {p.get('ai_score', '?')} | {p['title']}"
            for p in performance) + "\n\n"
    if rated:
        user += ("CREATOR FEEDBACK ON CLIP PICKS (+1/-1; 'rated' is an explicit thumbs up/down and counts most, "
                 "'published' means they posted it, 'dismissed' means they deleted an AI pick unrated):\n") + "\n".join(
            f"- {c['rating']:+d} {c.get('why', 'rated')} | {c['title']} | {c['text'][:300]}" for c in rated) + "\n\n"
    if not (references or performance or rated or notes or answers):
        raise RuntimeError("Add reference videos/channels, notes, or rate some clips first — there is nothing to learn from yet.")
    user += "Learn what performs for this creator and produce their style profile."
    return _ask("You are a short-form content strategist who reverse-engineers why videos perform and turns it into an editing playbook.",
                user, StyleProfile, effort="high").model_dump()


# ---------- live research (open web) & intake questions ----------

WEB_TOOL_NEW = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}


def research(prompt: str, max_uses: int = 8) -> tuple[str, list[dict]]:
    """Let Claude search the open web, then return its notes and the source pages it used."""
    key = config.api_key()
    if not key:
        raise RuntimeError("Add your Anthropic API key in Settings to use live research.")
    model = config.load()["model"]
    tool = {"type": "web_search_20260209" if model in WEB_TOOL_NEW else "web_search_20250305", "name": "web_search", "max_uses": max_uses}
    client = anthropic.Anthropic(api_key=key, max_retries=4)
    messages = [{"role": "user", "content": prompt}]
    kw = dict(model=model, max_tokens=16000, tools=[tool], output_config={"effort": "medium"})
    if model in FALLBACK_MODELS:
        kw.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    blocks = []
    try:
        for _ in range(6):  # server tools may pause a long turn; resume until it finishes
            r = client.beta.messages.create(messages=messages, **kw)
            usage.record("research", getattr(r, "model", None) or model, getattr(r, "usage", None) or {})
            blocks += r.content
            if r.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": r.content}]
    except anthropic.AuthenticationError:
        raise RuntimeError("Anthropic rejected the API key — check it in Settings.") from None
    except anthropic.RateLimitError:
        raise RuntimeError("Claude is rate-limiting requests right now — try again in a minute.") from None
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from None
    except anthropic.APIConnectionError:
        raise RuntimeError("Couldn't reach the Claude API — check your internet connection.") from None
    if r.stop_reason == "refusal":
        raise RuntimeError("Claude declined this research request.")
    text = "\n".join(b.text for b in blocks if getattr(b, "type", "") == "text")
    sources, seen = [], set()
    for b in blocks:
        if getattr(b, "type", "") == "web_search_tool_result" and isinstance(b.content, list):
            for res in b.content:
                url = getattr(res, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"title": getattr(res, "title", url), "url": url})
    return text, sources[:25]


class PlatformPlaybook(BaseModel):
    summary: str = Field(description="2-3 sentences: what is working on this platform right now for this creator's niche")
    algorithm_signals: list[str] = Field(description="what the ranking system currently rewards (watch time, rewatches, shares, saves...)")
    current_trends: list[str] = Field(description="formats, sounds, topics and edit styles trending now in this niche")
    hook_rules: list[str]
    ideal_length_s: list[int] = Field(description="[min, max] seconds")
    caption_rules: list[str] = Field(description="how to write the on-platform title/caption")
    hashtag_strategy: str
    best_posting_times: str
    format_specs: str = Field(description="resolution, aspect, max length, caption limits")
    avoid: list[str]


PLATFORM_NAMES = {"youtube": "YouTube Shorts (and long-form YouTube)", "tiktok": "TikTok", "instagram": "Instagram Reels"}


def platform_playbook(platform: str, creator_context: str) -> dict:
    """Research the open web for the latest on one platform, then distil it into a playbook."""
    from datetime import date

    name = PLATFORM_NAMES.get(platform, platform)
    notes, sources = research(
        f"Today is {date.today():%B %d, %Y}. Research the most up-to-date (last 3-6 months) guidance for growing on {name}: "
        f"how the algorithm currently ranks short videos, trending formats and edit styles, hook techniques, ideal lengths, "
        f"caption/title and hashtag practice, best posting times, and current format limits. Prefer official platform "
        f"sources and recent creator/industry analyses; note dates. Focus on this creator:\n{creator_context}")
    if not notes.strip():
        raise RuntimeError(f"Research for {name} came back empty — try again.")
    pb = _ask(f"You turn research notes into a precise, current {name} playbook for one creator. Use only what the notes support.",
              f"Creator:\n{creator_context}\n\nRESEARCH NOTES:\n{notes[:60000]}", PlatformPlaybook, effort="medium").model_dump()
    pb["sources"] = sources
    return pb


def playbook_digest(playbooks: dict) -> str:
    """Compact per-platform rules for prompts."""
    out = []
    for platform, pb in (playbooks or {}).items():
        if not pb or "summary" not in pb:
            continue
        out.append(f"[{PLATFORM_NAMES.get(platform, platform)}] {pb['summary']} Hooks: {'; '.join(pb['hook_rules'][:4])}. "
                   f"Length {'-'.join(map(str, pb['ideal_length_s']))}s. Captions: {'; '.join(pb['caption_rules'][:3])}. "
                   f"Hashtags: {pb['hashtag_strategy']}. Trends: {'; '.join(pb['current_trends'][:4])}.")
    return "\n".join(out)


class Question(BaseModel):
    id: str = Field(description="short snake_case key")
    question: str
    why: str = Field(description="one line on how the answer improves clips or publishing")
    placeholder: str = Field(description="an example answer")


class Questions(BaseModel):
    questions: list[Question]


def intake_questions(name: str, notes: str, references: list[str], answers: dict) -> list[dict]:
    """Ask only what is still unknown and would most improve clip picks, edits and per-platform publishing."""
    known = "\n".join(f"- {k}: {v}" for k, v in (answers or {}).items() if str(v).strip()) or "(nothing yet)"
    user = (f"Profile: {name}\nNotes: {notes or '(none)'}\nReference links: {references or '(none)'}\nAlready answered:\n{known}\n\n"
            "Ask the 4-6 most useful questions you still need (niche and sub-genre, target audience, goals per platform, tone, "
            "competitors/creators to emulate, content boundaries, posting capacity...). Do not repeat answered topics.")
    q = _ask("You are onboarding a creator onto an AI clipping and publishing tool. Ask short, concrete questions.", user, Questions, effort="low")
    return [x.model_dump() for x in q.questions][:6]
