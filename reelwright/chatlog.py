"""Live Twitch chat capture. While Reelwright runs and Settings → Signals names a channel, it joins that channel's chat
anonymously (read-only "justinfan" login over TLS IRC: no account, never sends a chat message) and appends every
message as {"ts": Twitch's send time, "m": text} — no usernames — to DATA/chatlogs/<channel>/<UTC date>.jsonl.
At analysis, signals.enrich slices the log to a recording's own clock for chat counts and reaction kinds.
Past chat is never fetched: Twitch serves chat replay only through an unofficial API, so only chat captured live exists.
"""
import json
import random
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, log

HOST, PORT = "irc.chat.twitch.tv", 6697
DIR = config.DATA / "chatlogs"  # ponytail: kept until deleted (≈1 MB per busy stream-hour); add to the space saver if it grows
QUIET_S = 400  # Twitch PINGs about every 5 min: this long without any data means a dead connection
_state: dict = {"thread": None, "stop": None, "channel": "", "connected": False, "error": "", "count": 0}
LOGIN_RE = re.compile(r"[a-z0-9_]{3,25}")


def clean(channel) -> str:
    """A Twitch login, or "" — the name becomes a folder and an IRC JOIN, so nothing else is ever used."""
    ch = str(channel or "").strip().lower().lstrip("#")
    return ch if LOGIN_RE.fullmatch(ch) else ""


def parse_line(line: str) -> tuple[float | None, str] | None:
    """(Twitch's send time in epoch seconds, or None without tags; message text) for a chat message, else None."""
    tags = ""
    if line.startswith("@"):
        tags, _, line = line.partition(" ")
    parts = line.split(" ", 3)
    if len(parts) < 4 or parts[1] != "PRIVMSG":
        return None
    text = parts[3][1:] if parts[3].startswith(":") else parts[3]
    if text.startswith("\x01ACTION "):  # /me messages
        text = text[8:].rstrip("\x01")
    ts = next((int(v) / 1000 for k, _, v in (t.partition("=") for t in tags[1:].split(";")) if k == "tmi-sent-ts" and v.isdigit()), None)
    return ts, text


def _command(line: str) -> str:
    if line.startswith("@"):
        line = line.partition(" ")[2]
    parts = line.split(" ")
    return parts[1] if line.startswith(":") and len(parts) > 1 else parts[0]


def _connect():
    raw = socket.create_connection((HOST, PORT), timeout=30)
    return ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)


def _write(channel: str, ts: float, text: str, out: dict) -> None:
    day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    if out.get("day") != day:  # one file per UTC day, kept open while it's today
        if out.get("f"):
            out["f"].close()
        (DIR / channel).mkdir(parents=True, exist_ok=True)
        out["f"], out["day"] = open(DIR / channel / f"{day}.jsonl", "a", encoding="utf-8"), day
    out["f"].write(json.dumps({"ts": round(ts, 3), "m": text[:300]}, ensure_ascii=False) + "\n")
    out["f"].flush()
    _state["count"] += 1


def run(channel: str, stop: threading.Event, connect=_connect, clock=time.time) -> None:
    """Capture until stop is set, reconnecting with backoff (2 s doubling to 2 min) after any drop."""
    channel = clean(channel)
    if not channel:
        return
    backoff, out = 2.0, {}
    mine = lambda **kw: _state["stop"] is stop and _state.update(kw)  # noqa: E731 — a replaced thread leaves the new one's state alone
    try:
        while not stop.is_set():
            sock = None
            try:
                sock = connect()
                sock.settimeout(10)
                for cmd in ("PASS SCHMOOPIIE", f"NICK justinfan{random.randint(10000, 99999)}", "CAP REQ :twitch.tv/tags",
                            f"JOIN #{channel}"):
                    sock.sendall(f"{cmd}\r\n".encode())
                buf, heard = b"", clock()
                while not stop.is_set():
                    try:
                        data = sock.recv(65536)
                    except TimeoutError:
                        if clock() - heard > QUIET_S:
                            raise ConnectionError(f"no data from Twitch chat for {QUIET_S} s")
                        continue
                    if not data:
                        raise ConnectionError("Twitch chat closed the connection")
                    heard, backoff = clock(), 2.0
                    mine(connected=True, error="")
                    *lines, buf = (buf + data).split(b"\r\n")
                    for raw in lines:
                        line = raw.decode("utf-8", "replace")
                        cmd = _command(line)
                        if cmd == "PING":
                            sock.sendall(f"PONG{line[4:]}\r\n".encode())
                        elif cmd == "RECONNECT":
                            raise ConnectionError("Twitch chat asked to reconnect")
                        elif not stop.is_set() and (msg := parse_line(line)):
                            _write(channel, msg[0] or clock(), msg[1], out)
            except Exception as e:
                mine(connected=False, error=str(e)[:200])
                if not stop.is_set():
                    log.event("chatlog", f"#{channel}: {e}; reconnecting in {backoff:.0f} s")
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            stop.wait(backoff)
            backoff = min(backoff * 2, 120.0)
    finally:
        if out.get("f"):
            out["f"].close()
        mine(connected=False)


def messages(channel: str, start: float, end: float) -> list[tuple[float, str]]:
    """(seconds since start, text) for every captured message in [start, end] (epoch seconds), in time order."""
    channel, out = clean(channel), []
    if not channel:
        return out
    day, last = datetime.fromtimestamp(start, timezone.utc).date(), datetime.fromtimestamp(end, timezone.utc).date()
    while day <= last:
        try:
            with open(DIR / channel / f"{day:%Y-%m-%d}.jsonl", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        m = json.loads(line)
                        if start <= m["ts"] <= end:
                            out.append((round(m["ts"] - start, 3), str(m["m"])))
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            pass
        day += timedelta(days=1)
    return sorted(out)


def apply() -> None:
    """Start, switch or stop the capture to match setting "twitch_chat_channel" ("" = off)."""
    ch = clean(config.load().get("twitch_chat_channel"))  # also guards a value saved through the generic settings route
    t = _state["thread"]
    if t and t.is_alive() and _state["channel"] == ch:
        return
    if _state["stop"]:
        _state["stop"].set()
    _state.update(thread=None, stop=None, channel=ch, connected=False, error="", count=0)
    if ch:
        stop = threading.Event()
        t = threading.Thread(target=run, args=(ch, stop), daemon=True, name="chatlog")
        _state.update(thread=t, stop=stop)
        t.start()
        log.event("chatlog", f"recording live chat of #{ch}")


def status() -> dict:
    t = _state["thread"]
    return {"channel": _state["channel"], "running": bool(t and t.is_alive()), "connected": _state["connected"],
            "error": _state["error"], "count": _state["count"]}
