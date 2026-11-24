"""Reelwright desktop app: runs the local engine and shows the UI in a native window (WebView2 on Windows)."""
import os
import socket
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
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def pick_port(want: int) -> tuple[int, bool]:
    """(port, already_running). Prefer `want` (the TikTok redirect URI is registered against it); if another program holds
    it, reuse a Reelwright already running on a fallback port, else take the first free one. 8766 is the LAN receiver's."""
    ports = [want] + [p for p in range(want + 1, want + 35) if p != 8766]
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
    want = int(os.environ.get("REELWRIGHT_PORT", 8765))
    try:
        port, running = pick_port(want)
    except RuntimeError as e:
        _alert(f"{e}\nClose one of them, or set REELWRIGHT_PORT to a free port, and start Reelwright again.")
        raise SystemExit(str(e))
    os.environ["REELWRIGHT_PORT"] = str(port)  # before importing server: its Host check reads this
    if running:  # already open (maybe minimized): bring that window back instead of opening a second copy
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/app/show", data=b"", method="POST"), timeout=3)
            return
        except Exception:
            pass  # an older build without the route: fall through and open a window on it
    else:
        import uvicorn

        from .config import DATA
        from .server import app

        log = open(DATA / "reelwright.log", "a", encoding="utf-8", buffering=1)  # frozen GUI apps have no console
        if sys.stdout is None:
            sys.stdout = sys.stderr = log
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
    WINDOW = webview.create_window("Reelwright", f"http://127.0.0.1:{port}/", width=1480, height=920, min_size=(900, 600),
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
