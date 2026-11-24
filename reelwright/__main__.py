"""`python -m reelwright` — start the app and open it in the browser.
`python -m reelwright doctor` — check the toolchain.  `python -m reelwright relay ...` — run the OBS relay on this PC."""
import os
import sys
import threading
import webbrowser

PORT = int(os.environ.get("REELWRIGHT_PORT", 8765))

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "doctor":
        from .doctor import main
        sys.exit(main())
    if cmd == "relay":
        from .relay import main
        sys.exit(main(sys.argv[2:]))
    import uvicorn

    if not os.environ.get("REELWRIGHT_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    uvicorn.run("reelwright.server:app", host="127.0.0.1", port=PORT, log_level="warning")
