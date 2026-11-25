"""FFmpeg/OpenCV media engine: probing, analysis signals, reframing, captions, rendering."""
import atexit
import functools
import math
import os
import json
import re
import shutil
import subprocess
import time
import weakref
from pathlib import Path

import numpy as np

def _tool(name: str) -> str:
    """Prefer an ffmpeg bundled next to Reelwright.exe, then PATH."""
    import sys

    bundled = Path(sys.executable).parent / f"{name}.exe"
    if getattr(sys, "frozen", False) and bundled.exists():
        return str(bundled)
    return shutil.which(name) or name


FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL_ONLY = ["-protocol_whitelist", "file,pipe"]  # a file is probed by content: a disguised playlist can't fetch URLs


def local_inputs(args: list) -> list:
    """ffmpeg args with LOCAL_ONLY before every -i (not lavfi sources, not where it's already there); ffprobe's one input
    gets it up front."""
    if args and args[0] == FFPROBE:
        return args if LOCAL_ONLY[0] in args else [args[0], *LOCAL_ONLY, *args[1:]]
    out = []
    for i, a in enumerate(args):
        if a == "-i" and args[i - 2:i] != ["-f", "lavfi"] and args[i - 2:i] != LOCAL_ONLY:
            out += LOCAL_ONLY
        out.append(a)
    return out


_live = weakref.WeakSet()  # ffmpeg pipes still running: killed at exit (the job object covers this on Windows)


def popen(args: list, **kw) -> subprocess.Popen:
    p = subprocess.Popen(local_inputs(args), creationflags=NO_WINDOW, **kw)
    _live.add(p)
    return p


@atexit.register
def kill_children() -> None:
    for p in list(_live):
        if p.poll() is None:
            p.kill()

OUTPUT_SIZES = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".ts", ".flv", ".wmv"}

DEFAULT_EDIT = {
    "aspect": "9:16",      # source | 9:16 | 1:1 | 4:5 | 16:9
    "fit": "crop",         # crop | blur (fit inside a blurred fill)
    "crop_x": 0.5, "crop_y": 0.5,
    "speed": 1.0,
    "brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0,
    "sharpen": False,
    "volume": 1.0, "loudnorm": True, "denoise": False,
    "fade_in": 0.0, "fade_out": 0.0,
    "captions": True, "title_text": "",
    "caption_style": "bold",  # bold | clean | boxed | neon
    "caption_pos": "low",     # low | middle | high
    "layout": "single",       # one of LAYOUTS; split = cam on top, main view below (see layout_slots)
    "cam_x": 0.7, "cam_y": 0.05, "cam_w": 0.28, "cam_h": 0.32, "split": 0.4,
    "regions": None,          # [{id, role, x, y, w, h, follow, out?}] source boxes (0..1); None = cam_* + whole frame
    "split2": 0.25,           # stack3: bottom tier's share of the height (split = the top tier's)
    "pip_corner": "tr", "pip_size": 0.38, "pip_radius": 0.15, "pip_border": 6,  # corner cam: width share, corner radius, px
    "zoom": 1.0,              # punch-in
    "sticker_text": "", "sticker_pos": "top-right",
    "remove_silence": False,  # AI jump cuts
    "remove_fillers": False,  # cut "um", "uh"… (word timestamps)
    "music": "", "music_volume": 0.15,
    "follow": False,          # crop follows the tracked subject (edit["track"], see track.py) instead of crop_x/crop_y
    "words": None,            # caption words override [{s, e, w}] (absolute source seconds); None = the transcript's
    "framing": None,          # follow framing {"offset_y": headroom, "zoom": base zoom}; see track.crop_filter
    "overlays": None,         # [{id, kind, asset_id|text, t, d, x, y, w, anim, volume, layer, cutaway}], clip time
    # text styling per element; None keeps the caption_style look (size is px at 1080 wide, colours #RRGGBB)
    **{f"{who}_{f}": None for who in ("caption", "title", "sticker", "headline")
       for f in ("font", "size", "weight", "color", "outline", "outline_color", "shadow", "spacing")},
    "headline": None,         # themed banner {on, text, kicker, theme, position, show, seconds, push}; see headline_spec
    "streamer_card": None,    # streamer title card / platform badge {on, style, name, handle, platform, …}; see card_spec
    # camera angles (project["angles"], see multicam.py): cuts between them and where the sound comes from
    "switches": None,         # [{t, angle}] cut points in clip time, angle = "main" or an angle id; see switch_windows
    "switch_region": "",      # "" = the cuts switch the whole frame (single layout); a region id = only that region
    "audio_source": "main",   # "main" or an angle id with audio
    # render & QA (WP2)
    "music_duck": True,       # the music dips under the dialogue (sidechain compressor keyed by the voice)
    "caption_anim": "highlight",  # how the spoken word stands out: one of CAPTION_ANIMS
    "caption_emphasis": False,    # numbers, SHOUTED and long words stay coloured and a little bigger
    "bleep": "off",           # off | beep | mute: words from Settings › Rendering's bleep list (see bleep_spans)
}
CAPTION_ANIMS = ("highlight", "pop", "bounce", "box", "reveal")
BLEEP_MODES = ("off", "beep", "mute")


def _run(args, **kw):
    r = subprocess.run(local_inputs(args), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).stem} failed: {r.stderr.strip()[-1500:]}")
    return r


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):  # ffprobe reports "N/A" for some streams
        return 0.0


def probe(path: Path) -> dict:
    d = json.loads(_run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]).stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise RuntimeError("This file has no video stream.")
    num, den = (v.get("avg_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den or 0) else 30.0
    w, h = int(v["width"]), int(v["height"])
    rot = int(v.get("tags", {}).get("rotate", 0) or 0)
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", rot) or rot)
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {
        "duration": _num(d["format"].get("duration")) or _num(v.get("duration")),
        "width": w, "height": h, "fps": round(fps, 3),
        "has_audio": any(s["codec_type"] == "audio" for s in d["streams"]),
        "vcodec": v.get("codec_name", ""), "container": d["format"].get("format_name", ""),
    }


def needs_proxy(meta: dict) -> bool:
    """Sources a browser scrubs badly (or can't play): HEVC/AV1/VP9, MKV, above 1080p, above 60 fps, or over an hour."""
    return (meta.get("vcodec") in ("hevc", "av1", "vp9") or "matroska" in str(meta.get("container", ""))
            or min(meta.get("width", 0), meta.get("height", 0)) > 1080 or float(meta.get("fps") or 0) > 61
            or float(meta.get("duration") or 0) > 3600)


def proxy(src: Path, out: Path, progress=None) -> None:
    """A light preview copy for the Editor/Studio players: at most 540p, a keyframe every second and no B-frames (any
    seek decodes under a second of video), H.264 on the encoder chain, AAC 96k, +faststart."""
    m = probe(src)
    fps = m["fps"] or 30
    args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src.resolve()), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"scale=-2:{min(540, m['height'] // 2 * 2)},format=yuv420p", "-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    _encode(args, out, out.parent, m["duration"], progress,
            extra=["-g", str(max(round(fps), 1)), "-bf", "0", "-b:v", "1200k", "-maxrate", "1500k", "-bufsize", "3000k"])


def poster(src: Path, t: float, out: Path) -> None:
    _run([FFMPEG, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(out)])


def sprite(src: Path, duration: float, out: Path, count: int) -> None:
    """One-row strip of `count` thumbnails for the timeline."""
    _run([FFMPEG, "-y", "-v", "error", "-i", str(src), "-an", "-vf",
          f"fps={count}/{duration:.3f},scale=160:90:force_original_aspect_ratio=increase,crop=160:90,tile={count}x1",
          "-frames:v", "1", "-q:v", "4", str(out)])


def audio_signals(src: Path, silences_out: list | None = None) -> tuple[list[float], list[dict]]:
    """Waveform peaks at 10/s (0..1) and the loudest moments (energy spikes ≈ laughs, shouts, hype).
    The 4 kHz mono PCM is read a minute at a time, so a 6-hour VOD never sits in memory whole (it was ~1 GB of arrays).
    silences_out: a list that also gets silences() from this same decode (the same silencedetect filter, on the same
    decoded audio, ahead of the downmix), so analysis reads the audio once instead of twice."""
    import tempfile

    win = 400  # 0.1 s
    sd = ["-af", "silencedetect=noise=-35dB:d=0.7"] if silences_out is not None else []
    errf = tempfile.TemporaryFile()  # silencedetect's lines; a file so ffmpeg never blocks on a full stderr pipe
    proc = popen([FFMPEG, "-hide_banner", "-nostats", "-v", "info" if sd else "error", "-i", str(src), "-vn", *sd,
                  "-ac", "1", "-ar", "4000", "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=errf)
    peaks, rms = [], []
    try:
        while buf := proc.stdout.read(win * 2 * 600):  # 60 s; only the very last block can be short
            x = np.frombuffer(buf[: len(buf) // (win * 2) * win * 2], dtype=np.int16).astype(np.float32) / 32768.0
            x = x.reshape(-1, win)
            peaks.append(np.abs(x).max(axis=1))
            rms.append(np.sqrt((x ** 2).mean(axis=1)))
    finally:
        proc.stdout.close()
        proc.wait()
        errf.seek(0)
        err = errf.read().decode("utf-8", "replace")
        errf.close()
    if silences_out is not None:
        silences_out[:] = _silence_pairs(err)
    peaks, rms = np.concatenate(peaks or [np.zeros(0, np.float32)]), np.concatenate(rms or [np.zeros(0, np.float32)])
    if len(peaks) == 0:
        return [], []
    peaks = peaks / (peaks.max() or 1)
    smooth = np.convolve(rms, np.ones(20) / 20, mode="same")  # 2 s window
    return [round(float(p), 3) for p in peaks], energy_spikes(smooth)


def energy_spikes(smooth, gap_s: float = 10) -> list[dict]:
    """Loudest moments (0.1 s frames), at least gap_s apart. The budget grows with length (≥25, ~1 per 90 s) so a
    6-hour VOD isn't summarised by 25 spikes that all land in one hype segment."""
    smooth = np.asarray(smooth, dtype=np.float32)
    limit, floor, top = max(25, len(smooth) // 900), float(np.median(smooth)) * 1.5, float(smooth.max() or 1)
    gap, blocked = int(gap_s * 10), np.zeros(len(smooth), dtype=bool)
    picked: list[int] = []
    for i in np.argsort(smooth)[::-1]:
        if len(picked) >= limit or smooth[i] <= floor:
            break
        if not blocked[i]:  # O(1) spacing check: busy hour-long VODs reject tens of thousands of near-peak frames
            picked.append(int(i))
            blocked[max(0, i - gap):i + gap + 1] = True
    return [{"t": round(i / 10, 1), "energy": round(float(smooth[i]) / top, 3)} for i in sorted(picked)]


SCENE_THRESHOLD = 0.32


def scene_times(showinfo_log: str) -> list[float]:
    return [round(float(t), 2) for t in re.findall(r"pts_time:([\d.]+)", showinfo_log)]


def scenes(src: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    """Hard-cut times. (Analysis normally gets these from vision.analyze's shared decode; this is the fallback.)"""
    r = subprocess.run([FFMPEG, "-hide_banner", *LOCAL_ONLY, "-i", str(src), "-an", "-vf",  # CPU decode: see vision.analyze
                        f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return scene_times(r.stderr)


def silences(src: Path, noise_db: int = -35, min_dur: float = 0.7) -> list[list[float]]:
    r = subprocess.run([FFMPEG, "-hide_banner", *LOCAL_ONLY, "-i", str(src), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return _silence_pairs(r.stderr)


def _silence_pairs(log: str) -> list[list[float]]:
    starts = [float(s) for s in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(s) for s in re.findall(r"silence_end: ([\d.]+)", log)]
    return [[round(s, 2), round(e, 2)] for s, e in zip(starts, ends)]


MODEL_MAX = 200 << 20  # the largest model fetched is ~27 MB; a download past this is refused, not read into memory


def fetch_model(name: str, url: str, sha256: str | None = None) -> Path:
    """A small model file in the data folder, downloaded once. With sha256, a download that doesn't match is refused."""
    import hashlib
    import urllib.request

    from .config import DATA

    model = DATA / "models" / name
    if not model.exists():  # download to .part then rename: an interrupted fetch never leaves a corrupt model behind
        model.parent.mkdir(parents=True, exist_ok=True)
        part = model.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=20) as r:
            body = r.read(MODEL_MAX + 1)
        if len(body) > MODEL_MAX:
            raise RuntimeError(f"Download of {name} is too big (over {MODEL_MAX >> 20} MB); not using it.")
        if sha256 and hashlib.sha256(body).hexdigest() != sha256:
            raise RuntimeError(f"Download of {name} failed its SHA-256 check (expected {sha256[:12]}…); not using it.")
        part.write_bytes(body)
        os.replace(part, model)
    return model


class DecodeError(RuntimeError):
    """ffmpeg couldn't read the file's audio (a problem with the file, not the GPU)."""


def pcm(src, start: float = 0.0, dur: float | None = None, sr: int = 16000) -> np.ndarray:
    """Mono float32 samples of [start, start+dur) at sr — one window, so a long file is never decoded whole.
    A partly corrupt stretch returns what could be read; nothing readable at all raises DecodeError."""
    r = subprocess.run([FFMPEG, "-v", "error", *(["-ss", f"{start:.3f}"] if start > 0 else []), *LOCAL_ONLY, "-i", str(src),
                        *(["-t", f"{dur:.3f}"] if dur else []), "-vn", "-sn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                       capture_output=True, creationflags=NO_WINDOW)
    if r.returncode and not r.stdout:
        raise DecodeError(f"Couldn't read the audio at {start:.0f}s: {r.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    return np.frombuffer(r.stdout[: len(r.stdout) // 2 * 2], "<i2").astype(np.float32) / 32768.0


# pinned to an opencv_zoo commit (main on 2026-11-25) and its SHA-256: a moved branch can't swap the model under us
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


def face_detector(w: int, h: int):
    """YuNet DNN face detector for w×h frames (model ~230 KB, fetched once into the data folder)."""
    import cv2

    model = fetch_model("face_detection_yunet_2023mar.onnx", YUNET_URL, sha256=YUNET_SHA256)
    try:
        return cv2.FaceDetectorYN.create(str(model), "", (w, h), 0.7)
    except cv2.error:
        model.unlink(missing_ok=True)  # unreadable model: fetch it fresh next time instead of failing forever
        raise


def face_center(src: Path, start: float, end: float, samples: int = 12) -> tuple[float, float, float, float] | None:
    """AI reframe / facecam detection: median face centre and size (fractions of the frame), or None if no face."""
    import cv2

    cap = cv2.VideoCapture(str(src))
    xs, ys, ws, hs = [], [], [], []
    detector = None
    try:
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + (end - start) * (i + 0.5) / samples) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            scale = 640 / max(w, h)
            small = cv2.resize(frame, None, fx=scale, fy=scale)
            if detector is None:
                detector = face_detector(small.shape[1], small.shape[0])
            _, faces = detector.detect(small)
            if faces is not None and len(faces):
                fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])[:4]
                xs.append((fx + fw / 2) / small.shape[1])
                ys.append((fy + fh / 2) / small.shape[0])
                ws.append(fw / small.shape[1])
                hs.append(fh / small.shape[0])
    finally:
        cap.release()
    if not xs:
        return None
    return float(np.median(xs)), float(np.median(ys)), float(np.median(ws)), float(np.median(hs))


def facecam_rect(face: tuple[float, float, float, float], sw: int, sh: int, top_ratio: float = 1080 / 768) -> dict:
    """A facecam crop around a detected face (about 2.6x the face height), shaped like the split layout's top panel."""
    cx, cy, fw, fh = face
    h = min(fh * 2.6, 1.0)
    w = min(h * sh * top_ratio / sw, 1.0)
    x = min(max(cx - w / 2, 0.0), 1.0 - w)
    y = min(max(cy - h / 2, 0.0), 1.0 - h)
    return {"cam_x": round(x, 4), "cam_y": round(y, 4), "cam_w": round(w, 4), "cam_h": round(h, 4)}


# ---------- multi-region layouts ----------
# A clip marks up to MAX_REGIONS source boxes (edit["regions"], 0..1 of the source) with a role; the layout decides where
# each goes in the output. Every region is cover-cropped (centred; the main view at crop_x/crop_y with zoom) to its slot.

LAYOUTS = ("single", "split", "split_rev", "stack3", "pip", "duo", "blurcam", "free")  # split = the stack with cam on top
REGION_LAYOUTS = ("stack3", "pip", "duo", "free")  # need marked regions: never auto-applied to a new clip without them
ROLES = ("main", "cam", "chat", "guest", "extra")
PIP_CORNERS = ("tl", "tr", "bl", "br")
MAX_REGIONS = 4
LAYOUT_KEYS = ("layout", "regions", "split", "split2", "pip_corner", "pip_size", "pip_radius", "pip_border",
               "cam_x", "cam_y", "cam_w", "cam_h")  # what "apply to all" / "save as default" copy
CAMS = ("cam", "guest", "extra", "chat")  # who fills a cam slot when there is no cam region
_FULL = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


def clamp_box(b: dict) -> dict:
    """A 0..1 {x, y, w, h} box clamped inside the frame (ValueError for missing or non-finite numbers)."""
    v = [float(b[k]) for k in "xywh"]
    if not all(map(math.isfinite, v)):
        raise ValueError("non-finite box")
    w, h = min(max(v[2], 0.02), 1.0), min(max(v[3], 0.02), 1.0)
    return {"x": round(min(max(v[0], 0.0), 1.0 - w), 4), "y": round(min(max(v[1], 0.0), 1.0 - h), 4),
            "w": round(w, 4), "h": round(h, 4)}


def regions_of(e: dict) -> list[dict]:
    """The clip's regions. One saved before regions existed gets its facecam box as the cam; no main = the whole frame."""
    rs = [dict(r) for r in e.get("regions") or []] or [{"id": "cam", "role": "cam", **{k: e[f"cam_{k}"] for k in "xywh"}}]
    if not any(r["role"] == "main" for r in rs):
        rs.append({"id": "main", "role": "main", **_FULL})
    return [{**r, **clamp_box(r)} for r in rs]


def seam(e: dict) -> float | None:
    """Where a stacked layout's top panel ends (fraction of the height): "middle" captions sit just under it."""
    if e["aspect"] == "source" or e["layout"] not in ("split", "split_rev", "stack3", "duo"):
        return None
    s = float(e["split"])
    return 1 - s if e["layout"] == "split_rev" else s


def _pick(rs: list, roles: tuple, used: set):
    for role in roles:
        for r in rs:
            if id(r) not in used and r["role"] == role:
                used.add(id(r))
                return r
    return None


def _region_crop(r: dict, ratio: float, sw: int, sh: int, ax: float = 0.5, ay: float = 0.5, zoom: float = 1.0):
    """Cover crop of the region's source box at `ratio` (w, h, x, y in source px), placed at (ax, ay) inside it."""
    rx, ry = round(r["x"] * sw), round(r["y"] * sh)
    rw, rh = min(max(round(r["w"] * sw), 2), sw - rx), min(max(round(r["h"] * sh), 2), sh - ry)
    w, h, x, y = crop_box(rw, rh, ratio, ax, ay, zoom)
    return w, h, rx + x, ry + y


def _pip_box(r: dict, e: dict, W: int, H: int, sw: int, sh: int) -> tuple[int, int, int, int]:
    """Corner cam: pip_size of the width in the cam's own shape (at most half the height), a margin off the corner."""
    ra = (r["w"] * sw) / (r["h"] * sh)
    w = _even(W * float(e["pip_size"]))
    h = _even(w / ra)
    if h > H // 2:
        h = _even(H / 2)
        w = _even(h * ra)
    m, c = _even(W * 0.04), e["pip_corner"]
    return (m if c[1] == "l" else W - w - m), (m if c[0] == "t" else H - h - m), w, h


def feed_of(r: dict, feeds: dict) -> str:
    """Which feed a region reads: its `source` ("main" or a camera-angle id) when that feed is wired up, else "main"."""
    src = r.get("source") or "main"
    return src if src in feeds else "main"


def layout_slots(e: dict, W: int, H: int, sw: int, sh: int, feeds: dict | None = None) -> tuple[str | None, list[dict]]:
    """Where each region goes in the W×H output -> (base, [{r, box (x, y, w, h) px, crop (w, h, x, y) source px, pip, feed}]).
    base None: the boxes tile the frame. "main": the first item fills the frame and the rest sit on it. "blur": a blurred
    copy of the main view lies under everything. A slot whose role has no region shows the main view.
    feeds = {source id: (ffmpeg input index, time offset s, width, height)}; "main" is always input 0 at sw×sh."""
    feeds = {**(feeds or {}), "main": (0, 0.0, sw, sh)}
    size = lambda r: feeds[feed_of(r, feeds)][2:4]  # noqa: E731  a region's box is a fraction of its own feed's frame
    rs, used, lay = regions_of(e), set(), e["layout"]
    main = _pick(rs, ("main",), used)
    s = _even(H * float(e["split"]))
    base, slots = None, []
    if lay == "split_rev":
        slots = [(main, (0, 0, W, H - s)), (_pick(rs, CAMS, used) or main, (0, H - s, W, s))]
    elif lay in ("pip", "blurcam"):
        cam = _pick(rs, CAMS, used)
        if lay == "pip":
            base, slots = "main", [(main, (0, 0, W, H))]
        else:  # the main view fitted (whole) over its own blur
            mw, mh = size(main)
            ra = (main["w"] * mw) / (main["h"] * mh)
            fw, fh = W, _even(W / ra)
            if fh > H:
                fw, fh = _even(H * ra), H
            base, slots = "blur", [(main, ((W - fw) // 2, (H - fh) // 2, fw, fh))]
        if cam:
            slots.append((cam, _pip_box(cam, e, W, H, *size(cam)), True))
    elif lay == "free":  # each region at its own output box ("out"), main first so the others sit on top
        base = "blur"
        for r in [main] + [r for r in rs if r is not main]:
            o = clamp_box(r.get("out") or (_FULL if r is main else {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.3}))
            x, y = round(o["x"] * W), round(o["y"] * H)
            slots.append((r, (x, y, min(_even(o["w"] * W), W - x - x % 2), min(_even(o["h"] * H), H - y - y % 2))))
    else:  # split / stack3 / duo: a top tier, the main view, (stack3) a bottom tier
        cam, top = _pick(rs, CAMS, used) or main, []
        if lay == "duo" and (guest := _pick(rs, ("guest", "extra", "chat", "cam"), used)):
            half = _even(W / 2)
            top = [(cam, (0, 0, half, s)), (guest, (half, 0, W - half, s))]
        third = _pick(rs, ("chat", "guest", "extra", "cam"), used) if lay == "stack3" else None
        t = min(_even(H * float(e["split2"])), H - s - _even(H * 0.15)) if third else 0  # main keeps ≥ 15%
        if t < 2:
            third, t = None, 0
        slots = (top or [(cam, (0, 0, W, s))]) + [(main, (0, s, W, H - s - t))] + ([(third, (0, H - t, W, t))] if third else [])
    out = []
    for r, box, *pip in slots:
        anchor = (float(e["crop_x"]), float(e["crop_y"]), max(float(e["zoom"]), 1.0)) if r is main else ()
        out.append({"r": r, "box": box, "crop": _region_crop(r, box[2] / box[3], *size(r), *anchor), "pip": bool(pip),
                    "feed": feed_of(r, feeds)})
    return base, out


def _pip_style(w: int, h: int, e: dict) -> list[str]:
    """Border (padding in white) and rounded corners (an alpha mask) for a corner cam already scaled to w×h."""
    b = min(int(float(e["pip_border"])), min(w, h) // 4)
    rad = round(float(e["pip_radius"]) * min(w, h))
    out = [f"pad={w}:{h}:{b}:{b}:color=white"] if b else []
    if rad >= 2:
        corner = f"pow(max(0,abs(X-W/2)-{w / 2 - rad:g}),2)+pow(max(0,abs(Y-H/2)-{h / 2 - rad:g}),2)"
        out += ["format=rgba", f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*lte({corner},{rad * rad})'"]
    return out


def _feed_head(feed: tuple, fps: float) -> list[str]:
    """How a camera-angle input enters the graph. feed = (index, offset s, w, h[, span s]): the angle is shifted by its
    offset; with a span it is also padded with black before it starts and after it ends and cut to exactly the clip,
    so it lines up frame for frame with the main video (jump cuts select by time on every feed alike)."""
    off = feed[1]
    if len(feed) < 5:
        return [f"setpts=PTS+{off:.3f}/TB"] if off else []
    pad = (f"start_duration={off:.3f}:" if off > 0 else "") + f"stop_mode=add:stop_duration={feed[4]:.3f}:color=black"
    return ["setpts=PTS-STARTPTS", f"fps={fps:g}", f"tpad={pad}", f"trim=duration={feed[4]:.3f}"]


def switch_windows(e: dict, keeps_rel, speed: float, feeds: dict) -> dict[str, list[tuple[float, float]]]:
    """edit["switches"] ([{t, angle}] cuts in clip time) -> {feed: [(a, b) output seconds]}: when each wired-up feed is the
    live one, through jump cuts and speed like captions. Before the first cut the default view shows (main, or the
    switching region's own source); a cut to an angle that isn't wired up (removed) shows "main"."""
    cuts = sorted((float(s["t"]), s.get("angle") or "main") for s in e.get("switches") or [])
    out_t = (lambda x: remap(x, keeps_rel) / speed) if keeps_rel else (lambda x: x / speed)
    out: dict = {}
    for i, (t, ang) in enumerate(cuts):
        a, b = out_t(t), out_t(cuts[i + 1][0]) if i + 1 < len(cuts) else 1e6
        if b > a:
            out.setdefault(ang if ang in feeds else "main", []).append((a, b))
    return out


def _enable(wins) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in wins)


def _switched(e: dict, sw: int, sh: int, W: int, H: int, pre: list, fps: float, feeds: dict, plan: dict) -> str:
    """Single layout cutting between angles: the main view framed as usual, each angle cover-cropped (centred) to W×H
    and laid over it while it is live. Left unlabelled so the look/caption chain continues from it."""
    follow = None
    if e["aspect"] != "source" and e["follow"]:
        from . import track
        follow = track.crop_filter(e, sw, sh, W / H, (W, H), fps)
    cw, ch, cx, cy = crop_box(sw, sh, W / H, float(e["crop_x"]), float(e["crop_y"]), float(e["zoom"]))
    g = "[0:v]" + ",".join(([follow] + pre if follow else pre + [f"crop={cw}:{ch}:{cx}:{cy}"]) + [f"scale={W}:{H}"]) + "[mc0]"
    live = [(s, w) for s, w in plan.items() if s != "main"]
    for i, (src, wins) in enumerate(live):
        aw, ah = feeds[src][2:4]
        cw, ch, cx, cy = crop_box(aw, ah, W / H, 0.5, 0.5)
        g += f";[{feeds[src][0]}:v]" + ",".join(_feed_head(feeds[src], fps) + pre + [f"crop={cw}:{ch}:{cx}:{cy}", f"scale={W}:{H}"])
        g += f"[mca{i}];[mc{i}][mca{i}]overlay=0:0:enable='{_enable(wins)}'" + (f"[mc{i + 1}]" if i < len(live) - 1 else "")
    return g


def _compose(e: dict, sw: int, sh: int, W: int, H: int, pre: list, fps: float, feeds: dict | None = None,
             plan: dict | None = None) -> str:
    """The regions composed into one W×H picture, left unlabelled so the look/caption chain continues from it.
    Cuts and speed run once per feed before its split, unless a region follows the tracked subject: its crop expression
    needs clip time, so then every branch crops first and cuts after (like the single-layout follow).
    feeds (see layout_slots): a region whose source is another camera angle reads [index:v], shifted by its offset.
    plan (switch_windows): the region e["switch_region"] shows each feed's copy of its box while that feed is live."""
    feeds = {**(feeds or {}), "main": (0, 0.0, sw, sh)}
    base, items = layout_slots(e, W, H, sw, sh, feeds)
    branches = []  # (feed, follow crop or None, rest of the chain, output label)
    merges = []    # a switching region's copies, laid over each other in their windows
    if base == "blur":
        cw, ch, cx, cy = _region_crop(items[0]["r"], W / H, *feeds[items[0]["feed"]][2:4])
        branches.append((items[0]["feed"], None, f"crop={cw}:{ch}:{cx}:{cy},scale={W}:{H},{_blur(24, 2)}", "S0"))
    for n_it, it in enumerate(items):
        x, y, w, h = it["box"]
        b = min(int(float(e["pip_border"])), min(w, h) // 4) if it["pip"] else 0
        fit = f"scale={w - 2 * b}:{h - 2 * b}:force_original_aspect_ratio=increase,crop={w - 2 * b}:{h - 2 * b}"
        follow = None
        if it["r"].get("follow") and it["feed"] == "main":  # the tracked path belongs to the main video
            from . import track
            zr = crop_box(sw, sh, w / h, 0.5, 0.5)[0] / _region_crop(it["r"], w / h, sw, sh)[0]  # region size as a zoom
            fr = e.get("framing") or {}
            z = zr * (float(fr.get("zoom") or e["zoom"] or 1) if it["r"]["role"] == "main" else 1.0)
            follow = track.crop_filter({**e, "zoom": z, "framing": {**fr, "zoom": z}}, sw, sh, w / h, (w, h), fps)
        cw, ch, cx, cy = it["crop"]
        rest = [fit] if follow else [f"crop={cw}:{ch}:{cx}:{cy}", fit]
        pip = _pip_style(w, h, e) if it["pip"] else []
        k = n_it + (base == "blur")  # this item's output label S{k}
        live = [(s, v) for s, v in (plan or {}).items() if s != it["feed"]] if it["r"]["id"] == e.get("switch_region") else []
        if not live:
            branches.append((it["feed"], follow, ",".join(rest + pip), f"S{k}"))
            continue
        branches.append((it["feed"], follow, ",".join(rest), f"S{k}x"))
        cur = f"S{k}x"
        for j, (src, wins) in enumerate(live):
            acw, ach, acx, acy = _region_crop(it["r"], w / h, *feeds[src][2:4])
            branches.append((src, None, f"crop={acw}:{ach}:{acx}:{acy},{fit}", f"S{k}x{j}"))
            last = j == len(live) - 1
            merges.append(f"[{cur}][S{k}x{j}]overlay=0:0:enable='{_enable(wins)}'" + ("," + ",".join(pip) if last and pip else "")
                          + (f"[S{k}]" if last else f"[S{k}m{j}]"))
            cur = f"S{k}m{j}"
    n, per_branch = len(items) + (base == "blur"), any(f for _, f, _, _ in branches)
    heads = []
    for src in dict.fromkeys(s for s, _, _, _ in branches):  # one decoded input per feed, split into its branches
        mine = [i for i, (s, _, _, _) in enumerate(branches) if s == src]
        chain = _feed_head(feeds[src], fps) + ([] if per_branch else pre)
        chain.append(f"split={len(mine)}" if len(mine) > 1 else "null")
        heads.append(f"[{feeds[src][0]}:v]" + ",".join(chain) + "".join(f"[L{i}]" for i in mine))
    g = ";".join(heads)
    for i, (_, follow, rest, label) in enumerate(branches):
        g += f";[L{i}]" + ",".join(([follow] if follow else []) + (pre if per_branch else []) + [rest]) + f"[{label}]"
    g += "".join(";" + m for m in merges)
    if base is None:
        tiles = "|".join(f"{x}_{y}" for x, y, _, _ in (it["box"] for it in items))
        return g + ";" + "".join(f"[S{i}]" for i in range(n)) + f"xstack=inputs={n}:layout={tiles}:fill=black"
    pos = [None] * (n - len(items)) + [it["box"][:2] for it in items]
    cur = "S0"
    for i in range(1, n):
        g += f";[{cur}][S{i}]overlay={pos[i][0]}:{pos[i][1]}" + (f"[V{i}]" if i < n - 1 else "")
        cur = f"V{i}"
    return g


def text_column(frames) -> tuple[float, float, float, float] | None:
    """A chat box (x, y, w, h fractions): a side column (outer 30%) with far more fine edges, i.e. text, than the middle.
    ponytail: edge density only; a busy HUD at the edge can fool it, which is why it's a suggestion the user adjusts."""
    import cv2

    edges = np.mean([cv2.Canny(f, 80, 160) > 0 for f in frames], axis=0)
    H, W = edges.shape
    k = int(W * 0.3)
    mid = float(edges[:, k:W - k].mean())
    for x0 in (W - k, 0):
        col = edges[:, x0:x0 + k]
        d = float(col.mean())
        if d < 0.02 or d < 2.5 * mid:
            continue
        xs, ys = np.nonzero(col.mean(axis=0) > d * 0.5)[0], np.nonzero(col.mean(axis=1) > d * 0.25)[0]
        if len(xs) and len(ys):
            pw, ph = W * 0.01, H * 0.01
            a, b = max(x0 + xs.min() - pw, 0), min(x0 + xs.max() + 1 + pw, W)
            c, d2 = max(ys.min() - ph, 0), min(ys.max() + 1 + ph, H)
            return a / W, c / H, (b - a) / W, (d2 - c) / H
    return None


def _avoid(rect: tuple, box: tuple, min_area: float = 0.4) -> tuple:
    """The largest part of rect (x, y, w, h) clear of box, if it keeps ≥ min_area of the frame; else rect as it is."""
    x, y, w, h = rect
    bx, by, bw, bh = box
    if bx >= x + w or bx + bw <= x or by >= y + h or by + bh <= y:
        return rect
    best = max([(x, y, bx - x, h), (bx + bw, y, x + w - bx - bw, h), (x, y, w, by - y), (x, by + bh, w, y + h - by - bh)],
               key=lambda c: c[2] * c[3])
    return best if best[2] > 0 and best[3] > 0 and best[2] * best[3] >= min_area else rect


def suggest_regions(face, frames, sw: int, sh: int) -> list[dict]:
    """Regions from a detected face (face_center) and small gray frames: the cam around the face, a chat column if one
    stands out, and the main view = the biggest part of the frame clear of both."""
    reg = lambda rid, b: {"id": rid, "role": rid, **clamp_box(dict(zip("xywh", b))), "follow": False}  # noqa: E731
    out, rect, cam = [], (0.0, 0.0, 1.0, 1.0), None
    if face:
        c = facecam_rect(face, sw, sh, 4 / 3)
        cam = (c["cam_x"], c["cam_y"], c["cam_w"], c["cam_h"])
        out.append(reg("cam", cam))
    chat = text_column(frames) if len(frames) else None
    if chat and cam:
        chat = _avoid(chat, cam, 0.0)
    if chat:
        rect = _avoid(rect, chat)
    if cam:
        rect = _avoid(rect, cam)
    out.append(reg("main", rect))
    if chat:
        out.append(reg("chat", chat))
    return out


def detect_regions(src: Path, start: float, end: float, sw: int, sh: int) -> list[dict]:
    """Auto-suggested regions for a clip (face = the YuNet detector tracking uses; chat = text_column on a few frames)."""
    import cv2

    from . import track

    face = face_center(src, start, end)
    w, h = track._even_size(sw, sh, 320)
    frames = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in track.decode(src, start, end, w, h, 6 / max(end - start, 0.5))]
    return suggest_regions(face, frames, sw, sh)


# ---------- rendering ----------

def crop_box(sw: int, sh: int, ratio: float, cx: float, cy: float, zoom: float = 1.0) -> tuple[int, int, int, int]:
    """Largest crop with the target ratio (divided by `zoom` for a punch-in), centred on (cx, cy) in 0..1, clamped, even."""
    zoom = max(float(zoom), 1.0)
    if sw / sh > ratio:
        h = sh / zoom
        w = h * ratio
    else:
        w = sw / zoom
        h = w / ratio
    w, h = int(round(w)), int(round(h))
    w, h = w - w % 2, h - h % 2
    x = min(max(round(sw * cx - w / 2), 0), sw - w)
    y = min(max(round(sh * cy - h / 2), 0), sh - h)
    return w, h, x, y


def output_size(aspect: str, sw: int, sh: int) -> tuple[int, int]:
    if aspect in OUTPUT_SIZES:
        return OUTPUT_SIZES[aspect]
    return sw - sw % 2, sh - sh % 2


def keep_segments(silences: list, start: float, end: float, pad: float = 0.12, min_gap: float = 0.45) -> list[tuple[float, float]]:
    """Jump cuts: the parts of [start, end] that are NOT silence (keeping `pad` seconds of air around speech)."""
    keeps, cur = [], start
    for s, e in sorted(silences):
        s2, e2 = max(s + pad, start), min(e - pad, end)
        if e2 - s2 < min_gap:
            continue
        if s2 > cur:
            keeps.append((cur, s2))
        cur = max(cur, e2)
    if end > cur:
        keeps.append((cur, end))
    keeps = [(round(a, 3), round(b, 3)) for a, b in keeps if b - a > 0.05]
    return keeps or [(start, end)]


FILLERS = {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "ah", "ahh", "hmm", "mm", "mhm"}


def filler_spans(words: list[dict], start: float, end: float) -> list[tuple[float, float]]:
    """Time spans of filler words ("um", "uh"...) inside [start, end], from Whisper's word timestamps."""
    return [(w["s"], w["e"]) for w in words
            if start <= w["s"] < end and w["w"].strip().strip(".,!?…-").lower() in FILLERS]


def bleep_list(raw) -> list[str]:
    """Settings › Rendering's bleep words ("fuck*, shit, ...": whole words, * = any ending) as clean lowercase entries."""
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    return [w for w in (str(x).strip().lower()[:40] for x in items[:300]) if w.strip("*")]


def _bad(word: str, bad: list[str]) -> bool:
    t = re.sub(r"[^\w']+", "", word.lower()).strip("'")
    return bool(t) and any(t == b or b.endswith("*") and t.startswith(b[:-1]) for b in bad)


def bleep_spans(words: list[dict], start: float, end: float, bad: list[str]) -> list[tuple[float, float]]:
    """Time spans of bleep-list words inside [start, end] (source seconds, from Whisper's word timestamps)."""
    return [(w["s"], w["e"]) for w in words if start <= w["s"] < end and _bad(w["w"], bad)]


def censor(words: list[dict], bad: list[str]) -> list[dict]:
    """Caption words with bleep-list words starred out after their first letter ("fucking," → "f******,")."""
    return [{**w, "w": re.sub(r"\w[\w']*", lambda m: m[0][0] + "*" * (len(m[0]) - 1), w["w"], count=1)} if _bad(w["w"], bad) else w
            for w in words]


def cut_spans(keeps: list[tuple[float, float]], spans: list[tuple[float, float]], min_len: float = 0.08):
    """Remove `spans` from the kept intervals (filler words on top of silence jump cuts)."""
    if not any(s < b and e > a for s, e in spans for a, b in keeps):
        return keeps  # nothing to cut: hand back the exact input so the render takes the plain (no-select) path
    out = []
    for a, b in keeps:
        cur = a
        for s, e in sorted(spans):
            if e <= cur or s >= b:
                continue
            if s - cur >= min_len:
                out.append((round(cur, 3), round(s, 3)))
            cur = max(cur, e)
        if b - cur >= min_len:
            out.append((round(cur, 3), round(b, 3)))
    return out or keeps


def remap(t: float, keeps: list[tuple[float, float]]) -> float:
    """Source time -> time in the jump-cut output (times inside a removed gap snap to the next kept frame)."""
    acc = 0.0
    for a, b in keeps:
        if t < a:
            return acc
        if t <= b:
            return acc + t - a
        acc += b - a
    return acc


def _atempo(speed: float) -> list[str]:
    out = []
    while speed > 2.0:
        out.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        out.append("atempo=0.5")
        speed /= 0.5
    if abs(speed - 1) > 1e-3:
        out.append(f"atempo={speed:.4f}")
    return out


def _even(v: float) -> int:
    v = int(round(v))
    return max(v - v % 2, 2)


def _between(keeps_rel) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keeps_rel)


def overlay_window(t: float, d: float, start: float, keeps, speed: float) -> tuple[float, float]:
    """An overlay's [t, t+d] in clip time -> its window in the output (through jump cuts and speed, like captions)."""
    m = (lambda x: remap(start + x, keeps)) if keeps else (lambda x: x)
    a, b = m(t) / speed, m(t + d) / speed
    return a, max(b, a + 0.04)


# ---------- timeline effects (edit.effects, validated by server._effects) ----------

def effect_windows(effects, start: float, keeps, speed: float) -> list[dict]:
    """edit.effects (clip time) -> output time: each window through jump cuts and speed like an overlay's, and each
    keyframe's time with it (absolute output seconds)."""
    m = (lambda x: remap(start + x, keeps)) if keeps else (lambda x: x)
    out = []
    for fx in effects or []:
        a, b = overlay_window(float(fx["t"]), float(fx["d"]), start, keeps, speed)
        keys = [[round(m(float(fx["t"]) + float(k[0])) / speed, 3), *k[1:]] for k in fx.get("keys") or []]
        out.append({**fx, "a": round(a, 3), "b": round(b, 3), "keys": keys})
    return out


SECTION_KEYS = ("layout", "regions", "split", "split2", "pip_corner", "pip_size", "pip_radius", "pip_border",
                "crop_x", "crop_y", "zoom")  # what a timeline layout section (edit.layout_segments) may override


def section_windows(sections, start: float, keeps, speed: float) -> list[dict]:
    """edit.layout_segments ({s, e} clip time + layout overrides) -> output time {a, b, **overrides}, through jump cuts and
    speed like an overlay's window."""
    out = []
    for sec in sections or []:
        if keeps and not any(k0 < start + float(sec["e"]) and k1 > start + float(sec["s"]) for k0, k1 in keeps):
            continue  # wholly inside a cut: nothing of it is in the output
        a, b = overlay_window(float(sec["s"]), float(sec["e"]) - float(sec["s"]), start, keeps, speed)
        out.append({**{k: v for k, v in sec.items() if k in SECTION_KEYS}, "a": round(a, 3), "b": round(b, 3)})
    return out


def _relabel(g: str, tag: str) -> str:
    """Every [label] of a graph made unique with `tag` (input pads like [0:v] start with a digit and stay as they are)."""
    return re.sub(r"\[([A-Za-z]\w*)\]", lambda m: f"[{m[1]}{tag}]", g)


def _lerp(keys: list, var: str, i: int = 1) -> str:
    """Keyframes [[t, v1, v2…], …] (sorted, output s) as a piecewise-linear expression of `var` for value i; flat outside."""
    expr = f"{keys[-1][i]:g}"
    for k0, k1 in reversed(list(zip(keys, keys[1:]))):
        (t0, a), (t1, b) = (k0[0], k0[i]), (k1[0], k1[i])
        seg = f"{a:g}+({b - a:g})*({var}-{t0:.3f})/{t1 - t0:.3f}" if t1 - t0 >= 0.001 else f"{b:g}"
        expr = f"if(lt({var},{t1:.3f}),{seg},{expr})"
    return f"if(lt({var},{keys[0][0]:.3f}),{keys[0][i]:g},{expr})"


def _effect_filters(effects: list, W: int, H: int, rate: float) -> tuple[list[str], list[str]]:
    """(picture filters: zoom + blur, under the overlays; dip filters: opacity, over them and under the captions).
    Zoom x/y place the zoomed window (0 = left/top edge, 0.5 = centred, 1 = right/bottom), so it never leaves the frame.
    ponytail: zoompan re-times frames at `rate` (CFR); a variable-frame-rate source drifts slightly while zoomed."""
    on = lambda fx, v: f"between({v},{fx['a']:.3f},{fx['b']:.3f})"  # noqa: E731
    pic, dip = [], []
    zs = [fx for fx in effects if fx["kind"] == "zoom" and fx.get("keys")]
    if zs:
        z = "1" + "".join(f"+if({on(fx, 'it')},{_lerp(fx['keys'], 'it')}-1,0)" for fx in zs)
        cx = cy = "0.5"
        for fx in reversed(zs):  # the earliest active zoom places the window
            cx, cy = (f"if({on(fx, 'it')},{_lerp(fx['keys'], 'it', i)},{prev})" for i, prev in ((2, cx), (3, cy)))
        pic.append(f"zoompan=z='max(1,min(4,{z}))':x='(iw-iw/zoom)*({cx})':y='(ih-ih/zoom)*({cy})':d=1:s={W}x{H}:fps={rate:g}")
    pic += [f"{_blur(int(fx['amount']), 2)}:enable='{on(fx, 't')}'" for fx in effects if fx["kind"] == "blur"]
    ops = [fx for fx in effects if fx["kind"] == "opacity" and fx.get("keys")]
    if ops:
        k = "*".join(f"if({on(fx, '{v}')},{_lerp(fx['keys'], '{v}')},1)" for fx in ops)
        if has_filter("eq"):  # y·k: contrast k around 0.5 plus brightness (k-1)/2; saturation k greys the chroma with it
            k = k.replace("{v}", "t")
            dip.append(f"eq=contrast='{k}':brightness='({k}-1)/2':saturation='{k}':eval=frame")
        else:  # LGPL builds lack eq: the same multiply per pixel (slow, but only on clips that use it)
            k = k.replace("{v}", "T")
            dip.append(f"geq=lum='lum(X,Y)*{k}':cb='128+(cb(X,Y)-128)*{k}':cr='128+(cr(X,Y)-128)*{k}'")
    return pic, dip


def _overlay_chain(o: dict, i: int, W: int, H: int) -> str:
    """[b{i}] + overlay input -> [b{i+1}]: sized, shifted to its window, animated with per-frame expressions."""
    a, b = o["a"], o["b"]
    if o.get("cutaway"):
        size, X, Y = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}", "0", "0"
    else:
        cx, cy = float(o.get("x", 0.5)) * W, float(o.get("y", 0.5)) * H
        size, X, Y = f"scale={_even(W * float(o.get('w', 0.3)))}:-2", f"{cx:.1f}-w/2", f"{cy:.1f}-h/2"
        if o.get("circle"):  # the streamer card's avatar: cover-cropped to a d×d square, masked round like a corner cam
            d = int(o["circle"])
            size = ",".join([f"scale={d}:{d}:force_original_aspect_ratio=increase,crop={d}:{d}"]
                            + _pip_style(d, d, {"pip_border": 0, "pip_radius": 0.5}))
    chain = f"[{o['input']}:v]{size},format=rgba,setpts=PTS-STARTPTS+{a:.3f}/TB"
    anim, since = o.get("anim", "none"), f"(t-{a:.3f})"
    if anim == "fade":
        chain += f",fade=t=in:st={a:.3f}:d=0.25:alpha=1,fade=t=out:st={max(a, b - 0.25):.3f}:d=0.25:alpha=1"
    elif anim == "pop":  # drops in with a damped bounce
        Y += f"-{H * 0.05:.0f}*exp(-{since}*9)*cos({since}*28)"
        chain += f",fade=t=in:st={a:.3f}:d=0.12:alpha=1"
    elif anim == "shake":
        X, Y = X + f"+{W * 0.012:.0f}*sin(t*63)", Y + f"+{H * 0.008:.0f}*cos(t*47)"
    elif anim == "slide":  # in from the left over 0.3 s (slide_px: by that distance, in step with the card's ASS \move)
        X = f"({X})-{o['slide_px']}*max(0,1-{since}/0.3)" if o.get("slide_px") else f"({X})-({X}+w)*max(0,1-{since}/0.3)"
    elif anim == "rise":  # the streamer card's pop: up from rise_px below over 0.2 s while it fades in
        Y += f"+{o['rise_px']}*max(0,1-{since}/0.2)"
        chain += f",fade=t=in:st={a:.3f}:d=0.15:alpha=1,fade=t=out:st={max(a, b - 0.25):.3f}:d=0.25:alpha=1"
    return (f"{chain}[o{i}];[b{i}][o{i}]overlay=x='{X}':y='{Y}':eof_action=pass:"
            f"enable='between(t,{a:.3f},{b:.3f})'[b{i + 1}]")


def _picture(e: dict, sw: int, sh: int, W: int, H: int, pre: list, fps: float, feeds, plan: dict, reframed: bool) -> str:
    """The framed picture (W×H, unlabelled) for one layout: build_filters' video before its look, overlays and captions."""
    if reframed and e["layout"] == "split" and not e.get("regions"):  # a split clip from before regions: its graph as it was
        th = _even(H * float(e["split"]))
        bh = H - th
        cw, ch = _even(sw * float(e["cam_w"])), _even(sh * float(e["cam_h"]))
        cx = min(max(int(sw * float(e["cam_x"])), 0), sw - cw)
        cy = min(max(int(sh * float(e["cam_y"])), 0), sh - ch)
        mw, mh, mx, my = crop_box(sw, sh, W / bh, float(e["crop_x"]), float(e["crop_y"]), float(e["zoom"]))
        g = (f"[0:v]{','.join(pre + ['split'])}[c][m];"
             f"[c]crop={cw}:{ch}:{cx}:{cy},scale={W}:{th}:force_original_aspect_ratio=increase,crop={W}:{th}[top];"
             f"[m]crop={mw}:{mh}:{mx}:{my},scale={W}:{bh}[bot];[top][bot]vstack")
    elif reframed and e["layout"] in LAYOUTS[1:]:
        g = _compose(e, sw, sh, W, H, pre, fps or 30, feeds, plan)
    elif set(plan) - {"main"}:  # single layout cutting between camera angles (fit + blur doesn't apply to a cut)
        g = _switched(e, sw, sh, W, H, pre, fps or 30, {**feeds, "main": (0, 0.0, sw, sh)}, plan)
    elif reframed and e["fit"] == "blur":
        g = (f"[0:v]{','.join(pre + ['split'])}[a][b];"
             f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},{_blur(24, 2)}[bg];"
             f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    else:
        chain = list(pre)
        follow = None
        if reframed and e["follow"]:
            from . import track
            follow = track.crop_filter(e, sw, sh, W / H, (W, H), fps or 30)
        if follow:
            chain.insert(0, follow)  # before jump cuts / speed, so the expression's t is still clip time
        elif reframed or float(e["zoom"]) > 1:
            cw, ch, cx, cy = crop_box(sw, sh, W / H, float(e["crop_x"]), float(e["crop_y"]), float(e["zoom"]))
            chain.append(f"crop={cw}:{ch}:{cx}:{cy}")
        chain.append(f"scale={W}:{H}")
        g = "[0:v]" + ",".join(chain)
    return g


def build_filters(edit: dict, sw: int, sh: int, out_dur: float, has_subs: bool, keeps_rel=None,
                  has_audio: bool = True, music: bool = False, fps: float = 30, overlays=None,
                  fontsdir: bool = False, feeds: dict | None = None, headline: dict | None = None,
                  bleeps: list | None = None, effects: list | None = None,
                  sections: list | None = None) -> tuple[str, tuple[int, int]]:
    """One filter_complex producing [v] (and [a] when there is any audio). keeps_rel = jump-cut ranges relative to the clip.
    feeds = extra camera-angle inputs for split-screen regions, {angle id: (input index, offset s, width, height)}.
    headline = a headline_spec(...): its band (push) letterboxes the picture, a glass theme blurs the video under its bar;
    the bar and text themselves are drawn by the ASS file (build_ass). bleeps = output-time spans the voice is muted
    (and beeped, edit "bleep") over."""
    e = {**DEFAULT_EDIT, **edit}
    speed = float(e["speed"])
    W, H = output_size(e["aspect"], sw, sh)
    lay = headline_layout(headline, W, H, seam(e)) if headline else None
    full_h, band = H, lay and lay["band"]
    if band:
        H -= band[1]  # every layout below composes into the area left under/over the banner
    pre = []
    if keeps_rel:
        pre += [f"select='{_between(keeps_rel)}'", "setpts=N/FRAME_RATE/TB"]
    if abs(speed - 1) > 1e-3:
        pre.append(f"setpts=PTS/{speed:g}")
    reframed = e["aspect"] != "source"
    plan = switch_windows(e, keeps_rel, speed, {**(feeds or {}), "main": None}) if feeds and e.get("switches") else {}
    g = _picture(e, sw, sh, W, H, pre, fps, feeds, plan, reframed)
    for i, sec in enumerate(sections or []):  # a timeline section's own layout, laid over the clip's while it runs
        # ponytail: each section composes its picture for the whole clip and shows it in its window (simple, exact frames);
        # trim each branch to its window if many sections on long clips get slow
        sub = _picture({**e, **{k: v for k, v in sec.items() if k in SECTION_KEYS}}, sw, sh, W, H, pre, fps, feeds, plan, reframed)
        g = f"{g}[lsb{i}];{_relabel(sub, f'_s{i}')}[lss{i}];[lsb{i}][lss{i}]overlay=0:0:enable='between(t,{sec['a']:.3f},{sec['b']:.3f})'"
    if band:
        g += f",pad={W}:{full_h}:0:{band[1] if band[0] == 0 else 0}:color=black"
        H = full_h
    if lay and headline["blur"]:  # glass: a blurred copy of the picture under the translucent bar, corners rounded to match
        x, y, w, h = lay["bar"]
        rad = round(float(headline["radius"]) * lay["k"])
        mask = _pip_style(w, h, {"pip_border": 0, "pip_radius": rad / min(w, h)})
        en = f":enable='lte(t,{float(headline['seconds']):.3f})'" if headline["show"] != "whole" else ""
        g += (f",split[hg0][hg1];[hg1]crop={w}:{h}:{x}:{y},{_blur(max(min(18, h // 4 - 1), 1), 2)}{''.join(',' + m for m in mask)}[hg2];"
              f"[hg0][hg2]overlay={x}:{y}{en}")
    post = ["setsar=1"]
    b, c, s, gm = (float(e[k]) for k in ("brightness", "contrast", "saturation", "gamma"))
    if (b, c, s, gm) != (0.0, 1.0, 1.0, 1.0):
        post.append(_eq(b, c, s, gm))
    if e["sharpen"]:
        post.append("unsharp=5:5:0.8")
    look, post = post, []  # overlays go on the finished picture, under fades and captions
    if effects:  # timeline effects = effect_windows(...): zoom/blur with the look, opacity over the overlays
        pic, post = _effect_filters(effects, W, H, (fps or 30) * speed)
        look += pic
    fi, fo = float(e["fade_in"]), float(e["fade_out"])
    if fi > 0:
        post.append(f"fade=t=in:st=0:d={fi:.2f}")
    if fo > 0:
        post.append(f"fade=t=out:st={max(out_dur - fo, 0):.2f}:d={fo:.2f}")
    if has_subs:
        # relative paths: ffmpeg runs with cwd=workdir (avoids Windows path escaping); fonts/ holds uploaded fonts
        post.append("subtitles=captions.ass" + (":fontsdir=fonts" if fontsdir else ""))
    post.append("format=yuv420p")
    vis = sorted((o for o in overlays or [] if o["kind"] in ("image", "gif", "video")), key=lambda o: o.get("layer", 0))
    if vis:
        g += "," + ",".join(look) + "[b0]"
        for i, o in enumerate(vis):
            g += ";" + _overlay_chain(o, i, W, H)
        g += f";[b{len(vis)}]" + ",".join(post) + "[v]"
    else:
        g += "," + ",".join(look + post) + "[v]"

    asrc, src = "[0:a]", e.get("audio_source") or "main"
    if src != "main" and src in (feeds or {}):  # the sound of a camera angle, lined up and padded like its picture
        f = feeds[src]
        asrc = (f"[{f[0]}:a]asetpts=PTS-STARTPTS" + (f",adelay={round(f[1] * 1000)}:all=1" if f[1] > 0 else "")
                + (f",apad,atrim=0:{f[4]:.3f}" if len(f) > 4 else "") + "[asrc];[asrc]")
    af = []
    if keeps_rel:
        af += [f"aselect='{_between(keeps_rel)}'", "asetpts=N/SR/TB"]
    af += _atempo(speed)
    if abs(float(e["volume"]) - 1) > 1e-3:
        af.append(f"volume={float(e['volume']):.3f}")
    if e["denoise"]:
        af.append("afftdn=nf=-25")
    beep = ""
    if bleeps and has_audio and e["bleep"] in ("beep", "mute"):  # bleeps = output-time spans (render maps the words)
        on = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in bleeps)
        af.append(f"volume=0:enable='{on}'")
        if e["bleep"] == "beep":
            beep = f"sine=f=1000:r=48000,atrim=0:{out_dur:.3f},volume='if({on},2,0)':eval=frame"
    pre_af = list(af)
    if e["loudnorm"]:
        af.append("loudnorm=I=-14:TP=-1.5:LRA=11")  # YouTube/TikTok playback loudness target
    if fi > 0:
        af.append(f"afade=t=in:st=0:d={fi:.2f}")
    if fo > 0:
        af.append(f"afade=t=out:st={max(out_dur - fo, 0):.2f}:d={fo:.2f}")
    bed = f"[1:a]volume={float(e['music_volume']):.3f},atrim=0:{out_dur:.3f},asetpts=PTS-STARTPTS"
    chain = ",".join(af) or "anull"
    sfx = [o for o in overlays or [] if o["kind"] == "sound" or o["kind"] == "video" and o.get("audio") and o.get("volume", 1) > 0]
    stereo = "aresample=48000,aformat=channel_layouts=stereo"
    fx = "".join(f";[{o['input']}:a]atrim=0:{float(o['d']):.3f},asetpts=PTS-STARTPTS,{stereo},"  # each effect at its start
                 f"volume={float(o.get('volume', 1)):.2f},adelay={int(round(o['a'] * 1000))}:all=1[s{i}]" for i, o in enumerate(sfx))
    if music or beep:  # dialogue, effects, beeps and music summed first; loudness and fades then go over the whole mix
        dlg = (f"{asrc}{','.join(pre_af + ([stereo] if sfx else [])) or 'anull'}" if has_audio
               else f"anullsrc=r=48000:cl=stereo,atrim=0:{out_dur:.3f}" if sfx else None)
        extra = [f"[s{i}]" for i in range(len(sfx))] + (["[bp]"] if beep else [])
        g += fx + (f";{beep}[bp]" if beep else "") + ";" + mix_audio(dlg, bed if music else None, extra, af[len(pre_af):],
                                                                    music and has_audio and e["music_duck"])
    elif sfx:  # each effect delayed to its start, mixed under the dialogue, then the usual loudness/fades over the whole mix
        base = f"{asrc}{','.join(pre_af + [stereo])}" if has_audio else f"anullsrc=r=48000:cl=stereo,atrim=0:{out_dur:.3f}"
        tail = ",".join(af[len(pre_af):]) or "anull"
        g += f";{base}[a0]{fx};[a0]{''.join(f'[s{i}]' for i in range(len(sfx)))}amix=inputs={len(sfx) + 1}:duration=first:" \
             f"dropout_transition=0:normalize=0,{tail}[a]"
    elif has_audio:
        g += f";{asrc}{chain}[a]"
    return g, (W, H)


DUCK = "sidechaincompress=threshold=0.03:ratio=8:attack=15:release=350"  # ~-12 dB on the music under normal speech


def mix_audio(dlg: str | None, bed: str | None, extra: list[str], tail: list[str], duck: bool) -> str:
    """The final audio mix → [a]. dlg / bed = the dialogue and music-bed chains (a label can follow them), extra = more
    labelled inputs (effects, beeps). Everything is summed first, then `tail` (loudness, fades) runs over the whole mix,
    so the music is normalised and faded with the rest; duck = the music dips under the dialogue (sidechain)."""
    end = "".join("," + f for f in tail)
    if not dlg and not extra:
        return f"{bed}{end}[a]"
    if dlg and bed and duck:
        g = f"{dlg},asplit[dlg][key];{bed}[bed];[bed][key]{DUCK}[mus];"
    else:
        g = (f"{dlg}[dlg];" if dlg else "") + (f"{bed}[mus];" if bed else "")
    ins = (["[dlg]"] if dlg else []) + (["[mus]"] if bed else []) + extra
    return g + f"{''.join(ins)}amix=inputs={len(ins)}:duration=first:dropout_transition=0:normalize=0{end}[a]"


def _ass_time(t: float) -> str:
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _ass_text(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ass_color(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}&".upper() if len(h) == 6 else "&H0000D4FF&"


def caption_chunks(words: list[dict], start: float, end: float, max_words: int, keeps=None) -> list[list[dict]]:
    """Group clip words into short caption chunks, breaking on pauses and sentence ends (dropping jump-cut words)."""
    inside = [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05 and w["w"].strip()]
    if keeps:
        inside = [w for w in inside if any(a <= (w["s"] + w["e"]) / 2 <= b for a, b in keeps)]
    chunks, cur = [], []
    for w in inside:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > 0.6 or cur[-1]["w"].rstrip()[-1:] in ".?!"):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


CAPTION_POS = {"low": (0.24, 0.08), "middle": (0.42, 0.40), "high": (0.68, 0.72)}  # fraction of height from bottom (vertical, landscape)
TEXT_FIELDS = ("font", "size", "weight", "color", "outline", "outline_color", "shadow", "spacing")


def safe_font(v) -> str:
    """A font family name that can't break out of ASS: no override braces, backslashes, commas (Style fields) or
    control characters / line breaks, at most 80 characters."""
    return re.sub(r"[{}\\,\x00-\x1f\x7f]", "", str(v or ""))[:80].strip()


def _restyle(line: str, st: dict, W: int) -> str:
    """Apply the set text-style fields to one ASS "Style:" line (unset fields keep the caption_style look)."""
    f = line.split(",")
    for key, idx, val in (("font", [1], lambda v: safe_font(v) or f[1]), ("size", [2], lambda v: str(round(float(v) * W / 1080))),
                          ("color", [3, 4], _ass_color), ("outline_color", [5], _ass_color),
                          ("weight", [7], lambda v: "-1" if v == "bold" else "0"), ("spacing", [13], lambda v: f"{float(v):g}"),
                          ("outline", [16], lambda v: f"{float(v):g}"), ("shadow", [17], lambda v: f"{float(v):g}")):
        if st.get(key) is not None:
            for i in idx:
                f[i] = val(st[key])
    return ",".join(f)


def _text_tags(o: dict, W: int) -> str:
    """Inline ASS overrides for a styled text/emoji overlay."""
    tags = {"font": lambda v: f"\\fn{safe_font(v)}", "size": lambda v: f"\\fs{round(float(v) * W / 1080)}",
            "weight": lambda v: f"\\b{1 if v == 'bold' else 0}", "color": lambda v: f"\\c{_ass_color(v)}",
            "outline": lambda v: f"\\bord{float(v):g}", "outline_color": lambda v: f"\\3c{_ass_color(v)}",
            "shadow": lambda v: f"\\shad{float(v):g}", "spacing": lambda v: f"\\fsp{float(v):g}"}
    return "".join(fn(o[k]) for k, fn in tags.items() if o.get(k) is not None)


OVER_ANIM = {"pop": "\\fscx30\\fscy30\\t(0,150,\\fscx115\\fscy115)\\t(150,260,\\fscx100\\fscy100)", "fade": "\\fad(200,200)",
             "shake": "\\t(0,70,\\frz6)\\t(70,140,\\frz-6)\\t(140,210,\\frz3)\\t(210,280,\\frz0)"}  # ASS tags per text anim
STICKER_ALIGN = {"top-left": 7, "top-center": 8, "top-right": 9, "bottom-left": 1, "bottom-right": 3}


# ---------- headline banner ("headliner" + kicker / "top line") ----------
# Fonts a theme may use: the UI's bundled fallback list (app.js FONT_FALLBACK), so the gallery cards draw what renders.
HEADLINE_FONTS = ("Arial", "Arial Black", "Bahnschrift", "Calibri", "Cambria", "Candara", "Comic Sans MS", "Consolas", "Constantia",
                  "Corbel", "Courier New", "Franklin Gothic Medium", "Gabriola", "Georgia", "Impact", "Lucida Console",
                  "Palatino Linotype", "Segoe UI", "Segoe UI Black", "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana")
# sizes/paddings are px at 1080 wide; cw = average character width as a share of the font size (fitting, uppercase-ish)
_TH = dict(font="Arial Black", weight="bold", size=78, color="#FFFFFF", outline=0, outline_color="#000000", shadow=0, spacing=0,
           uppercase=True, cw=0.53, bar="#E8262B", bar2=None, bar_opacity=1.0, bar_border=0, bar_border_color="#000000",
           radius=18, pad=26, accent=None, accent_side="bottom", kicker_font="Arial Black", kicker_color="#111111",
           kicker_bg="#FFD400", kicker_layout="above", kicker_scale=0.5, glow=0, tilt=0, blur=False)  # kicker size = scale × size
HEADLINE_THEME_KEYS = {"name", *_TH}


def _theme(name: str, **kw) -> dict:
    return {"name": name, **_TH, **kw}


HEADLINE_THEMES = {
    "bold_bar": _theme("Bold Bar"),
    "gradient": _theme("Gradient", font="Segoe UI Black", cw=0.5, bar="#7B2FF7", bar2="#F7743B", radius=28,
                       kicker_bg="#FFFFFF", kicker_color="#7B2FF7"),
    "outline_pop": _theme("Outline Pop", font="Impact", size=96, cw=0.39, color="#FFE14D", outline=7, shadow=4, bar=None, tilt=-2,
                          kicker_font="Impact", kicker_bg=None, kicker_color="#FFFFFF", kicker_scale=0.6),
    "glass": _theme("Glass", font="Segoe UI Black", size=72, cw=0.48, bar="#10141C", bar_opacity=0.55, radius=32, outline=2,
                    outline_color="#0B0D12", shadow=3,
                    uppercase=False, blur=True, kicker_font="Segoe UI", kicker_bg="#FFFFFF"),
    "minimal": _theme("Minimal", font="Segoe UI", size=70, cw=0.45, bar=None, outline=4, shadow=4, uppercase=False,
                      kicker_font="Segoe UI", kicker_bg=None, kicker_color="#FFFFFF", kicker_scale=0.62),
    "neon": _theme("Neon", font="Bahnschrift", size=80, cw=0.44, outline=3, outline_color="#FF2BD6", glow=4, bar="#0B0B1A",
                   bar_opacity=0.7, bar_border=4, bar_border_color="#FF2BD6", radius=22, kicker_font="Bahnschrift",
                   kicker_bg="#22E6FF", kicker_color="#0B0B1A"),
    "news_ticker": _theme("News Ticker", font="Franklin Gothic Medium", size=64, cw=0.45, color="#111111", bar="#FFFFFF", radius=0,
                          pad=20, accent="#D7141A", kicker_layout="left", kicker_bg="#D7141A", kicker_color="#FFFFFF"),
    "comic": _theme("Comic", font="Comic Sans MS", size=78, cw=0.47, color="#111111", bar="#FFE14D", bar_border=6,
                    bar_border_color="#111111", radius=14, tilt=-2, kicker_font="Comic Sans MS", kicker_bg="#FF4D4D",
                    kicker_color="#FFFFFF"),
    "gaming_hud": _theme("Gaming HUD", font="Bahnschrift", size=76, cw=0.44, color="#E9FFF4", bar="#0A0F14", bar_opacity=0.85,
                         radius=0, bar_border=2, bar_border_color="#39FF88", accent="#39FF88", accent_side="left",
                         kicker_font="Consolas", kicker_bg="#39FF88", kicker_color="#0A0F14"),
    "podcast_card": _theme("Podcast Card", font="Georgia", size=66, cw=0.45, color="#151515", bar="#FFFFFF", radius=36, pad=34,
                           uppercase=False, accent="#F2A33A", kicker_font="Segoe UI", kicker_bg=None, kicker_color="#FFFFFF",
                           kicker_scale=0.68),
    # platform colour schemes (the name is plain text; no logo artwork)
    "twitch_purple": _theme("Twitch Purple", font="Segoe UI Black", cw=0.5, bar="#9146FF", radius=20, kicker_bg="#EB0400",
                            kicker_color="#FFFFFF", kicker_font="Segoe UI Black"),
    "kick_green": _theme("Kick Green", font="Bahnschrift", size=78, cw=0.44, color="#FFFFFF", bar="#0B0E0F", bar_opacity=0.92,
                         radius=10, bar_border=4, bar_border_color="#53FC18", accent="#53FC18", accent_side="left",
                         kicker_font="Bahnschrift", kicker_bg="#53FC18", kicker_color="#0B0E0F"),
    "youtube_red": _theme("YouTube Red", font="Segoe UI Black", cw=0.5, bar="#FF0033", radius=16, uppercase=False,
                          kicker_font="Segoe UI Black", kicker_bg="#FFFFFF", kicker_color="#FF0033"),
}
HEADLINE_AUTO = {"gaming": "gaming_hud", "vtuber": "neon", "just_chatting": "glass", "podcast": "podcast_card",
                 "reaction": "outline_pop", "tutorial": "bold_bar", "sports": "news_ticker", "music": "gradient", "vlog": "glass",
                 "comedy": "comic", "other": "bold_bar"}  # theme "auto": by the project's detected video type
HEADLINE_DEFAULT = {"on": False, "text": "", "kicker": "", "theme": "auto", "position": "top", "show": "whole", "seconds": 3.0,
                    "push": False}
HEADLINE_POSITIONS, HEADLINE_SHOW = ("top", "seam", "bottom"), ("whole", "first_n_seconds")
HEADLINE_MAX, KICKER_MAX = 90, 40


def headline_text(s, cap: int = HEADLINE_MAX) -> str:
    """One line of at most cap characters, cut at a word boundary (a hook or title used as the headline)."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= cap else (s[:cap - 1].rsplit(" ", 1)[0] or s[:cap - 1]) + "…"


def headline_spec(e: dict, video_type: str | None = None, custom: dict | None = None) -> dict | None:
    """The clip's headline resolved for rendering: the theme ("auto" by video type, "custom" = the saved theme) with the
    headline_* text-style overrides on top, plus text and placement. None when it is off or has no text."""
    h = {**HEADLINE_DEFAULT, **(e.get("headline") or {})}
    if not h["on"] or not str(h["text"] or "").strip():
        return None
    tid = HEADLINE_AUTO.get(video_type or "", "bold_bar") if h["theme"] == "auto" else h["theme"]
    th = (custom or {}) if tid == "custom" else HEADLINE_THEMES.get(tid, {})
    over = {f: e[f"headline_{f}"] for f in TEXT_FIELDS if e.get(f"headline_{f}") is not None}
    return {**HEADLINE_THEMES["bold_bar"], **th, **over, "theme": tid,
            **{k: h[k] for k in ("text", "kicker", "position", "show", "seconds", "push")}}


def _wrap(words: list[str], n: int) -> list[str]:
    lines: list[str] = []
    for w in words:
        if lines and len(lines[-1]) + 1 + len(w) <= n:
            lines[-1] += " " + w
        else:
            lines.append(w)
    return lines


def fit_headline(text: str, size: float, cw: float, max_w: float, max_lines: int = 2, floor: float = 0.5) -> tuple[list[str], int]:
    """Wrap to at most max_lines, shrinking the font (down to floor × size) until every line fits max_w.
    ponytail: widths are estimated (chars × size × cw), not measured; swap in Pillow font metrics if fits look off."""
    words, s, low = text.split(), round(size), round(size * floor)
    while True:
        n = max(int(max_w / (s * cw)), 1)
        lines = _wrap(words, n)
        if len(lines) <= max_lines and all(len(x) <= n for x in lines) or s <= low:
            break
        s = max(low, int(s * 0.92))
    if len(lines) > max_lines or any(len(x) > n for x in lines):  # still too long at the floor: cut, with an ellipsis
        lines = [x[:n] for x in lines[:max_lines]]
        lines[-1] = lines[-1][:max(n - 1, 1)].rstrip() + "…"
    return lines, s


def headline_layout(spec: dict, W: int, H: int, seam_frac: float | None = None) -> dict:
    """Where the banner goes in the W×H frame: fitted lines and font size, the bar, the kicker box, the text anchor, the
    whole occupied rect (x, y, w, h) and, with push, the reserved band (y, h) the video is letterboxed out of."""
    k = W / 1080
    up = str.upper if spec["uppercase"] else str
    text, kick = up(str(spec["text"]).strip()), up(str(spec.get("kicker") or "").strip())
    m, pad, gap = round(40 * k), round(float(spec["pad"]) * k), round(H * 0.02)
    kfs = round(max(float(spec["size"]) * float(spec.get("kicker_scale") or 0.5), 32) * k)
    kw, kh = (round(len(kick) * kfs * 0.66 + kfs * 1.2), round(kfs * 1.55)) if kick else (0, 0)
    left = bool(kick) and spec["kicker_layout"] == "left"
    bx, bw = m, W - 2 * m
    tx = bx + pad + (kw + pad if left else 0)
    lines, fs = fit_headline(text, float(spec["size"]) * k, float(spec["cw"]), bx + bw - pad - tx)
    bh = max(round(len(lines) * fs * 1.18 + 2 * pad), kh + 2 * pad if left else 0)
    lift = (round(kh * 0.6) if spec["kicker_bg"] else kh) if kick and not left else 0  # a pill overlaps the bar's top edge
    pos = "top" if spec["position"] == "seam" and not seam_frac else spec["position"]
    band = None
    if pos == "seam":
        by = round(seam_frac * H - bh / 2)
    elif spec.get("push"):
        bandh = _even(bh + lift + 2 * gap)
        band = (0, bandh) if pos == "top" else (H - bandh, bandh)
        by = band[0] + gap + lift
    else:
        by = H - round(H * 0.07) - bh if pos == "bottom" else round(H * 0.07) + lift
    by = min(max(by, lift), H - bh)
    if left:
        kbox = (bx, by, kw + pad, bh)
    else:
        kbox = ((W - kw) // 2, by - lift, kw, kh) if kick else None
    return {"k": k, "lines": lines, "fs": fs, "kicker": kick, "kfs": kfs, "bar": (bx, by, bw, bh), "kick_box": kbox,
            "text_xy": (tx, by + bh // 2) if left else (W // 2, by + bh // 2), "an": 4 if left else 5,
            "rect": (bx, by - lift, bw, bh + lift), "band": band}


def _rrect(x: int, y: int, w: int, h: int, r: int) -> str:
    """An ASS drawing path for a rounded rectangle (bezier corners)."""
    r = min(r, w // 2, h // 2)
    if r < 1:
        return f"m {x} {y} l {x + w} {y} {x + w} {y + h} {x} {y + h}"
    c, X, Y = round(r * 0.448), x + w, y + h  # control-point inset for a circular quarter
    return (f"m {x + r} {y} l {X - r} {y} b {X - c} {y} {X} {y + c} {X} {y + r} l {X} {Y - r} b {X} {Y - c} {X - c} {Y} {X - r} {Y} "
            f"l {x + r} {Y} b {x + c} {Y} {x} {Y - c} {x} {Y - r} l {x} {y + r} b {x} {y + c} {x + c} {y} {x + r} {y}")


def _mix(a: str, b: str, t: float) -> str:
    pa, pb = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(p + (q - p) * t):02X}" for p, q in zip(pa, pb))


def _headline_events(spec: dict, lay: dict, end: float) -> list[str]:
    """The banner as ASS events: bar (solid, gradient bands or bordered), accent stripe, kicker and the fitted text."""
    k, (x, y, w, h) = lay["k"], lay["bar"]
    r = round(float(spec["radius"]) * k)
    head = f"{_ass_time(0)},{_ass_time(end)},Over,,0,0,0,,"
    fx = (f"\\org({x + w // 2},{y + h // 2})" + (f"\\frz{float(spec['tilt']):g}" if spec["tilt"] else "")
          + ("\\fad(0,250)" if spec["show"] != "whole" else ""))
    shape = _rrect(x, y, w, h, r)

    def draw(layer, tags, path):
        return f"Dialogue: {layer},{head}{{\\an7\\pos(0,0){fx}\\shad0{tags}\\p1}}{path}{{\\p0}}"

    def rect(a, b, c, d):
        return f"m {a} {b} l {a + c} {b} {a + c} {b + d} {a} {b + d}"

    out = []
    if spec["bar"]:
        alpha = f"\\1a&H{round((1 - float(spec['bar_opacity'])) * 255):02X}&"
        bord, glow = round(float(spec["bar_border"]) * k), f"\\blur{float(spec['glow']):g}" if spec["glow"] else ""
        if spec["bar2"]:  # ASS has no gradients: 12 bands, clipped to the rounded bar
            for i in range(12):
                a0, a1 = x + w * i // 12, x + w * (i + 1) // 12 + 1
                col = _ass_color(_mix(spec["bar"], spec["bar2"], i / 11))
                out.append(draw(5, f"\\bord0\\1c{col}{alpha}\\clip({shape})", rect(a0, y, a1 - a0, h)))
        out.append(draw(5, f"\\bord{bord}\\3c{_ass_color(spec['bar_border_color'])}\\1c{_ass_color(spec['bar'])}"
                           + ("\\1a&HFF&" if spec["bar2"] else alpha) + glow, shape))
        if spec["accent"]:
            s = round(10 * k) if spec["accent_side"] == "left" else round(7 * k)
            stripe = rect(x, y, s, h) if spec["accent_side"] == "left" else rect(x, y + h - s, w, s)
            out.append(draw(5, f"\\bord0\\1c{_ass_color(spec['accent'])}" + (f"\\clip({shape})" if r else ""), stripe))
    kb = lay["kick_box"]
    if kb:
        if spec["kicker_bg"]:
            kr = r if spec["kicker_layout"] == "left" else kb[3] // 2
            out.append(draw(6, f"\\bord0\\1c{_ass_color(spec['kicker_bg'])}", _rrect(*kb, kr)))
            ktags = "\\bord0\\shad0"
        else:
            ktags = f"\\bord{max(float(spec['outline']), 2) * k:g}\\3c{_ass_color(spec['outline_color'])}\\shad{max(float(spec['shadow']), 2) * k:g}"
        out.append(f"Dialogue: 7,{head}{{\\an5\\pos({kb[0] + kb[2] // 2},{kb[1] + kb[3] // 2}){fx}\\q2\\fn{safe_font(spec['kicker_font'])}"
                   f"\\fs{lay['kfs']}\\b1\\c{_ass_color(spec['kicker_color'])}{ktags}}}{_ass_text(lay['kicker'])}")
    tx, ty = lay["text_xy"]
    tags = (f"\\an{lay['an']}\\pos({tx},{ty}){fx}\\q2\\fn{safe_font(spec['font'])}\\fs{lay['fs']}\\b{1 if spec['weight'] == 'bold' else 0}"
            f"\\c{_ass_color(spec['color'])}\\3c{_ass_color(spec['outline_color'])}\\bord{float(spec['outline']) * k:g}"
            f"\\shad{float(spec['shadow']) * k:g}\\fsp{float(spec['spacing']) * k:g}" + (f"\\blur{float(spec['glow']):g}" if spec["glow"] else ""))
    out.append(f"Dialogue: 7,{head}{{{tags}}}" + "\\N".join(_ass_text(x) for x in lay["lines"]))
    return out


# ---------- streamer badges + the streamer title card (edit "streamer_card") ----------
# Original designs: each platform's colour scheme with its name as plain text (a nominative reference to the creator's own
# channel); no logo artwork or trademark glyphs. The card is drawn in ASS; a library avatar is overlaid round (render).
PLATFORM_NAMES = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube", "generic": "Live"}
_PALETTES = {"twitch": dict(accent="#9146FF", on_accent="#FFFFFF", bg="#18181B", fg="#FFFFFF", sub="#BFBFCB"),
             "kick": dict(accent="#53FC18", on_accent="#0B0E0F", bg="#0B0E0F", fg="#FFFFFF", sub="#A3AAB4"),
             "youtube": dict(accent="#FF0033", on_accent="#FFFFFF", bg="#0F0F0F", fg="#FFFFFF", sub="#B5B5B5"),
             "generic": dict(accent="#FFFFFF", on_accent="#111111", bg="#111111", fg="#FFFFFF", sub="#BBBBBB")}


def _badge(label: str, platform: str, kind: str, **kw) -> dict:
    return {"label": label, "platform": platform, "kind": kind, **_PALETTES[platform], "bg_opacity": 0.9, "dot": "#FF2A2A",
            "font": "Segoe UI Black", "sub_font": "Segoe UI", **kw}


BADGE_THEMES = {  # kind: live = pill with a pulsing dot + channel URL, card = avatar + name lines, minimal = tag + handle
    "twitch_live": _badge("Twitch Live", "twitch", "live"),
    "twitch_card": _badge("Twitch Card", "twitch", "card"),
    "twitch_minimal": _badge("Twitch Minimal", "twitch", "minimal"),
    "kick_live": _badge("Kick Live", "kick", "live", font="Bahnschrift"),
    "kick_card": _badge("Kick Card", "kick", "card", font="Bahnschrift"),
    "kick_minimal": _badge("Kick Minimal", "kick", "minimal", font="Bahnschrift"),
    "youtube_live": _badge("YouTube Live", "youtube", "live"),
    "youtube_card": _badge("YouTube Card", "youtube", "card"),
    "generic_live": _badge("Generic Live", "generic", "live"),
    "generic_card": _badge("Generic Card", "generic", "card"),
}
CARD_DEFAULT = {"on": False, "style": "twitch_card", "name": "", "handle": "", "platform": "", "avatar_asset_id": None,
                "category": "", "accent": None, "show": "whole", "seconds": 4.0, "position": "tl", "x": 0.5, "y": 0.2,
                "scale": 1.0, "anim": "slide"}  # platform "" = the style's own
CARD_POSITIONS, CARD_ANIMS, CARD_PLATFORMS = ("tl", "tr", "bl", "br", "top", "bottom", "custom"), ("none", "slide", "pop", "fade"), tuple(PLATFORM_NAMES)
CARD_TEXT_MAX = 40  # name, handle and category
CARD_SAFE = {"top": 0.10, "bottom": 0.20, "right": 0.12}  # the most any app's UI covers (qa.SAFE_ZONES; a test keeps them in step)


def card_url(platform: str, handle: str) -> str:
    h = str(handle or "").strip().lstrip("@")
    return ({"twitch": f"twitch.tv/{h}", "kick": f"kick.com/{h}", "youtube": f"youtube.com/@{h}"}.get(platform, f"@{h}")) if h else ""


def card_spec(e: dict, avatar: str | None = None) -> dict | None:
    """The clip's streamer card resolved for rendering: its badge theme (accent override on top) plus text and placement;
    avatar = the library image's path (None draws a lettered circle). None when off or without a name or handle."""
    c = {**CARD_DEFAULT, **(e.get("streamer_card") or {})}
    if not c["on"] or not (str(c["name"] or "").strip() or str(c["handle"] or "").strip()):
        return None
    th = BADGE_THEMES.get(c["style"]) or BADGE_THEMES["twitch_card"]
    return {**th, **({"accent": c["accent"]} if c["accent"] else {}), "style": c["style"] if c["style"] in BADGE_THEMES else "twitch_card",
            **{k: c[k] for k in ("name", "handle", "category", "show", "seconds", "position", "x", "y", "scale", "anim")},
            "platform": c["platform"] or th["platform"], "avatar": avatar}


def _tw(s: str, fs: float, cw: float = 0.56) -> int:
    return round(len(s) * fs * cw)  # ponytail: estimated like fit_headline, not measured


def _card_shape(sp: dict, k: float) -> tuple[int, int, list[dict], int | None]:
    """The card at scale k with its top-left at (0, 0): (w, h, items, avatar diameter or None). Items are drawn shapes
    {path, c, a, bord, bc, fx, layer} and texts {x, y, an, font, fs, b, c, s, bord, layer}."""
    R = lambda v: round(v * k)  # noqa: E731
    name, handle = str(sp["name"] or "").strip(), str(sp["handle"] or "").strip().lstrip("@")
    url, items = card_url(sp["platform"], handle), []
    shape = lambda path, c, layer=8, **kw: items.append({"path": path, "c": c, "layer": layer, **kw})  # noqa: E731
    text = lambda x, y, an, font, fs, c, s, b=1, layer=9, **kw: items.append(  # noqa: E731
        {"x": x, "y": y, "an": an, "font": font, "fs": fs, "b": b, "c": c, "s": s, "layer": layer, **kw})
    if sp["kind"] == "live":
        h, fs, pad, dot = R(60), R(28), R(20), R(16)
        aw = pad + dot + R(10) + _tw("LIVE", fs, 0.62) + pad
        w = aw + (_tw(url or name, fs, 0.55) + 2 * pad if url or name else 0)
        if w > aw:
            shape(_rrect(0, 0, w, h, h // 2), sp["bg"], a=sp["bg_opacity"])
            text(aw + pad, h // 2, 4, sp["sub_font"], fs, sp["fg"], url or name, b=1)
        shape(_rrect(0, 0, aw, h, h // 2), sp["accent"])
        # a ring in the pill's text colour (white on Twitch/YouTube, near-black on Kick's green and the white pill) keeps
        # the red dot readable on every pill, also the red one; only its fill pulses
        shape(_rrect(pad, (h - dot) // 2, dot, dot, dot // 2), sp["dot"], layer=9, pulse=True, bord=max(R(3), 1), bc=sp["on_accent"])
        text(pad + dot + R(10), h // 2, 4, sp["font"], fs, sp["on_accent"], "LIVE")
        return w, h, items, None
    if sp["kind"] == "minimal":
        h, tfs, hfs, tp = R(46), R(24), R(34), R(14)
        tag = PLATFORM_NAMES.get(sp["platform"], "Live").upper()
        tag_w = _tw(tag, tfs, 0.66) + 2 * tp
        who = (f"/{handle}" if sp["platform"] in ("twitch", "kick") else f"@{handle}") if handle else name
        w = tag_w + R(12) + _tw(who, hfs) + R(14)
        # the handle on a soft see-through dark chip tucked under the tag: clean on any frame, no heavy outline
        shape(_rrect(tag_w - R(8), 0, w - tag_w + R(8), h, R(8)), sp["bg"], a=0.78)
        shape(_rrect(0, 0, tag_w, h, R(8)), sp["accent"])
        text(tag_w // 2, h // 2, 5, sp["font"], tfs, sp["on_accent"], tag)
        text(tag_w + R(12), h // 2, 4, sp["font"], hfs, sp["fg"], who)
        return w, h, items, None
    # card: avatar circle, then a plate with the name, the channel and a category line
    pad, nfs, hfs, cfs, gap = R(18), R(40), R(28), R(24), R(12)
    cat = str(sp.get("category") or "").strip()
    lines = [(name or handle, sp["font"], nfs, sp["fg"], 1)] + ([(url, sp["sub_font"], hfs, sp["accent"] if sp["platform"] != "generic"
                                                                  else sp["sub"], 1)] if url and name else []) + ([(cat, sp["sub_font"], cfs, sp["sub"], 0)] if cat else [])
    h = _even(2 * pad + sum(fs * 1.22 for _, _, fs, _, _ in lines))
    d, ring = h, max(R(5), 2)
    pw = 2 * pad + max(_tw(s, fs, 0.56 if b else 0.5) for s, _, fs, _, b in lines)
    w = d + gap + pw
    shape(_rrect(d + gap, 0, pw, h, R(16)), sp["bg"], a=sp["bg_opacity"], bord=max(R(3), 1), bc=sp["accent"])
    if not sp.get("avatar"):  # no picture: a lettered circle in the accent colour
        shape(_rrect(0, 0, d, d, d // 2), sp["accent"])
        text(d // 2, d // 2, 5, sp["font"], round(d * 0.45), sp["on_accent"], (name or handle or "?")[0].upper())
    shape(_rrect(0, 0, d, d, d // 2), sp["accent"], layer=9, a=0, bord=ring, bc=sp["accent"])  # the ring (border only)
    y = pad
    for s, font, fs, col, b in lines:
        text(d + gap + pad, y, 7, font, fs, col, s, b=b)
        y += round(fs * 1.22)
    return w, h, items, d


def card_layout(sp: dict, W: int, H: int, avoid=()) -> dict:
    """Where the card goes in the W×H frame: its rect (x, y, w, h), the avatar circle (cx, cy, d) for the render's round
    overlay, its drawn items and the entrance distances. Corner/top/bottom spots stay inside the apps' safe zones on
    vertical video; any spot moves off the avoid bands [(y0, y1)] (headline, captions) to the nearest clear place."""
    base = W / 1080
    k = base * float(sp["scale"])
    m = round(36 * base)
    vertical = H > W
    top = round(CARD_SAFE["top"] * H) if vertical else m
    bottom = round((1 - CARD_SAFE["bottom"]) * H) if vertical else H - m
    right = round((1 - CARD_SAFE["right"]) * W) if vertical else W - m
    w, h, items, d = _card_shape(sp, k)
    if w > W - 2 * m:  # too wide for the frame (long name, big scale): shrink to fit
        k *= (W - 2 * m) / w
        w, h, items, d = _card_shape(sp, k)
    pos = sp["position"]
    if pos == "custom":
        x, y = float(sp["x"]) * W - w / 2, float(sp["y"]) * H - h / 2
    else:
        x = m if pos[-1] == "l" and len(pos) == 2 else right - w if pos[-1] == "r" and len(pos) == 2 else (W - w) / 2
        y = top if pos in ("tl", "tr", "top") else bottom - h
    x, y = round(min(max(x, 0), W - w)), round(min(max(y, 0), H - h))
    gap = round(H * 0.015)

    def hit(v):
        return any(v < b1 + gap and b0 - gap < v + h for b0, b1 in avoid)
    if hit(y):  # just past a band it would touch, the nearest such spot (inside the safe zones when one is)
        cands = [round(c) for b0, b1 in avoid for c in (b1 + gap, b0 - gap - h) if 0 <= c <= H - h and not hit(round(c))]
        pick = [c for c in cands if top <= c <= bottom - h] or cands
        if pick:
            y = min(pick, key=lambda c: abs(c - y))
    return {"k": k, "rect": (x, y, w, h), "avatar": (x + d // 2, y + h // 2, d) if d else None, "items": items,
            "slide_px": x + w + 4, "rise_px": round(24 * k)}


def caption_band(ass: str, H: int) -> tuple[float, float] | None:
    """(top, bottom) px the captions of a build_ass script can fill (two lines over their margin), None without any."""
    rows = ass.splitlines()
    if not any(r.startswith("Dialogue:") and ",Cap," in r for r in rows):
        return None
    f = next(r for r in rows if r.startswith("Style: Cap,")).split(",")
    return H - int(f[21]) - 2.4 * float(f[2]), float(H - int(f[21]))


def card_place(card: dict, W: int, H: int, headline: dict | None = None, seam_frac: float | None = None, ass: str = "") -> dict:
    """card_layout clear of the headline banner and the script's captions: build_ass draws it here, render overlays the
    avatar here (same inputs, same spot)."""
    avoid = []
    if headline:
        _, y, _, h = headline_layout(headline, W, H, seam_frac)["rect"]
        avoid.append((y, y + h))
    if band := caption_band(ass, H):
        avoid.append(band)
    return card_layout(card, W, H, avoid)


def _card_events(sp: dict, lay: dict, end: float) -> list[str]:
    """The card as ASS events (style Card): absolute drawings like the headline's, texts, the pulsing live dot, and the
    entrance (slide in from the left, pop up, fade) that render gives the avatar overlay too."""
    x0, y0 = lay["rect"][:2]
    head, anim, D, Rp = f"{_ass_time(0)},{_ass_time(end)},Card,,0,0,0,,", sp["anim"], lay["slide_px"], lay["rise_px"]

    def at(px, py):
        if anim == "slide":
            return f"\\move({px - D},{py},{px},{py},0,300)"
        if anim == "pop":
            return f"\\move({px},{py + Rp},{px},{py},0,200)\\fad(150,250)"
        return f"\\pos({px},{py})" + ("\\fad(250,250)" if anim == "fade" else "")
    pulse = "".join(f"\\t({i * 1000},{i * 1000 + 500},\\1a&HC0&)\\t({i * 1000 + 500},{i * 1000 + 1000},\\1a&H00&)"
                    for i in range(min(math.ceil(end), 300)))
    out = []
    for it in lay["items"]:
        if "path" in it:
            # the shape was drawn at (0, 0): every "x y" pair shifted to the card's spot
            path = re.sub(r"-?\d+ -?\d+", lambda mt: " ".join(str(int(v) + o) for v, o in zip(mt.group().split(), (x0, y0))), it["path"])
            bord = f"\\bord{it['bord']}\\3c{_ass_color(it['bc'])}" if it.get("bord") else "\\bord0"
            fill = f"\\1c{_ass_color(it['c'])}\\1a&H{round((1 - float(it.get('a', 1))) * 255):02X}&"
            out.append(f"Dialogue: {it['layer']},{head}{{\\an7{at(0, 0)}\\shad0{bord}{fill}{pulse if it.get('pulse') else ''}\\p1}}{path}{{\\p0}}")
        else:
            tags = (f"\\an{it['an']}{at(x0 + it['x'], y0 + it['y'])}\\q2\\fn{safe_font(it['font'])}\\fs{it['fs']}\\b{it['b']}"
                    f"\\c{_ass_color(it['c'])}\\bord{it.get('bord', 0)}\\3c&H00000000&\\shad{it.get('shad', 0)}")
            out.append(f"Dialogue: {it['layer']},{head}{{{tags}}}{_ass_text(it['s'])}")
    return out


def _set_margin(lines: list[str], style: str, v: int) -> None:
    i = next(n for n, line in enumerate(lines) if line.startswith(f"Style: {style},"))
    f = lines[i].split(",")
    f[21] = str(v)
    lines[i] = ",".join(f)


def _style_field(lines: list[str], style: str, idx: int) -> int:
    return int(float(next(line for line in lines if line.startswith(f"Style: {style},")).split(",")[idx]))


def build_ass(words: list[dict], start: float, end: float, speed: float, size: tuple[int, int], title: str = "",
              uppercase: bool = True, highlight: str = "#FFD400", captions: bool = True, style: str = "bold",
              pos: str = "low", sticker: str = "", sticker_pos: str = "top-right", keeps=None, split: float | None = None,
              overlays=None, text_style: dict | None = None, headline: dict | None = None, anim: str = "highlight",
              emphasis: bool = False, card: dict | None = None) -> str:
    """headline = a headline_spec(...) to draw the themed banner; captions, title card and stickers then keep clear of it.
    anim = how the spoken word stands out (CAPTION_ANIMS); emphasis = keywords stay coloured and a little bigger.
    card = a card_spec(...): the streamer card, placed clear of the banner and the captions (card_place)."""
    W, H = size
    vertical = H > W
    fs = round(min(W, H) * (0.078 if vertical else 0.06))
    hl = _ass_color(highlight)
    mv_frac = CAPTION_POS.get(pos, CAPTION_POS["low"])[0 if vertical else 1]
    if split and pos == "middle":
        mv_frac = 1 - split - 0.07  # just under the facecam seam
    mv = round(H * mv_frac)
    font, outline, shadow, border, outline_col, size_k = "Arial Black", max(fs // 12, 3), 3, 1, "&H00000000&", 1.0
    if style == "clean":
        font, outline, shadow, size_k = "Arial", 2, 2, 0.9
    elif style == "boxed":
        border, outline, shadow, outline_col = 3, max(fs // 6, 8), 0, "&H60000000&"
    elif style == "neon":
        outline, shadow, outline_col = max(fs // 10, 4), 0, hl
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{round(fs * size_k)},&H00FFFFFF,&H00FFFFFF,{outline_col},&H64000000,-1,0,0,0,100,100,0,0,{border},{outline},{shadow},2,60,60,{mv},1",
        f"Style: Title,Arial Black,{round(fs * 0.8)},&H00FFFFFF,&H00FFFFFF,&H00111111,&H28111111,-1,0,0,0,100,100,0,0,3,{max(fs // 6, 8)},0,8,60,60,{round(H * 0.09)},1",
        f"Style: Sticker,Arial Black,{round(fs * 0.55)},&H00111111,&H00111111,{hl},&H00000000,-1,0,0,0,100,100,0,0,3,{max(fs // 7, 6)},0,"
        f"{STICKER_ALIGN.get(sticker_pos, 9)},50,50,{round(H * 0.05)},1",
        f"Style: Over,Arial Black,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(fs // 12, 3)},2,5,0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for who, name in (("caption", "Cap"), ("title", "Title"), ("sticker", "Sticker")):
        i = next(n for n, line in enumerate(lines) if line.startswith(f"Style: {name},"))
        lines[i] = _restyle(lines[i], {f: (text_style or {}).get(f"{who}_{f}") for f in TEXT_FIELDS}, W)
    rel = (lambda t: remap(t, keeps) / speed) if keeps else (lambda t: (t - start) / speed)
    clip_len = rel(end)
    lay = headline_layout(headline, W, H, split) if headline else None
    if lay:
        _, y0, _, hh = lay["rect"]
        y1, gap, band = y0 + hh, round(H * 0.015), lay["band"]
        if band and split and pos == "middle":  # letterboxed: the stacked seam moved with the video
            mv = round(H - ((band[1] if band[0] == 0 else 0) + split * (H - band[1])) - 0.07 * H)
        cap_h = 2.4 * _style_field(lines, "Cap", 2)  # up to two caption lines
        if H - mv > y0 - gap and H - mv - cap_h < y1 + gap:  # captions would touch the banner: move to its far side
            mv = round(H - (y1 + gap + cap_h)) if y0 + y1 < H else round(H - (y0 - gap))
        _set_margin(lines, "Cap", min(max(mv, round(H * 0.04)), round(H * 0.92)))
        for name in ("Title", "Sticker"):  # top-anchored title / sticker go under a banner they would hit, bottom ones above
            top = _style_field(lines, name, 18) >= 7
            m, tall = _style_field(lines, name, 21), 2.4 * _style_field(lines, name, 2)
            if top and m < y1 + gap and m + tall > y0 - gap:
                _set_margin(lines, name, y1 + gap)
            elif not top and H - m > y0 - gap and H - m - tall < y1 + gap:
                _set_margin(lines, name, H - y0 + gap)
        hl_end = min(float(headline["seconds"]), clip_len) if headline["show"] != "whole" else clip_len
        lines += _headline_events(headline, lay, hl_end)
    if title.strip():
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(clip_len)},Title,,0,0,0,,{_ass_text(title)}")
    if sticker.strip():
        lines.append(f"Dialogue: 2,{_ass_time(0)},{_ass_time(clip_len)},Sticker,,0,0,0,,{_ass_text(sticker)}")
    for o in overlays or []:  # text / emoji overlays (clip time, like the captions)
        text = str(o.get("text") or "")
        if o.get("kind") not in ("text", "emoji") or not text.strip():
            continue
        t0, t1 = rel(start + float(o["t"])), rel(start + float(o["t"]) + float(o["d"]))
        ox, oy, ow = round(float(o.get("x", 0.5)) * W), round(float(o.get("y", 0.5)) * H), float(o.get("w", 0.3)) * W
        emoji = o["kind"] == "emoji"
        fsz = round(min(ow * 0.8 if emoji else ow / max(len(text), 1) * 1.7, H * 0.2))
        tags = f"\\an5\\fs{fsz}" + ("\\fnSegoe UI Emoji" if emoji else "") + _text_tags(o, W)
        if o.get("anim") == "slide":  # in from the left over 0.3 s
            tags += f"\\move({round(-ow / 2)},{oy},{ox},{oy},0,300)"
        else:
            tags += f"\\pos({ox},{oy})" + OVER_ANIM.get(o.get("anim", "none"), "")
        lines.append(f"Dialogue: {max(0, 3 + int(o.get('layer', 0)))},{_ass_time(t0)},{_ass_time(max(t1, t0 + 0.05))},Over,,0,0,0,,"
                     f"{{{tags}}}{_ass_text(text)}")
    if captions:
        glow = "{\\blur3}" if style == "neon" else ""
        if anim == "box":  # a copy of the caption style that draws an opaque box in the highlight colour (same font, place)
            i = next(n for n, line in enumerate(lines) if line.startswith("Style: Cap,"))
            f = lines[i].split(",")
            f[0], f[5], f[15], f[16], f[17] = "Style: CapBox", hl, "3", str(max(round(float(f[2]) * 0.18), 6)), "0"
            lines.insert(i + 1, ",".join(f))
        for chunk in caption_chunks(words, start, end, 3 if vertical else 6, keeps):
            texts = [_ass_text(w["w"].strip()) for w in chunk]
            if uppercase:
                texts = [t.upper() for t in texts]
            emph = [emphasis and _keyword(w["w"]) for w in chunk]
            for i, w in enumerate(chunk):  # one event per word: the spoken word is highlighted
                t0 = rel(w["s"])
                t1 = rel(chunk[i + 1]["s"]) if i + 1 < len(chunk) else rel(w["e"]) + 0.15
                when = f"{_ass_time(t0)},{_ass_time(min(t1, clip_len))}"
                parts = [_cap_word(t, j == i, emph[j], hl, anim) for j, t in enumerate(texts)]
                if anim == "reveal" and i + 1 < len(texts):  # the words still to come keep their place, unseen
                    body = " ".join(parts[:i + 1]) + "{\\alpha&HFF&} " + " ".join(parts[i + 1:])
                else:
                    body = " ".join(parts)
                if anim == "box":  # under the text (layer 0): only the spoken word's box shows
                    lines.append(f"Dialogue: 0,{when},CapBox,,0,0,0,,{_box_line(texts, i, emph)}")
                lines.append(f"Dialogue: {1 if anim == 'box' else 0},{when},Cap,,0,0,0,,{glow}{body}")
    if card:
        lay_c = card_place(card, W, H, headline, split, "\n".join(lines))
        lines.insert(lines.index("[Events]") - 1, f"Style: Card,Arial,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,"
                                                   "100,100,0,0,1,0,0,7,0,0,0,1")
        lines += _card_events(card, lay_c, min(float(card["seconds"]), clip_len) if card["show"] != "whole" else clip_len)
    return "\n".join(lines) + "\n"


CAP_ANIM = {"pop": "\\fscx125\\fscy125\\t(0,120,\\fscx100\\fscy100)",  # ASS tags on the spoken word (times from its start)
            "bounce": "\\fscx70\\fscy70\\t(0,90,\\fscx120\\fscy120)\\t(90,180,\\fscx100\\fscy100)"}
EMPH = "\\fscx112\\fscy112"
_PLAIN = {"because", "actually", "literally", "basically", "something", "anything", "everything", "nothing", "probably",
          "through", "without", "another", "whatever", "already", "anyway", "honestly", "seriously"}


def _keyword(w: str) -> bool:
    """Worth emphasising: a number, a SHOUTED word, or a long word that isn't filler."""
    t = re.sub(r"\W", "", w)
    return bool(t) and (any(ch.isdigit() for ch in t) or len(t) >= 3 and t.isupper() or len(t) >= 7 and t.lower() not in _PLAIN)


def _cap_word(t: str, active: bool, emph: bool, hl: str, anim: str) -> str:
    if active and anim == "box":  # the box marks it; the text stays white
        return f"{{{EMPH}}}{t}{{\\fscx100\\fscy100}}" if emph else t
    if active:
        tags = CAP_ANIM.get(anim, "")
        reset = "\\fscx100\\fscy100" if tags else ""
        return f"{{\\c{hl}{tags}}}{t}{{\\c&H00FFFFFF&{reset}}}"
    return f"{{\\c{hl}{EMPH}}}{t}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}" if emph else t


def _box_line(texts: list[str], i: int, emph: list[bool]) -> str:
    """The CapBox event for word i: every glyph transparent, only word i's box opaque; sized like the caption text."""
    sz = [f"{{{EMPH}}}{t}{{\\fscx100\\fscy100}}" if emph[j] else t for j, t in enumerate(texts)]
    return (("{\\alpha&HFF&}" + " ".join(sz[:i]) + " ") if i else "") + "{\\1a&HFF&\\3a&H00&}" + sz[i] + \
        ("{\\alpha&HFF&} " + " ".join(sz[i + 1:]) if i + 1 < len(sz) else "")


# ---------- the ffmpeg build: H.264 encoders and filters ----------
# Tried in this order. A hardware encoder is used only after a real 1-frame test encode worked on this PC (listed is not
# usable: no such GPU, an old driver); a software one when the build has it. libx264 is GPL-only: LGPL builds
# (scripts/build_exe.py --lgpl) fall back to OpenH264.
ENCODERS = (
    ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0"]),
    ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "21"]),
    ("h264_amf", ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22"]),
    ("libx264", ["-c:v", "libx264", "-preset", "medium", "-crf", "19"]),
    ("libopenh264", ["-c:v", "libopenh264", "-b:v", "8M"]),
)
HW_ENCODERS = {"h264_nvenc", "h264_qsv", "h264_amf"}


@functools.cache
def _ffmpeg_list(what: str) -> frozenset:
    """Names from `ffmpeg -encoders` / `-filters` (empty when ffmpeg can't be run)."""
    try:
        out = _run([FFMPEG, "-hide_banner", what], timeout=30).stdout
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(p[1] for p in (line.split() for line in out.splitlines()) if len(p) > 2 and not p[1].startswith("="))


def _encoder_names() -> frozenset:
    return _ffmpeg_list("-encoders")


def _filter_names() -> frozenset:
    return _ffmpeg_list("-filters")


def _encoder_works(name: str) -> bool:
    """A real one-frame test encode with the render settings. A pass is kept for the session; a failure only for
    PROBE_RETRY s (the GPU may have been busy or a driver resetting), then it is tried again."""
    ok, at = _probes.get(name, (None, 0.0))
    if ok or (ok is False and time.monotonic() - at < PROBE_RETRY):
        return bool(ok)
    _probes[name] = (_probe(name), time.monotonic())
    return _probes[name][0]


PROBE_RETRY = 600.0
_probes: dict = {}


def _probe(name: str) -> bool:
    try:
        _run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=black:s=256x256:r=30", "-frames:v", "1", "-pix_fmt", "yuv420p",
              *dict(ENCODERS)[name], "-f", "null", "-"], timeout=60)
        return True
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return False


def _chain(names, works) -> tuple:
    return tuple((n, a) for n, a in ENCODERS if n in names and (n not in HW_ENCODERS or works(n)))


def encoders() -> tuple:
    """The usable H.264 encoders on this PC, best first: ((name, ffmpeg args), ...)."""
    return _chain(_encoder_names(), _encoder_works)


def _has_nvenc() -> bool:
    return any(n == "h264_nvenc" for n, _ in encoders())


def has_filter(name: str) -> bool:
    names = _filter_names()
    return not names or name in names  # can't tell (no ffmpeg yet): assume a full build


@functools.cache
def licence() -> str:
    """"GPL" or "LGPL": how this ffmpeg was configured (a GPL build can't be redistributed with a closed app)."""
    try:
        return "GPL" if "--enable-gpl" in _run([FFMPEG, "-hide_banner", "-version"], timeout=30).stdout else "LGPL"
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return "unknown"


def _blur(r: int, p: int) -> str:
    """boxblur radius r, p passes; LGPL builds lack boxblur (GPL-only): a gaussian blur of the same spread instead."""
    return f"boxblur={r}:{p}" if has_filter("boxblur") else f"gblur=sigma={math.sqrt(p * ((2 * r + 1) ** 2 - 1) / 12):.1f}"


def _eq(b: float, c: float, s: float, g: float) -> str:
    """The eq look (brightness, contrast, saturation, gamma); LGPL builds lack eq (GPL-only): the same curves as lutyuv."""
    if has_filter("eq"):
        return f"eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}:gamma={g:.3f}"
    y, uv = f"{c:.3f}*(val/255-0.5)+0.5+{b:.3f}", f"'clip(256*({s:.3f}*(val/255-0.5)+0.5),0,255)'"
    return f"lutyuv=y='if(lte({y},0),0,min(256*pow({y},{1 / g:.4f}),255))':u={uv}:v={uv}"


def _encode(args: list, out: Path, cwd: Path, out_dur: float, progress, extra: list | None = None) -> None:
    """Encode with the first encoder of the chain that succeeds (extra = more output options for every encoder)."""
    tmp = out.with_name(out.stem + ".rendering.mp4")  # never overwrite a file an upload may be reading
    tail = ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(tmp.resolve())]
    chain = [a + (extra or []) for _, a in encoders()]
    if not chain:
        raise RuntimeError("No usable H.264 encoder in this ffmpeg build (tried NVIDIA NVENC, Intel Quick Sync, AMD AMF, "
                           "libx264, OpenH264). Update the graphics driver, or use an ffmpeg build with libx264 or OpenH264.")
    err = ""
    try:
        for enc in chain:
            log = cwd / f"ffmpeg_{out.stem}.log"
            with open(log, "w", encoding="utf-8") as lf:
                p = popen(args + enc + tail, cwd=cwd, stdout=subprocess.PIPE, stderr=lf, text=True)
                try:
                    for line in p.stdout:
                        if line.startswith("out_time_us=") and progress and out_dur > 0:
                            try:
                                progress(min(int(line.split("=")[1]) / 1e6 / out_dur, 1.0))
                            except ValueError:
                                pass
                    p.wait()
                finally:
                    if p.poll() is None:  # interrupted mid-render: ffmpeg must not keep writing tmp
                        p.kill()
                        p.wait()
            err = log.read_text("utf-8", "replace")
            log.unlink(missing_ok=True)
            if p.returncode == 0:
                os.replace(tmp, out)
                return
    finally:
        tmp.unlink(missing_ok=True)  # a failed or interrupted attempt; gone already after a successful os.replace
    raise RuntimeError(f"Render failed: {err.strip()[-1500:]}")


def used_angles(e: dict) -> set:
    """Camera-angle ids an edit reads: region sources (split-screen layouts), switch targets and the audio source."""
    multi = e["aspect"] != "source" and e["layout"] in LAYOUTS[1:]
    ids = {r.get("source") for r in e.get("regions") or []} if multi else set()
    ids |= {s.get("angle") for s in e.get("switches") or []} | {e.get("audio_source")}
    return ids - {None, "", "main"}


def _angle_span(a: dict, start: float, dur: float) -> tuple[float, float, bool]:
    """(seek into the angle, lead = clip seconds before it starts, overlaps the clip at all). main t = angle t + offset_s."""
    seek = start - float(a.get("offset_s") or 0)
    lead = max(-seek, 0.0)
    return max(seek, 0.0), lead, lead < dur and float(a.get("duration") or 0) - max(seek, 0.0) > 0.05


def angle_inputs(angles: dict, e: dict, start: float, dur: float, first: int, fps: float = 30) -> tuple[list[str], dict]:
    """ffmpeg input args and build_filters feeds for the angles this edit uses; angles = {id: {path, offset_s, width,
    height, duration, has_audio}}. Where an angle doesn't cover the clip (starts later, ends earlier) its region or cut
    shows BLACK, never another camera: the padding is part of the feed (see _feed_head), and an angle that misses the
    clip entirely is a black frame."""
    args, feeds = [], {}
    for aid in sorted(used_angles(e) & angles.keys()):
        a = angles[aid]
        seek, lead, ok = _angle_span(a, start, dur)
        w, h = int(a["width"]), int(a["height"])
        if ok:
            args += (["-ss", f"{seek:.3f}"] if seek else []) + ["-t", f"{dur - lead:.3f}", "-i", str(Path(a["path"]).resolve())]
        else:
            args += ["-f", "lavfi", "-i", f"color=black:s={w}x{h}:r={fps:g}:d={dur:.3f}"]
            lead = 0.0
        feeds[aid] = (first + len(feeds), round(lead, 3), w, h, round(dur, 3))
    return args, feeds


def render(src: Path, out: Path, start: float, end: float, edit: dict, meta: dict, ass: str | None, progress=None,
           keeps: list | None = None, music: Path | None = None, overlays: list | None = None, fonts: list | None = None,
           headline: dict | None = None, angles: dict | None = None, bleeps: list | None = None, card: dict | None = None) -> None:
    """overlays = media overlays ({kind image|gif|video|sound, path, t, d, ...} in clip time); text/emoji ride in `ass`.
    angles = the project's camera angles (see angle_inputs) for angle regions, switches and an angle's audio.
    bleeps = source-time spans to bleep (edit "bleep"), mapped through the jump cuts and speed like the captions.
    card = the card_spec drawn in `ass`: its avatar image is overlaid round at the same spot, with the same timing and entrance."""
    e = {**DEFAULT_EDIT, **edit}
    dur = end - start
    kept = sum(b - a for a, b in keeps) if keeps else dur
    out_dur = kept / float(e["speed"])
    keeps_rel = [(a - start, b - start) for a, b in keeps] if keeps else None
    extra: list[str] = []
    placed: list[dict] = []
    for o in overlays or []:
        a, b = overlay_window(float(o["t"]), float(o["d"]), start, keeps, float(e["speed"]))
        if a >= out_dur:
            continue
        span = f"{b - a + 0.1:.3f}"
        loop = {"image": ["-loop", "1", "-framerate", "30", "-t", span], "gif": ["-ignore_loop", "0", "-t", span],
                "video": ["-t", span]}.get(o["kind"], [])
        if o.get("trim_in") and o["kind"] in ("gif", "video", "sound"):  # the asset's trim: start from its in point
            loop = ["-ss", f"{float(o['trim_in']):.3f}", *loop]
        placed.append({**o, "a": a, "b": b, "input": 1 + bool(music) + len(placed),
                       "audio": o["kind"] == "video" and probe(Path(o["path"]))["has_audio"]})
        extra += loop + ["-i", str(Path(o["path"]).resolve())]
    if card and card.get("avatar") and ass:
        W, H = output_size(e["aspect"], meta["width"], meta["height"])
        lay = card_place(card, W, H, headline, seam(e), ass)
        if lay["avatar"]:
            cx, cy, d = lay["avatar"]
            b = min(float(card["seconds"]), out_dur) if card["show"] != "whole" else out_dur
            anim, more = {"slide": ("slide", {"slide_px": lay["slide_px"]}), "pop": ("rise", {"rise_px": lay["rise_px"]})}.get(
                card["anim"], (card["anim"], {}))
            placed.append({"kind": "image", "a": 0.0, "b": b, "x": cx / W, "y": cy / H, "circle": d, "anim": anim, **more,
                           "layer": 10 ** 6, "input": 1 + bool(music) + len(placed), "audio": False})
            extra += ["-loop", "1", "-framerate", "30", "-t", f"{b + 0.1:.3f}", "-i", str(Path(card["avatar"]).resolve())]
    angles = angles or {}
    cams, feeds = angle_inputs(angles, e, start, dur, 1 + bool(music) + len(placed), meta.get("fps") or 30)
    aud = e.get("audio_source") or "main"
    has_audio = bool(meta.get("has_audio"))
    if aud != "main":  # an angle's sound only when it has some and overlaps the clip; else the main video's
        has_audio = aud in feeds and bool(angles[aud].get("has_audio")) and _angle_span(angles[aud], start, dur)[2]
        e = e if has_audio else {**e, "audio_source": "main"}
        has_audio = has_audio or bool(meta.get("has_audio"))
    extra += cams
    m = (lambda t: remap(t, keeps)) if keeps else (lambda t: t - start)
    spans = [(m(a) / float(e["speed"]), m(b) / float(e["speed"])) for a, b in bleeps or []]
    spans = [(max(a, 0.0), min(b, out_dur)) for a, b in spans if b - a > 0.02 and b > 0 and a < out_dur]  # a cut word: gone
    graph, _ = build_filters(e, meta["width"], meta["height"], out_dur, bool(ass), keeps_rel, has_audio,
                             bool(music), meta.get("fps") or 30, placed, bool(ass and fonts), feeds=feeds, headline=headline,
                             bleeps=spans, effects=[w for w in effect_windows(e.get("effects"), start, keeps, float(e["speed"]))
                                                    if w["a"] < out_dur] or None,
                             sections=[w for w in section_windows(e.get("layout_segments"), start, keeps, float(e["speed"]))
                                       if w["a"] < out_dur] or None)
    work = out.parent / f".work_{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        if ass:
            (work / "captions.ass").write_text(ass, "utf-8")
            if fonts:  # uploaded fonts, copied next to the captions for libass
                (work / "fonts").mkdir(exist_ok=True)
                for f in fonts:
                    shutil.copy(f, work / "fonts" / Path(f).name)
        args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src.resolve())]
        if music:
            args += ["-stream_loop", "-1", "-i", str(music.resolve())]
        args += extra + ["-filter_complex", graph, "-map", "[v]"]
        if has_audio or music or any(o["kind"] == "sound" or o["audio"] for o in placed):
            args += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
        _encode(args, out, work, out_dur, progress)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def concat(files: list[Path], out: Path, size: tuple[int, int], progress=None) -> None:
    """Montage: join rendered clips (normalised to one size; clips without sound get a silent track)."""
    W, H = size
    args, parts, total, idx = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"], [], 0.0, 0
    for f in files:
        m = probe(f)
        total += m["duration"]
        args += ["-i", str(f.resolve())]
        v = idx
        idx += 1
        if m["has_audio"]:
            a = f"[{v}:a]"
        else:
            args += ["-f", "lavfi", "-t", f"{m['duration']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            a = f"[{idx}:a]"
            idx += 1
        parts.append((v, a))
    g = ";".join(f"[{v}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}];"
                 f"{a}aresample=48000,aformat=channel_layouts=stereo[a{i}]" for i, (v, a) in enumerate(parts))
    g += ";" + "".join(f"[v{i}][a{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=1[v][a]"
    _encode(args + ["-filter_complex", g, "-map", "[v]", "-map", "[a]", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"],
            out, out.parent, total, progress)
