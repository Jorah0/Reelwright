"""On-screen activity: what the picture is doing, so clip finding isn't blind on gaming / VTuber streams whose payoff is
visual (clutch plays, deaths, jump scares, flashes) with little speech.

One streamed ffmpeg pass at 2 fps (small frames read from a pipe, never the whole video in memory, so a 6-hour VOD costs
the same RAM as a 6-minute clip) gives per-second series and "visual spikes" {t, kind, strength}:
- action: mean frame difference, with one-sample jumps (hard cuts) removed so a scene change isn't "action";
- flash: a sharp brightness rise that falls back within ~2 s (explosions, muzzle flash, jump scares, hit flashes);
- color: a big sustained saturation shift not caused by a cut (death screens going grey or red);
- face_reaction: the streamer's face (YuNet / anime cascade from track.py, 0.5 fps) moving a lot on the facecam.
ponytail: no on-screen text density (an edge heuristic is noisy on detailed game HUDs); add it if kill-feed/caption
detection is ever needed."""
import math
import subprocess

import numpy as np

from . import media

RATE = 2          # samples per second
FACE_EVERY = 4    # face detection on every 4th sample (0.5 fps)
WIDTH = 320       # decoded width: faces need ~320 px; motion/brightness use a 160 px copy


def frames(src, start: float, end: float, n: int = 4, width: int = 384) -> list[bytes]:
    """n small JPEGs (≈quality 70) spread evenly through [start, end], from one ffmpeg call: what the judge looks at."""
    import tempfile
    from pathlib import Path

    span = max(end - start, 0.5)
    with tempfile.TemporaryDirectory(prefix="rw-frames-") as d:
        media._run([media.FFMPEG, "-y", "-v", "error", "-ss", f"{start + span / (2 * n):.3f}", "-i", str(src), "-an", "-sn",
                    "-t", f"{span:.3f}", "-vf", f"fps={n / span:.5f},scale={width}:-2", "-frames:v", str(n), "-q:v", "5",
                    str(Path(d) / "f%d.jpg")])
        return [p.read_bytes() for p in sorted(Path(d).glob("f*.jpg"), key=lambda p: int(p.stem[1:]))]


def _peaks(x, floor: float, gap: int, limit: int) -> list[int]:
    """Indices of the highest values above floor, at least gap apart (same picker as media.energy_spikes)."""
    blocked, out = np.zeros(len(x), dtype=bool), []
    for i in np.argsort(x)[::-1]:
        if len(out) >= limit or x[i] <= floor:
            break
        if not blocked[i]:
            out.append(int(i))
            blocked[max(0, i - gap):i + gap + 1] = True
    return out


def _per_second(x, n_s: int):
    x = np.asarray(x, dtype=np.float32)
    x = np.pad(x, (0, n_s * RATE - len(x)), mode="edge") if len(x) < n_s * RATE else x[:n_s * RATE]
    return x.reshape(n_s, RATE).mean(axis=1)


def _smooth(x, k: int):
    return np.convolve(x, np.ones(k) / k, mode="same") if len(x) >= k else x  # "same" would lengthen a tiny series


def analyze(src, meta: dict, progress=None, faces: bool = True, scenes: bool = False, hud=None) -> dict:
    """{"motion": [per s], "face": [per s, 0..1 or empty], "spikes": [{t, kind, strength}]}.
    scenes=True also returns "scenes" (cut times, exactly as media.scenes finds them) from the same decode: decoding is
    nearly all of the cost, so sharing it makes this analyzer almost free on top of scene detection.
    hud: taught screen events (hud.active()) matched in the same frames; adds "hud": [{t, kind: "hud:<name>", label,
    strength, d}]. Without it nothing changes."""
    import cv2
    import tempfile

    sw, sh, dur = int(meta.get("width") or 0), int(meta.get("height") or 0), float(meta.get("duration") or 0)
    if not (sw and sh and dur > 0):
        raise RuntimeError("no video stream")
    w, h = WIDTH, max(2, round(WIDTH * sh / sw / 2) * 2)
    gw, gh = 160, max(2, round(160 * sh / sw))
    yunet = anime = None
    if faces:
        from . import track
        yunet, anime = track._detectors(w, h)
    detect = yunet is not None or anime is not None
    hm = hs = None
    if hud:
        from . import hud as hudmod
        hm, hs = hudmod.matchers(hud, w, h), []
    ours = f"fps={RATE},scale={w}:{h}:flags=area,format=bgr24"
    if scenes:  # media.scenes' exact filter chain on one branch, ours on the other
        cmd = [media.FFMPEG, "-hide_banner", "-hwaccel", "auto", "-i", str(src), "-an", "-sn", "-dn", "-filter_complex",
               f"[0:v]scale=320:-2,split[a][b];[a]select='gt(scene,{media.SCENE_THRESHOLD})',showinfo[s];[b]{ours}[v]",
               "-map", "[s]", "-f", "null", "-", "-map", "[v]", "-f", "rawvideo", "-"]
    else:
        cmd = [media.FFMPEG, "-v", "error", "-i", str(src), "-an", "-sn", "-dn", "-vf", ours, "-f", "rawvideo", "-"]
    errf = tempfile.TemporaryFile()  # showinfo lines; a file (not a pipe) so ffmpeg can never block on a full stderr
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, creationflags=media.NO_WINDOW)
    size = w * h * 3
    diff, bright, sat, face, fmove = [], [], [], [], []
    prev = box = None
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            small = cv2.resize(frame, (gw, gh), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            bright.append(float(gray.mean()) / 255)
            sat.append(float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[..., 1].mean()) / 255)
            d = cv2.absdiff(gray, prev) if prev is not None else None
            diff.append(float(d.mean()) / 255 if d is not None else 0.0)
            prev = gray
            i = len(bright) - 1
            if detect:
                if i % FACE_EVERY == 0:
                    found, _ = track.candidates(frame, None, yunet, anime, mode="faces")
                    box = max(found, key=lambda c: c["w"] * c["h"], default=None)
                face.append(1.0 if box else 0.0)
                move = 0.0
                if box and d is not None:  # motion inside the face box (1.5× for head movement)
                    x0, x1 = int((box["cx"] - box["w"] * 0.75) * gw), int((box["cx"] + box["w"] * 0.75) * gw) + 1
                    y0, y1 = int((box["cy"] - box["h"] * 0.75) * gh), int((box["cy"] + box["h"] * 0.75) * gh) + 1
                    roi = d[max(0, y0):y1, max(0, x0):x1]
                    move = float(roi.mean()) / 255 if roi.size else 0.0
                fmove.append(move)
            if hm:
                hs.append(hudmod.scores(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), hm))
            if progress and i % 240 == 0:
                progress(min(i / RATE / dur, 1.0))
    finally:
        p.stdout.close()
        if p.poll() is None:
            p.kill()
        p.wait()
        errf.seek(0)
        err = errf.read().decode("utf-8", "replace")
        errf.close()
    n = len(bright)
    if n < 2:
        raise RuntimeError("couldn't decode the video")
    n_s = max(1, min(math.ceil(n / RATE), math.ceil(dur)))
    limit = max(25, n_s // 90)
    spikes = []

    raw = np.asarray(diff, dtype=np.float32)
    # erode over 3 samples: a hard cut (one huge diff) or a one-frame flash (two) vanishes; real action lasts
    motion = np.minimum(raw, np.minimum(np.r_[0.0, raw[:-1]], np.r_[raw[1:], 0.0]))
    cut = raw - motion > 0.1
    m1 = _per_second(motion, n_s)
    sm = _smooth(m1, 3)
    med = float(np.median(sm))
    top = float(sm.max()) or 1.0
    for i in _peaks(sm, max(2.0 * med, med + 0.01, 0.012), 8, limit):
        spikes.append({"t": float(i), "kind": "action", "strength": round(float(sm[i]) / top, 2)})

    b = np.asarray(bright, dtype=np.float32)
    last = -99
    for i in range(1, n):
        rise = float(b[i] - b[max(0, i - 2):i].min())
        after = b[i + 1:i + 1 + 2 * RATE]
        if rise >= 0.2 and i - last >= 3 * RATE and after.size and float(after.min()) <= b[i] - 0.5 * rise:
            spikes.append({"t": round(i / RATE, 1), "kind": "flash", "strength": round(min(rise / 0.6, 1.0), 2)})
            last = i

    s1 = _per_second(sat, n_s)
    shift = np.zeros(n_s, dtype=np.float32)
    for i in range(3, n_s - 2):
        shift[i] = abs(float(s1[i:i + 3].mean() - s1[i - 3:i].mean()))
    cut_s = _per_second(cut.astype(np.float32), n_s) > 0
    shift[cut_s | np.r_[cut_s[1:], False] | np.r_[False, cut_s[:-1]]] = 0  # a new scene changes colour too
    for i in _peaks(shift, 0.15, 10, limit):
        spikes.append({"t": float(i), "kind": "color", "strength": round(min(float(shift[i]) / 0.4, 1.0), 2)})

    face_s = []
    if detect:
        face_s = _per_second(face, n_s)
        fm = _smooth(_per_second(fmove, n_s), 2) * (face_s > 0)
        fmed = float(np.median(fm[face_s > 0])) if (face_s > 0).any() else 0.0
        ftop = float(fm.max()) or 1.0
        for i in _peaks(fm, max(2.5 * fmed, 0.02), 8, limit):
            spikes.append({"t": float(i), "kind": "face_reaction", "strength": round(float(fm[i]) / ftop, 2)})
    out = {"motion": [round(float(v), 4) for v in m1], "face": [round(float(v), 2) for v in face_s],
           "spikes": sorted(spikes, key=lambda s: s["t"])}
    if scenes:
        out["scenes"] = media.scene_times(err)
    if hud:
        out["hud"] = hudmod.events(hs, hm, RATE)
    return out
