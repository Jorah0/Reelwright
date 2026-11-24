"""AI layer: Whisper transcription, Claude-powered clip finding / search / edit assistant / YouTube metadata."""
from __future__ import annotations

import base64
import bisect
import contextvars
import math
import threading
from typing import Literal, Optional

import anthropic
import numpy as np
from pydantic import BaseModel, Field

from . import config, log, media, providers, usage
from .media import LAYOUTS, REGION_LAYOUTS

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


WINDOW_S, WINDOW_PRE_S, WINDOW_POST_S = 30 * 60.0, 10.0, 30.0  # Whisper hears 30-minute windows of 16 kHz audio
# (~115 MB) instead of the whole file as float32 (~1.4 GB for 6 h); the overlap lets speech across a boundary be heard whole
BATCH = 8  # RTX 4090, 2 h of speech: batch 16 was no faster than 8 (41.7 vs 42.9 s) and reserved 1.7 GB more RAM


def _stitch(out: list, segs, off: float, floor: float, until: float | None, progress, duration) -> float:
    """Append one window's segments (window clock + off = source clock). Words before floor came from the previous
    window's overlap and are dropped; segments starting at/after until belong to the next window. -> the next floor."""
    cut = floor
    for seg in segs:
        if until is not None and off + seg.start >= until:
            break  # the next window hears this part with its own lead-in
        words = [{"s": round(off + w.start, 2), "e": round(off + w.end, 2), "w": w.word} for w in (seg.words or [])]
        kept = [w for w in words if w["s"] >= floor]
        if (not kept) if words else off + seg.end <= floor:
            continue
        whole = len(kept) == len(words)
        out.append({"start": round(off + seg.start, 2) if whole else kept[0]["s"], "end": round(off + seg.end, 2),
                    "text": seg.text.strip() if whole else "".join(w["w"] for w in kept).strip(), "words": kept})
        cut = max(cut, out[-1]["end"], *(w["e"] for w in kept))
        if progress and duration:
            progress(min(out[-1]["end"] / duration, 1.0))
    return cut


def transcribe(path, duration: float, progress=None, vocab: str = "") -> dict:
    """Speech -> segments with word times. Audio goes in WINDOW_S windows (media.pcm), on CUDA through faster-whisper's
    batched pipeline. vocab = hotwords (brand names, slang) for every window; setting "whisper_language" pins the
    language, else the first window with speech (or a confident detection) decides it for the rest."""
    global _cuda_broken
    s = config.load()
    devices = ["cuda", "cpu"] if s["whisper_device"] == "auto" else [s["whisper_device"]]
    if _cuda_broken and len(devices) > 1:
        devices = ["cpu"]  # a failed CUDA context can hang on retry; stay on CPU for this session
    last = None
    with _whisper_lock:
        for device in devices:
            try:
                model = _model(s["whisper_model"], device)
                opts = {"word_timestamps": True, "vad_filter": True, "hotwords": vocab or None,
                        **({"beam_size": 2} if "turbo" in s["whisper_model"] else {})}  # turbo loses ~nothing at beam 2
                if device == "cuda":
                    from faster_whisper import BatchedInferencePipeline

                    model, opts = BatchedInferencePipeline(model), {**opts, "batch_size": BATCH}
                lang, out, cut = s.get("whisper_language") or None, [], 0.0
                n = max(1, math.ceil((duration or 0) / WINDOW_S))
                for k in range(n):
                    off, end, final = max(0.0, k * WINDOW_S - WINDOW_PRE_S), (k + 1) * WINDOW_S, k == n - 1
                    length = None if final else min(duration, end + WINDOW_POST_S) - off  # the last reads to the real end
                    had = len(out)
                    segs, info = model.transcribe(media.pcm(path, off, length), language=lang, **opts)
                    cut = _stitch(out, segs, off, cut, None if final else end, progress, duration)
                    if not lang and (len(out) > had or (getattr(info, "language_probability", 0) or 0) >= 0.5):
                        lang = info.language  # a silent or music-only opening doesn't decide the whole video's language
                return {"language": lang or info.language, "device": device, "segments": out}
            except media.DecodeError:
                raise  # the file, not the GPU: no CPU retry, and CUDA stays trusted
            except Exception as e:  # CUDA libs missing etc. → fall back to CPU
                last = e
                _whisper.clear()
                _cuda_broken = _cuda_broken or device == "cuda"
    raise RuntimeError(f"Transcription failed: {last}")


# ---------- Claude ----------

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}

# Settings "ai_routes" picks a provider per task; the task comes from the schema, so callers stay unchanged
TASKS = {"find": "Finding clips", "judge": "Ranking clips (long videos)", "classify": "Video type", "meta": "Titles & captions",
         "plan": "Edit plans", "assist": "Edit assistant", "other": "Everything else (virality, profiles, research)"}
TASK_OF = {"MomentList": "find", "Ranking": "judge", "VideoType": "classify", "YouTubeMeta": "meta", "Plan": "plan",
           "EditPatch": "assist"}


def _release(task: str) -> dict | None:
    """The Models page's active release for a task ({provider, model, digest}); None = Settings → AI decides."""
    try:
        from . import models  # lazy; inside the try so even an import failure leaves Settings → AI in charge
        return models.routing(task)
    except Exception as e:  # a broken release record must never stop AI calls: Settings → AI still works
        log.event("models", f"release lookup for {task} failed: {e}")
        return None


def route(task: str) -> str:
    rel = _release(task)
    p = (rel or {}).get("provider") or (config.load().get("ai_routes") or {}).get(task) or "anthropic"
    return p if p in providers.NAMES else "anthropic"


def provider_cfg(provider: str, task: str) -> dict:
    """A provider's settings, with the model pinned by the task's active release when it routes there."""
    cfg, rel = config.provider(provider), _release(task)
    return {**cfg, "model": rel["model"]} if rel and rel["provider"] == provider and rel.get("model") else cfg


def provider_ready(name: str) -> bool:
    return bool(config.api_key()) if name == "anthropic" else bool(config.provider(name).get("model"))


def ready(task: str) -> bool:
    """Whether the AI path for a task can run (callers fall back to heuristics when not)."""
    p = route(task)
    return provider_ready(p) or (p != "anthropic" and bool(provider_cfg(p, task).get("model")))


def _ask(system: str, user: str | list | tuple[str, str | list], schema: type[BaseModel], effort: str = "high",
         model: str | None = None, provider: str | None = None) -> BaseModel:
    """One structured Claude call. Pass user as (context, task) to cache the context: repeated searches or analyses
    of the same video then re-read the transcript from cache (~10% of the price, faster). Keep volatile text in task.
    user (or task) may also be a list of content blocks, e.g. text interleaved with image blocks.
    Routed (setting "ai_routes", or provider=) to another provider, the same call goes through providers.ask."""
    routed = TASK_OF.get(schema.__name__, "other")
    provider = provider or route(routed)
    if provider != "anthropic":
        return providers.ask(provider, provider_cfg(provider, routed), system, user, schema)
    key = config.api_key()
    if not key:
        raise RuntimeError("Add your Anthropic API key in Settings to use Claude-powered features.")
    model = model or (provider_cfg("anthropic", routed).get("model") if _release(routed) else None) or config.load()["model"]
    client = anthropic.Anthropic(api_key=key, max_retries=4)  # rides out short 429/529 overload bursts
    if isinstance(user, tuple):
        context, task = user
        user = [{"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
                *(task if isinstance(task, list) else [{"type": "text", "text": task}])]
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


# ---------- video type: decided before the expensive analysis, it steers what the finder and the judge look for ----------

VIDEO_TYPES = {"gaming": "Gaming", "vtuber": "VTuber", "just_chatting": "Just chatting / IRL", "podcast": "Podcast / interview",
               "reaction": "Reaction", "tutorial": "Tutorial / educational", "sports": "Sports", "music": "Music / performance",
               "vlog": "Vlog", "comedy": "Comedy / sketch", "other": "Other"}
SUB_GENRES = {"fps": "FPS / competitive", "horror": "Horror", "variety": "Variety / casual", "speedrun": "Speedrun"}  # gaming only

PLAYBOOKS = {
    "gaming": "Payoffs are visual: clutch plays, wins, deaths, fails, rage and hype. Trust ACTION / FLASH / LOUD / CHAT SPIKE "
              "tags over the words; a clip may carry little speech. Start a few seconds before the play (the setup), end on the "
              "outcome plus the streamer's reaction. Typical length 15-45 s. Down-weight menus, loading, lobby chatter and "
              "long quiet stretches of routine play.",
    "vtuber": "The avatar's reaction and the voice carry the clip: screams, laughter, in-character bits, chat interactions and "
              "lore moments. LOUD, fast and CHAT SPIKE matter most; ON-CAM is the avatar reacting. Hook on the funniest line "
              "or scream. Typical length 15-45 s. Down-weight long gameplay with no talking and reading superchats aloud.",
    "just_chatting": "Stories, hot takes, chat-bait questions and reactions to chat. CHAT SPIKE and LOUD lines mark the moments; "
                     "ON-CAM faces reacting help. The clip must open on the topic, not mid-thought. Typical length 20-60 s. "
                     "Down-weight thanking subs/donations, setup, and dead air.",
    "podcast": "Words are the clip: quotable one-liners, strong opinions, surprising facts, a full mini-story, laughs, and "
               "back-and-forth between speakers (a question and its answer belong together). Hook on the boldest line. "
               "Typical length 30-75 s. Down-weight visual tags (ACTION/FLASH mean little in a studio), intros, ad reads "
               "and housekeeping.",
    "reaction": "Show the thing AND the reaction: the clip needs the moment being reacted to plus the strongest reaction "
                "(LOUD, ON-CAM, laughter, disbelief). Keep it understandable without the original. Typical length 15-45 s. "
                "Down-weight long silent watching and pauses to explain.",
    "tutorial": "Complete, self-contained explanations: one tip, one fix or one 'aha' with its result, start to finish. A "
                "clear promise in the first line ('how to…', 'the mistake everyone makes…'). Typical length 30-90 s. "
                "Down-weight loudness, chat and action tags; never cut mid-step or before the result is shown.",
    "sports": "Plays and crowd energy: goals, saves, big hits, finishes, celebrations. ACTION plus LOUD (crowd/commentator) mark "
              "them; include a short build-up and the replay or reaction if close. Typical length 10-40 s. Down-weight "
              "studio talk, stoppages and ads.",
    "music": "Peak musical moments: the drop, the chorus, a solo, a high note, a crowd singing along. LOUD and ACTION help; "
             "start on a bar/phrase boundary and end on a resolved phrase, never mid-word. Typical length 15-60 s. "
             "Down-weight tuning, banter between songs and quiet stretches.",
    "vlog": "Story beats and personality: a reveal, a funny mishap, a striking place, a relatable confession. Needs a clear "
            "setup and payoff in one clip. Typical length 20-60 s. Down-weight transit filler and b-roll without narration.",
    "comedy": "Setup-punchline structure: keep the full setup and end right after the laugh (LOUD laughter is the marker). "
              "One bit per clip. Typical length 15-60 s. Down-weight scene-setting with no joke.",
    "other": "Use the general rules: a strong hook, self-contained, a clear payoff, and the measured tags as evidence.",
}
SUB_PLAYBOOKS = {
    "fps": "FPS / competitive: kills, multi-kills, clutches and aces (ACTION + FLASH bursts, kill-feed moments), loud callouts "
           "and comms; round wins and losses. Tight cuts, 10-35 s.",
    "horror": "Horror: jump scares are FLASH + LOUD + an ON-CAM face reaction within a second or two; include ~5 s of tension "
              "before the scare and the scream after. Laughing it off afterwards is part of the payoff.",
    "variety": "Variety / casual: funny fails, glitches, surprising events and banter; personality beats raw skill.",
    "speedrun": "Speedrun: records, near-misses, tricks and skips, resets in frustration; the timer or split matters, so keep "
                "the moment of success or failure in frame.",
}


class VideoType(BaseModel):
    type: Literal["gaming", "vtuber", "just_chatting", "podcast", "reaction", "tutorial", "sports", "music", "vlog",
                  "comedy", "other"]
    sub_genre: Optional[Literal["fps", "horror", "variety", "speedrun"]] = Field(default=None, description="gaming only")
    confidence: float = Field(description="0-1")
    people_on_cam: int = Field(description="people visibly on camera (0 if none, e.g. gameplay only)")
    facecam: bool = Field(description="a streamer facecam overlay over gameplay/screen content")
    language: str = Field(description="primary spoken language, ISO 639-1 code, or 'unknown'")
    reasons: list[str] = Field(description="up to 3 short reasons")


CLASSIFY_SYSTEM = ("You classify a video before it is clipped, from its title/metadata and a few small frames spread across it. "
                   "Types: " + ", ".join(f"{k} ({v})" for k, v in VIDEO_TYPES.items()) + ". For gaming also pick sub_genre: "
                   + ", ".join(f"{k} ({v})" for k, v in SUB_GENRES.items()) + ". Be honest about confidence.")


def classify_video(about: str, shots: list[bytes], excerpt: str = "") -> dict:
    """One cheap Claude call (low effort, setting "classify_model") → the video type dict stored on the project."""
    text = about + (f"\n\nTranscript excerpt:\n{excerpt[:4000]}" if excerpt else "")
    user = [{"type": "text", "text": text}] + [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(j).decode()}}
        for j in shots]
    s = config.load()
    cm = s.get("classify_model") or None
    try:
        vt = _ask(CLASSIFY_SYSTEM, user, VideoType, effort="low", model=cm)
    except RuntimeError as e:  # e.g. the cheap model rejects the effort setting: one retry on the main model
        if not cm or cm == s["model"]:
            raise
        log.event("classify", f"{cm} failed ({e}); retrying with {s['model']}")
        vt = _ask(CLASSIFY_SYSTEM, user, VideoType, effort="low", model=s["model"])
    vt = vt.model_dump()
    if vt["type"] != "gaming":
        vt["sub_genre"] = None
    return {**vt, "confidence": round(max(0.0, min(1.0, vt["confidence"])), 2), "reasons": vt["reasons"][:3], "source": "ai"}


# ponytail: keyword regexes on title/tags, first match wins (order matters: "minecraft speedrun" is a speedrun, "Elden Ring
# episode 5" is gaming); extend as misses show up
_KEYWORDS = [
    ("vtuber", None, r"vtuber|vtube"),
    ("gaming", "speedrun", r"speedrun|any%|world record|\bwr\b"),
    ("gaming", "horror", r"horror|phasmophobia|scary|resident evil|outlast|fnaf|five nights|lethal company|dead by daylight"),
    ("gaming", "fps", r"valorant|\bcs2\b|cs:?go|counter.strike|\bapex\b|warzone|call of duty|\bcod\b|fortnite|overwatch|"
                      r"rainbow six|\br6\b|pubg|\bhalo\b|battlefield|tarkov|ranked|clutch"),
    ("gaming", "variety", r"minecraft|let'?s play|gameplay|playthrough|walkthrough|gaming|\bgta\b|elden ring|roblox"),
    ("podcast", None, r"podcast|episode|\bep\.? ?\d|interview|talk show"),
    ("reaction", None, r"\breact|reaction"),
    ("tutorial", None, r"tutorial|how to|\bguide\b|explained|(crash|full|free|online) course|\blessons? \d"),
    ("sports", None, r"\bnba\b|\bnfl\b|football|soccer|basketball|\bufc\b|boxing|\bf1\b|tennis|hockey"),
    ("music", None, r"music video|official video|live performance|concert|\bsong\b|\bcover\b|lyrics"),
    ("comedy", None, r"sketch|comedy|stand.?up|\bskit\b|parody|prank"),
    ("vlog", None, r"\bvlog|day in the life|\bgrwm\b"),
    ("just_chatting", None, r"just chatting|\birl\b|chatting|q ?& ?a"),
]


def heuristic_type(about: str, signals: dict | None = None, segments: list | None = None, duration: float = 0) -> dict:
    """No-key fallback: title keywords first, then the measured signals (motion, faces on screen, speech density)."""
    import re

    vis = (signals or {}).get("visual") or {}
    motion = float(np.median(vis["motion"])) if vis.get("motion") else None
    face = float(np.mean(vis["face"])) if vis.get("face") else 0.0
    wps = sum(len(s["text"].split()) for s in segments or []) / duration if duration else 0.0
    out = {"type": "other", "sub_genre": None, "confidence": 0.2, "people_on_cam": int(face >= 0.3), "facecam": False,
           "language": "unknown", "reasons": [], "source": "heuristic"}
    low = (about or "").lower()
    hit = next(((t, sub, m.group(0)) for t, sub, rx in _KEYWORDS if (m := re.search(rx, low))), None)
    if hit:
        out.update(type=hit[0], sub_genre=hit[1], confidence=0.6, reasons=[f"title/metadata mentions “{hit[2]}”"])
    elif motion is not None and motion >= 0.03:
        out.update(type="gaming", sub_genre="variety", confidence=0.4, reasons=["lots of on-screen motion"])
    elif face >= 0.5:
        out.update(type="just_chatting", confidence=0.4, reasons=["a face on camera most of the time"])
    elif wps >= 1.5:
        out.update(type="podcast", confidence=0.35, reasons=["dense speech with little on-screen action"])
    out["facecam"] = out["type"] == "gaming" and face >= 0.2
    return out


def fixed_type(kind, sub=None) -> dict | None:
    """A creator-chosen type (None for "auto"). Raises ValueError for anything outside the taxonomy."""
    if kind in (None, "", "auto"):
        return None
    if (not isinstance(kind, str) or kind not in VIDEO_TYPES
            or (sub not in (None, "") and (kind != "gaming" or not isinstance(sub, str) or sub not in SUB_GENRES))):
        raise ValueError(f"Unknown video type {kind!r}{f' / {sub!r}' if sub else ''}.")
    return {"type": kind, "sub_genre": sub or None, "confidence": 1.0, "source": "user", "reasons": ["set by the creator"]}


def playbook(vt: dict | None) -> str:
    """The per-type 'what to comb for' block for the finder's cached context and the judge's system prompt."""
    t = (vt or {}).get("type")
    if t not in VIDEO_TYPES:
        return ""
    sub = vt.get("sub_genre") if t == "gaming" and vt.get("sub_genre") in SUB_GENRES else None
    label = VIDEO_TYPES[t] + (f" — {SUB_GENRES[sub]}" if sub else "") + (" (set by the creator)" if vt.get("source") == "user" else "")
    return (f"VIDEO TYPE: {label}\nWhat makes a good clip in this kind of video (playbook):\n{PLAYBOOKS[t]}"
            + (f"\n{SUB_PLAYBOOKS[sub]}" if sub else ""))


FIND_SYSTEM = """You are a world-class short-form video editor and content strategist (YouTube Shorts, TikTok, Reels).
You find the moments in long videos that work as standalone clips. A great clip:
- opens with a hook in the first 3 seconds (bold claim, question, conflict, surprise, emotion) — never mid-sentence;
- is self-contained: a viewer with no context understands it;
- has a payoff (punchline, answer, reveal, insight) and ends right after it, on a complete sentence;
- prefers energy spikes (laughter, raised voices), strong opinions, stories, practical value and quotable lines.
Use the timestamps given; start/end must align with transcript line boundaries. Never overlap clips. Score honestly.
Transcript lines may carry measured tags: LOUD / VERY LOUD (audio well above this video's normal level: laughter,
shouting, hype), fast (rapid speech), CHAT SPIKE (live chat exploded) — strong evidence of a reaction-worthy moment,
but the words still have to stand alone. On-screen tags come from the picture: ACTION (far more motion than usual:
fights, clutch plays, chases), FLASH (a sudden flash: explosion, jump scare, hit), ON-CAM (the streamer's face reacting
on camera). In gaming and VTuber streams a visual payoff can carry a clip that has little speech.
Streams may also carry: CLIP-MARKER (the creator said "clip that" or pressed a mark button: the 30-60 s BEFORE it is a
clip they asked for — include its setup), EVENT <kind> (a game or app event), VIEWER-CLIPPED ×n (n viewers clipped
this part on Twitch: proven demand), CHAT: laughing/hype/scared/... (what chat reacted with), and detected sounds
LAUGHTER, SCREAM, CHEERING, APPLAUSE, GUNSHOT, EXPLOSION, CRYING, MUSIC.
When the context names a VIDEO TYPE with a playbook, follow it: it says which signals matter and which to discount
(its typical lengths are only a guide: the lengths in the TASK always win)."""
CLIP_MARKS = {"voice", "obs", "comment"}  # marker kinds meaning "the creator wants this clipped" (others are events)
CHAT_WORDS = {"laugh": "laughing", "hype": "hype", "scary": "scared", "sad": "sad", "cringe": "cringe", "shock": "shocked",
              "clip": "asking to clip"}


def even_sample(xs: list, k: int) -> list:
    """k items spread evenly across xs, first and last included (so long videos aren't judged by their opening)."""
    if len(xs) <= k:
        return list(xs)
    return [xs[round(i * (len(xs) - 1) / (k - 1))] for i in range(k)]


def tagged_lines(segments: list[dict], signals: dict) -> str:
    """Transcript lines annotated with what the audio/chat/picture was doing: (LOUD/VERY LOUD, fast, CHAT SPIKE,
    ACTION, FLASH, ON-CAM, plus CLIP-MARKER / EVENT, VIEWER-CLIPPED ×n, CHAT: <reaction>, LAUGHTER/GUNSHOT/… from
    signals.enrich). Gives the model per-line prosody instead of a separate list of timestamps it has to line up."""
    marks = sorted(signals.get("markers") or [], key=lambda m: m["t"])
    vclips = signals.get("viewer_clips") or []
    kinds = {k: np.asarray(v, dtype=np.float32) for k, v in (signals.get("chat_kinds") or {}).items() if v}
    kind_hi = {k: max(float(np.percentile(v, 95)), 3.0) for k, v in kinds.items()}
    sounds = sorted(signals.get("audio_events") or [], key=lambda e: e["t"])
    mark_t, sound_t = [m["t"] for m in marks], [e["t"] for e in sounds]
    wave = np.asarray(signals.get("waveform") or [], dtype=np.float32)
    wmed = float(np.median(wave)) if wave.size else 0.0
    chat = np.asarray(signals.get("chat") or [], dtype=np.float32)  # messages per 10 s
    chat_hi = max(float(np.percentile(chat, 95)), 2 * float(np.median(chat)), 3.0) if chat.size else None
    vis = signals.get("visual") or {}
    motion = np.asarray(vis.get("motion") or [], dtype=np.float32)  # per second
    motion_hi = max(2.0 * float(np.median(motion)), 0.012) if motion.size else None
    face = np.asarray(vis.get("face") or [], dtype=np.float32)
    cam_rare = face.size and float(face.mean()) < 0.5  # a face on screen only means something when the cam isn't always on
    vspikes = vis.get("spikes") or []
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
        a, b = int(s["start"]), int(s["end"]) + 1
        if motion_hi is not None and motion[a:b].size and float(motion[a:b].mean()) >= motion_hi:
            tags.append("ACTION")
        near = {m["kind"] for m in vspikes if s["start"] - 0.5 <= m["t"] <= s["end"] + 0.5}
        if "flash" in near:
            tags.append("FLASH")
        if "face_reaction" in near or (cam_rare and face[a:b].size and float(face[a:b].mean()) >= 0.5):
            tags.append("ON-CAM")
        lo, hi = s["start"] - 0.5, s["end"] + 0.5
        mk = marks[bisect.bisect_left(mark_t, lo):bisect.bisect_right(mark_t, hi)]
        if any(m.get("kind") in CLIP_MARKS for m in mk):
            tags.append("CLIP-MARKER")
        tags += sorted({f"EVENT {m['kind']}" for m in mk if m.get("kind") not in CLIP_MARKS})
        if n := sum(c["t"] < s["end"] and c["t"] + c.get("d", 0) > s["start"] for c in vclips):
            tags.append(f"VIEWER-CLIPPED ×{n}")
        hot = [CHAT_WORDS.get(k, k) for k, v in kinds.items()
               if (part := v[int(s["start"] // 10):int(s["end"] // 10) + 1]).size and float(part.max()) >= kind_hi[k]]
        if hot:
            tags.append("CHAT: " + "/".join(hot))
        tags += sorted({e["kind"].upper() for e in sounds[bisect.bisect_left(sound_t, lo):bisect.bisect_right(sound_t, hi)]})
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
                 guidance: str = "", taste: str = "", avoid=(), progress=None, target: float | None = None) -> list[dict]:
    """Short videos: one pass. Long ones: scan each window in parallel, then a judge pass ranks all candidates
    on one scale (a single pass over a 6-hour transcript under-samples its later hours). signals["video"] (the source
    file, when known) lets the judge look at a few frames of each candidate (setting "visual_judge")."""
    kw = dict(query=query, guidance=guidance, taste=taste, avoid=avoid, **({"target": target} if target else {}))
    pair = consensus_pair()
    if pair:  # "local first, Claude agrees": a cheap scout over-collects, the reviewer rules on each candidate
        try:
            cands, _ = _scan(segments, duration, signals, math.ceil(count * 2.5), min_len, max_len, kw, progress, pair[0])
            if not cands:
                raise RuntimeError("no candidates")
        except Exception as e:
            log.event("consensus", f"scout {pair[0]} failed ({e}); using the single-model path")
        else:
            try:
                out = review_moments(cands, segments, signals, count, guidance=guidance, taste=taste, query=query,
                                     scout=pair[0], reviewer=pair[1])
                if out:
                    return out
                log.event("consensus", f"{pair[1]} kept none of the picks; showing {pair[0]}'s best instead")
            except Exception as e:  # never throw away a finished scout pass
                log.event("consensus", f"reviewer {pair[1]} failed ({e}); showing {pair[0]}'s best picks unreviewed")
            return sorted(cands, key=lambda c: -c.get("score", 0))[:count]
    cands, chunked = _scan(segments, duration, signals, count, min_len, max_len, kw, progress)
    if not chunked or len(cands) <= count:
        return cands
    try:
        return rank_moments(cands, segments, count, guidance=guidance, taste=taste, query=query, video=signals.get("video"),
                            video_type=signals.get("video_type"))
    except Exception as e:
        if route("judge") == route("find"):
            raise  # same provider: the error is real news (bad key, outage), not a missing judge
        log.event("ai", f"judge ({route('judge')}) failed ({e}); keeping the finder's own ranking")
        return sorted(cands, key=lambda c: -c.get("score", 0))[:count]


def _scan(segments, duration, signals, count, min_len, max_len, kw, progress=None, provider=None) -> tuple[list[dict], bool]:
    """The find pass: one call for short videos, parallel windows (deduped) for long ones. -> (candidates, chunked)"""
    if provider:
        kw = {**kw, "provider": provider}
    if duration <= CHUNK_S * 1.5:
        return _find_pass(segments, duration, signals, count, min_len, max_len, **kw), False
    from concurrent.futures import ThreadPoolExecutor, as_completed

    windows = [(t, min(t + CHUNK_S + OVERLAP_S, duration)) for t in range(0, int(duration), CHUNK_S)]
    per = max(2, math.ceil(count * 2 * CHUNK_S / duration))
    jobs, avoid = [], kw.get("avoid") or ()
    for a, b in windows:
        segs = [s for s in segments if a <= s["start"] < b]
        if segs:
            sig = {**signals, "scenes": [t for t in signals.get("scenes", []) if a <= t < b],
                   "loud": [m for m in signals.get("loud", []) if a <= m["t"] < b]}
            jobs.append((segs, sig, [r for r in avoid if a <= r[0] < b]))
    cands, done = [], 0
    workers = 1 if (kw.get("provider") or route("find")) == "ollama" else 4  # Ollama serves one request at a time
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # each window runs in a copy of the caller's context: a model benchmark's routing (a ContextVar) must reach
        # every chunk, or long videos would silently fall back to the default (possibly paid) provider
        futs = [pool.submit(contextvars.copy_context().run, _find_pass, segs, duration, sig, per, min_len, max_len,
                            **{**kw, "avoid": av}, window=(segs[0]["start"], segs[-1]["end"])) for segs, sig, av in jobs]
        for f in as_completed(futs):
            cands += f.result()
            done += 1
            if progress:
                progress(done / (len(futs) + 1))
    return _dedupe(cands), True


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


JUDGE_SHOWN, JUDGE_FRAMES = 12, 4  # the visual judge sees 4 small frames of each of the 12 best-scored candidates


def _judge_frames(cands: list[dict], video) -> dict[int, list[str]]:
    """Base64 JPEGs per candidate index. Best-effort: a failed extraction leaves that candidate text-only."""
    if not video or not config.load().get("visual_judge", True):
        return {}
    from . import vision

    out = {}
    for i, c in sorted(enumerate(cands), key=lambda ic: -ic[1].get("score", 0))[:JUDGE_SHOWN]:
        try:
            out[i] = [base64.b64encode(j).decode() for j in vision.frames(video, c["start"], c["end"], JUDGE_FRAMES)]
        except Exception as e:
            log.event("vision", f"judge frames skipped at {c['start']:.0f}s: {e}")
    return out


def _with_frames(system: str, entries: list[str], tail: str, shots: dict) -> tuple[str, str | list]:
    """Candidate listing for a judge, with each candidate's still frames right after it when there are any."""
    if not shots:
        listing = "\n\n".join(entries)
        return system, f"CANDIDATES:\n{listing}\n\n{tail}"
    system += (" Some candidates come with still frames from inside the clip, in time order: judge the visual payoff "
               "too (action, reactions, on-screen events), not only the words.")
    user = [{"type": "text", "text": "CANDIDATES:"}]
    for i, entry in enumerate(entries):
        user.append({"type": "text", "text": entry})
        user += [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": d}} for d in shots.get(i, [])]
    user.append({"type": "text", "text": tail})
    return system, user


def rank_moments(cands: list[dict], segments: list[dict], count: int, guidance: str = "", taste: str = "",
                 query: str = "", video=None, video_type: dict | None = None) -> list[dict]:
    """Judge pass: compare candidates from different parts of a long video side by side and keep the best.
    With the source video (and the "visual_judge" setting on) the top candidates carry a few still frames each."""
    entries = [f"#{i} [{c['start']:.0f}-{c['end']:.0f}s] {c.get('title', '')} — hook: {c.get('hook', '')}\n"
               f"{' '.join(s['text'] for s in segments if c['start'] <= s['start'] < c['end'])[:700]}"
               for i, c in enumerate(cands)]
    goal = f'matching the request "{query}"' if query else "for short-form (Shorts/TikTok/Reels)"
    system = ("You are the final editor choosing which candidate clips to publish. Candidates were found in separate "
              "parts of one long video by different passes, so their original scores are not comparable: rescore them "
              "on one honest scale." + (f"\n\n{book}" if (book := playbook(video_type)) else "")
              + (f"\n\nCreator style profile:\n{guidance}" if guidance else ""))
    tail = (f"The creator's reactions to earlier picks:\n{taste}\n\n" if taste else "") + f"Pick the {count} strongest {goal}, best first."
    system, user = _with_frames(system, entries, tail, _judge_frames(cands, video))
    res = _ask(system, user, Ranking, effort="medium")
    out, seen = [], set()
    for r in res.ranked:
        if 0 <= r.index < len(cands) and r.index not in seen:
            seen.add(r.index)
            out.append({**cands[r.index], "score": max(0, min(100, r.score)), "reason": r.reason or cands[r.index].get("reason", "")})
    out += [c for i, c in sorted(enumerate(cands), key=lambda ic: -ic[1].get("score", 0)) if i not in seen]  # judge skipped some
    return out[:count]


def _find_pass(segments, duration, signals: dict, count: int, min_len: int, max_len: int, query: str = "",
               guidance: str = "", taste: str = "", avoid=(), window=None, target: float | None = None,
               provider: str | None = None) -> list[dict]:
    aim = (f" Aim for about {target:.0f}s each (shorter is fine when the moment is complete); never longer than {max_len:.0f}s."
           if target else f" Never longer than {max_len:.0f}s.")
    span = f"{min_len:.0f}-{max_len:.0f}s.{aim}" if target else f"{max(5, min_len // 2):.0f}-{max_len * 2:.0f}s."  # picked length vs open search
    task = (f'Find up to {count} moments matching this request: "{query}". Length may be {span} '
            "Relevance to the request matters most." if query else
            f"Find the {count} best clips, each {min_len:.0f}-{max_len:.0f} seconds long, sorted best first.{aim}")
    if window:
        task += f" This is the part of the video from {window[0]:.0f}s to {window[1]:.0f}s; only pick moments inside it."
    chat = chat_spikes(signals.get("chat") or [])
    vspikes = [(m["t"], m["kind"], m["strength"]) for m in (signals.get("visual") or {}).get("spikes", [])
               if not window or window[0] <= m["t"] < window[1]]
    # stable per video → cached; taste and task change between calls so they come after the cache breakpoint
    book = playbook(signals.get("video_type"))
    context = ((f"{book}\n\n" if book else "") + f"Video duration: {duration:.1f}s\n"
               f"Scene cuts (s): {even_sample(signals.get('scenes', []), 400)}\n"
               f"Audio energy spikes (t, relative energy): {[(m['t'], m['energy']) for m in signals.get('loud', [])]}\n"
               + (f"Visual spikes (t, kind, strength): {vspikes}\n" if vspikes else "")
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
    res = _ask(system, (context, task), MomentList, effort="high", **({"provider": provider} if provider else {}))
    return [m.model_dump() for m in res.clips]


# ---------- consensus: a scout model over-collects, a reviewer model rules on every candidate ----------

def consensus_pair() -> tuple[str, str] | None:
    """(scout, reviewer) from setting "ai_consensus" when both are set up, else None (single-model path)."""
    mode = config.load().get("ai_consensus") or "off"
    if mode == "off":
        return None
    scout, _, reviewer = ("ollama:anthropic" if mode == "local_then_claude" else mode).partition(":")
    if scout == reviewer or not {scout, reviewer} <= set(providers.NAMES):
        return None
    if not (provider_ready(scout) and provider_ready(reviewer)):
        log.event("consensus", f"{mode} is on but {scout} or {reviewer} isn't set up; using the single-model path")
        return None
    return scout, reviewer


def model_name(provider: str) -> str:
    return config.load()["model"] if provider == "anthropic" else config.provider(provider).get("model", "")


class Verdict(BaseModel):
    index: int = Field(description="candidate number")
    verdict: Literal["agree", "adjust", "reject"]
    start: Optional[float] = Field(default=None, description="adjust only: new start time in seconds")
    end: Optional[float] = Field(default=None, description="adjust only: new end time in seconds")
    score: int = Field(description="0-100, your own score on one honest scale")
    reason: str = Field(description="one sentence")


class Review(BaseModel):
    verdicts: list[Verdict] = Field(description="exactly one verdict per candidate, in candidate order")


REVIEW_SYSTEM = """You are the senior short-form editor double-checking clip picks made by a faster first-pass model.
Go through EVERY candidate and rule on it:
- agree: it works as a standalone clip as cut (hook in the first seconds, self-contained, a payoff, ends on a complete sentence);
- adjust: the moment is good but the cut is wrong (opens mid-thought, misses the setup or the payoff, runs long) - give the
  new start/end on transcript line boundaries;
- reject: not a strong standalone clip.
Score every candidate 0-100 on one honest scale. Transcript tags are measured: LOUD / VERY LOUD, fast, CHAT SPIKE,
ACTION, FLASH, ON-CAM."""


def review_moments(cands: list[dict], segments: list[dict], signals: dict, count: int, guidance: str = "", taste: str = "",
                   query: str = "", scout: str = "ollama", reviewer: str = "anthropic") -> list[dict]:
    """The reviewer's pass over the scout's candidates -> agreed and adjusted clips, best first, each carrying both
    models' reasons in clip["consensus"]. Rejected candidates go to the event log only."""
    entries = [f"#{i} [{c['start']:.1f}-{c['end']:.1f}s] {c.get('title', '')} — hook: {c.get('hook', '')}\n"
               f"First pass (score {c.get('score', '?')}): {c.get('reason', '')}\n"
               + tagged_lines([s for s in segments if c["start"] - 8 <= s["start"] < c["end"] + 8], signals)[:2500]
               for i, c in enumerate(cands)]
    goal = f' matching the request "{query}"' if query else ""
    system = (REVIEW_SYSTEM + (f"\n\n{book}" if (book := playbook(signals.get("video_type"))) else "")
              + (f"\n\nCreator style profile:\n{guidance}" if guidance else ""))
    tail = ((f"The creator's reactions to earlier picks:\n{taste}\n\n" if taste else "")
            + f"Rule on all {len(cands)} candidates{goal}; about {count} should make the final cut.")
    system, user = _with_frames(system, entries, tail, _judge_frames(cands, signals.get("video")))
    res = _ask(system, user, Review, effort="medium", provider=reviewer)
    who = {"scout": {"provider": scout, "model": model_name(scout)}, "reviewer": {"provider": reviewer, "model": model_name(reviewer)}}
    out, seen = [], set()
    for v in res.verdicts:
        if not 0 <= v.index < len(cands) or v.index in seen:
            continue
        seen.add(v.index)
        c = cands[v.index]
        if v.verdict == "reject":
            log.event("consensus", f"{reviewer} rejected {c['start']:.0f}-{c['end']:.0f}s “{c.get('title', '')}”: {v.reason}")
            continue
        moved = v.verdict == "adjust" and v.start is not None and v.end is not None and v.end > v.start
        s, e = (float(v.start), float(v.end)) if moved else (c["start"], c["end"])
        out.append({**c, "start": s, "end": e, "score": max(0, min(100, v.score)), "reason": v.reason or c.get("reason", ""),
                    "consensus": {"verdict": "adjust" if moved else "agree",
                                  "scout": {**who["scout"], "score": c.get("score"), "reason": c.get("reason", "")},
                                  "reviewer": {**who["reviewer"], "score": v.score, "reason": v.reason},
                                  **({"from": [c["start"], c["end"]]} if moved else {})}})
    for i, c in enumerate(cands):
        if i not in seen:
            log.event("consensus", f"{reviewer} skipped {c['start']:.0f}-{c['end']:.0f}s “{c.get('title', '')}” (dropped)")
    log.event("consensus", f"{scout} proposed {len(cands)}, {reviewer} kept {len(out)}")
    return sorted(out, key=lambda c: -c["score"])[:count]


def refine(clips: list[dict], segments: list[dict], duration: float, min_len: float, max_len: float,
           target: float | None = None) -> list[dict]:
    """Snap AI/heuristic clip bounds to sentence boundaries, enforce length, drop overlaps (keeps higher score).
    With a target length, over-long picks end at the sentence boundary nearest the target (never past max_len)."""
    near = (lambda s: (lambda t: abs(t - s - target))) if target else None
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
            e = (min(fit, key=near(s)) if near else max(fit)) if fit else s + min(max_len, target or max_len)
        if near and e - s > target * 1.5:  # far over the target: pull back to the sentence end nearest it
            fit = [t for t in ends if s + min_len <= t <= s + target * 1.5]
            e = min(fit, key=near(s)) if fit else e
        if e - s < min_len:
            fit = [t for t in ends if s + min_len <= t <= s + max_len]
            e = min(fit) if fit else min(duration, s + min_len)
        if e - s < 1:
            continue
        if any(min(e, o["end"]) - max(s, o["start"]) > 0.5 * min(e - s, o["end"] - o["start"]) for o in out):
            continue
        out.append({**c, "start": round(s, 2), "end": round(e, 2)})
    return out


def heuristic_moments(segments, loud: list[dict], duration: float, count: int, min_len: float, max_len: float,
                      marks=()) -> list[dict]:
    """No-API-key fallback: windows of whole sentences scored by speech density and audio energy. marks (signals'
    markers and viewer_clips) seed their own windows first: the ~45 s before a clip marker, a viewer clip's own span."""
    cands = []
    for m in marks or ():
        t = float(m["t"])
        if m.get("d"):  # a Twitch viewer clip: its span with a short lead-in; more views, more trust
            a, b, views = t - 3, t + float(m["d"]), int(m.get("views") or 0)
            score, why = min(95, 80 + int(math.log10(1 + views) * 5)), f"Viewers clipped this moment on Twitch ({views} views)."
        else:  # "clip that" comes after the moment: end just past it, start up to 50 s earlier
            b = t + 5
            a = b - min(50.0, max_len)
            score = 100 if m.get("kind") in CLIP_MARKS else 90
            why = f"Clip marker ({m.get('label') or m.get('kind')}) at {int(t // 60)}:{int(t % 60):02d}."
        a, b = max(0.0, a), min(duration, b)
        text = " ".join(s["text"] for s in segments if a <= s["start"] < b).strip()
        cands.append({"start": a, "end": b, "score": score, "title": (text[:60] + "…") if text else why[:60], "hook": text[:120],
                      "reason": why, "hashtags": []})
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


class PlanEdit(BaseModel):
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
    zoom: Optional[float] = Field(default=None, description="punch-in 1-4")
    remove_silence: Optional[bool] = None
    remove_fillers: Optional[bool] = None
    title_text: Optional[str] = None
    sticker_text: Optional[str] = None
    aspect: Optional[Literal["source", "9:16", "1:1", "4:5", "16:9"]] = None
    fit: Optional[Literal["crop", "blur"]] = None


class PlanOverlay(BaseModel):
    kind: Literal["image", "gif", "video", "sound", "text", "emoji"]
    asset_id: Optional[str] = Field(default=None, description="an id from the asset list, for media kinds; never invent one")
    text: Optional[str] = Field(default=None, description="for text/emoji: at most 80 characters")
    t: float = Field(description="start, seconds from the clip start")
    d: Optional[float] = Field(default=None, description="seconds on screen; omit for sounds to play the whole effect")
    x: Optional[float] = Field(default=None, description="centre 0..1 of the output width")
    y: Optional[float] = Field(default=None, description="centre 0..1 of the output height")
    w: Optional[float] = Field(default=None, description="width as a fraction of the output width")
    anim: Optional[Literal["none", "pop", "fade", "shake", "slide"]] = None
    volume: Optional[float] = Field(default=None, description="0-2, sounds and video snippets")
    cutaway: Optional[bool] = Field(default=None, description="video only: full-screen cutaway")


class PlanTrack(BaseModel):
    mode: Optional[Literal["auto", "face"]] = Field(default=None, description="re-run subject tracking in this mode")
    offset_y: Optional[float] = Field(default=None, description="headroom -0.4..0.4 of the crop height (+ = subject higher)")
    zoom: Optional[float] = Field(default=None, description="follow-crop zoom 1-4")


class PlanCaptions(BaseModel):
    captions: Optional[bool] = None
    caption_style: Optional[Literal["bold", "clean", "boxed", "neon"]] = None
    caption_pos: Optional[Literal["low", "middle", "high"]] = None


class PlanItem(BaseModel):
    kind: Literal["edit", "overlay", "track", "captions"]
    title: str = Field(description="short label for the checkbox, e.g. 'Boom on the punchline'")
    why: str = Field(description="one sentence: why this makes the clip better")
    edit: Optional[PlanEdit] = Field(default=None, description="for kind edit")
    overlay: Optional[PlanOverlay] = Field(default=None, description="for kind overlay: exactly one overlay")
    track: Optional[PlanTrack] = Field(default=None, description="for kind track")
    captions: Optional[PlanCaptions] = Field(default=None, description="for kind captions")


class Plan(BaseModel):
    summary: str = Field(description="one or two sentences on the overall direction")
    items: list[PlanItem] = Field(description="independent suggestions the creator can tick individually, best first")


PLAN_SYSTEM = """You are the senior short-form editor inside Reelwright. Propose a concrete edit plan for ONE clip as a list of
independent items the creator can approve one by one. Use timing from the transcript (seconds from the clip start): sound
effects land on punchlines and reveals, text pops on the hook in the first 2 seconds, cutaways only where they add context.
Only reference assets by an id from the provided list; if nothing fits, use text or emoji instead. Prefer a few strong,
tasteful items over many. Respect the creator's style guidance. If the creator gave no instruction, make smart suggestions."""


def plan(context: str, prompt: str) -> Plan:
    """An edit plan for one clip. context = clip, transcript, signals, edit, library (cached); prompt = the request."""
    task = f"Creator's request: {prompt.strip()}" if prompt.strip() else "No specific request: suggest the best improvements."
    return _ask(PLAN_SYSTEM, (context, task), Plan, effort="medium")


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

THEME_LAYOUTS = tuple(x for x in LAYOUTS if x not in REGION_LAYOUTS)  # a trained theme styles new clips: no regions


class Theme(BaseModel):
    caption_style: Literal["bold", "clean", "boxed", "neon"]
    caption_color: str = Field(description="hex colour for the highlighted caption word, e.g. #FFD400")
    caption_pos: Literal["low", "middle", "high"]
    aspect: Literal["9:16", "1:1", "4:5", "16:9"]
    layout: Literal[THEME_LAYOUTS] = Field(description="split = facecam on top (streamers/gaming), split_rev = facecam below, "
                                                     "blurcam = the whole frame over its own blur, single otherwise")


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


class PostingSlot(BaseModel):
    weekday: int = Field(description="0 = Monday … 6 = Sunday")
    hour: int = Field(description="0-23, creator's local time")


class PlatformPlaybook(BaseModel):
    summary: str = Field(description="2-3 sentences: what is working on this platform right now for this creator's niche")
    algorithm_signals: list[str] = Field(description="what the ranking system currently rewards (watch time, rewatches, shares, saves...)")
    current_trends: list[str] = Field(description="formats, sounds, topics and edit styles trending now in this niche")
    hook_rules: list[str]
    ideal_length_s: list[int] = Field(description="[min, max] seconds")
    caption_rules: list[str] = Field(description="how to write the on-platform title/caption")
    hashtag_strategy: str
    best_posting_times: str
    posting_slots: list[PostingSlot] = Field(default_factory=list, description="5-12 best weekly posting slots for this "
                                             "creator's audience, converted to the creator's local time zone given in the prompt")
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


# ---------- long-form edits (longform.py): chapter titles and regular-video metadata ----------

class ChapterTitles(BaseModel):
    titles: list[str] = Field(description="one title per chapter, in order, each 2-6 words, max 60 characters")


def longform_chapter_titles(texts: list[str], guidance: str = "") -> list[str]:
    """A short title per chapter of a long-form video, from what is said in each (in order)."""
    listing = "\n\n".join(f"CHAPTER {i + 1}:\n{t or '(no speech)'}" for i, t in enumerate(texts))
    system = ("You title the chapters of a long YouTube video. Each title says what happens in that chapter, plainly and "
              "specifically, like a table of contents. No numbering, no emoji, no clickbait."
              + (f"\nThis creator's style profile:\n{guidance}" if guidance else ""))
    res = _ask(system, f"{listing}\n\nWrite exactly {len(texts)} titles, in order.", ChapterTitles, effort="low")
    return [t.strip()[:60] for t in res.titles]


def longform_meta(title: str, chapters_text: str, transcript: str, guidance: str = "") -> dict:
    """Title, description, tags and category for a regular (not #shorts) YouTube video. The chapter block is placed in
    the description by the caller exactly as given, so YouTube shows chapters."""
    system = ("You are a YouTube growth strategist writing searchable, honest, high-CTR metadata for a regular long-form "
              "YouTube video (not a Short: never add #Shorts)." + (f"\nThis creator's trained style profile:\n{guidance}" if guidance else ""))
    user = (f"Working title: {title}\nChapters:\n{chapters_text or '(none)'}\n\nTranscript excerpt:\n{transcript[:12000]}\n\n"
            "Write the metadata. The description: 2-3 short paragraphs and a few hashtags; leave out timestamps (the chapter "
            "list is added separately).")
    import re

    d = _ask(system, user, YouTubeMeta, effort="low").model_dump()
    d["title"] = d["title"][:100]
    d["description"] = re.sub(r"(?i)\s*#shorts\b", "", d["description"]).strip()
    tags, total = [], 0
    for t in d["tags"]:  # YouTube caps tags at ~500 chars total
        if total + len(t) + 1 > 480:
            break
        tags.append(t)
        total += len(t) + 1
    d["tags"] = tags
    return d



# ---------- headline banner ----------

class HeadlineOptions(BaseModel):
    options: list[str] = Field(description="exactly 3 different on-screen headlines for the clip, each at most 60 characters")
    kicker: str = Field(description="a short top line above the headline (at most 24 characters), e.g. the speaker or show "
                                    "name, 'PART 2', or the topic; empty if nothing fits honestly")


def headline_options(clip: dict, segments: list[dict]) -> dict:
    """3 short headline options and a kicker for the clip's banner, grounded in what is actually said."""
    user = (f"Working title: {clip.get('title')}\nHook: {clip.get('hook')}\n"
            f"Transcript:\n{transcript_lines(segments, clip['start'], clip['end'])}\n\n"
            "Write 3 different headlines for the banner above this vertical clip, plus a kicker.")
    system = ("You write the bold headline banner that sits above a short vertical video. Each headline is punchy, at most "
              "60 characters, and honest: it only claims what really happens in the clip. No clickbait lies, no invented "
              "events, numbers or quotes; no hashtags.")
    r = _ask(system, user, HeadlineOptions, effort="low")
    opts = list(dict.fromkeys(" ".join(o.split())[:90] for o in r.options if o.strip()))[:3]
    return {"options": opts, "kicker": " ".join(r.kicker.split())[:40]}
