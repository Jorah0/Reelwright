"""Teach-by-example screen events: the creator boxes something on one frame ("YOU DIED", a kill-feed entry, a VICTORY
banner) and names it; analysis then finds it through the whole video with OpenCV template matching on the 2 fps frames
vision.analyze already decodes, and every appearance becomes a `hud:<name>` marker (markers.json -> signals["markers"]).

A template is saved once (DATA/hud/<id>.png + a "hud" doc: the box relative to the frame, a game name to group it,
a match threshold) and reused for every later video while it's switched on. Matching is multi-scale (a HUD shifts a
little with resolution and UI scale) and looks only around the taught box, so it adds little to the shared decode.
ponytail: matched on the 320 px analysis frames, where small HUD text (a 1080p kill feed) gets fuzzy; if that matters,
add a full-resolution crop of the taught boxes as another branch of the same ffmpeg pass.
"""
import re
import subprocess

import numpy as np

from . import config, log, media, store

DIR = config.DATA / "hud"
SCALES = (0.8, 0.9, 1.0, 1.12, 1.25)
MAX_ACTIVE = 8      # templates matched per analysis
MIN_DETAIL = 8.0    # grey-level spread a template needs: a flat patch would "match" every flat patch
GAP = 2             # samples (1 s at 2 fps) a match may drop out and still be the same appearance


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or "event"


def grab(src, t: float, width: int | None = None):
    """One BGR frame at t seconds (scaled to width when given)."""
    import cv2
    vf = ["-vf", f"scale={int(width)}:-2"] if width else []
    r = subprocess.run([media.FFMPEG, "-v", "error", "-ss", f"{max(float(t), 0.0):.3f}", "-i", str(src), "-frames:v", "1", *vf,
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=60, creationflags=media.NO_WINDOW)
    img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR) if r.stdout else None
    if img is None:
        raise RuntimeError(f"Couldn't read a frame at {float(t):.1f} s.")
    return img


def teach(src, meta: dict, t: float, box, name: str, game: str = "", threshold: float = 0.8) -> dict:
    """Save the boxed part of the frame at t as a template. box = [x, y, w, h], fractions of the frame."""
    import cv2
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:40]
    if not name:
        raise ValueError("Give the screen event a name, e.g. “You died”.")
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        raise ValueError("Draw a box around the thing to look for.") from None
    if not (0 <= x < 1 and 0 <= y < 1 and 0.01 <= w and 0.01 <= h and x + w <= 1.001 and y + h <= 1.001):
        raise ValueError("Draw the box inside the picture.")
    sw = int(meta.get("width") or 0)
    frame = grab(src, t, 1920 if sw > 1920 else None)  # 1080p detail is plenty: matching runs at 320 px
    H, W = frame.shape[:2]
    crop = frame[round(y * H):round((y + h) * H), round(x * W):round((x + w) * W)]
    if crop.size == 0 or float(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).std()) < MIN_DETAIL:
        raise ValueError("That box has too little detail to recognise: draw it tightly around the text or icon.")
    hid = store.new_id()
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{hid}.png").write_bytes(cv2.imencode(".png", crop)[1].tobytes())  # bytes, not cv2.imwrite: non-ASCII paths
    return store.put("hud", {"id": hid, "name": name, "kind": f"hud:{slug(name)}", "game": str(game or "").strip()[:40],
                             "roi": [round(v, 4) for v in (x, y, w, h)], "threshold": min(max(float(threshold), 0.5), 0.99),
                             "enabled": True, "file": f"hud/{hid}.png", "size": [W, H]})


def remove(hid: str) -> None:
    if not re.fullmatch(r"\w{1,40}", hid, re.ASCII):  # the id names a file: never a path
        raise ValueError("bad screen event id")
    store.delete("hud", hid)
    (DIR / f"{hid}.png").unlink(missing_ok=True)


def active() -> list[dict]:
    """The switched-on templates for vision.analyze(hud=...). Never raises: a broken template is skipped."""
    import cv2
    out = []
    for d in store.find("hud", enabled=True)[:MAX_ACTIVE]:
        try:
            img = cv2.imdecode(np.frombuffer((DIR / f"{d['id']}.png").read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise ValueError("unreadable image")
            out.append({"id": d["id"], "name": d["name"], "kind": d["kind"], "roi": d["roi"], "threshold": float(d["threshold"]), "img": img})
        except Exception as e:
            log.event("hud", f"screen event {d.get('name')!r} skipped: {e}")
    return out


def matchers(templates: list[dict], w: int, h: int) -> list[dict]:
    """Each template resized for frames of w x h at every scale, with the search area around its box (half its size
    plus a little each way, so a HUD that sits slightly elsewhere is still found)."""
    import cv2
    out = []
    for tp in templates:
        x, y, rw, rh = tp["roi"]
        mx, my = rw / 2 + 0.02, rh / 2 + 0.02
        x0, y0 = int(max(0.0, x - mx) * w), int(max(0.0, y - my) * h)
        x1, y1 = int(min(1.0, x + rw + mx) * w), int(min(1.0, y + rh + my) * h)
        sized = []
        for s in SCALES:
            tw, th = round(rw * w * s), round(rh * h * s)
            if 6 <= tw <= x1 - x0 and 6 <= th <= y1 - y0:
                sized.append(cv2.resize(tp["img"], (tw, th), interpolation=cv2.INTER_AREA))
        if sized:
            out.append({k: tp[k] for k in ("name", "kind", "threshold")} | {"region": (x0, y0, x1, y1), "sized": sized})
        else:
            log.event("hud", f"screen event {tp['name']!r} is too small to find in {w} px analysis frames")
    return out


def scores(gray, ms: list[dict]) -> list[float]:
    """Best match (-1..1) of each template in one greyscale analysis frame."""
    import cv2
    out = []
    for m in ms:
        x0, y0, x1, y1 = m["region"]
        reg, best = gray[y0:y1, x0:x1], 0.0
        for t in m["sized"]:
            r = np.nan_to_num(cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED), nan=0.0, posinf=0.0, neginf=0.0)
            best = max(best, float(r.max()))
        out.append(min(best, 1.0))
    return out


def events(series: list[list[float]], ms: list[dict], rate: float) -> list[dict]:
    """Runs of frames at or above each template's threshold -> markers {t, kind, label, strength, d}."""
    if not series or not ms:
        return []
    arr, out = np.asarray(series, dtype=np.float32), []
    for j, m in enumerate(ms):
        runs: list[list[int]] = []
        for i in np.flatnonzero(arr[:, j] >= m["threshold"]):
            if runs and i - runs[-1][1] <= GAP:
                runs[-1][1] = int(i)
            else:
                runs.append([int(i), int(i)])
        out += [{"t": round(a / rate, 2), "kind": m["kind"], "label": m["name"], "strength": round(float(arr[a:b + 1, j].max()), 2),
                 "d": round((b - a + 1) / rate, 2)} for a, b in runs]
    return sorted(out, key=lambda m: m["t"])


def save_markers(pid: str, marks: list | None, templates: list[dict], analyzed: bool = False) -> None:
    """Put matches in the project's markers.json, replacing earlier matches of the same templates. analyzed=True also
    updates signals.json (a scan after teaching: the analysis already merged markers.json). Best-effort when marks is None."""
    if marks is None:
        return
    try:
        _save_markers(pid, marks, {t["kind"] for t in templates}, analyzed)
    except (OSError, ValueError) as e:
        log.event("hud", f"screen-event markers not saved for {pid}: {e}")


def _save_markers(pid: str, marks: list, kinds: set, analyzed: bool) -> None:
    from . import pipeline, signals
    folder = pipeline.pdir(pid)
    keep = [m for m in signals.read_markers(folder) if m["kind"] not in kinds]
    if keep or marks or (folder / "markers.json").exists():
        pipeline.save_json(pid, "markers.json", {"markers": sorted(keep + list(marks), key=lambda m: m["t"])})
    if analyzed:
        sig = pipeline.load_json(pid, "signals.json", {}) or {}
        sig["markers"] = signals.merge_markers([m for m in sig.get("markers") or [] if m["kind"] not in kinds], marks)
        pipeline.save_json(pid, "signals.json", sig)


def scan_job(job: dict) -> None:
    """Job "hud_scan": look for the switched-on screen events in a project (one decode) and save what is found."""
    from . import pipeline, vision
    pid = job["project_id"]
    proj = store.get("project", pid)
    if not proj:
        raise RuntimeError("The project was deleted.")
    tpl = active()
    if not tpl:
        raise RuntimeError("No screen events are switched on: teach one first (Editor → Teach a screen event).")
    out = vision.analyze(pipeline.source_file(proj), proj.get("meta") or {}, lambda f: pipeline._progress(job["id"], f, "Looking for screen events"),
                         faces=False, hud=tpl)
    marks = out.get("hud") or []
    now = store.get("project", pid)
    if not now:
        raise RuntimeError("The project was deleted during the scan.")
    save_markers(pid, marks, tpl, analyzed=now.get("status") == "ready")
    store.update("job", job["id"], {"message": f"Found {len(marks)} screen event{'s' if len(marks) != 1 else ''}"})
