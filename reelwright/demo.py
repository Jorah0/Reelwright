"""Demo mode: a seeded sandbox library for the phone simulator, the mobile tests and trying Reelwright out.

`python -m reelwright demo [--port N] [--remote-port N] [--data DIR] [--open]` makes a new temporary data folder, seeds it
(sample projects with small generated videos, AI clips to review, a rendered clip, sandbox accounts, scheduled posts, a
preset and a long form) and serves the app plus the phone remote with the simulator on. It prints one JSON line with the
addresses. Demo mode (config.DEMO, from REELWRIGHT_DEMO=1) never reaches a platform or an AI provider: platforms.publish
pretends, only sandbox accounts look connected, youtube._creds and ai._ask refuse. It never runs on the real library.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

WORDS = ("no way that actually worked", "okay chat watch this", "clutch or kick", "the boss is at one hp",
         "that was the cleanest play all night", "who put that wall there", "I'm never doing that again", "let's go")


def check_data_dir(path) -> Path:
    """The demo's data folder, never the real library (the app's default folders): SystemExit otherwise."""
    p = Path(path).resolve()
    real = {(config.ROOT / "data").resolve(), (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Reelwright").resolve()}
    if any(p == r or r in p.parents or p in r.parents for r in real):
        raise SystemExit(f"Demo mode never runs on your real library ({p}): it uses a temporary folder of its own.")
    return p


def _ffmpeg(out: Path, seconds: float, size: str, rate: int, hue: int) -> None:
    from . import media
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([media.FFMPEG, "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency={220 + hue}:duration={seconds}",
                    "-vf", f"hue=h={hue}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(out)], check=True, timeout=120, creationflags=media.NO_WINDOW)


def _still(src: Path, t: float, out: Path) -> None:
    from . import media
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-ss", str(t), "-i", str(src), "-frames:v", "1", str(out)], check=True, timeout=60,
                   creationflags=media.NO_WINDOW)


def _segments(duration: float) -> list[dict]:
    segs, t, i = [], 1.0, 0
    while t + 3 < duration:
        text = WORDS[i % len(WORDS)]
        words, w0 = [], t
        for w in text.split():
            words.append({"s": round(w0, 2), "e": round(w0 + 0.3, 2), "w": f" {w}"})
            w0 += 0.35
        segs.append({"start": t, "end": round(w0, 2), "text": text, "words": words})
        t, i = t + 4.0, i + 1
    return segs


def _project(name: str, seconds: float, small: bool, hue: int) -> dict:
    from . import pipeline, store
    size, rate = ("640x360", 15) if small else ("1280x720", 30)
    p = store.put("project", {"name": name, "status": "importing", "stage": "", "progress": 0.0, "error": "", "source": "source.mp4",
                              "meta": {"duration": float(seconds), "width": int(size.split("x")[0]), "height": int(size.split("x")[1]), "fps": rate},
                              "demo": True})
    folder = pipeline.pdir(p["id"])
    _ffmpeg(folder / "source.mp4", seconds, size, rate, hue)
    _still(folder / "source.mp4", 2, folder / "poster.jpg")
    pipeline.save_json(p["id"], "transcript.json", {"segments": _segments(seconds)})
    pipeline.save_json(p["id"], "signals.json", {"silences": []})
    return store.update("project", p["id"], {"status": "ready", "stage": "Ready", "progress": 1.0})


def _rendered(clip: dict, small: bool) -> dict:
    """A small vertical stand-in for the clip's render (the real render path is the desktop's; this keeps seeding fast)."""
    from . import pipeline, store
    folder = pipeline.pdir(clip["project_id"]) / "exports"
    out = folder / f"{clip['id']}.mp4"
    _ffmpeg(out, round(clip["end"] - clip["start"], 2), "360x640" if small else "720x1280", 15 if small else 30, 140)
    _still(out, 1, folder / f"{clip['id']}.jpg")
    return store.update("clip", clip["id"], {"export": {"status": "done", "rev": clip["rev"], "file": f"exports/{clip['id']}.mp4",
                                                         "cover": f"exports/{clip['id']}.jpg", "cover_t": 1.0}})


def seed(small: bool = False) -> dict:
    """Fill the (demo) library; returns the ids the tests and the mobile suite drive."""
    from . import accounts, longform, pipeline, store
    if not config.DEMO:
        raise SystemExit("demo.seed only runs in demo mode (REELWRIGHT_DEMO=1).")
    check_data_dir(config.DATA)
    secs = 40 if small else 90
    a = _project("Demo stream: ranked night", secs, small, 0)
    b = _project("Demo podcast: episode 12", secs, small, 200)
    clip = lambda p, s, e, title, score, hook="": pipeline.new_clip(p["id"], {"start": s, "end": e, "title": title, "score": score,  # noqa: E731
                                                                               "hook": hook, "hashtags": ["gaming", "clips"]}, source="ai")
    review = [clip(a, 2.0, 14.0, "The one-HP comeback", 91, "He had one HP left"), clip(a, 16.0, 28.0, "Who put that wall there", 77),
              clip(b, 4.0, 18.0, "Never doing that again", 69, "A story you won't believe"), clip(b, 22.0, 34.0, "Okay chat, watch this", 64),
              clip(a, 30.0, 38.0, "Clutch or kick", 58)]
    edit = clip(a, 5.0, 17.0, "Cleanest play all night", 88, "The cleanest play of the night")
    edit = store.update("clip", edit["id"], {"rating": 1})
    edit = _rendered(edit, small)
    edit2 = _rendered(store.update("clip", clip(b, 8.0, 20.0, "The story, told right", 82)["id"], {"rating": 1}), small)
    posted = _rendered(store.update("clip", clip(b, 20.0, 30.0, "Already posted", 60)["id"], {"rating": 1}), small)

    yt = accounts.add("youtube", name="Demo Channel", handle="@reelwright-demo", approved=True, sandbox=True)
    ig = accounts.add("instagram", name="Demo Reels", handle="@reelwright.demo", approved=True, sandbox=True)
    no = accounts.add("youtube", name="Second channel (not approved)", handle="@demo-two", approved=False, sandbox=True)
    accounts.update(yt["id"], {"description_template": "{description}\n\nFull stream on the channel."})

    later = lambda h: (datetime.now(timezone.utc) + timedelta(hours=h)).replace(minute=0, second=0, microsecond=0).isoformat()  # noqa: E731
    pipeline.request_upload(edit["id"], {"platform": "youtube", "account_id": yt["id"], "title": edit["title"], "upload_at": later(26)})
    pipeline.request_upload(edit["id"], {"platform": "instagram", "account_id": ig["id"], "title": edit["title"], "publish_at": later(50)})
    store.put("upload", {"platform": "youtube", "account_id": yt["id"], "clip_id": posted["id"], "project_id": posted["project_id"],
                         "title": posted["title"], "status": "done", "uploaded_at": later(-30), "url": "https://demo.invalid/youtube/posted",
                         "description": "", "caption": "", "tags": [], "privacy": "public", "publish_at": None, "upload_at": None})

    preset = store.put("preset", {"name": "Bold captions", "scope": "global", "fav": True,
                                  "parts": {"captions": {"captions": True, "caption_style": "boxed"}, "layout": {"layout": "split"}}})
    lf = longform.create(a["id"], "clips", "Best of ranked night", [[2.0, 14.0], [16.0, 28.0]], [], {"clip_ids": [review[0]["id"], review[1]["id"]]}, 0)
    return {"projects": [a["id"], b["id"]], "review": [c["id"] for c in review], "edit": edit["id"], "edits": [edit["id"], edit2["id"]], "posted": posted["id"],
            "accounts": [yt["id"], ig["id"], no["id"]], "unapproved": no["id"], "preset": preset["id"], "longform": lf["id"]}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reelwright demo", description="Reelwright demo sandbox (no real accounts, no uploads).")
    ap.add_argument("--port", type=int, default=0, help="the app's port (default: a free one)")
    ap.add_argument("--remote-port", type=int, default=0, help="the phone remote's port (default: a free one)")
    ap.add_argument("--data", default="", help="the demo's data folder (default: a new temporary one)")
    ap.add_argument("--small", action="store_true", help="smaller, faster sample videos")
    ap.add_argument("--open", action="store_true", help="open the phone simulator in the browser")
    args = ap.parse_args(argv)
    if os.environ.get("REELWRIGHT_DEMO") != "1":  # config was loaded for the real library: run again inside the sandbox
        data = check_data_dir(args.data or tempfile.mkdtemp(prefix="reelwright-demo-"))
        env = {**os.environ, "REELWRIGHT_DATA": str(data), "REELWRIGHT_DEMO": "1", "REELWRIGHT_NO_BROWSER": "1"}
        try:
            return subprocess.call([sys.executable, "-m", "reelwright", "demo", *(argv if argv is not None else sys.argv[2:])], env=env)
        finally:
            if not args.data:  # the temporary sandbox this run made (sample videos, store): gone with it
                shutil.rmtree(data, ignore_errors=True)
    import uvicorn

    from . import remote
    port, rport = args.port or _free_port(), args.remote_port or _free_port()
    os.environ["REELWRIGHT_PORT"] = str(port)  # before server.py loads: its local-host check uses it
    from . import server  # after the port is set; its per-launch token is printed below for the demo's tools
    server._publish_token()
    ids = seed(small=args.small)
    config.save({"remote_enabled": True, "remote_port": rport, "remote_dev_sim": True})
    remote.apply()
    sim = remote.sim_link()
    print(json.dumps({"app": f"http://127.0.0.1:{port}/", "remote": f"http://127.0.0.1:{rport}/", "sim": sim["url"], "data": str(config.DATA),
                      "token": server.TOKEN, "open": f"http://127.0.0.1:{port}/?rw_token={server.TOKEN}", "ids": ids}), flush=True)
    if args.open:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(sim["url"])).start()
    started = time.time()
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")
    return 0 if time.time() - started > 1 else 1
