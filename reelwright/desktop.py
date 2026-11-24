"""Reelwright desktop app: runs the local engine and shows the UI in a native window (WebView2 on Windows)."""
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request


def _port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_reelwright(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def main() -> None:
    port = int(os.environ.get("REELWRIGHT_PORT", 8765))
    os.environ["REELWRIGHT_PORT"] = str(port)
    if _port_in_use(port):
        if not _is_reelwright(port):
            # ponytail: fixed port because the TikTok OAuth redirect URI is registered against it
            raise SystemExit(f"Port {port} is used by another program. Set REELWRIGHT_PORT to a free port and try again.")
    else:
        import uvicorn

        from .config import DATA
        from .server import app

        log = open(DATA / "reelwright.log", "a", encoding="utf-8", buffering=1)  # frozen GUI apps have no console
        if sys.stdout is None:
            sys.stdout = sys.stderr = log
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(150):
            if _is_reelwright(port):
                break
            time.sleep(0.1)

    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.create_window("Reelwright", f"http://127.0.0.1:{port}/", width=1480, height=920, min_size=(900, 600),
                          background_color="#0B0C10", text_select=True)
    webview.start(private_mode=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
