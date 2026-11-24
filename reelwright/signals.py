"""Extra clip-finding signals, added to signals.json after transcription by enrich() (each part best-effort):

- markers: "clip that" voice commands in the transcript (voice_markers) plus the project's markers.json sidecar (OBS
  marks, game events… written at ingest) -> signals["markers"] = [{t, kind, label, strength, d?}]
- chat_kinds: what chat reacted with (laugh, hype, scary, sad, cringe, shock, clip) per 10 s, from YouTube chat replay
  and the live Twitch chat log (chatlog.py) -> {kind: [messages per 10 s]}; the live log also fills signals["chat"]
- viewer_clips: clips viewers made of this Twitch VOD (twitch.py) -> [{t, d, views, title, id}]
- audio_events: laughter, screams, cheering, applause, gunshots, explosions, crying, music — a small local model
  (sherpa-onnx audio tagging) listens around the energy/chat spikes and markers -> [{t, kind, p}]
Plus vocab(): the hotwords Whisper hears (brand vocabulary, channel, tags, the profile's topics).
"""
import importlib.util
import json
import math
import re
import threading
from pathlib import Path

from . import ai, chatlog, config, log, media, twitch

# ---------- Whisper hotwords ----------

VOCAB_CHARS = 600  # ≈150-200 tokens: faster-whisper keeps at most half of its 448-token prompt for hotwords


def vocab(proj: dict | None, settings: dict, profile: dict | None = None) -> str:
    """Comma-separated hotwords: the creator's brand vocabulary first (it survives the length cap), then the channel,
    its tags and categories, then the active style profile's winning topics. Case-insensitive duplicates drop."""
    info = (proj or {}).get("source_info") or {}
    terms = [*re.split(r"[,;\n]+", settings.get("brand_vocab") or ""), info.get("channel") or "", *(info.get("tags") or []),
             *(info.get("categories") or []), *(((profile or {}).get("trained") or {}).get("winning_topics") or [])]
    out, seen = [], set()
    for t in terms:
        t = " ".join(str(t).split())
        if t and t.lower() not in seen and len(", ".join([*out, t])) <= VOCAB_CHARS:
            seen.add(t.lower())
            out.append(t)
    return ", ".join(out)


# ---------- markers ----------

VOICE_TRIGGERS = ("clip that", "clip it", "clip this", "that's a clip", "that is a clip")


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _norm(w: str) -> str:
    return re.sub(r"[^\w']+", "", str(w).lower().replace("’", "'"))


def voice_markers(segments: list[dict], phrases=None, gap: float = 10.0) -> list[dict]:
    """{t, kind: "voice"} where the creator says a clip phrase (comma-separated phrases, else VOICE_TRIGGERS) — the moment
    they want is the 30-60 s before it. Phrase words must follow within 2 s; repeats within gap seconds count once."""
    if isinstance(phrases, str):
        phrases = re.split(r"[,;\n]+", phrases)
    pats = [toks for p in (phrases or VOICE_TRIGGERS) if (toks := [n for w in str(p).split() if (n := _norm(w))])]
    words = []
    for s in segments:
        ws = s.get("words") or [{"s": s["start"], "w": w} for w in s.get("text", "").split()]  # old transcripts: line times
        words += [(float(w["s"]), n) for w in ws if (n := _norm(w["w"]))]
    out = []
    for i, (t, _) in enumerate(words):
        for toks in pats:
            seq = words[i:i + len(toks)]
            if [w for _, w in seq] == toks and all(b[0] - a[0] <= 2.0 for a, b in zip(seq, seq[1:])):
                if not out or t - out[-1]["t"] > gap:
                    out.append({"t": round(t, 2), "kind": "voice", "label": " ".join(toks), "strength": 1.0})
                break
    return out


def read_markers(folder) -> list[dict]:
    """The project's markers.json (a list, or {"markers": [...]}), cleaned: bad entries are dropped, strength 0..1."""
    try:
        raw = json.loads((Path(folder) / "markers.json").read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    out = []
    for m in (raw.get("markers") if isinstance(raw, dict) else raw) or []:
        t, kind = _num(m.get("t")) if isinstance(m, dict) else None, m.get("kind") if isinstance(m, dict) else None
        if t is None or t < 0 or not isinstance(kind, str) or not kind.strip():
            continue
        st = _num(m.get("strength"))
        x = {"t": round(t, 2), "kind": kind.strip()[:40], "label": str(m.get("label") or kind)[:80],
             "strength": 1.0 if st is None else min(max(st, 0.0), 1.0)}
        if (d := _num(m.get("d"))) and d > 0:
            x["d"] = round(d, 2)
        out.append(x)
    return out


def merge_markers(*lists) -> list[dict]:
    """One time-sorted list; the same kind again within 2 s (the sidecar and the transcript both caught it) counts once."""
    out = []
    for m in sorted((m for ms in lists for m in ms), key=lambda m: m["t"]):
        if not any(o["kind"] == m["kind"] and m["t"] - o["t"] <= 2.0 for o in out[-8:]):
            out.append(m)
    return out


# ---------- chat reactions ----------

EMOTES = {  # lowercase; single words match whole words, emoji and phrases match anywhere in the message
    "laugh": ("kekw", "lul", "lulw", "omegalul", "kek", "lol", "lmao", "lmfao", "rofl", "xd", "icant", "pepelaugh", "😂", "🤣", "💀"),
    "hype": ("pog", "pogu", "poggers", "pogchamp", "poggies", "w", "hype", "letsgo", "ez", "clutch", "gigachad", "insane", "🔥", "🐐"),
    "scary": ("monkas", "monkaw", "monkagiga", "scary", "jumpscare", "😱", "😨"),
    "sad": ("sadge", "f", "rip", "pepehands", "biblethump", "😭", "😢"),
    "cringe": ("cringe", "yikes", "😬"),
    "shock": ("wtf", "omg", "wut", "😳", "🤯", "😮"),
    "clip": ("clip", "clipit", "clipped", "clip it", "clip that"),
}
LAUGH_RE = re.compile(r"^(?:a?(?:ha){2,}h?|(?:he){2,}|lo+l|lmf?a+o+|xd+|kek+w?|(?:ja){2,}|(?:ks){2,}k?)$")
_WORD = re.compile(r"[\w']+")


def emote_table(channel: str = "", overrides: dict | None = None) -> dict:
    """{kind: (whole-word tokens, anywhere tokens)}: the defaults plus the creator's additions (setting "chat_emotes":
    {"*" or channel login: {kind: [tokens]}}) for every channel and for this one."""
    table = {k: set(v) for k, v in EMOTES.items()}
    over = overrides if overrides is not None else config.load().get("chat_emotes") or {}
    for key in ("*", (channel or "").strip().lower()):
        kinds = over.get(key) if isinstance(over, dict) else None
        for kind, toks in (kinds or {}).items() if isinstance(kinds, dict) else ():
            table.setdefault(str(kind), set()).update(str(t).lower() for t in _toks(toks) if str(t).strip())
    return {k: (frozenset(t for t in v if _WORD.fullmatch(t)), tuple(t for t in v if not _WORD.fullmatch(t))) for k, v in table.items()}


def classify(text: str, table: dict) -> set[str]:
    """The reaction kinds one chat message shows (a message can be both laughing and hype)."""
    low = str(text).lower()
    words = set(_WORD.findall(low))
    kinds = {k for k, (whole, anywhere) in table.items() if words & whole or any(t in low for t in anywhere)}
    if "laugh" in table and any(LAUGH_RE.match(w) for w in words):
        kinds.add("laugh")
    return kinds


def chat_kinds(messages, duration: float, table: dict, bin_s: int = 10) -> dict:
    """{kind: [messages per 10 s]} from (t, text) pairs; kinds nobody used are left out."""
    n, out = int(duration // bin_s) + 1, {}
    for t, text in messages:
        i = int(t // bin_s)
        if 0 <= i < n:
            for k in classify(text, table):
                out.setdefault(k, [0] * n)[i] += 1
    return out


_EMOTE_LINE = re.compile(r"^\s*(?:#(\w{1,25})\s+)?([a-z_]{2,20})\s*:\s*(\S.*)$", re.I)


def parse_emote_text(text: str) -> dict:
    """Settings text -> the "chat_emotes" setting. One line per kind: "laugh: myLaugh otherEmote" (every channel) or
    "#channel laugh: emote" (one channel). Lines that don't fit are ignored."""
    out: dict = {}
    for line in str(text or "").splitlines()[:200]:
        m = _EMOTE_LINE.match(line)
        if m:
            toks = [t.lower() for t in m.group(3).split()][:100]
            out.setdefault((m.group(1) or "*").lower(), {}).setdefault(m.group(2).lower(), []).extend(toks)
    return out


def _toks(v) -> list:
    return list(v) if isinstance(v, (list, tuple)) else []  # a hand-edited settings.json may hold anything


def emote_text(over: dict) -> str:
    return "\n".join(f"{'' if ch == '*' else f'#{ch} '}{kind}: {' '.join(map(str, _toks(toks)))}"
                     for ch, kinds in (over if isinstance(over, dict) else {}).items() if isinstance(kinds, dict)
                     for kind, toks in kinds.items() if _toks(toks))


def _emoji(e: dict) -> str:
    if not e.get("isCustomEmoji"):
        return e.get("emojiId") or ""
    return " " + str((e.get("shortcuts") or [""])[0]).strip(":").lstrip("_") + " "  # a channel emote: its name


def youtube_chat(path) -> list[tuple[float, str]]:
    """(t, text) per chat message in a yt-dlp live_chat.json (text runs and emoji, custom emotes by name)."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                a = json.loads(line)["replayChatItemAction"]
                t = int(a["videoOffsetTimeMsec"]) / 1000
                runs = a["actions"][0]["addChatItemAction"]["item"]["liveChatTextMessageRenderer"]["message"]["runs"]
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            text = " ".join("".join(r.get("text") or _emoji(r.get("emoji") or {}) for r in runs if isinstance(r, dict)).split())
            if text:
                out.append((t, text))
    return out


def _same_channel(a, b) -> bool:
    key = lambda s: re.sub(r"\W", "", str(s or "").lower())  # noqa: E731 — display names vs logins
    return bool(key(a)) and key(a) == key(b)


def _live_chat(proj: dict, src, dur: float, channel: str) -> list[tuple[float, str]]:
    """The captured live chat on this recording's clock: an OBS recording started at its file time minus its length;
    an imported VOD of the same channel at the stream start the downloader reported (project "recorded_at")."""
    start = None
    if (proj.get("origin") or {}).get("obs") and src:
        start = Path(src).stat().st_mtime - dur  # OBS wrote the file until the recording stopped (copied with its time)
    elif _num(proj.get("recorded_at")) and _same_channel(channel, (proj.get("source_info") or {}).get("channel")):
        start = float(proj["recorded_at"])
    return chatlog.messages(channel, start, start + dur) if start else []


# ---------- audio events ----------

TAGGER_MODEL = "sherpa-onnx-zipformer-small-audio-tagging-2024-04-15"
TAGGER_URL = ("https://huggingface.co/k2-fsa/sherpa-onnx-zipformer-small-audio-tagging-2024-04-15/resolve/"
              "c24f9d0fdf7d0b8c0c4f9733aa0cac51fda10c95/")  # a pinned revision, and each file's SHA-256 below
TAGGER_FILES = {"model.int8.onnx": "69304b8a1b96bbe6b7d16c24079f0732c65faf3568a14cb82a8238c709afe76c",
                "class_labels_indices.csv": "cdd1049833c4b86127c2773ac0d14a2754b6a6d0d1798002ed5c66e699708429"}
TAGGER_LICENCE = ("Apache-2.0 — zipformer audio tagger by the k2-fsa / icefall project, trained on AudioSet "
                  "(label names: AudioSet ontology, CC BY-SA 4.0)")
AUDIO_KINDS = {"Laughter": "laughter", "Baby laughter": "laughter", "Giggle": "laughter", "Belly laugh": "laughter",
               "Chuckle, chortle": "laughter", "Screaming": "scream", "Shout": "scream", "Yell": "scream",
               "Cheering": "cheering", "Whoop": "cheering", "Applause": "applause", "Gunshot, gunfire": "gunshot",
               "Machine gun": "gunshot", "Explosion": "explosion", "Boom": "explosion", "Crying, sobbing": "crying",
               "Music": "music"}
MIN_P = {"music": 0.5}  # other kinds: 0.3
SR, WIN, HOP, RADIUS, MAX_CENTERS = 16000, 2.0, 1.0, 10.0, 150  # ponytail: ±10 s around ≤150 spikes; a full-VOD pass later
_TAGGER: dict = {}
_tagger_lock = threading.Lock()


def _tagger():
    """The sherpa-onnx tagger (model fetched once, ~27 MB, checked against its SHA-256), or None when unavailable."""
    with _tagger_lock:
        if "tagger" in _TAGGER or "error" in _TAGGER:
            return _TAGGER.get("tagger")
        try:
            import sherpa_onnx
        except ImportError:
            _TAGGER["error"] = "sherpa-onnx isn't installed in this build"
            log.event("signals", "audio events unavailable: sherpa-onnx isn't installed")
            return None
        try:  # a failed download isn't remembered: the next analysis tries again
            path = {n: media.fetch_model(f"audio-tagger-{n}", TAGGER_URL + n, sha256=h) for n, h in TAGGER_FILES.items()}
            cfg = sherpa_onnx.AudioTaggingConfig(model=sherpa_onnx.AudioTaggingModelConfig(
                zipformer=sherpa_onnx.OfflineZipformerAudioTaggingModelConfig(model=str(path["model.int8.onnx"])),
                num_threads=2, provider="cpu"), labels=str(path["class_labels_indices.csv"]), top_k=10)
            _TAGGER["tagger"] = sherpa_onnx.AudioTagging(cfg)
        except Exception as e:
            log.event("signals", f"audio events skipped: {e}")
        return _TAGGER.get("tagger")


def tagger_status() -> dict:
    try:
        installed = importlib.util.find_spec("sherpa_onnx") is not None
    except (ImportError, ValueError, AttributeError):
        installed = False
    return {"available": installed, "model": TAGGER_MODEL, "licence": TAGGER_LICENCE, "size_mb": 27,
            "page": "https://huggingface.co/k2-fsa/sherpa-onnx-zipformer-small-audio-tagging-2024-04-15",
            "sha256": TAGGER_FILES["model.int8.onnx"], "model_ready": (config.DATA / "models" / "audio-tagger-model.int8.onnx").exists(),
            "error": _TAGGER.get("error", "")}


def audio_events(src, centers, duration: float) -> list[dict]:
    """[{t, kind, p}]: 2-s windows (1-s hop) within ±RADIUS of each center (the first MAX_CENTERS, by priority), one
    event per burst of the same kind at its most confident window. [] when the tagger is unavailable."""
    if not centers or not src:
        return []
    tagger = _tagger()
    if tagger is None:
        return []
    regions: list[list[float]] = []
    for c in sorted(centers[:MAX_CENTERS]):
        a, b = max(0.0, c - RADIUS), min(duration, c + RADIUS)
        if regions and a <= regions[-1][1]:
            regions[-1][1] = max(regions[-1][1], b)
        elif b - a >= WIN / 2:
            regions.append([a, b])
    win, hop, hits = int(WIN * SR), int(HOP * SR), []
    for a, b in regions:
        x = media.pcm(src, a, b - a)
        for k in range(max(0, len(x) - win) // hop + 1):
            seg = x[k * hop:k * hop + win]
            if len(seg) < SR // 2:
                break
            st = tagger.create_stream()
            st.accept_waveform(sample_rate=SR, waveform=seg)
            for r in tagger.compute(st):
                kind = AUDIO_KINDS.get(r.name)
                if kind and r.prob >= MIN_P.get(kind, 0.3):
                    hits.append((round(a + k * HOP + WIN / 2, 1), kind, round(float(r.prob), 3)))
    out = []
    for kind in sorted({h[1] for h in hits}):
        burst: list = []
        for h in sorted(h for h in hits if h[1] == kind):
            if burst and h[0] - burst[-1][0] > 2.0:
                out.append(max(burst, key=lambda x: x[2]))
                burst = []
            burst.append(h)
        out.append(max(burst, key=lambda x: x[2]))
    return [{"t": t, "kind": k, "p": p} for t, k, p in sorted(out)]


# ---------- all of it, after transcription ----------

def enrich(folder, proj: dict | None, src, dur: float, sig: dict, segments: list[dict], settings: dict | None = None) -> dict:
    """Add markers, chat_kinds (+ chat counts from the live log), viewer_clips and audio_events to sig in place.
    Each part is best-effort: a failure is logged and the analysis carries on without that signal."""
    s = settings if settings is not None else config.load()
    proj = proj or {}
    try:
        marks = merge_markers(voice_markers(segments, s.get("voice_triggers") or None), read_markers(folder) if folder else [])
        if marks:
            sig["markers"] = marks
    except Exception as e:
        log.event("signals", f"markers skipped: {e}")
    try:
        channel = (s.get("twitch_chat_channel") or "").strip().lower()
        replay = next(Path(folder).glob("*.live_chat.json"), None) if folder else None
        msgs = youtube_chat(replay) if replay else []
        live = _live_chat(proj, src, dur, channel) if channel else []
        if live and not sig.get("chat"):  # Twitch has no chat replay: the live capture gives the per-10 s counts
            sig["chat"] = [0] * (int(dur // 10) + 1)
            for t, _ in live:
                if 0 <= int(t // 10) < len(sig["chat"]):
                    sig["chat"][int(t // 10)] += 1
        kinds = chat_kinds(msgs + live, dur, emote_table(channel if live else (proj.get("source_info") or {}).get("channel") or "",
                                                         s.get("chat_emotes") or {}))
        if kinds:
            sig["chat_kinds"] = kinds
    except Exception as e:
        log.event("signals", f"chat reactions skipped: {e}")
    try:
        vod = twitch.vod_id(proj.get("source_url") or (proj.get("origin") or {}).get("url") or "")
        if vod and twitch.has_app():
            clips = twitch.clips_for_vod(vod)
            if clips:
                sig["viewer_clips"] = clips
    except Exception as e:
        log.event("signals", f"Twitch viewer clips skipped: {e}")
    try:
        if s.get("audio_events", True) and src:
            centers = ([m["t"] for m in sig.get("markers", [])] + [c["t"] + c["d"] / 2 for c in sig.get("viewer_clips", [])]
                       + [t + 5 for t in ai.chat_spikes(sig.get("chat") or [])] + [m["t"] for m in sig.get("loud", [])])
            ev = audio_events(src, centers, dur)
            if ev:
                sig["audio_events"] = ev
    except Exception as e:
        log.event("signals", f"audio events skipped: {e}")
    return sig
