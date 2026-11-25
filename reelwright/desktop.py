"""Reelwright desktop app: runs the local engine and shows the UI in a native window (WebView2 on Windows)."""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request


def _port_in_use(port: int) -> bool:
    # a bind test is instant; connect_ex to a closed localhost port takes ~2 s on Windows (SYN retries), which made
    # scanning fallback ports stall startup for a minute
    with socket.socket() as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # also catches a listener on 0.0.0.0
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _is_reelwright(port: int) -> bool:
    """Our /api/settings answers (other local apps, e.g. Lodestar on 8765, may have a route by that name too)."""
    try:
        with urllib.request.urlopen(_req(port, "/api/settings"), timeout=2) as r:
            return r.status == 200 and "whisper_model" in json.loads(r.read(1 << 20) or b"{}")
    except Exception:
        return False


def _req(port: int, path: str, data: bytes | None = None):
    """A request to the app on `port` with its per-launch token (server.py writes it to config.TOKEN_FILE at start-up)."""
    from .config import app_token
    return urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if data is not None else "GET",
                                  headers={"X-Reelwright-Token": app_token()})


OLD_APP_PORTS = [8765, *range(8767, 8800)]  # where builds before the 38765 block ran (8766 was their LAN receiver)


def find_old_copy() -> int | None:
    """The port of an older Reelwright still running on the old block (maybe minimized), else None. Bind tests are
    instant, so only ports something listens on get an HTTP check."""
    return next((p for p in OLD_APP_PORTS if _port_in_use(p) and _is_reelwright(p)), None)


def pick_port(want: int) -> tuple[int, bool]:
    """(port, already_running). Prefer `want` (the TikTok redirect URI is registered against it); if another program holds
    it, reuse a Reelwright already running on a fallback port, else take the first free one (config.app_ports)."""
    from .config import app_ports
    ports = app_ports(want)
    for p in ports:
        if _port_in_use(p) and _is_reelwright(p):
            return p, True
    for p in ports:
        if not _port_in_use(p):
            return p, False
    raise RuntimeError(f"Ports {want}-{want + 34} are all used by other programs.")


WINDOW = None        # the app window, when running as the desktop app (None in browser/dev mode)
_quitting = False    # set by "Quit Reelwright": the next close really exits
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def on_closing(window):
    """Clicking ✕ minimizes to the taskbar (renders, uploads and Autopilot keep running) unless the user turned
    that off in Settings or chose Quit. Returning False cancels the close in pywebview."""
    from . import config
    if _quitting or not config.load().get("close_to_taskbar", True):
        return None
    window.minimize()
    return False


def show_window() -> None:
    if WINDOW:
        WINDOW.restore()
        WINDOW.show()


def quit_app() -> None:
    global _quitting
    _quitting = True
    if WINDOW:
        WINDOW.destroy()


_job = None  # the job object handle: kept open for the life of the process (closing it is what kills the children)
# KILL_ON_JOB_CLOSE | BREAKAWAY_OK: only a process started with CREATE_BREAKAWAY_FROM_JOB (open_url) leaves the job.
# Never SILENT_BREAKAWAY_OK (0x1000): that would let every child (ffmpeg too) out.
JOB_LIMITS = 0x2000 | 0x800
BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
RUNDLL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "rundll32.exe")


def open_url(url: str) -> bool:
    """Open an http(s) link in the default browser from outside the job: a browser the app started inside it (none was
    running yet) would be killed when Reelwright quits. rundll32 url.dll,FileProtocolHandler hands the link to the
    shell; no shell parsing, and anything but http(s) (a file, a program, another protocol) is refused."""
    if not re.match(r"https?://[^\s]", url or "", re.I):
        return False
    url = re.sub(r'[\x00-\x20"\x7f]', lambda m: f"%{ord(m.group()):02X}", url)  # one argument, passed through as-is
    try:
        subprocess.Popen(f'"{RUNDLL}" url.dll,FileProtocolHandler {url}', creationflags=BREAKAWAY, close_fds=True)
    except OSError:  # e.g. a parent job that forbids breakaway: webbrowser's next choice opens it (inside the job)
        return False
    return True


def route_browser() -> None:
    """Every webbrowser.open in the process goes through open_url: server.py's Watch-render links, pywebview's external
    links (OPEN_EXTERNAL_LINKS_IN_BROWSER calls webbrowser.open) and the YouTube sign-in (google_auth_oauthlib)."""
    import webbrowser

    class OutsideJob(webbrowser.BaseBrowser):
        def open(self, url, new=0, autoraise=True):
            return open_url(url)
    webbrowser.register("reelwright", None, OutsideJob("reelwright"), preferred=True)


def kill_children_on_exit() -> bool:
    """Windows: put this process in a job object that kills every process in it when the last handle closes, i.e. when
    the app exits or crashes. Children (ffmpeg, yt-dlp, a training worker, WebView2) inherit the job, so a Quit or a
    crash never leaves an ffmpeg writing a render that pipeline.start will queue again. Browsers the app opens are
    started outside it (route_browser). True when set up."""
    global _job
    if sys.platform != "win32" or _job:
        return bool(_job)
    import ctypes
    from ctypes import wintypes

    class Limits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (x64/x86 layout via ctypes)
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
                    ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k.CreateJobObjectW(None, None)
    info = Limits(LimitFlags=JOB_LIMITS)
    if not job or not k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or \
            not k.AssignProcessToJobObject(job, k.GetCurrentProcess()):  # 9 = JobObjectExtendedLimitInformation
        return False  # ponytail: e.g. a parent job that forbids nesting; media.kill_children still runs at exit
    _job = job
    route_browser()
    return True


def boot_command() -> str:
    """What Windows runs at sign-in: the packaged exe, or pythonw + Reelwright.pyw from a source checkout."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" "{root / "Reelwright.pyw"}" --minimized'


def _run_key_get():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, "Reelwright")[0]
    except FileNotFoundError:
        return None


def _run_key_set(value) -> None:  # per-user Run key: no admin rights needed; None removes it
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if value:
            winreg.SetValueEx(k, "Reelwright", 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(k, "Reelwright")
            except FileNotFoundError:
                pass


def start_on_boot() -> bool:
    try:
        return bool(_run_key_get())
    except (ImportError, OSError):
        return False


def set_start_on_boot(on: bool) -> None:
    _run_key_set(boot_command() if on else None)


def _alert(text: str) -> None:  # windowed .exe: no console, so a failure must be shown, not printed
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Reelwright", 0x10)
    except Exception:
        pass


def main() -> None:
    if "--mcp" in sys.argv:  # started by Claude Desktop: speak MCP over stdio to the running app; no window, no server
        from .mcp_server import main as mcp_main
        raise SystemExit(mcp_main())
    from .config import DEFAULT_PORT
    want = int(os.environ.get("REELWRIGHT_PORT", DEFAULT_PORT))
    try:
        port, running = pick_port(want)
    except RuntimeError as e:
        _alert(f"{e}\nClose one of them, or set REELWRIGHT_PORT to a free port, and start Reelwright again.")
        raise SystemExit(str(e))
    old = None if running else find_old_copy()
    if old:  # a build from before the port move: a second server would share its database
        try:
            urllib.request.urlopen(_req(old, "/api/app/show", b""), timeout=3)
        except Exception:
            pass
        _alert(f"An older Reelwright is still running (port {old}, maybe minimized in the taskbar). Close it first "
               "(right-click its taskbar icon › Close, or Quit in its window), then start Reelwright again.")
        raise SystemExit(f"an older Reelwright is running on port {old}")
    os.environ["REELWRIGHT_PORT"] = str(port)  # before importing server: its Host check reads this
    if running:  # already open (maybe minimized): bring that window back instead of opening a second copy
        try:
            urllib.request.urlopen(_req(port, "/api/app/show", b""), timeout=3)
            return
        except Exception:
            pass  # an older build without the route: fall through and open a window on it
    else:
        import uvicorn

        kill_children_on_exit()  # this process runs the engine: its ffmpeg children go with it
        from .config import DATA
        from .server import app

        from .log import RotatingStream
        if sys.stdout is None:  # frozen GUI apps have no console; capped at 2 MB × 3 old files
            sys.stdout = sys.stderr = RotatingStream(DATA / "reelwright.log")
        if port != want:
            from . import log as events
            events.event("startup", f"port {want} is used by another program; running on {port}. "
                                    f"Connecting TikTok needs port {want} free (its redirect URI is registered there).")
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(150):
            if _is_reelwright(port):
                break
            time.sleep(0.1)

    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    global WINDOW
    from .config import app_token
    url = f"http://127.0.0.1:{port}/?rw_token={app_token().split(',')[0]}"  # this data folder's: swapped for a session cookie
    WINDOW = webview.create_window("Reelwright", url, width=1480, height=920, min_size=(900, 600),
                                   background_color="#0B0C10", text_select=True,
                                   minimized="--minimized" in sys.argv)  # started with Windows: wait in the taskbar
    WINDOW.events.closing += on_closing
    webview.start(private_mode=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
