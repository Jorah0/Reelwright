"""`python -m reelwright` — start the app and open it in the browser.
`python -m reelwright doctor` — check the toolchain.  `python -m reelwright relay ...` — run the OBS relay on this PC.
`python -m reelwright mcp` — the MCP server for Claude Desktop.
`python -m reelwright demo` — a seeded demo sandbox with the phone simulator (demo.py)."""
import os
import sys
import threading
import webbrowser

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "doctor":
        from .doctor import main
        sys.exit(main())
    if cmd == "mcp":  # Claude Desktop's MCP server (stdio), talking to the running app
        from .mcp_server import main
        sys.exit(main())
    if cmd == "demo":  # a seeded sandbox (demo.py): its own temporary data folder; nothing is ever posted
        from .demo import main
        sys.exit(main(sys.argv[2:]))
    if cmd == "relay":
        from .relay import main
        sys.exit(main(sys.argv[2:]))
    import uvicorn

    from .desktop import kill_children_on_exit
    kill_children_on_exit()  # ffmpeg & co. die with the server (Windows job object)
    from .config import DEFAULT_PORT
    PORT = int(os.environ.get("REELWRIGHT_PORT", DEFAULT_PORT))
    from . import server  # after the port is known: its host check and token cookie use it
    url = f"http://127.0.0.1:{PORT}/?rw_token={server.TOKEN}"  # the browser swaps it for a session cookie
    if not os.environ.get("REELWRIGHT_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    else:
        print(f"Reelwright: {url}", flush=True)
    uvicorn.run(server.app, host="127.0.0.1", port=PORT, log_level="warning")
