"""The phone app, end to end, on emulated phones:  python scripts/mobile_test.py [--out DIR] [--headed]

Starts a demo-mode Reelwright (reelwright/demo.py: its own temporary library, sandbox accounts, nothing ever posted) on
free ports, then drives the REAL phone app through its signed API with Playwright's iPhone and Pixel profiles (touch,
viewport, user agent): pairing, every tab, a review swipe, trim + re-render, a preset, a schedule move, a publish refused
by account approval and an action queued offline then sent. Saves a screenshot per page per device. Tears it all down.

Dev only (not in verify.py, not in the .exe): pip install -r requirements-dev.txt && python -m playwright install chromium
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEVICES = ("iPhone 15", "Pixel 8")


def start_demo(data: str) -> tuple[subprocess.Popen, dict]:
    env = {**os.environ, "REELWRIGHT_DATA": data, "REELWRIGHT_DEMO": "1", "REELWRIGHT_NO_BROWSER": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "reelwright", "demo", "--small"], cwd=ROOT, env=env, stdout=subprocess.PIPE, text=True)
    got: dict = {}
    t = threading.Thread(target=lambda: got.update(json.loads(proc.stdout.readline() or "{}")), daemon=True)
    t.start()
    t.join(180)
    if not got.get("app"):
        proc.kill()
        raise SystemExit("The demo server didn't start (see its output above).")
    for _ in range(100):  # the app answers once uvicorn is up
        try:
            urllib.request.urlopen(urllib.request.Request(got["app"] + "api/remote", headers={"X-Reelwright-Token": got["token"]}), timeout=2)
            break
        except OSError:
            time.sleep(0.3)
    return proc, got


def pair_link(app: str, token: str) -> str:
    """A fresh simulator pairing code from the app's loopback-only endpoint, opened as a plain phone page."""
    req = urllib.request.Request(app + "api/remote/simulator", data=b"{}", headers={"Content-Type": "application/json", "X-Reelwright-Token": token}, method="POST")
    url = json.loads(urllib.request.urlopen(req, timeout=10).read())["url"]
    return url.replace("/sim.html#", "/#")


class Run:
    def __init__(self, out: Path):
        self.out, self.results = out, []

    def check(self, device: str, name: str, fn):
        t0 = time.time()
        try:
            fn()
            self.results.append((device, name, True, ""))
            print(f"  PASS  {name} ({time.time() - t0:.1f}s)")
        except Exception as e:  # report and go on: one broken screen shouldn't hide the rest
            self.results.append((device, name, False, str(e).splitlines()[0][:200]))
            print(f"  FAIL  {name}: {str(e).splitlines()[0][:200]}")


def drive(pw, device: str, info: dict, run: Run, headed: bool) -> None:
    desc = {k: v for k, v in pw.devices[device].items() if k != "default_browser_type"}
    browser = pw.chromium.launch(headless=not headed)
    ctx = browser.new_context(**desc)
    page = ctx.new_page()
    page.set_default_timeout(15000)
    shots = run.out / device.replace(" ", "-").lower()
    shots.mkdir(parents=True, exist_ok=True)
    ids = {**info["ids"], "edit": info["ids"]["edits"][DEVICES.index(device) % len(info["ids"]["edits"])]}  # its own clip:
    # a schedule move can make a post due, and a posted clip is locked for edits (as on the PC)

    def shot(name):
        page.wait_for_selector("#view .spin", state="detached", timeout=15000)
        page.screenshot(path=str(shots / f"{name}.png"), full_page=True)

    def toast(text, timeout=15000):
        page.locator("#toasts .toast", has_text=text).first.wait_for(timeout=timeout)

    def go(hash_):
        page.evaluate(f"location.hash = {json.dumps(hash_)}")
        page.wait_for_timeout(300)

    def pairing():
        page.goto(pair_link(info["app"], info["token"]))
        page.click("#pgo")
        toast("Paired")
        page.locator("[data-close]").last.click()
        page.wait_for_selector("#tabs a.on")
        shot("home")

    def tabs():
        for h in ("review", "projects", "schedule", "more", "longform", "board", "obs", "activity"):
            go(f"#{h}")
            page.wait_for_selector("#view h1, #view .empty b")
            shot(h)

    def review_swipe():
        go("#review")
        card = page.locator("#rc")
        box = card.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 3
        cdp = ctx.new_cdp_session(page)  # a real touch drag (finger down, slide right, lift), as on the phone
        touch = lambda kind, px: cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [{"x": px, "y": y}] if px is not None else []})  # noqa: E731
        touch("touchStart", x)
        for i in range(1, 13):
            touch("touchMove", x + i * 15)
            page.wait_for_timeout(16)
        touch("touchEnd", None)
        toast("Kept")
        shot("review-after-swipe")

    def preset():
        go(f"#edit/{ids['edit']}")
        page.click(".chip[data-preset]")
        page.click("#cy")
        toast("Preset applied")
        shot("editor-preset")

    def trim_render():
        go(f"#edit/{ids['edit']}")
        page.click('button[data-step="start"][data-d="1"]')
        page.click('button[data-step="end"][data-d="-f"]')
        page.click("#e-render")
        toast("Rendering on the PC")
        toast("Rendered", timeout=120000)  # the demo server renders it for real (ffmpeg), then the preview updates
        shot("editor-rendered")

    def schedule_move():
        go("#schedule")
        page.locator("button[data-move]", has_text="Scheduled").first.click()
        when = (datetime.now() + timedelta(days=3)).replace(hour=10, minute=0).strftime("%Y-%m-%dT%H:%M")
        page.fill("#mt", when)
        shot("schedule-move")
        page.click("#mgo")
        toast("Moved to")
        page.click('#smode button[data-m="month"]')
        shot("schedule-month")

    def publish_blocked():
        go(f"#clip/{ids['edit']}")
        page.click("#pub")
        page.locator(".sheet", has_text="not approved for uploads").wait_for()
        shot("publish-sheet")
        msg = page.evaluate(f"api('clips/{ids['edit']}/publish', {{accounts: [{json.dumps(ids['unapproved'])}], title: 'x'}}).then(() => 'posted', (e) => e.message)")
        assert "approved" in msg, msg  # the PC's approval gate, through the signed API

    def offline_queue():
        go("#review")
        page.wait_for_selector("#keep")
        ctx.set_offline(True)
        page.click("#keep")
        toast("queued")
        ctx.set_offline(False)
        page.evaluate("window.dispatchEvent(new Event('online'))")
        toast("Sent:", timeout=30000)
        shot("offline-retried")

    print(f"\n{device}")
    def checked(name, fn):
        def wrapped():
            try:
                fn()
            except Exception:
                page.screenshot(path=str(shots / f"FAILED-{name.replace(' ', '-')}.png"), full_page=True)
                raise
        return wrapped

    for name, fn in (("pairing", pairing), ("every tab", tabs), ("review swipe", review_swipe), ("preset apply", preset),
                     ("trim and re-render", trim_render), ("schedule move", schedule_move),
                     ("publish blocked by approval", publish_blocked), ("offline action queued and retried", offline_queue)):
        run.check(device, name, checked(name, fn))
    browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "reelwright-mobile-test"), help="screenshots folder")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    args = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Needs Playwright: pip install -r requirements-dev.txt && python -m playwright install chromium")
        return 2
    run = Run(Path(args.out))
    data = tempfile.mkdtemp(prefix="rw-mobile-")
    proc, info = start_demo(data)
    print(f"demo server: {info['app']}  phone app: {info['remote']}  data: {data}")
    try:
        with sync_playwright() as pw:
            for dev in DEVICES:
                drive(pw, dev, info, run, args.headed)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    bad = [r for r in run.results if not r[2]]
    print(f"\n{len(run.results) - len(bad)} passed, {len(bad)} failed. Screenshots: {run.out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
