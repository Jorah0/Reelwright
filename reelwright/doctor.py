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


def _ffmpeg():
    try:
        out = subprocess.run([media.FFMPEG, "-hide_banner", "-version"], capture_output=True, text=True,
                             creationflags=media.NO_WINDOW, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return FAIL, "ffmpeg not found", "Install ffmpeg and put ffmpeg/ffprobe on PATH (the packaged .exe bundles them)."
    return OK, out.split("\n", 1)[0][:90], ""


def _nvenc():
    if media._has_nvenc():
        return OK, "NVIDIA hardware encoding (NVENC) available", ""
    return WARN, "rendering uses the CPU (x264): works, slower", "An NVIDIA GPU with a current driver enables NVENC."


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
    free = shutil.disk_usage(config.DATA).free / 1e9
    if free < 5:
        return FAIL, f"{free:.1f} GB free at {config.DATA}", "Free disk space: renders and VOD downloads need room."
    return (WARN if free < 25 else OK), f"{free:.0f} GB free at {config.DATA}", "Long VODs need ~2-6 GB per hour." if free < 25 else ""


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


CHECKS = [("FFmpeg", _ffmpeg), ("GPU encoding", _nvenc), ("GPU transcription", _cuda), ("Claude", _claude),
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
