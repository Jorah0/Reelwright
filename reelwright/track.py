"""Subject tracking for reframed clips: find the streamer / VTuber through the clip, lock onto them, smooth the path like a
camera operator, and turn it into a frame-accurate ffmpeg crop expression.

Path points are [t, cx, cy, w, h]: t in seconds from the clip start, the rest fractions of the source frame."""
import math
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import log, media

RATE = 4.0            # detection samples per second
MAX_SAMPLES = 600     # long clips sample less often instead of taking forever
DEAD = 0.15           # hold still while the subject stays in the central 15% of the crop
MAX_SPEED = 0.5       # fastest pan, in source-frame widths per second
SMOOTH_TIME = 0.35    # s, critically damped easing toward the subject
MAX_JUMP = 0.25       # a detection this far from the locked subject is someone else...
REACQUIRE = 1.5       # ...unless the subject has been missing this long (s)
BLEND = 1.5           # manual keyframes pull the path within ± this many seconds
MAX_KEYS = 160        # bounds the ffmpeg expression (~35 chars a keyframe)
# MIT; pinned to a commit (master on 2026-11-25) and its SHA-256
ANIME_URL = "https://raw.githubusercontent.com/nagadomi/lbpcascade_animeface/4433ab1ae1166ea75acfe99eb0f18709dac329a0/lbpcascade_animeface.xml"
ANIME_SHA256 = "9376d30ac38db6bda2a68b88b3b76bbd7e6aa33af47f7f5c76bc88ca75f1ce30"


# ---------- detection ----------

def _detectors(w: int, h: int):
    """(YuNet real faces, anime-face cascade); either is None when its model can't be loaded (offline first run)."""
    import cv2

    yunet = anime = None
    try:
        yunet = media.face_detector(w, h)
    except Exception:
        log.error("track", "face model unavailable")
    cascade = getattr(cv2, "CascadeClassifier", None)
    if cascade is None:  # ponytail: OpenCV 5 moved cascades to opencv-contrib; without it motion covers VTubers
        return yunet, None
    try:
        path = media.fetch_model("lbpcascade_animeface.xml", ANIME_URL, sha256=ANIME_SHA256)
        anime = cascade(str(path))
        if anime.empty():
            path.unlink(missing_ok=True)  # corrupt download: fetch fresh next time
            anime = None
    except Exception:
        log.error("track", "anime face model unavailable")
    return yunet, anime


def _cand(x, y, w, h, score, source, fw, fh) -> dict:
    return {"cx": float(x + w / 2) / fw, "cy": float(y + h / 2) / fh, "w": float(w) / fw, "h": float(h) / fh,
            "score": float(score), "source": source}


def motion(prev, cur, thresh: int = 25) -> dict | None:
    """The moving region between two blurred gray frames (a full-body VTuber or a face-less streamer still moves)."""
    import cv2

    mask = cv2.absdiff(prev, cur) > thresh
    frac = float(mask.mean())
    if frac < 0.002 or frac > 0.5:  # nothing moved, or everything did (camera move / cut): no subject signal
        return None
    ys, xs = np.nonzero(mask)
    x0, x1 = np.percentile(xs, [5, 95])
    y0, y1 = np.percentile(ys, [5, 95])
    H, W = mask.shape
    return {"cx": float(xs.mean()) / W, "cy": float(ys.mean()) / H, "w": float(x1 - x0 + 1) / W, "h": float(y1 - y0 + 1) / H,
            "score": 0.3, "source": "motion"}


_saliency = None


def salient(frame, top: int = 3) -> list[dict]:
    """The most salient blobs (spectral-residual saliency): "important components" when nobody's face is visible.
    Several are returned so the subject lock can stay on the one it was already following."""
    import cv2

    global _saliency
    if not hasattr(cv2, "saliency"):  # plain (non-contrib) OpenCV: motion covers it
        return []
    if _saliency is None:
        _saliency = cv2.saliency.StaticSaliencySpectralResidual_create()
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (128, max(2, 128 * h // w)))
    ok, m = _saliency.computeSaliency(small)
    if not ok:
        return []
    m = cv2.GaussianBlur(m.astype(np.float32), (5, 5), 0)
    mask = (m > m.mean() + 2 * m.std()).astype(np.uint8)  # spectral residual lights up edges: close them into one object
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    weight = np.bincount(labels.ravel(), weights=m.ravel(), minlength=n)
    H, W = m.shape
    blobs = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= 0.002 * H * W]
    total = sum(weight[i] for i in blobs) or 1.0
    return [_cand(*stats[i, :4], 0.5 * weight[i] / total, "salient", W, H)
            for i in sorted(blobs, key=lambda i: -weight[i])[:top]]


def candidates(frame, prev_gray, yunet, anime, mode: str = "auto"):
    """Subjects in one BGR frame from the first source that finds any: real + anime faces, then (auto mode) salient
    components, then the moving region. Returns (candidates, gray frame for the next motion diff)."""
    import cv2

    h, w = frame.shape[:2]
    out = []
    if yunet is not None:
        _, faces = yunet.detect(frame)
        out += [_cand(*f[:4], f[14], "face", w, h) for f in (faces if faces is not None else [])]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if anime is not None:
        out += [_cand(*r, 0.6, "anime", w, h) for r in anime.detectMultiScale(cv2.equalizeHist(gray), 1.1, 5, minSize=(24, 24))]
    small = cv2.GaussianBlur(cv2.resize(gray, (160, max(2, 160 * h // w))), (5, 5), 0)
    if not out and mode == "auto":
        out = salient(frame)
        if not out and prev_gray is not None:
            m = motion(prev_gray, small)
            out = [m] if m else []
    return out, small


# ---------- a picked target ----------

LOCAL_OK, GLOBAL_OK, LOST = 0.5, 0.6, 0.3  # template-match confidence: keep / re-acquire / tracker drifted off


def _new_tracker(frame, box):
    """CSRT (else KCF) from whichever namespace this OpenCV build has, initialised on box; None without contrib trackers."""
    import cv2

    for mod in (cv2, getattr(cv2, "legacy", None)):
        for name in ("TrackerCSRT_create", "TrackerKCF_create"):
            make = getattr(mod, name, None)
            if make:
                try:
                    t = make()
                    t.init(frame, tuple(int(v) for v in box))
                    return t
                except cv2.error:
                    continue
    return None


def _crop(frame, box):
    x, y, w, h = (int(v) for v in box)
    return frame[max(y, 0):y + h, max(x, 0):x + w]


def _match(frame, tmpl, box, local: bool):
    """Best multi-scale template match ((x, y, w, h), confidence) near box (local) or anywhere in the frame."""
    import cv2

    H, W = frame.shape[:2]
    x, y, w, h = box
    x0, y0, x1, y1 = (max(0, x - w), max(0, y - h), min(W, x + 2 * w), min(H, y + 2 * h)) if local else (0, 0, W, H)
    roi, base, best = frame[int(y0):int(y1), int(x0):int(x1)], w / tmpl.shape[1], (box, -1.0)
    for s in (0.8, 0.9, 1.0, 1.1, 1.25):
        t = cv2.resize(tmpl, (max(4, round(tmpl.shape[1] * base * s)), max(4, round(tmpl.shape[0] * base * s))))
        if t.shape[0] > roi.shape[0] or t.shape[1] > roi.shape[1]:
            continue
        _, mx, _, loc = cv2.minMaxLoc(np.nan_to_num(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED)))
        if mx > best[1]:
            best = ((int(x0) + loc[0], int(y0) + loc[1], t.shape[1], t.shape[0]), float(mx))
    return best


def _likeness(frame, box, tmpl) -> float:
    import cv2

    patch = _crop(frame, box)
    if patch.size == 0:
        return 0.0
    if tmpl.std() < 5:  # a flat-coloured target can't be scored by correlation: trust the tracker
        return 1.0
    r = cv2.matchTemplate(cv2.resize(patch, (tmpl.shape[1], tmpl.shape[0])), tmpl, cv2.TM_CCOEFF_NORMED)
    return float(np.nan_to_num(r).max())


def _follow_box(frames, i0: int, box, step: int) -> dict:
    """Follow box from frame i0 in one direction: a CSRT/KCF tracker checked against the picked look (else template
    matching near the last position with a slowly refreshed template); when lost, search the whole frame for the
    original look. Frames where it stays lost are left out, so the path interpolates over them instead of jumping."""
    import cv2

    tmpl0 = _crop(frames[i0], box).copy()
    tmpl, out = tmpl0.copy(), {}
    tracker = _new_tracker(frames[i0], box)
    use_tracker = tracker is not None
    for i in range(i0 + step, len(frames) if step > 0 else -1, step):
        f, got = frames[i], None
        if use_tracker:
            ok, b = tracker.update(f)
            b = tuple(int(v) for v in b)
            if ok and b[2] > 2 and b[3] > 2 and _likeness(f, b, tmpl0) >= LOST:
                got = b
        else:
            b, conf = _match(f, tmpl, box, local=True)
            if conf >= LOCAL_OK:
                got = b
                tmpl = cv2.addWeighted(tmpl, 0.9, cv2.resize(_crop(f, b), (tmpl.shape[1], tmpl.shape[0])), 0.1, 0)
        if got is None:
            b, conf = _match(f, tmpl0, box, local=False)
            if conf >= GLOBAL_OK:
                got = b
                if use_tracker:
                    tracker = _new_tracker(f, b) or tracker
        if got is not None:
            out[i] = box = got
    return out


def target_frames(frames, rate: float, target: dict) -> list:
    """The user's picked box ({t, x, y, w, h}: t from the clip start, the rest frame fractions), followed forward and
    backward from its frame -> [(t, [candidate] or [])] for follow()."""
    frames = list(frames)
    if not frames:
        return []
    H, W = frames[0].shape[:2]
    i0 = min(max(round(target["t"] * rate), 0), len(frames) - 1)
    box = (round(target["x"] * W), round(target["y"] * H), max(4, round(target["w"] * W)), max(4, round(target["h"] * H)))
    boxes = {i0: box, **_follow_box(frames, i0, box, 1), **_follow_box(frames, i0, box, -1)}
    return [(i / rate, [_cand(*boxes[i], 1.0, "target", W, H)] if i in boxes else []) for i in range(len(frames))]


# ---------- subject lock ----------

def associate(cands: list[dict], prev) -> dict | None:
    """The candidate that is our subject: nearest the previous position (bigger / surer ones win ties), or with no lock yet
    the most prominent one."""
    if not cands:
        return None
    if prev is None:
        return max(cands, key=lambda c: c["w"] * c["h"] * c["score"])
    return min(cands, key=lambda c: math.hypot(c["cx"] - prev[0], c["cy"] - prev[1]) - 0.3 * math.sqrt(c["w"] * c["h"]) * c["score"])


def follow(frames, cuts) -> list[list]:
    """frames = [(t, candidates)] -> raw path [[t, cx, cy, w, h, source]]. A lone far detection right after the subject was
    seen is someone else and is skipped (the gap gets interpolated); the lock resets at scene cuts and after long gaps."""
    out, prev, seen, last_t = [], None, -1e9, -1e9
    for t, cands in frames:
        if any(last_t < cut <= t for cut in cuts):
            prev = None  # new shot: re-acquire
        last_t = t
        c = associate(cands, prev)
        if c is None:
            continue
        if prev is not None and t - seen <= REACQUIRE and math.hypot(c["cx"] - prev[0], c["cy"] - prev[1]) > MAX_JUMP:
            continue
        prev, seen = (c["cx"], c["cy"]), t
        out.append([t, c["cx"], c["cy"], c["w"], c["h"], c["source"]])
    return out


# ---------- camera operator ----------

def _damp(cur, target, vel, dt):
    """Critically damped spring step (Game Programming Gems 4 'SmoothDamp'), then a hard pan-speed cap."""
    omega = 2 / SMOOTH_TIME
    x = omega * dt
    k = 1 / (1 + x + 0.48 * x * x + 0.235 * x * x * x)
    change = cur - target
    temp = (vel + omega * change) * dt
    vel = (vel - omega * temp) * k
    nxt = target + (change + temp) * k
    step = max(min(nxt - cur, MAX_SPEED * dt), -MAX_SPEED * dt)
    return cur + step, (vel if step == nxt - cur else step / dt)


def _thin(pts: list[list], eps: float = 0.002, gap: float = 0.5) -> list[list]:
    """Drop points the straight line between their neighbours already reproduces, then keep at most one per `gap` s."""
    if len(pts) <= 2:
        return pts
    kept, k = [pts[0]], 0
    for i in range(1, len(pts) - 1):
        a, b = pts[k], pts[i + 1]  # would the line from the last kept point to the next one still cover everything between?
        if any(abs(a[j] + (b[j] - a[j]) * (p[0] - a[0]) / ((b[0] - a[0]) or 1) - p[j]) > eps
               for p in pts[k + 1:i + 1] for j in (1, 2)):
            kept.append(pts[i])
            k = i
    kept.append(pts[-1])
    out = [kept[0]]
    for p in kept[1:-1]:
        if p[0] - out[-1][0] >= gap:
            out.append(p)
    return out + [kept[-1]]


def smooth(raw: list[list], cuts, dur: float, crop_w: float = 1.0, crop_h: float = 1.0, dead: float = DEAD,
           step: float = 1 / RATE) -> list[list]:
    """Raw detections -> the camera path: gaps interpolated, a dead zone (no motion while the subject stays in the central
    `dead` of the crop), eased pans capped at MAX_SPEED, and an instant reset at each scene cut. ≤ ~2 points/s."""
    if not raw:
        return []
    bounds = [0.0, *sorted(c for c in cuts if 0 < c < dur), dur]
    out, last = [], raw[0][1:5]
    for a, b in zip(bounds, bounds[1:]):
        seg = [p for p in raw if a <= p[0] < b] or [[a, *last]]
        ts = list(np.arange(a, b, step)) + [b - 0.01 if b < dur else b]  # end just before a cut so the cut is a cut
        vals = [np.interp(ts, [p[0] for p in seg], [p[j] for p in seg]) for j in (1, 2, 3, 4)]
        cam, vel, dz = [vals[0][0], vals[1][0]], [0.0, 0.0], (dead / 2 * crop_w, dead / 2 * crop_h)
        pts = []
        for i, t in enumerate(ts):
            if i:
                for j in (0, 1):
                    diff = vals[j][i] - cam[j]
                    want = cam[j] + diff - max(min(diff, dz[j]), -dz[j])  # only the part outside the dead zone
                    cam[j], vel[j] = _damp(cam[j], want, vel[j], ts[i] - ts[i - 1])
            pts.append([round(float(t), 3), round(cam[0], 4), round(cam[1], 4), round(float(vals[2][i]), 4), round(float(vals[3][i]), 4)])
        last = pts[-1][1:5]
        out += _thin(pts)
    return out


# ---------- manual keyframes ----------

def at(pts: list[list], t: float):
    """(cx, cy) on a piecewise-linear path at time t (held flat beyond its ends), or None for an empty path."""
    if not pts:
        return None
    ts = [p[0] for p in pts]
    return float(np.interp(t, ts, [p[1] for p in pts])), float(np.interp(t, ts, [p[2] for p in pts]))


def _knots(kfs: list[tuple], blend: float) -> list[tuple]:
    """[(t, offset)] -> knots of a piecewise-linear offset that is exact at each keyframe and fades to 0 over ± blend
    (straight across keyframes closer than 2·blend)."""
    knots = []
    for i, (t, off) in enumerate(kfs):
        if i == 0:
            knots.append((t - blend, 0.0))
        elif t - kfs[i - 1][0] > 2 * blend:
            knots += [(kfs[i - 1][0] + blend, 0.0), (t - blend, 0.0)]
        knots.append((t, off))
    return knots + [(kfs[-1][0] + blend, 0.0)] if kfs else []


def _offset(knots, t: float) -> float:
    return float(np.interp(t, [k[0] for k in knots], [k[1] for k in knots])) if knots else 0.0


def merge(pts: list[list], manual: list[list], base=(0.5, 0.5), blend: float = BLEND, zoom: float = 1.0) -> list[list]:
    """The AI path with manual [t, cx, cy] / [t, cx, cy, zoom] keyframes applied: exact at each keyframe, blending back
    to the AI path (and to `zoom`) over ± `blend` s. Returns [[t, cx, cy, zoom]]."""
    man = sorted(manual or [])
    xs, ys, zs = [], [], []
    for m in man:
        ax, ay = at(pts, m[0]) or base
        xs.append((m[0], m[1] - ax))
        ys.append((m[0], m[2] - ay))
        if len(m) > 3:
            zs.append((m[0], m[3] - zoom))
    kx, ky, kz = _knots(xs, blend), _knots(ys, blend), _knots(zs, blend)
    out = []
    for t in sorted({*(p[0] for p in pts), *(k[0] for k in kx), *(k[0] for k in kz)}):
        ax, ay = at(pts, t) or base
        out.append([t, min(max(ax + _offset(kx, t), 0.0), 1.0), min(max(ay + _offset(ky, t), 0.0), 1.0),
                    min(max(zoom + _offset(kz, t), 1.0), 4.0)])
    return out


# ---------- rendering ----------

def crop_path(pts: list[list], sw: int, sh: int, ratio: float, zoom: float = 1.0):
    """Path -> (crop w, crop h, [(t, x, y)]) in source pixels, each keyframe clamped like media.crop_box."""
    w, h, _, _ = media.crop_box(sw, sh, ratio, 0.5, 0.5, zoom)
    return w, h, [(p[0], *media.crop_box(sw, sh, ratio, p[1], p[2], zoom)[2:]) for p in pts]


def _f(v: float, nd: int) -> str:
    s = f"{v:.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def ffmpeg_expr(keys: list[tuple], max_keys: int = MAX_KEYS, var: str = "t", eps: float = 0.5) -> str:
    """[(t, v)] -> a piecewise-linear expression of ffmpeg's time variable `var`, as a flat sum
    v0 + Σ slope·clip(var − tᵢ, 0, Δtᵢ) (flat so ffmpeg's expression parser never nests deeply; held at the end values
    outside the keyframes). Keyframes the line through their neighbours reproduces within `eps` are dropped."""
    keys = sorted(keys)
    if not keys:
        return "0"
    if len(keys) > 2:
        kept = [keys[0]]
        for i in range(1, len(keys) - 1):
            (t0, v0), (t1, v1) = kept[-1], keys[i + 1]
            if abs(v0 + (v1 - v0) * (keys[i][0] - t0) / ((t1 - t0) or 1) - keys[i][1]) > eps:
                kept.append(keys[i])
        keys = kept + [keys[-1]]
    if len(keys) > max_keys:  # ponytail: uniform decimation, fine for ≤2 pts/s paths; a smarter simplifier if long clips look off
        n = math.ceil(len(keys) / max_keys)
        keys = keys[::n] + ([keys[-1]] if (len(keys) - 1) % n else [])
    if max(v for _, v in keys) - min(v for _, v in keys) < eps:
        return _f(keys[0][1], 4)
    terms = [_f(keys[0][1], 4)]
    for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
        if v1 == v0:
            continue
        if t1 - t0 <= 1e-6:
            terms.append(f"{v1 - v0:+.4f}*gte({var},{_f(t0, 3)})")  # a hard step
        else:
            terms.append(f"{(v1 - v0) / (t1 - t0):+.4f}*clip({var}-{_f(t0, 3)},0,{_f(t1 - t0, 3)})")
    return "".join(terms)


def crop_filter(edit: dict, sw: int, sh: int, ratio: float, out: tuple[int, int] | None = None, fps: float = 30) -> str | None:
    """Filters that make the crop follow the tracked (+ manually corrected) path, or None when there is nothing to follow.

    edit["framing"] = {"offset_y": headroom (crop centre = subject + offset_y × crop height), "zoom": the base zoom}.
    Constant zoom: one crop whose x/y are per-frame expressions. Keyframed zoom: ffmpeg's crop size is fixed once, so
    crop the widest window the path needs (following per frame), then zoompan inside it (zoom/x/y per frame, d=1 keeps
    one output frame per input frame) straight to the output size."""
    tr = edit.get("track") or {}
    pts, manual = tr.get("points") or [], tr.get("manual") or []
    if not (pts or manual):
        return None
    fr = edit.get("framing") or {}
    path = merge(pts, manual, (float(edit.get("crop_x", 0.5)), float(edit.get("crop_y", 0.5))),
                 zoom=float(fr.get("zoom") or edit.get("zoom") or 1.0))
    off = float(fr.get("offset_y") or 0.0)
    head = lambda p: p[2] + off * media.crop_box(sw, sh, ratio, 0.5, 0.5, p[3])[1] / sh  # noqa: E731
    q = lambda s: f"'{s}'" if "t" in s else s  # noqa: E731  quoted: the expression's commas aren't filter separators
    zmin = min(p[3] for p in path)
    w, h, _, _ = media.crop_box(sw, sh, ratio, 0.5, 0.5, zmin)
    win = [(p[0], *media.crop_box(sw, sh, ratio, p[1], head(p), zmin)[2:]) for p in path]
    crop = f"crop={w}:{h}:{q(ffmpeg_expr([(t, x) for t, x, _ in win]))}:{q(ffmpeg_expr([(t, y) for t, _, y in win]))}"
    if max(p[3] for p in path) - zmin < 0.01:
        return crop
    W, H = out or (w, h)
    z = ffmpeg_expr([(p[0], p[3] / zmin) for p in path], var="it", eps=0.002)
    sx = ffmpeg_expr([(p[0], p[1] * sw - k[1]) for p, k in zip(path, win)], var="it")  # subject inside the window
    sy = ffmpeg_expr([(p[0], head(p) * sh - k[2]) for p, k in zip(path, win)], var="it")
    return (f"{crop},zoompan=z='{z}':x='clip(({sx})-iw/zoom/2,0,iw-iw/zoom)':y='clip(({sy})-ih/zoom/2,0,ih-ih/zoom)'"
            f":d=1:s={W}x{H}:fps={fps:g}")


# ---------- the job ----------

def decode(src: Path, start: float, end: float, w: int, h: int, rate: float):
    """Frames of [start, end] at `rate` per second, scaled to w×h BGR (an ffmpeg pipe: exact sample times, cheap scaling)."""
    proc = media.popen([media.FFMPEG, "-v", "error", "-ss", f"{start:.3f}", "-t", f"{max(end - start, 0.05):.3f}",
                        "-i", str(src), "-an", "-vf", f"fps={rate:.4f},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while len(buf := proc.stdout.read(w * h * 3)) == w * h * 3:
            yield np.frombuffer(bytearray(buf), np.uint8).reshape(h, w, 3)
    finally:
        proc.kill()
        proc.wait()


def _even_size(sw: int, sh: int, longest: int) -> tuple[int, int]:
    k = min(1.0, longest / max(sw, sh))
    return max(2, int(sw * k) // 2 * 2), max(2, int(sh * k) // 2 * 2)


def track(src: Path, start: float, end: float, meta: dict, cuts, ratio: float = 9 / 16, zoom: float = 1.0, progress=None,
          mode: str = "auto", target: dict | None = None) -> dict:
    """Detect, lock and smooth the subject over [start, end]. cuts = scene-cut times relative to the clip start.
    mode: auto (faces, anime faces, salient components, motion) | face (faces only) | target (follow the picked `target`)."""
    sw, sh = int(meta["width"]), int(meta["height"])
    dur = max(end - start, 0.05)
    rate = min(RATE, MAX_SAMPLES / dur)
    expected = dur * rate + 1
    tick = (lambda n: progress(min(n / expected, 1.0))) if progress else (lambda n: None)  # noqa: E731
    if mode == "target":
        frames = []  # ponytail: held in memory (≤600 frames × 320 px ≈ 100 MB) to track both ways from the picked frame
        for f in decode(src, start, end, *_even_size(sw, sh, 320), rate):
            frames.append(f)
            tick(len(frames) * 0.6)
        samples = target_frames(frames, rate, target)
        del frames
    else:
        w, h = _even_size(sw, sh, 640)
        yunet, anime = _detectors(w, h)
        samples, prev = [], None
        for f in decode(src, start, end, w, h, rate):
            cands, prev = candidates(f, prev, yunet, anime, mode)
            samples.append((len(samples) / rate, cands))
            tick(len(samples))
    if not samples:
        raise RuntimeError("Couldn't read the video for tracking.")
    cuts = sorted(c for c in cuts if 0 < c < dur)
    raw = follow(samples, cuts)
    if not raw:
        raise RuntimeError({"face": "No face found in this clip — try Auto, or pick what to follow.",
                            "target": "Lost the picked object right away — draw the box tighter around it."}.get(
            mode, "No face or moving subject found in this clip — place the crop by hand instead."))
    cw, ch, _, _ = media.crop_box(sw, sh, ratio, 0.5, 0.5, zoom)
    return {"points": smooth(raw, cuts, dur, cw / sw, ch / sh), "source_counts": dict(Counter(p[5] for p in raw)),
            "updated": datetime.now(timezone.utc).isoformat()}
