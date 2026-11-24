"""Health check for the whole toolchain: what works, what's degraded, and the exact fix.

Used by Settings → Diagnostics and `python -m reelwright doctor` (also handy when developing: run it after
changing dependencies, GPUs or the packaged build).
"""
import shutil
import subprocess
import sys
from datetime import datetime

from . import config, media

OK, WARN, FAIL = "ok", "warn", "fail"


def _check(name, fn):
    try:
        status, detail, fix = fn()
    except Exception as e:
        status, detail, fix = FAIL, f"check crashed: {e}", ""
    return {"name": name, "status": status, "detail": detail, "fix": fix}


# filters renders, analysis and QA run (boxblur and eq are GPL-only: LGPL builds get substitutes, see media._blur/_eq);
# lut3d and arnndn are for LUTs and voice clean-up
NEEDED_FILTERS = ("subtitles", "loudnorm", "sidechaincompress", "amix", "zoompan", "silencedetect", "ebur128", "astats",
                  "blackdetect", "freezedetect", "lut3d", "arnndn")


def _ffmpeg():
    try:
        out = subprocess.run([media.FFMPEG, "-hide_banner", "-version"], capture_output=True, text=True,
                             creationflags=media.NO_WINDOW, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return FAIL, "ffmpeg not found", "Install ffmpeg and put ffmpeg/ffprobe on PATH (the packaged .exe bundles them)."
    detail = f"{out.splitlines()[0][:70] if out else 'ffmpeg'} ({media.licence()} build)"
    missing = [f for f in NEEDED_FILTERS if f not in media._filter_names()]
    if missing:
        return FAIL, f"{detail}; missing filters: {', '.join(missing)}", \
            "Use a full ffmpeg build (gyan.dev 'full', or BtbN's win64 gpl/lgpl builds)."
    return OK, detail, ""


def _encoders():
    names = [n for n, _ in media.encoders()]
    if not names:
        return FAIL, "no usable H.264 encoder: renders fail", \
            "Update the graphics driver, or use an ffmpeg build with libx264 or OpenH264."
    if set(names) & media.HW_ENCODERS:
        return OK, f"usable: {', '.join(names)} (hardware first)", ""
    return WARN, f"CPU encoding only ({', '.join(names)}): works, slower", \
        "A current NVIDIA, Intel or AMD graphics driver enables hardware encoding."


def _cuda():
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
    except Exception as e:
        return WARN, f"can't query CUDA ({e})", ""
    if n:
        return OK, f"{n} CUDA device(s): Whisper transcribes on the GPU", ""
    return WARN, "no CUDA device: Whisper runs on the CPU (much slower on long VODs)", \
        "Install an NVIDIA driver; from source also `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`."


def _claude():
    if not config.api_key():
        return WARN, "no API key: offline heuristics + your learned reranker only", "Settings: paste an Anthropic API key."
    return OK, f"API key set; model {config.load()['model']}", ""


def _claude_live():
    import anthropic
    key = config.api_key()
    if not key:
        return WARN, "skipped (no key)", ""
    try:
        anthropic.Anthropic(api_key=key).models.retrieve(config.load()["model"])
    except anthropic.AuthenticationError:
        return FAIL, "the API key was rejected", "Paste a valid key from console.anthropic.com."
    except anthropic.NotFoundError:
        return FAIL, f"model {config.load()['model']} isn't available to this key", "Pick another model in Settings."
    except anthropic.APIConnectionError:
        return FAIL, "can't reach api.anthropic.com", "Check the internet connection / proxy."
    return OK, "key and model verified with Anthropic", ""


def _disk():
    where = config.projects_dir()  # renders and VOD downloads land in the storage folder (Settings › Storage)
    if not where.is_dir():
        return FAIL, f"the storage folder {where} isn't available", "Reconnect its drive, or pick another folder in Settings › Storage."
    free = shutil.disk_usage(where).free / 1e9
    if free < 5:
        return FAIL, f"{free:.1f} GB free at {where}", "Free disk space: renders and VOD downloads need room."
    return (WARN if free < 25 else OK), f"{free:.0f} GB free at {where}", "Long VODs need ~2-6 GB per hour." if free < 25 else ""


def _ytdlp():
    import yt_dlp.version
    v = yt_dlp.version.__version__
    age = (datetime.now() - datetime.strptime(v[:10], "%Y.%m.%d")).days
    if age > 60:
        return WARN, f"yt-dlp {v} is {age} days old: sites change and imports start failing", "pip install -U yt-dlp"
    return OK, f"yt-dlp {v}", ""


def _obs():
    try:
        import websockets  # noqa: F401
    except ImportError:
        return WARN, "websockets not installed: direct OBS connections unavailable", "pip install websockets"
    from . import lan, store
    conns = store.find("obs")
    live = sum(c.get("status") == "connected" for c in conns)
    s = lan.status()
    parts = [f"{live}/{len(conns)} OBS connections live" if conns else "no direct OBS connections",
             f"network receiver {'on, port ' + str(s['port']) if s['running'] else 'off'}"]
    bad = [c["name"] for c in conns if c.get("enabled") and c.get("status") != "connected"]
    if s["enabled"] and not s["running"]:
        return FAIL, "; ".join(parts) + f" - {s['error']}", "Pick a free port on the OBS page (Network receiver)."
    if bad:
        return WARN, "; ".join(parts) + f" - offline: {', '.join(bad)}", "Is OBS running with its WebSocket server enabled?"
    return OK, "; ".join(parts), ""


def _publishing():
    from . import platforms
    st = platforms.status()
    on = [p for p in ("youtube", "tiktok", "instagram") if st.get(p, {}).get("connected")]
    return (OK if on else WARN), (f"connected: {', '.join(on)}" if on else "no platforms connected"), \
        "" if on else "Publish page: connect YouTube, TikTok or Instagram."


def _learning():
    from . import learn, pipeline
    fb = pipeline.feedback()
    m = learn.train(fb)
    if not fb:
        return WARN, "no feedback yet", "Rate clips (thumbs up/down) in the editor: the reranker starts learning after ~12."
    detail = f"{len(fb)} lessons"
    if m:
        detail += f"; reranker {m['cv_acc']:.0%} vs {m['baseline']:.0%} baseline ({'active' if m['useful'] else 'not yet better'})"
    return OK, detail, ""


CHECKS = [("FFmpeg", _ffmpeg), ("Video encoders", _encoders), ("GPU transcription", _cuda), ("Claude", _claude),
          ("Disk space", _disk), ("Video downloader", _ytdlp), ("OBS link", _obs), ("Publishing", _publishing),
          ("Learning", _learning)]


def run(deep: bool = False) -> list[dict]:
    checks = CHECKS + ([("Claude (live)", _claude_live)] if deep else [])
    return [_check(n, f) for n, f in checks]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252: never crash on a ✓
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    marks = {OK: "[ok]", WARN: "[! ]", FAIL: "[XX]"}
    results = run(deep=True)
    for r in results:
        print(f" {marks[r['status']]} {r['name']:<18} {r['detail']}" + (f"\n      -> {r['fix']}" if r["fix"] else ""))
    return 1 if any(r["status"] == FAIL for r in results) else 0
