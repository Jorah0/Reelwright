"""Camera angles: extra recordings of the same moment (a facecam, a wide shot, a second phone) added to a project.

project["angles"] = [{id (12 hex), name, file ("angles/<id>.<ext>" inside the project folder), offset_s, meta {width,
height, duration, fps, has_audio}, sync {method, confidence, note}, status, thumb, error}], at most MAX_ANGLES.
offset_s says where the angle sits on the main video's clock: main time = angle time + offset_s (an angle that started
recording 5 s after the main camera has offset_s = 5).

Sync: both soundtracks are decoded as mono 8 kHz, turned into an onset envelope (rise of log energy per 10 ms) and
cross-correlated with numpy's FFT over ±10 min (coarse, with a parabolic sub-hop peak), then refined on a 1 ms
envelope of a 20 s excerpt around that lag (fine). Confidence = the correlation peak-to-sidelobe ratio; below
MIN_CONFIDENCE, or with no audio on either side, the offset falls back to 0 and the note says why (nudge it by hand).

Rendering uses them through media.render(angles=render_angles(...)): region sources, edit["switches"] (cuts between
angles) and edit["audio_source"]. Where an angle doesn't cover a moment its picture is black (media.angle_inputs).
Auto-switch (suggest_for_clip) proposes cuts: the loudest angle's camera when angles have their own sound, else the
one with the most motion, with a minimum shot length and hysteresis; the creator approves or edits them.
"""
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

from . import log, media, pipeline, store

MAX_ANGLES = 6
SR = 8000                 # decode rate for sync and loudness
HOP = SR // 100           # 10 ms envelope hop (coarse)
FINE_SR, FINE_HOP = 16000, 16  # 1 ms envelope hop (fine)
WINDOW_S = 600.0          # search ±10 min
MIN_OVERLAP_S = 3.0
MIN_CONFIDENCE = 8.0      # peak-to-sidelobe ratio; unrelated audio scores ~4-6
FILE_RE = re.compile(r"angles/[0-9a-f]{12}\.[0-9a-z]{2,4}")


class AngleError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# ---------- sync ----------

def _pcm(path, sr: int, start: float = 0.0, dur: float | None = None):
    """Mono float32 samples streamed out of ffmpeg in chunks (never the whole decoded file at once)."""
    cmd = [media.FFMPEG, "-v", "error", *(["-ss", f"{start:.3f}"] if start > 0 else []), "-i", str(path),
           *(["-t", f"{dur:.3f}"] if dur else []), "-vn", "-sn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    p = media.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while chunk := p.stdout.read(sr * 2 * 20):  # 20 s per read
            yield np.frombuffer(chunk[: len(chunk) // 2 * 2], "<i2").astype(np.float32) / 32768
    finally:
        p.stdout.close()
        p.wait()


def energy(path, sr: int = SR, hop: int = HOP, start: float = 0.0, dur: float | None = None) -> np.ndarray:
    """Mean power per hop (len 0 when the file has no audio)."""
    out, rest = [], np.zeros(0, np.float32)
    for x in _pcm(path, sr, start, dur):
        x = np.concatenate([rest, x])
        n = len(x) // hop * hop
        out.append((x[:n].reshape(-1, hop) ** 2).mean(axis=1))
        rest = x[n:]
    return np.concatenate(out) if out else np.zeros(0)


def onsets(e: np.ndarray) -> np.ndarray:
    """Onset envelope: positive rises of log energy (gain-independent), zero-mean unit-variance."""
    le = np.log(e + 1e-7)
    o = np.maximum(np.diff(le, prepend=le[:1]), 0)
    return (o - o.mean()) / (o.std() or 1)


def xcorr_peak(m: np.ndarray, a: np.ndarray, lo: int, hi: int, guard: int) -> tuple[float, float]:
    """Best lag L in [lo, hi] for m[t + L] ≈ a[t] (FFT cross-correlation), sub-sample by a parabola -> (L, PSR)."""
    n = 1 << int(np.ceil(np.log2(len(m) + len(a))))
    c = np.fft.irfft(np.fft.rfft(m, n) * np.conj(np.fft.rfft(a, n)), n)
    lags = np.arange(lo, hi + 1)
    v = c[lags % n]
    i = int(np.argmax(v))
    side = np.concatenate([v[: max(i - guard, 0)], v[i + guard + 1:]])
    psr = float((v[i] - side.mean()) / (side.std() or 1)) if len(side) > 10 else 0.0
    d = 0.0
    if 0 < i < len(v) - 1:
        den = v[i - 1] - 2 * v[i] + v[i + 1]
        d = float(0.5 * (v[i - 1] - v[i + 1]) / den) if den < 0 else 0.0
    return lags[i] + d, psr


def sync(main_path, angle_path) -> dict:
    """{offset_s, method "audio" | "none", confidence, note}: where the angle sits on the main video's clock."""
    none = lambda note, conf=0.0: {"offset_s": 0.0, "method": "none", "confidence": round(conf, 2), "note": note}  # noqa: E731
    me, ae = energy(main_path), energy(angle_path)
    if len(me) < 100 or len(ae) < 100:
        return none(f"No audio on the {'main video' if len(me) < 100 else 'angle'} to line it up by: set the offset by hand.")
    m, a = onsets(me), onsets(ae)
    rate, ov = SR / HOP, int(MIN_OVERLAP_S * SR / HOP)
    lo, hi = max(-int(WINDOW_S * rate), -(len(a) - ov)), min(int(WINDOW_S * rate), len(m) - ov)
    if hi <= lo:
        return none("The recordings overlap too little to line up: set the offset by hand.")
    lag, conf = xcorr_peak(m, a, lo, hi, guard=int(0.5 * rate))
    if conf < MIN_CONFIDENCE:
        return none(f"Low sync confidence ({conf:.1f}): the sound doesn't match well enough. Set the offset by hand.", conf)
    off = lag / rate
    # fine: a 1 ms envelope of a 20 s excerpt where both play, ±40 ms around the coarse lag
    first = max(0.0, 0.05 - off)  # the first angle second whose main excerpt (50 ms earlier) is inside the main file
    a0 = first + max(0.0, (min(len(a) / rate, len(m) / rate - off) - first) / 2 - 10)
    fm = onsets(energy(main_path, FINE_SR, FINE_HOP, a0 + off - 0.05, 20.1))
    fa = onsets(energy(angle_path, FINE_SR, FINE_HOP, a0, 20.0))
    if len(fm) > 2000 and len(fa) > 2000:
        d, fconf = xcorr_peak(fm, fa, 10, 90, guard=5)  # main excerpt starts 50 ms early: lag 50 = no change
        if fconf >= MIN_CONFIDENCE / 2 and 12 <= d <= 88:  # a peak on the search edge is no refinement: keep coarse
            off += (d - 50) / 1000
    return {"offset_s": round(off, 4), "method": "audio", "confidence": round(conf, 2), "note": ""}


# ---------- angles on a project ----------

def angles_of(proj: dict | None) -> list[dict]:
    return [a for a in (proj or {}).get("angles") or [] if isinstance(a, dict)]


def angle_path(pid: str, a: dict, key: str = "file") -> Path | None:
    """The angle's file inside the project's angles/ folder, or None (anything else is refused, never resolved)."""
    rel = str(a.get(key) or "")
    if not (FILE_RE.fullmatch(rel) if key == "file" else re.fullmatch(r"angles/[0-9a-f]{12}\.jpg", rel)):
        return None
    folder = (pipeline.pdir(pid) / "angles").resolve()
    p = (folder / rel.split("/", 1)[1]).resolve()
    return p if p.parent == folder else None


def _get(pid: str, aid: str) -> dict:
    proj = store.get("project", pid)
    if not proj:
        raise AngleError(404, "No such project")
    a = next((a for a in angles_of(proj) if a.get("id") == aid), None)
    if not a:
        raise AngleError(404, "No such camera angle")
    return a


def _set(pid: str, aid: str, **fields) -> dict | None:
    def apply(p):
        for a in angles_of(p):
            if a.get("id") == aid:
                a.update(fields)
    p = store.update("project", pid, fn=apply)
    return next((a for a in angles_of(p) if a.get("id") == aid), None)


def new_angle(pid: str, name: str, ext: str = "", **extra) -> dict:
    """Reserve an angle slot (409 when the project already has MAX_ANGLES)."""
    if ext and ext not in media.VIDEO_EXT:
        raise AngleError(400, f"Unsupported file type {ext}.")
    if not store.get("project", pid):
        raise AngleError(404, "No such project")
    aid = store.new_id()
    doc = {"id": aid, "name": (str(name or "").strip() or "Camera")[:60], "file": f"angles/{aid}{ext}" if ext else "",
           "offset_s": 0.0, "meta": None, "sync": None, "status": "importing", "thumb": None, "error": "", **extra}
    with store._lock:
        if len(angles_of(store.get("project", pid))) >= MAX_ANGLES:
            raise AngleError(409, f"A project can have at most {MAX_ANGLES} camera angles.")
        store.update("project", pid, fn=lambda p: p.update(angles=angles_of(p) + [doc]))
    (pipeline.pdir(pid) / "angles").mkdir(exist_ok=True)
    return doc


def add_upload(pid: str, filename: str, fileobj, name: str = "") -> dict:
    ext = Path(filename or "").suffix.lower()
    if ext not in media.VIDEO_EXT:
        raise AngleError(400, f"Unsupported file type {ext or '(none)'}.")
    a = new_angle(pid, name or Path(filename).stem, ext)
    try:
        with open(angle_path(pid, a), "wb") as f:
            shutil.copyfileobj(fileobj, f, 8 * 1024 * 1024)
    except Exception:
        remove(pid, a["id"])
        raise
    pipeline.submit("angle", project_id=pid, angle_id=a["id"])
    return _set(pid, a["id"], status="syncing")


def add_url(pid: str, url: str, name: str = "") -> dict:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise AngleError(400, "Enter a full http(s) link.")
    a = new_angle(pid, name or "Linked camera", source_url=url)
    pipeline.submit("angle", project_id=pid, angle_id=a["id"], url=url)
    return a


def obs_folders() -> list[dict]:
    """Recordings folders of the OBS connections this PC can read: {obs_id, name, folder, files [{name, size, mtime}]}."""
    from . import obs
    out = []
    for conn in store.find("obs"):
        dirs = []
        if conn.get("status") == "connected":
            try:
                rd = obs.probe(conn).get("record_dir")
                dirs.append(obs.map_path(rd, conn.get("path_map"), conn["host"]) if rd else None)
            except Exception:
                pass
        if conn.get("last_file"):
            dirs.append(str(Path(conn["last_file"]).parent))
        for d in dict.fromkeys(x for x in dirs if x):
            folder = Path(d)
            if not folder.is_dir():
                continue
            files = sorted((f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in media.VIDEO_EXT),
                           key=lambda f: -f.stat().st_mtime)[:40]
            out.append({"obs_id": conn["id"], "name": conn.get("name") or "OBS", "folder": str(folder),
                        "files": [{"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime} for f in files]})
    return out


def add_obs(pid: str, obs_id: str, file: str, name: str = "") -> dict:
    """An OBS recording picked by plain file name: it must be listed in that connection's recordings folder."""
    file = str(file or "")
    if not file or Path(file).name != file or any(c in file for c in "/\\:") or file in (".", ".."):
        raise AngleError(400, "Pick a recording from the list.")
    for f in obs_folders():
        if f["obs_id"] == obs_id and any(x["name"] == file for x in f["files"]):
            src = (Path(f["folder"]) / file).resolve()
            if src.parent != Path(f["folder"]).resolve() or not src.is_file():
                break
            a = new_angle(pid, name or src.stem, src.suffix.lower(), origin={"obs": f["name"], "path": str(src)})
            pipeline.submit("angle", project_id=pid, angle_id=a["id"], path=str(src))
            return _set(pid, a["id"], status="importing")
    raise AngleError(404, "That recording isn't in an OBS recordings folder this PC can read.")


def update(pid: str, aid: str, body: dict) -> dict:
    """Rename and/or set the offset by hand (the nudge buttons)."""
    a = _get(pid, aid)
    fields = {}
    if "name" in body:
        fields["name"] = (str(body["name"] or "").strip() or a["name"])[:60]
    if "offset_s" in body:
        try:
            off = float(body["offset_s"])
        except (TypeError, ValueError):
            raise AngleError(400, "offset_s must be a number.") from None
        if not np.isfinite(off) or abs(off) > 6 * 3600:
            raise AngleError(400, "offset_s is out of range.")
        fields["offset_s"] = round(off, 3)
        fields["sync"] = {**(a.get("sync") or {}), "method": "manual", "note": "Set by hand."}
    out = _set(pid, aid, **fields)
    if "offset_s" in fields and fields["offset_s"] != a.get("offset_s"):
        stale_renders(pid, aid)
    return out


def stale_renders(pid: str, aid: str) -> None:
    """The angle's sync changed: every clip and long form that reads it needs a new render (rev + 1), so an old,
    mis-synced file is never posted. Posted ones keep theirs (like remove())."""
    for kind, key in (("clip", "clip_id"), ("longform", "longform_id")):
        for d in store.find(kind, project_id=pid):
            if aid in media.used_angles({**media.DEFAULT_EDIT, **d["edit"]}) and not store.find("upload", **{key: d["id"]}, status="done"):
                store.update(kind, d["id"], fn=lambda x: x.update(rev=x.get("rev", 0) + 1))


def resync(pid: str, aid: str) -> dict:
    a = _get(pid, aid)
    if a.get("status") not in ("ready", "failed") or not angle_path(pid, a) or not angle_path(pid, a).exists():
        raise AngleError(409, "This angle is still importing.")
    pipeline.submit("angle", project_id=pid, angle_id=aid, resync=True)
    return _set(pid, aid, status="syncing", error="")


def tidy_switches(sw: list, default: str = "main") -> list:
    """Drop cuts that don't change the live camera (repeats, and leading cuts to the default view)."""
    out, cur = [], default
    for s in sorted(sw or [], key=lambda s: s["t"]):
        if s["angle"] != cur:
            out.append(s)
            cur = s["angle"]
    return out


def remove(pid: str, aid: str) -> dict:
    """Remove an angle: its files go, regions reading it go back to the main video, cuts to it become cuts to main,
    an audio source on it goes back to main."""
    a = _get(pid, aid)
    store.update("project", pid, fn=lambda p: p.update(angles=[x for x in angles_of(p) if x.get("id") != aid]))
    for key in ("file", "thumb"):
        f = angle_path(pid, a, key)
        if f:
            f.unlink(missing_ok=True)
    for c in store.find("clip", project_id=pid):
        def fix(c):
            e, before = c["edit"], repr(c["edit"])
            regs = [{k: v for k, v in r.items() if not (k == "source" and v == aid)} for r in e.get("regions") or []]
            if e.get("regions"):
                e["regions"] = regs
            own = next((r.get("source") or "main" for r in regs if r.get("id") == e.get("switch_region")), "main")
            if e.get("switches"):
                e["switches"] = tidy_switches([{**s, "angle": "main" if s["angle"] == aid else s["angle"]} for s in e["switches"]], own)
            if e.get("audio_source") == aid:
                e["audio_source"] = "main"
            if repr(e) != before and not store.find("upload", clip_id=c["id"], status="done"):
                c["rev"] = c.get("rev", 0) + 1
        store.update("clip", c["id"], fn=fix)
    return {"ok": True}


def render_angles(pid: str, proj: dict | None = None) -> dict:
    """{id: {path, offset_s, width, height, duration, has_audio}} of the ready angles whose files exist (for media.render)."""
    out = {}
    for a in angles_of(proj or store.get("project", pid)):
        p = angle_path(pid, a)
        if a.get("status") == "ready" and a.get("meta") and p and p.exists():
            out[a["id"]] = {**a["meta"], "path": p, "offset_s": float(a.get("offset_s") or 0)}
    return out


def angle_job(job):
    """Fetch (link / OBS copy) if needed, probe, thumbnail, then sync against the main video."""
    pid, aid = job["project_id"], job["angle_id"]
    a = _get(pid, aid)
    folder = pipeline.pdir(pid) / "angles"
    folder.mkdir(exist_ok=True)
    if job.get("url") and not job.get("resync"):
        def hook(d):
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if d.get("status") == "downloading" and total:
                pipeline._progress(job["id"], (d.get("downloaded_bytes") or 0) / total * 0.8, "Downloading camera angle")
        info = pipeline.ytdlp_download(job["url"], str(folder / f"{aid}.%(ext)s"), hook)
        got = next((p for p in folder.iterdir() if p.stem == aid and p.suffix in media.VIDEO_EXT), None)
        if not got:
            raise RuntimeError("The download didn't produce a video file.")
        a = _set(pid, aid, file=f"angles/{got.name}", name=a["name"] if a["name"] != "Linked camera" else (info.get("title") or a["name"])[:60])
    elif job.get("path") and not job.get("resync"):
        pipeline._progress(job["id"], 0.1, "Copying the recording")
        shutil.copy2(job["path"], angle_path(pid, a))  # copy, never move: OBS's recordings stay where they are
    path = angle_path(pid, a)
    if not path or not path.exists():
        raise RuntimeError("The camera angle's file is missing.")
    meta = media.probe(path)
    thumb = f"angles/{aid}.jpg"
    try:
        media.poster(path, min(1.0, (meta["duration"] or 0) / 2), pipeline.pdir(pid) / thumb)
    except RuntimeError:
        thumb = None
    _set(pid, aid, status="syncing", meta=meta, thumb=thumb)
    pipeline._progress(job["id"], 0.85, "Lining up the sound")
    proj = store.get("project", pid)
    main = pipeline.pdir(pid) / proj["source"] if proj.get("source") else None
    r = sync(main, path) if main and main.exists() else {"offset_s": 0.0, "method": "none", "confidence": 0.0,
                                                            "note": "The main video isn't imported yet: re-sync later."}
    log.event("multicam", f"{pid}/{aid}: offset {r['offset_s']} ({r['method']}, {r['confidence']})")
    _set(pid, aid, status="ready", offset_s=r["offset_s"], sync={k: r[k] for k in ("method", "confidence", "note")}, error="")
    if r["offset_s"] != a.get("offset_s"):
        stale_renders(pid, aid)


def angle_failed(job, err):
    try:
        _set(job["project_id"], job["angle_id"], status="failed", error=str(err)[:300])
    except Exception:
        pass


# ---------- auto-switch ----------

def suggest(scores: dict, step: float, min_shot: float = 2.0, hysteresis: float = 3.0) -> list[dict]:
    """Cuts [{t, angle}] from per-camera scores sampled every `step` s (higher = this camera should be live): the best
    camera wins only when it beats the live one by `hysteresis`, then shots shorter than `min_shot` merge into the one
    before them. The default view before the first cut is "main" (a cut at t=0 is emitted when another camera opens)."""
    names = list(scores)
    s = np.vstack([np.asarray(scores[k], float) for k in names])
    n, k = s.shape[1], max(1, int(round(min_shot / step)))
    if not n:
        return []
    cur = int(np.argmax(s[:, :k].mean(axis=1)))
    live = np.empty(n, int)
    for i in range(n):
        best = int(np.argmax(s[:, i]))
        if best != cur and s[best, i] > s[cur, i] + hysteresis:
            cur = best
        live[i] = cur
    segs = []  # [start bin, camera]
    for i in range(n):
        if not segs or segs[-1][1] != live[i]:
            segs.append([i, live[i]])
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for j in range(len(segs)):
            end = segs[j + 1][0] if j + 1 < len(segs) else n
            if end - segs[j][0] < k:
                if j == 0:
                    segs[1][0] = 0
                del segs[j]
                segs = [g for i2, g in enumerate(segs) if i2 == 0 or g[1] != segs[i2 - 1][1]]
                changed = True
                break
    cuts = [{"t": round(b * step, 2), "angle": names[c]} for b, c in segs]
    return tidy_switches(cuts, "main")


def _loudness(path, start: float, lead: float, dur: float, step: float) -> np.ndarray:
    n = int(round(dur / step))
    e = energy(path, SR, int(SR * step), start, max(dur - lead, 0.1))
    db = 10 * np.log10(e + 1e-9)
    out = np.full(n, -90.0)
    i0 = int(round(lead / step))
    out[i0:i0 + len(db)] = db[: max(n - i0, 0)]
    return out


def _motion(path, start: float, lead: float, dur: float, step: float) -> np.ndarray:
    n = int(round(dur / step))
    raw = subprocess.run([media.FFMPEG, "-v", "error", *(["-ss", f"{start:.3f}"] if start > 0 else []), *media.LOCAL_ONLY, "-i", str(path),
                          "-t", f"{max(dur - lead, 0.1):.3f}", "-an", "-vf", f"fps={1 / step:g},scale=96:54,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, creationflags=media.NO_WINDOW).stdout
    f = np.frombuffer(raw[: len(raw) // 5184 * 5184], np.uint8).reshape(-1, 5184).astype(np.float32)
    d = np.abs(np.diff(f, axis=0)).mean(axis=1) if len(f) > 1 else np.zeros(0)
    out = np.zeros(n)
    i0 = int(round(lead / step)) + 1
    out[i0:i0 + len(d)] = d[: max(n - i0, 0)]
    return out


def suggest_for_clip(clip: dict, min_shot: float = 2.0) -> dict:
    """{switches, method "audio" | "motion", note}: auto-switch suggestions for a clip, for the creator to approve."""
    pid = clip["project_id"]
    proj = store.get("project", pid)
    angles = render_angles(pid, proj)
    if not angles:
        raise AngleError(400, "Add a camera angle (and let it sync) first.")
    start, dur = float(clip["start"]), float(clip["end"]) - float(clip["start"])
    srcs = {"main": (pipeline.source_file(proj), start, 0.0, bool((proj.get("meta") or {}).get("has_audio")))}
    for aid, a in angles.items():
        seek, lead, ok = media._angle_span(a, start, dur)
        if ok:
            srcs[aid] = (a["path"], seek, lead, bool(a.get("has_audio")))
    if any(v[3] for k, v in srcs.items() if k != "main"):
        step = 0.1
        scores = {k: _loudness(p, s, lead, dur, step) for k, (p, s, lead, aud) in srcs.items() if aud}
        for k, v in scores.items():  # each mic against its own usual level: the one picking up its speaker stands out
            live = v[v > -80]
            v -= np.median(live) if len(live) else 0
            scores[k] = np.convolve(v, np.ones(5) / 5, mode="same")
        cuts, method, note = suggest(scores, step, min_shot, 3.0), "audio", "Cuts to whoever is loudest on their own camera's mic."
    else:
        step = 0.5
        scores = {k: _motion(p, s, lead, dur, step) for k, (p, s, lead, _) in srcs.items()}
        sd = float(np.std(np.concatenate(list(scores.values())))) or 1.0
        scores = {k: np.convolve(v / sd, np.ones(3) / 3, mode="same") for k, v in scores.items()}
        cuts, method, note = suggest(scores, step, min_shot, 0.5), "motion", "No angle has its own sound: cuts to the camera with the most movement."
    return {"switches": [c for c in cuts if c["t"] < dur], "method": method, "note": note}
