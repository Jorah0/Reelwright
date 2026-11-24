"""Personal learning from the creator's own behaviour (no API key needed):

- features(): measurable traits of a clip (length, loudness, speech rate, energy spikes, cuts, chat, hook shape...)
- train()/rerank(): a small logistic-regression reranker fitted to the creator's feedback (👍/👎, posts, dismissals,
  real results: lift over the posting account's own normal). It is cross-validated and only steers picks when it beats
  always-guessing-the-majority. model_for(): the same per account or platform, shrunk to the global model.
- house_style(): the edit settings the creator keeps choosing on clips they like/publish → new clips start that way.
- trim_bias()/apply_trim(): how the creator re-trims AI picks (e.g. always cuts 1.5 s of preamble) → applied up front.
"""
import math
from collections import Counter

import numpy as np

from .media import BLEEP_MODES, CAPTION_ANIMS, LAYOUTS, REGION_LAYOUTS

FEATURES = ("length", "ai", "wps", "loud", "spikes", "cuts", "chat", "pos", "question", "lead", "motion", "flashes", "face_share",
            "markers", "viewer_clips", "chat_laugh", "chat_hype", "laugh")  # the last five: signals.enrich (WP1)
NEUTRAL = {"motion": 1.0,  # value for a feature a lesson predates (same as a clip without that signal), so it isn't learned
           "markers": 0.0, "viewer_clips": 0.0, "chat_laugh": 1.0, "chat_hype": 1.0, "laugh": 0.0}
WH = {"what", "why", "how", "who", "when", "where", "which", "did", "have", "ever", "guess"}
WHY_WEIGHT = {"rated": 1.0, "published": 0.6, "dismissed": 0.4}
STYLE_KEYS = ("aspect", "fit", "layout", "caption_style", "caption_pos", "captions", "remove_silence", "loudnorm", "denoise",
              "caption_anim", "caption_emphasis", "music_duck", "bleep")  # the last four (WP2): lessons without them keep defaults
NUM_KEYS = ("zoom", "music_volume", "speed")
STYLE_VALUES = {"aspect": {"source", "9:16", "1:1", "4:5", "16:9"}, "fit": {"crop", "blur"}, "layout": set(LAYOUTS),
                "caption_style": {"bold", "clean", "boxed", "neon"}, "caption_pos": {"low", "middle", "high"},
                "caption_anim": set(CAPTION_ANIMS), "bleep": set(BLEEP_MODES)}
NUM_RANGES = {"zoom": (1.0, 4.0), "music_volume": (0.0, 2.0), "speed": (0.25, 4.0)}


def num(v) -> float | None:
    """A finite real number, else None (bools, strings, NaN/inf and nested data are rejected)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def clean_edit(e) -> dict:
    """Only edit settings learning uses, each a valid value: anything else could poison the learned house style."""
    if not isinstance(e, dict):
        return {}
    out: dict = {}
    for k in STYLE_KEYS:
        v = e.get(k)
        if k in STYLE_VALUES and isinstance(v, str) and v in STYLE_VALUES[k] or k not in STYLE_VALUES and isinstance(v, bool):
            out[k] = v
    for k, (lo, hi) in NUM_RANGES.items():
        v = num(e.get(k))
        if v is not None and lo <= v <= hi:
            out[k] = v
    return out


def _rel(series, start: float, end: float, per_s: float) -> float:
    """Mean of a per-time series over [start, end) relative to its whole-video median (1.0 = typical)."""
    if not series:
        return 1.0
    x = np.asarray(series, dtype=np.float32)
    part = x[int(start * per_s):max(int(end * per_s), int(start * per_s) + 1)]
    med = float(np.median(x)) or float(x.mean()) or 1e-3
    return round(min(float(part.mean()) / med, 6.0), 3) if part.size else 1.0


def features(clip: dict, segments: list[dict], signals: dict, duration: float) -> dict:
    s, e = float(clip["start"]), float(clip["end"])
    span = max(e - s, 0.1)
    segs = [x for x in segments if s <= x["start"] < e]
    words = sum(len(x["text"].split()) for x in segs)
    first = segs[0] if segs else None
    opener = first["text"].strip().lower() if first else ""
    question = bool(opener) and (opener.endswith("?") or opener.split(" ", 1)[0].strip(",.!?") in WH)
    vis = signals.get("visual") or {}
    on_cam = np.asarray(vis.get("face") or [], dtype=np.float32)[int(s):max(int(e), int(s) + 1)]  # per second
    return {
        "length": span / 60,
        "ai": (clip.get("score") or 0) / 100,
        "wps": words / span / 4,
        "loud": _rel(signals.get("waveform"), s, e, 10),
        "spikes": sum(s <= m["t"] < e for m in signals.get("loud", [])) * 60 / span / 4,
        "cuts": sum(s <= t < e for t in signals.get("scenes", [])) * 60 / span / 10,
        "chat": _rel(signals.get("chat"), s, e, 0.1),
        "pos": s / max(duration, 1.0),
        "question": float(question),
        "lead": ((first["start"] - s) if first else span) / 5,
        "motion": _rel(vis.get("motion"), s, e, 1),
        "flashes": sum(s <= m["t"] < e for m in vis.get("spikes", []) if m.get("kind") == "flash") * 60 / span / 4,
        "face_share": round(float(on_cam.mean()), 3) if on_cam.size else 0.0,
        # signals.enrich: a "clip that" said up to 15 s after the end still counts; viewer clips overlapping the clip
        "markers": float(min(3, sum(s <= m["t"] <= e + 15 for m in signals.get("markers", [])))),
        "viewer_clips": float(min(5, sum(c["t"] < e and c["t"] + c.get("d", 0) > s for c in signals.get("viewer_clips", [])))),
        "chat_laugh": _rel((signals.get("chat_kinds") or {}).get("laugh"), s, e, 0.1),
        "chat_hype": _rel((signals.get("chat_kinds") or {}).get("hype"), s, e, 0.1),
        "laugh": sum(s <= x["t"] < e for x in signals.get("audio_events", []) if x.get("kind") == "laughter") * 60 / span / 4,
    }


def samples(feedback: list[dict]):
    """(X, y, weights). Explicit ratings weigh most; a published clip's label follows its results when known: its lift
    (views against that account's own posts of the same age, see metrics.lifts) or, for lessons from before lift, its
    views against the median of the other such lessons."""
    views = [num(f.get("views")) for f in feedback if f.get("why") == "published" and num(f.get("lift")) is None]
    views = [v for v in views if v is not None]
    med = float(np.median(views)) if len(views) >= 3 else None
    X, y, w = [], [], []
    for f in feedback:
        feats = f.get("features")
        if not isinstance(feats, dict) or not feats or f.get("signal") not in (1, -1):
            continue
        label, weight = (1.0 if f["signal"] > 0 else 0.0), WHY_WEIGHT.get(f.get("why"), 0.5)
        v, lf = num(f.get("views")), num(f.get("lift"))
        if f.get("why") == "published" and lf is not None:
            label, weight = (1.0 if lf >= 0 else 0.0), 1.0
        elif f.get("why") == "published" and med is not None and v is not None:
            label, weight = (1.0 if v >= med else 0.0), 1.0
        X.append([val if (val := num(feats.get(k))) is not None else NEUTRAL.get(k, 0.0) for k in FEATURES])
        y.append(label)
        w.append(weight)
    return np.asarray(X, dtype=np.float64).reshape(-1, len(FEATURES)), np.asarray(y), np.asarray(w)


def _fit(X, y, w, l2: float = 1.0, iters: int = 400, lr: float = 0.5, prior=None):
    """prior = (W0, b0): start there and pull the weights back to it with strength l2 (pseudo-samples) instead of to 0."""
    W, b, sw = np.zeros(X.shape[1]), 0.0, w.sum()
    W0, b0, den, pull = np.zeros(X.shape[1]), 0.0, sw, 0.0
    if prior is not None:
        W0, b0, den, pull = np.asarray(prior[0], dtype=np.float64), float(prior[1]), sw + l2, l2  # stable even for one lesson
        W, b = W0.copy(), b0
    for _ in range(iters):  # ponytail: plain gradient descent; fine for ≤ a few thousand samples
        p = 1 / (1 + np.exp(-np.clip(X @ W + b, -30, 30)))
        g = (p - y) * w
        W -= lr * (X.T @ g + l2 * (W - W0)) / den
        b -= lr * (g.sum() + pull * (b - b0)) / den
    return W, b


SHRINK = 12.0  # a per-account model is pulled to the global one as if by this many lessons: little data, little change


def train(feedback: list[dict], folds: int = 5, prior: dict | None = None) -> dict | None:
    """The reranker. With prior (the global model) any number of lessons works: they are standardised the global way and
    shrunk to its weights, and the result is useful when the global one is, or when its own cross-validation says so."""
    X, y, w = samples(feedback)
    pos = int(y.sum())
    enough = len(y) >= 12 and min(pos, len(y) - pos) >= 4
    if not (enough or (prior and len(y))):
        return None
    if prior:
        mu, sd, l2, start = np.asarray(prior["mu"]), np.asarray(prior["sd"]), SHRINK, (prior["w"], prior["b"])
    else:
        mu, sd, l2, start = X.mean(0), X.std(0), 1.0, None
        sd[sd < 1e-6] = 1.0
    Z = (X - mu) / sd
    cv_acc, baseline = None, max(y.mean(), 1 - y.mean())
    if enough:
        hits = 0
        for k in range(folds):  # honest accuracy: score each sample with a model that never saw it
            test = np.arange(len(y)) % folds == k
            W, b = _fit(Z[~test], y[~test], w[~test], l2, prior=start)
            hits += int(((Z[test] @ W + b > 0) == (y[test] > 0.5)).sum())
        cv_acc = hits / len(y)
    W, b = _fit(Z, y, w, l2, prior=start)
    own = cv_acc is not None and cv_acc >= baseline + 0.05
    return {"mu": mu.tolist(), "sd": sd.tolist(), "w": W.tolist(), "b": float(b), "n": int(len(y)), "positive": pos,
            "cv_acc": round(cv_acc, 3) if cv_acc is not None else None, "baseline": round(float(baseline), 3),
            "useful": bool(own or (prior and prior.get("useful"))), "shrunk": bool(prior),
            "weights": {k: round(float(v), 3) for k, v in zip(FEATURES, W)}}


def scoped(feedback: list[dict], account_id: str | None = None, platform: str | None = None) -> list[dict]:
    """Lessons from posts to that account and/or platform (lessons from before accounts carry neither)."""
    return [f for f in feedback if (account_id is None or f.get("account_id") == account_id)
            and (platform is None or f.get("platform") == platform)]


def model_for(feedback: list[dict], prior: dict | None, account_id: str | None = None, platform: str | None = None) -> dict | None:
    """One account's (or platform's) taste: its own lessons shrunk to the global model, which it falls back to when it
    has none."""
    own = scoped(feedback, account_id, platform)
    if not own:
        return prior
    return train(own, prior=prior) if prior else train(own)


def predict(model: dict, feats: dict) -> float:
    z = (np.asarray([float(feats.get(k, NEUTRAL.get(k, 0.0))) for k in FEATURES]) - np.asarray(model["mu"])) / np.asarray(model["sd"])
    return float(1 / (1 + np.exp(-np.clip(z @ np.asarray(model["w"]) + model["b"], -30, 30))))


def rerank(clips: list[dict], model: dict | None, blend: float = 0.35) -> list[dict]:
    """Blend the AI's score with how well each clip fits this creator's learned taste, then sort best first."""
    if not model or not model.get("useful"):
        return clips
    for c in clips:
        if c.get("features"):
            c["fit"] = round(predict(model, c["features"]) * 100)
            c["ai_score"] = c.get("score", 0)
            c["score"] = round((1 - blend) * c["ai_score"] + blend * c["fit"])
    return sorted(clips, key=lambda c: -c.get("score", 0))


def house_style(feedback: list[dict], min_n: int = 3, agree: float = 0.6) -> dict:
    """Edit settings the creator consistently uses on clips they liked or posted (needs real agreement)."""
    edits = [clean_edit(f["edit"]) for f in feedback if f.get("signal") == 1 and isinstance(f.get("edit"), dict)]
    edits = [e for e in edits if e]
    if len(edits) < min_n:
        return {}
    out = {}
    for k in STYLE_KEYS:
        vals = [e[k] for e in edits if k in e]
        if len(vals) >= min_n:
            v, n = Counter(vals).most_common(1)[0]
            if n / len(vals) >= agree and not (k == "layout" and v in REGION_LAYOUTS):  # new clips have no regions
                out[k] = v
    for k in NUM_KEYS:
        vals = [float(e[k]) for e in edits if isinstance(e.get(k), (int, float)) and not isinstance(e.get(k), bool)]
        if len(vals) >= min_n:
            m = float(np.median(vals))
            if sum(abs(v - m) <= 0.1 * max(abs(m), 0.1) for v in vals) / len(vals) >= agree:
                out[k] = round(m, 3)
    return out


def _pair(t) -> bool:
    return isinstance(t, list) and len(t) == 2 and all(num(x) is not None for x in t)


def _retention(feedback: list[dict]) -> list[dict]:
    """Retention summaries (metrics.retention) of posts that have one."""
    return [r for f in feedback if isinstance(r := f.get("retention"), dict) and r.get("available")]


def trim_bias(feedback: list[dict], min_n: int = 5) -> dict | None:
    """Median seconds the creator moves AI start/end on clips they kept (positive start = starts later). Where real
    viewers left a post (its retention curve's drop-offs) counts as one more re-trim of that post."""
    trims = [f["trim"] for f in feedback if f.get("signal") == 1 and _pair(f.get("trim"))]
    trims += [r["trim"] for r in _retention(feedback) if _pair(r.get("trim")) and any(r["trim"])]  # ponytail: a drop-off
    # counts as one more re-trim, though it is relative to the posted cut rather than the AI's; fine while both are small
    if len(trims) < min_n:
        return None
    return {"start": round(float(np.median([t[0] for t in trims])), 2),
            "end": round(float(np.median([t[1] for t in trims])), 2), "n": len(trims)}


def apply_trim(clips: list[dict], bias: dict | None, cap: float = 5.0) -> list[dict]:
    """Shift AI bounds by the learned bias; ai_bounds keeps the unshifted pick so later trims stay measurable."""
    if not bias:
        return clips
    ds, de = (max(-cap, min(cap, bias[k])) for k in ("start", "end"))
    return [{**c, "ai_bounds": [c["start"], c["end"]], "start": round(c["start"] + ds, 2), "end": round(c["end"] + de, 2)}
            for c in clips]


def hints(feedback: list[dict], model: dict | None = None, video_type: str | None = None) -> str:
    """Plain-language lessons for the clip-finder prompt. With the video's type, lessons from the same kind of video
    (feedback rows carry "video_type"; older rows have none and only count overall) add their own line."""
    liked = [num(f.get("length")) for f in feedback if f.get("signal") == 1]
    liked = [v for v in liked if v]
    out = []
    if len(liked) >= 5:
        out.append(f"Clips this creator keeps are typically {np.percentile(liked, 25):.0f}-{np.percentile(liked, 75):.0f}s long.")
    same = [f for f in feedback if video_type and f.get("video_type") == video_type and f.get("signal") in (1, -1)]
    kept = [v for f in same if f["signal"] == 1 and (v := num(f.get("length")))]
    if len(kept) >= 3:  # ponytail: length + verdict split only; a per-type reranker needs far more lessons per type
        out.append(f"In {video_type.replace('_', ' ')} videos they kept {sum(f['signal'] == 1 for f in same)} and rejected "
                   f"{sum(f['signal'] == -1 for f in same)} picks; "
                   f"kept ones run {np.percentile(kept, 25):.0f}-{np.percentile(kept, 75):.0f}s.")
    ret = [r for r in _retention(feedback) if num(r.get("hold_3s")) is not None and num(r.get("drop_s")) is not None]
    if len(ret) >= 3:
        drop, hold = np.median([r["drop_s"] for r in ret]), np.median([r["hold_3s"] for r in ret])
        out.append(f"Real viewers of their posts drop off most around {drop:.0f}s in, and {hold:.0%} are still watching at 3s: "
                   f"land the hook in the first {max(1.0, min(3.0, drop - 0.5)):.0f}s.")
    if model and model["useful"]:
        names = {"loud": "loud/high-energy audio", "wps": "fast speech", "spikes": "energy spikes", "cuts": "frequent scene cuts",
                 "chat": "chat activity", "question": "opening with a question", "lead": "a slow start before speech",
                 "length": "longer clips", "pos": "moments late in the video", "ai": "your own high scores",
                 "motion": "lots of on-screen action", "flashes": "on-screen flashes", "face_share": "the streamer on camera"}
        top = sorted(model["weights"].items(), key=lambda kv: -abs(kv[1]))[:3]
        out.append("Learned from their feedback: they favour " + "; ".join(
            f"{'more' if v > 0 else 'less'} {names.get(k, k)}" for k, v in top) + ".")
    return " ".join(out)
