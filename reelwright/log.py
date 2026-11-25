"""Event log: one rotating file of what happened (jobs, OBS, AI errors) for debugging, viewable in Activity.

Separate from reelwright.log (the desktop app's raw stdout capture), so rotation never fights an open handle.
"""
import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

_log = logging.getLogger("reelwright")
_path: Path | None = None
_setup_lock = threading.Lock()


def setup(path=None, max_bytes: int = 2 * 1024 * 1024, backups: int = 3) -> None:
    with _setup_lock:  # worker threads may log first at the same moment
        _setup(path, max_bytes, backups)


def _setup(path, max_bytes: int, backups: int) -> None:
    global _path
    if path is None:
        from .config import DATA
        path = DATA / "events.log"
    path = Path(path)
    for h in list(_log.handlers):
        _log.removeHandler(h)
        h.close()
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False
    _path = path


def _ready():
    if _path is None:
        with _setup_lock:
            if _path is None:
                _setup(None, 2 * 1024 * 1024, 3)


def event(kind: str, msg: str) -> None:
    _ready()
    _log.info("%s: %s", kind, msg)


def error(kind: str, msg: str, exc: bool = True) -> None:
    """Log an error; with exc=True (inside an except block) the traceback is included."""
    _ready()
    _log.error("%s: %s", kind, msg, exc_info=exc)


class RotatingStream:
    """sys.stdout/stderr of the windowed .exe (reelwright.log): a text stream that rolls over at max_bytes and keeps
    `backups` old files, so yt-dlp progress and tracebacks can't grow it forever. A file already over the cap from an
    earlier run rolls over on the first write."""

    def __init__(self, path, max_bytes: int = 2 * 1024 * 1024, backups: int = 3):
        self._h = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")

    def write(self, s: str) -> int:
        h = self._h
        h.acquire()
        try:
            pos = h.stream.tell()
            if pos and pos + len(s.encode("utf-8", "replace")) > h.maxBytes:
                h.doRollover()
            h.stream.write(s)
            h.stream.flush()
        finally:
            h.release()
        return len(s)

    def flush(self) -> None:
        self._h.flush()

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        self._h.close()


def quiet_connection_resets(loop) -> None:
    """On Windows the Proactor loop logs a full traceback whenever a browser drops a keep-alive connection
    (ConnectionResetError, WinError 10054). Harmless: drop exactly that one; everything else goes to the default handler."""
    def _ignore_reset(lp, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
            return
        lp.default_exception_handler(context)
    loop.set_exception_handler(_ignore_reset)


def tail(n: int = 200) -> list[str]:
    if _path is None or not _path.exists():
        return []
    handlers = list(_log.handlers)
    for h in handlers:  # hold the handler lock: on Windows a rotation can't rename a file we have open
        h.acquire()
    try:
        for h in handlers:
            h.flush()
        last: deque[str] = deque(maxlen=n)
        for p in (_path.with_name(_path.name + ".1"), _path):  # just after a rotation the live file is nearly empty
            if p.exists():
                with open(p, encoding="utf-8", errors="replace") as f:
                    last.extend(ln.rstrip("\n") for ln in f)
        return list(last)
    finally:
        for h in handlers:
            h.release()
