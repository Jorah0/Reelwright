"""Keep dist/Reelwright/Reelwright.exe in step with the source:  python scripts/update_exe.py [--force] [--no-launch]

1. Skips the build when the .exe is already newer than every source file (use --force to rebuild anyway).
2. If Reelwright is running, waits until it has no running jobs or uploads (never cuts off a render or a post).
3. Closes Reelwright, rebuilds with scripts/build_exe.py, and reopens the new build.
Your library and settings live in %LOCALAPPDATA%\\Reelwright, outside the build, so nothing is lost.
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "Reelwright" / "Reelwright.exe"
SOURCES = [ROOT / "reelwright", ROOT / "Reelwright.pyw", ROOT / "requirements.txt", ROOT / "scripts" / "build_exe.py", ROOT / "assets"]
PORTS = [8765, *range(8767, 8800)]  # desktop.pick_port: 8765, or a fallback when another program holds it


def newest_source(paths=SOURCES) -> float:
    newest = 0.0
    for p in paths:
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts] if p.exists() else []
        newest = max([newest, *(f.stat().st_mtime for f in files)])
    return newest


def outdated(exe: Path = EXE, paths=SOURCES) -> bool:
    return not exe.exists() or exe.stat().st_mtime < newest_source(paths)


def _get(path: str, port: int):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": f"127.0.0.1:{port}"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def _app_port() -> int | None:
    """The port the running Reelwright answers on (another app on 8765 answers differently and is skipped)."""
    sys.path.insert(0, str(ROOT))
    from reelwright.desktop import _port_in_use  # instant bind test; a request to a closed port costs ~2 s on Windows
    for port in PORTS:
        if not _port_in_use(port):
            continue
        try:
            if isinstance(_get("/api/jobs", port), list):
                return port
        except (OSError, ValueError):
            pass
    return None


def busy() -> list[str] | None:
    """What the running app is doing (None = not running)."""
    port = _app_port()
    if port is None:
        return None
    try:
        jobs, ups = _get("/api/jobs", port), _get("/api/uploads", port)
    except (OSError, ValueError):
        return None
    return ([f"{j['kind']} ({j['status']})" for j in jobs if j["status"] in ("running", "queued")]
            + [f"{u.get('platform')} upload" for u in ups if u.get("status") == "uploading"])


def running() -> bool:
    """Any Reelwright.exe process at all (the API check above misses one that is still starting or has no window)."""
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Reelwright.exe", "/NH"], capture_output=True, text=True).stdout
    return "Reelwright.exe" in out


def close_app(timeout: float = 20) -> None:
    """Close every Reelwright process and wait until Windows has released the build's files."""
    if not running():
        return
    subprocess.run(["taskkill", "/IM", "Reelwright.exe", "/F", "/T"], capture_output=True)
    end = time.time() + timeout
    while running() and time.time() < end:
        time.sleep(0.5)
    time.sleep(1)  # file handles are released a moment after the process ends


def main(argv=None) -> int:
    args = set(argv if argv is not None else sys.argv[1:])
    if not outdated() and "--force" not in args:
        print(f"Reelwright.exe is up to date (built {time.ctime(EXE.stat().st_mtime)}).")
        return 0
    deadline = time.time() + 30 * 60
    while (work := busy()):
        if time.time() > deadline:
            print("Reelwright has been busy for 30 minutes; try again later:", ", ".join(work))
            return 2
        print("Waiting for Reelwright to finish:", ", ".join(work))
        time.sleep(15)
    close_app()
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_exe.py")], cwd=ROOT)
    if r.returncode:
        print("Build failed; the previous Reelwright.exe was left as it was (if it still exists).")
        return r.returncode
    if "--no-launch" not in args:
        subprocess.Popen([str(EXE)], cwd=EXE.parent, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        print("Reopened the updated Reelwright.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
