"""FFmpeg/OpenCV media engine: probing, analysis signals, reframing, captions, rendering."""
import functools
import os
import json
import re
import shutil
import subprocess
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
    "layout": "single",       # single | split (facecam on top, main view below)
    "cam_x": 0.7, "cam_y": 0.05, "cam_w": 0.28, "cam_h": 0.32, "split": 0.4,
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
    **{f"{who}_{f}": None for who in ("caption", "title", "sticker")
       for f in ("font", "size", "weight", "color", "outline", "outline_color", "shadow", "spacing")},
}


def _run(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, **kw)
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
    }


def poster(src: Path, t: float, out: Path) -> None:
    _run([FFMPEG, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(out)])


def sprite(src: Path, duration: float, out: Path, count: int) -> None:
    """One-row strip of `count` thumbnails for the timeline."""
    _run([FFMPEG, "-y", "-v", "error", "-i", str(src), "-an", "-vf",
          f"fps={count}/{duration:.3f},scale=160:90:force_original_aspect_ratio=increase,crop=160:90,tile={count}x1",
          "-frames:v", "1", "-q:v", "4", str(out)])


def audio_signals(src: Path) -> tuple[list[float], list[dict]]:
    """Waveform peaks at 10/s (0..1) and the loudest moments (energy spikes ≈ laughs, shouts, hype)."""
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "4000", "-f", "s16le", "-"],
                       capture_output=True, creationflags=NO_WINDOW)
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    win = 400  # 0.1 s
    n = len(x) // win
    if n == 0:
        return [], []
    x = x[: n * win].reshape(n, win)
    peaks = np.abs(x).max(axis=1)
    peaks = peaks / (peaks.max() or 1)
    rms = np.sqrt((x ** 2).mean(axis=1))
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


def scenes(src: Path, threshold: float = 0.32) -> list[float]:
    r = subprocess.run([FFMPEG, "-hide_banner", "-hwaccel", "auto", "-i", str(src), "-an", "-vf",
                        f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return [round(float(t), 2) for t in re.findall(r"pts_time:([\d.]+)", r.stderr)]


def silences(src: Path, noise_db: int = -35, min_dur: float = 0.7) -> list[list[float]]:
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(src), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    starts = [float(s) for s in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(s) for s in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return [[round(s, 2), round(e, 2)] for s, e in zip(starts, ends)]


def fetch_model(name: str, url: str) -> Path:
    """A small model file in the data folder, downloaded once."""
    import urllib.request

    from .config import DATA

    model = DATA / "models" / name
    if not model.exists():  # download to .part then rename: an interrupted fetch never leaves a corrupt model behind
        model.parent.mkdir(exist_ok=True)
        part = model.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=20) as r:
            part.write_bytes(r.read())
        os.replace(part, model)
    return model


def face_detector(w: int, h: int):
    """YuNet DNN face detector for w×h frames (model ~230 KB, fetched once into the data folder)."""
    import cv2

    model = fetch_model("face_detection_yunet_2023mar.onnx", "https://github.com/opencv/opencv_zoo/raw/main/models/"
                        "face_detection_yunet/face_detection_yunet_2023mar.onnx")
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


def _overlay_chain(o: dict, i: int, W: int, H: int) -> str:
    """[b{i}] + overlay input -> [b{i+1}]: sized, shifted to its window, animated with per-frame expressions."""
    a, b = o["a"], o["b"]
    if o.get("cutaway"):
        size, X, Y = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}", "0", "0"
    else:
        cx, cy = float(o.get("x", 0.5)) * W, float(o.get("y", 0.5)) * H
        size, X, Y = f"scale={_even(W * float(o.get('w', 0.3)))}:-2", f"{cx:.1f}-w/2", f"{cy:.1f}-h/2"
    chain = f"[{o['input']}:v]{size},format=rgba,setpts=PTS-STARTPTS+{a:.3f}/TB"
    anim, since = o.get("anim", "none"), f"(t-{a:.3f})"
    if anim == "fade":
        chain += f",fade=t=in:st={a:.3f}:d=0.25:alpha=1,fade=t=out:st={max(a, b - 0.25):.3f}:d=0.25:alpha=1"
    elif anim == "pop":  # drops in with a damped bounce
        Y += f"-{H * 0.05:.0f}*exp(-{since}*9)*cos({since}*28)"
        chain += f",fade=t=in:st={a:.3f}:d=0.12:alpha=1"
    elif anim == "shake":
        X, Y = X + f"+{W * 0.012:.0f}*sin(t*63)", Y + f"+{H * 0.008:.0f}*cos(t*47)"
    elif anim == "slide":  # in from the left over 0.3 s
        X = f"({X})-({X}+w)*max(0,1-{since}/0.3)"
    return (f"{chain}[o{i}];[b{i}][o{i}]overlay=x='{X}':y='{Y}':eof_action=pass:"
            f"enable='between(t,{a:.3f},{b:.3f})'[b{i + 1}]")


def build_filters(edit: dict, sw: int, sh: int, out_dur: float, has_subs: bool, keeps_rel=None,
                  has_audio: bool = True, music: bool = False, fps: float = 30, overlays=None,
                  fontsdir: bool = False) -> tuple[str, tuple[int, int]]:
    """One filter_complex producing [v] (and [a] when there is any audio). keeps_rel = jump-cut ranges relative to the clip."""
    e = {**DEFAULT_EDIT, **edit}
    speed = float(e["speed"])
    W, H = output_size(e["aspect"], sw, sh)
    pre = []
    if keeps_rel:
        pre += [f"select='{_between(keeps_rel)}'", "setpts=N/FRAME_RATE/TB"]
    if abs(speed - 1) > 1e-3:
        pre.append(f"setpts=PTS/{speed:g}")
    reframed = e["aspect"] != "source"
    if reframed and e["layout"] == "split":  # ponytail: split keeps its static crops; follow applies to single layout only
        th = _even(H * float(e["split"]))
        bh = H - th
        cw, ch = _even(sw * float(e["cam_w"])), _even(sh * float(e["cam_h"]))
        cx = min(max(int(sw * float(e["cam_x"])), 0), sw - cw)
        cy = min(max(int(sh * float(e["cam_y"])), 0), sh - ch)
        mw, mh, mx, my = crop_box(sw, sh, W / bh, float(e["crop_x"]), float(e["crop_y"]), float(e["zoom"]))
        g = (f"[0:v]{','.join(pre + ['split'])}[c][m];"
             f"[c]crop={cw}:{ch}:{cx}:{cy},scale={W}:{th}:force_original_aspect_ratio=increase,crop={W}:{th}[top];"
             f"[m]crop={mw}:{mh}:{mx}:{my},scale={W}:{bh}[bot];[top][bot]vstack")
    elif reframed and e["fit"] == "blur":
        g = (f"[0:v]{','.join(pre + ['split'])}[a][b];"
             f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2[bg];"
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
    post = ["setsar=1"]
    b, c, s, gm = (float(e[k]) for k in ("brightness", "contrast", "saturation", "gamma"))
    if (b, c, s, gm) != (0.0, 1.0, 1.0, 1.0):
        post.append(f"eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}:gamma={gm:.3f}")
    if e["sharpen"]:
        post.append("unsharp=5:5:0.8")
    look, post = post, []  # overlays go on the finished picture, under fades and captions
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

    af = []
    if keeps_rel:
        af += [f"aselect='{_between(keeps_rel)}'", "asetpts=N/SR/TB"]
    af += _atempo(speed)
    if abs(float(e["volume"]) - 1) > 1e-3:
        af.append(f"volume={float(e['volume']):.3f}")
    if e["denoise"]:
        af.append("afftdn=nf=-25")
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
    if sfx:  # each effect delayed to its start, mixed under the dialogue, then the usual loudness/fades over the whole mix
        stereo = "aresample=48000,aformat=channel_layouts=stereo"
        base = f"[0:a]{','.join(pre_af + [stereo])}" if has_audio else f"anullsrc=r=48000:cl=stereo,atrim=0:{out_dur:.3f}"
        g += f";{base}[a0]"
        for i, o in enumerate(sfx):
            ms = int(round(o["a"] * 1000))
            g += (f";[{o['input']}:a]atrim=0:{float(o['d']):.3f},asetpts=PTS-STARTPTS,{stereo},"
                  f"volume={float(o.get('volume', 1)):.2f},adelay={ms}:all=1[s{i}]")
        tail = ",".join(af[len(pre_af):]) or "anull"
        g += f";[a0]{''.join(f'[s{i}]' for i in range(len(sfx)))}amix=inputs={len(sfx) + 1}:duration=first:" \
             f"dropout_transition=0:normalize=0,{tail}"
        if music:
            g += f"[a1];{bed}[m];[a1][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
        else:
            g += "[a]"
    elif has_audio and music:
        g += f";[0:a]{chain}[a0];{bed}[m];[a0][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    elif has_audio:
        g += f";[0:a]{chain}[a]"
    elif music:
        g += f";{bed}[a]"
    return g, (W, H)


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


def _restyle(line: str, st: dict, W: int) -> str:
    """Apply the set text-style fields to one ASS "Style:" line (unset fields keep the caption_style look)."""
    f = line.split(",")
    for key, idx, val in (("font", [1], str), ("size", [2], lambda v: str(round(float(v) * W / 1080))),
                          ("color", [3, 4], _ass_color), ("outline_color", [5], _ass_color),
                          ("weight", [7], lambda v: "-1" if v == "bold" else "0"), ("spacing", [13], lambda v: f"{float(v):g}"),
                          ("outline", [16], lambda v: f"{float(v):g}"), ("shadow", [17], lambda v: f"{float(v):g}")):
        if st.get(key) is not None:
            for i in idx:
                f[i] = val(st[key])
    return ",".join(f)


def _text_tags(o: dict, W: int) -> str:
    """Inline ASS overrides for a styled text/emoji overlay."""
    tags = {"font": lambda v: f"\\fn{v}", "size": lambda v: f"\\fs{round(float(v) * W / 1080)}",
            "weight": lambda v: f"\\b{1 if v == 'bold' else 0}", "color": lambda v: f"\\c{_ass_color(v)}",
            "outline": lambda v: f"\\bord{float(v):g}", "outline_color": lambda v: f"\\3c{_ass_color(v)}",
            "shadow": lambda v: f"\\shad{float(v):g}", "spacing": lambda v: f"\\fsp{float(v):g}"}
    return "".join(fn(o[k]) for k, fn in tags.items() if o.get(k) is not None)


OVER_ANIM = {"pop": "\\fscx30\\fscy30\\t(0,150,\\fscx115\\fscy115)\\t(150,260,\\fscx100\\fscy100)", "fade": "\\fad(200,200)",
             "shake": "\\t(0,70,\\frz6)\\t(70,140,\\frz-6)\\t(140,210,\\frz3)\\t(210,280,\\frz0)"}  # ASS tags per text anim
STICKER_ALIGN = {"top-left": 7, "top-center": 8, "top-right": 9, "bottom-left": 1, "bottom-right": 3}


def build_ass(words: list[dict], start: float, end: float, speed: float, size: tuple[int, int], title: str = "",
              uppercase: bool = True, highlight: str = "#FFD400", captions: bool = True, style: str = "bold",
              pos: str = "low", sticker: str = "", sticker_pos: str = "top-right", keeps=None, split: float | None = None,
              overlays=None, text_style: dict | None = None) -> str:
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
        for chunk in caption_chunks(words, start, end, 3 if vertical else 6, keeps):
            texts = [_ass_text(w["w"].strip()) for w in chunk]
            if uppercase:
                texts = [t.upper() for t in texts]
            for i, w in enumerate(chunk):  # one event per word: the spoken word is highlighted
                t0 = rel(w["s"])
                t1 = rel(chunk[i + 1]["s"]) if i + 1 < len(chunk) else rel(w["e"]) + 0.15
                body = " ".join(f"{{\\c{hl}}}{t}{{\\c&H00FFFFFF&}}" if j == i else t for j, t in enumerate(texts))
                lines.append(f"Dialogue: 0,{_ass_time(t0)},{_ass_time(min(t1, clip_len))},Cap,,0,0,0,,{glow}{body}")
    return "\n".join(lines) + "\n"


@functools.cache
def _has_nvenc() -> bool:
    try:
        return "h264_nvenc" in _run([FFMPEG, "-hide_banner", "-encoders"]).stdout
    except RuntimeError:
        return False


def _encode(args: list, out: Path, cwd: Path, out_dur: float, progress) -> None:
    tmp = out.with_name(out.stem + ".rendering.mp4")  # never overwrite a file an upload may be reading
    tail = ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(tmp.resolve())]
    encoders = [["-c:v", "libx264", "-preset", "medium", "-crf", "19"]]
    if _has_nvenc():
        encoders.insert(0, ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0"])
    err = ""
    for enc in encoders:
        log = cwd / f"ffmpeg_{out.stem}.log"
        with open(log, "w", encoding="utf-8") as lf:
            p = subprocess.Popen(args + enc + tail, cwd=cwd, stdout=subprocess.PIPE, stderr=lf, text=True, creationflags=NO_WINDOW)
            for line in p.stdout:
                if line.startswith("out_time_us=") and progress and out_dur > 0:
                    try:
                        progress(min(int(line.split("=")[1]) / 1e6 / out_dur, 1.0))
                    except ValueError:
                        pass
            p.wait()
        err = log.read_text("utf-8", "replace")
        log.unlink(missing_ok=True)
        if p.returncode == 0:
            os.replace(tmp, out)
            return
    raise RuntimeError(f"Render failed: {err.strip()[-1500:]}")


def render(src: Path, out: Path, start: float, end: float, edit: dict, meta: dict, ass: str | None, progress=None,
           keeps: list | None = None, music: Path | None = None, overlays: list | None = None, fonts: list | None = None) -> None:
    """overlays = media overlays ({kind image|gif|video|sound, path, t, d, ...} in clip time); text/emoji ride in `ass`."""
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
        placed.append({**o, "a": a, "b": b, "input": 1 + bool(music) + len(placed),
                       "audio": o["kind"] == "video" and probe(Path(o["path"]))["has_audio"]})
        extra += loop + ["-i", str(Path(o["path"]).resolve())]
    graph, _ = build_filters(e, meta["width"], meta["height"], out_dur, bool(ass), keeps_rel, bool(meta.get("has_audio")),
                             bool(music), meta.get("fps") or 30, placed, bool(ass and fonts))
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
        if meta.get("has_audio") or music or any(o["kind"] == "sound" or o["audio"] for o in placed):
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
