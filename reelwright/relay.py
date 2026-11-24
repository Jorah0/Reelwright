#!/usr/bin/env python3
"""Reelwright Relay — run this on a PC that records with OBS to send its clips to Reelwright on another PC.

Single file, Python 3.10+ standard library. OBS mode also needs `pip install websockets`.

  OBS mode (recommended): sends every replay-buffer save (and, with --recordings, finished recordings)
      python reelwright_relay.py --token TOKEN [--server http://192.168.1.20:8766] [--obs-password PASS]
      In OBS: Tools → WebSocket Server Settings → Enable (port 4455), copy the password.
  Folder mode (no OBS websocket): sends every new video that appears in a folder
      python reelwright_relay.py --token TOKEN --watch "D:/Videos/OBS"

Without --server it finds Reelwright on the local network by itself. The token is shown in Reelwright → OBS.
Reelwright imports this module too (OBS client, file-ready check), so it must stay dependency-free at import time.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import socket
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

DISCOVERY_PORT = 8766
DISCOVER_MAGIC = b"REELWRIGHT_DISCOVER_V1"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".ts", ".flv", ".wmv"}
EVENT_OUTPUTS = 1 << 6  # obs-websocket v5 subscription bit: record / replay-buffer / stream output events


# ---------- obs-websocket v5 ----------

class OBSError(RuntimeError):
    pass


def obs_auth(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 auth: base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class OBS:
    """Minimal synchronous obs-websocket v5 client: identify, request, and read events."""

    def __init__(self, url: str, password: str = "", events: bool = True, timeout: float = 8.0):
        self.url, self.password, self.events, self.timeout = url, password, events, timeout
        self.ws: Any = None
        self._pending: list[tuple[str, dict]] = []

    def connect(self) -> "OBS":
        try:
            from websockets.exceptions import ConnectionClosed
            from websockets.sync.client import connect
        except ImportError:
            raise OBSError("OBS mode needs the websockets package: pip install websockets") from None
        try:
            self.ws = connect(self.url, open_timeout=self.timeout, max_size=2 ** 24, ping_interval=20)
            hello = json.loads(self.ws.recv(timeout=self.timeout))
            if hello.get("op") != 0:
                raise OBSError("That port answered, but it isn't obs-websocket v5 (OBS 28 or newer).")
            ident: dict = {"rpcVersion": 1, "eventSubscriptions": EVENT_OUTPUTS if self.events else 0}
            auth = hello["d"].get("authentication")
            if auth:
                if not self.password:
                    raise OBSError("OBS requires a password (Tools → WebSocket Server Settings → Show Connect Info).")
                ident["authentication"] = obs_auth(self.password, auth["salt"], auth["challenge"])
            self.ws.send(json.dumps({"op": 1, "d": ident}))
            if json.loads(self.ws.recv(timeout=self.timeout)).get("op") != 2:
                raise OBSError("OBS did not accept the connection.")
        except ConnectionClosed as e:
            self.close()
            code = getattr(getattr(e, "rcvd", None), "code", None)
            raise OBSError("Wrong OBS websocket password." if code == 4009 else f"OBS closed the connection ({code}).") from None
        except ConnectionRefusedError:
            self.close()
            raise OBSError(f"Nothing is listening at {self.url}: is OBS running with Tools → WebSocket Server "
                           "Settings → Enable turned on (and the port right)?") from None
        except (OSError, TimeoutError) as e:
            self.close()
            raise OBSError(f"Can't reach OBS at {self.url} ({e}). Check the address, and that the OBS PC's firewall "
                           "allows the websocket port.") from None
        return self

    def request(self, kind: str, data: dict | None = None) -> dict:
        rid = uuid.uuid4().hex
        self.ws.send(json.dumps({"op": 6, "d": {"requestType": kind, "requestId": rid, **({"requestData": data} if data else {})}}))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            msg = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if msg.get("op") == 5:  # an event arrived first: keep it for next_event()
                self._pending.append((msg["d"]["eventType"], msg["d"].get("eventData") or {}))
            elif msg.get("op") == 7 and msg["d"].get("requestId") == rid:
                st = msg["d"]["requestStatus"]
                if not st.get("result"):
                    raise OBSError(f"OBS refused {kind}: {st.get('comment') or st.get('code')}")
                return msg["d"].get("responseData") or {}
        raise OBSError(f"OBS didn't answer {kind} in time.")

    def next_event(self, timeout: float = 1.0) -> tuple[str, dict] | None:
        if self._pending:
            return self._pending.pop(0)
        try:
            msg = json.loads(self.ws.recv(timeout=timeout))
        except TimeoutError:
            return None
        return (msg["d"]["eventType"], msg["d"].get("eventData") or {}) if msg.get("op") == 5 else None

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()


def saved_file(event: str, data: dict) -> tuple[str | None, str | None]:
    """(path, kind) for events that mean OBS finished writing a file, else (None, None)."""
    if event == "ReplayBufferSaved" and data.get("savedReplayPath"):
        return data["savedReplayPath"], "replay"
    if event == "RecordStateChanged" and data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED" and data.get("outputPath"):
        return data["outputPath"], "recording"
    return None, None


def wait_stable(path, checks: int = 3, interval: float = 1.0, timeout: float = 300) -> bool:
    """True once the file exists, is non-empty, its size stops changing and it can be opened for reading."""
    p, last, same, end = Path(path), -1, 0, time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            size = p.stat().st_size
            with open(p, "rb"):
                pass
        except OSError:
            size = -1
        same = same + 1 if size > 0 and size == last else 0
        if same >= checks:
            return True
        last = size
        time.sleep(interval)
    return False


# ---------- talking to Reelwright ----------

def discover(timeout: float = 3.0, port: int = DISCOVERY_PORT) -> str | None:
    """Find a Reelwright receiver on the LAN by UDP broadcast; returns its base URL."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        end = time.monotonic() + timeout
        while time.monotonic() < end:  # UDP is lossy (Wi-Fi, busy PCs): probe again every 0.5 s until someone answers
            for target in ("255.255.255.255", "127.0.0.1"):
                try:
                    s.sendto(DISCOVER_MAGIC, (target, port))
                except OSError:
                    pass
            s.settimeout(max(0.05, min(0.5, end - time.monotonic())))
            try:
                data, (ip, _) = s.recvfrom(2048)
                info = json.loads(data)
                if info.get("app") == "reelwright":
                    return f"http://{ip}:{int(info['port'])}"
            except (OSError, ValueError, KeyError):
                continue
    return None


def _mac(token: str, msg: str) -> str:
    return hmac.new(token.encode(), msg.encode(), hashlib.sha256).hexdigest()


def sign(token: str, method: str, path: str, ts: int | None = None, nonce: str | None = None) -> dict:
    """Auth headers that prove we know the token without sending it (the LAN is plain HTTP and discovery is
    spoofable). Each signature is time-boxed and single-use on the receiver."""
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or uuid.uuid4().hex
    return {"X-Reelwright-Time": str(ts), "X-Reelwright-Nonce": nonce,
            "X-Reelwright-Sig": _mac(token, f"{method}\n{path}\n{ts}\n{nonce}")}


def server_proof(token: str, nonce: str) -> str:
    return _mac(token, f"server\n{nonce}")


def _conn(server: str, timeout: float = 30):
    u = urllib.parse.urlparse(server if "://" in server else f"http://{server}")
    return http.client.HTTPConnection(u.hostname, u.port or DISCOVERY_PORT, timeout=timeout)


def ping(server: str, token: str) -> dict:
    """Mutual check: Reelwright verifies our signature, and must answer with a proof only the real one can make."""
    hdrs = sign(token, "GET", "/ping")
    c = _conn(server, 10)
    c.request("GET", "/ping", headers=hdrs)
    r = c.getresponse()
    body = r.read()
    if r.status != 200:
        raise RuntimeError("Reelwright rejected the token (or this PC's clock is more than 10 minutes off)."
                           if r.status == 401 else f"Reelwright answered {r.status}")
    info = json.loads(body)
    if not hmac.compare_digest(str(info.get("proof", "")), server_proof(token, hdrs["X-Reelwright-Nonce"])):
        raise RuntimeError(f"{server} isn't the Reelwright you paired with (it can't prove it knows the token). Nothing was sent.")
    return info


def upload(server: str, token: str, path, source: str, kind: str = "file", retries: int = 3, log=print) -> dict:
    """Stream one file to Reelwright's receiver (PUT /ingest). Retries transient network errors."""
    p = Path(path)
    if p.suffix.lower() not in VIDEO_EXT:
        raise PermissionError(f"Not a video file: {p.name}")
    try:  # pre-flight: a wrong token or a full disk fails cleanly here instead of as a connection abort mid-upload
        info = ping(server, token)
    except RuntimeError as e:
        raise PermissionError(str(e)) from None
    if "max_bytes" in info and p.stat().st_size > info["max_bytes"]:
        raise PermissionError(f"Not enough disk space on the Reelwright PC for {p.name} "
                              f"({p.stat().st_size / 1e9:.1f} GB, room for {info['max_bytes'] / 1e9:.1f} GB)")
    for attempt in range(1, retries + 1):
        try:
            size = p.stat().st_size
            c = _conn(server, timeout=120)
            c.putrequest("PUT", "/ingest")
            for k, v in {**sign(token, "PUT", "/ingest"), "X-Filename": urllib.parse.quote(p.name), "X-Source": urllib.parse.quote(source),
                         "X-Kind": kind, "Content-Type": "application/octet-stream", "Content-Length": str(size)}.items():
                c.putheader(k, v)
            c.endheaders()
            try:
                with open(p, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        c.send(chunk)
            except OSError:  # the server may have answered early (rejected) and closed: read that answer if we can
                try:
                    r = c.getresponse()
                except OSError:
                    raise
                raise PermissionError(json.loads(r.read() or b"{}").get("detail") or f"HTTP {r.status}") from None
            r = c.getresponse()
            body = r.read()
            if r.status in (400, 401, 413, 507):  # permanent: don't retry
                raise PermissionError(json.loads(body or b"{}").get("detail") or f"HTTP {r.status}")
            if r.status != 200:
                raise OSError(f"HTTP {r.status}")
            return json.loads(body)
        except PermissionError:
            raise
        except OSError as e:
            if attempt == retries:
                raise
            log(f"  upload failed ({e}); retrying in {5 * attempt}s")
            time.sleep(5 * attempt)
    return {}


# ---------- the relay loops ----------

def run_obs(args, log=print):
    url = args.obs
    backoff = 2
    while True:
        try:
            with OBS(url, args.obs_password) as obs:
                v = obs.request("GetVersion")
                log(f"Connected to OBS {v.get('obsVersion')} at {url}. Waiting for replay saves"
                    f"{' and recordings' if args.recordings else ''}… (Ctrl+C to stop)")
                backoff = 2
                while True:
                    ev = obs.next_event(timeout=5)
                    if not ev:
                        continue
                    path, kind = saved_file(*ev)
                    if path and (kind == "replay" or args.recordings):
                        send(args, path, kind, log)
        except KeyboardInterrupt:
            return
        except Exception as e:  # OBS closed / restarted: keep trying
            log(f"OBS: {e} — retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def run_watch(args, log=print):
    folder = Path(args.watch)
    if not folder.is_dir():
        raise SystemExit(f"Folder not found: {folder}")
    seen = {p for p in folder.rglob("*") if p.suffix.lower() in VIDEO_EXT}  # existing files are not sent
    sizes: dict[Path, tuple[int, int]] = {}
    log(f"Watching {folder} for new videos ({len(seen)} existing ignored)… (Ctrl+C to stop)")
    try:
        while True:
            for p in folder.rglob("*"):
                if p in seen or p.suffix.lower() not in VIDEO_EXT:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                prev, stable = sizes.get(p, (-1, 0))
                stable = stable + 1 if size == prev and size > 0 else 0
                sizes[p] = (size, stable)
                if stable >= 3:
                    seen.add(p)
                    sizes.pop(p, None)
                    send(args, p, "file", log)
            time.sleep(2)
    except KeyboardInterrupt:
        return


def send(args, path, kind, log=print):
    p = Path(path)
    if p.suffix.lower() not in VIDEO_EXT:
        return log(f"Skipped {p.name} (not a video)")
    if not wait_stable(p):
        return log(f"Skipped {p.name}: it never finished writing")
    log(f"Sending {p.name} ({p.stat().st_size / 1e6:.1f} MB, {kind})…")
    try:
        res = upload(args.server, args.token, p, args.name, kind, log=log)
        log(f"  ✓ received by Reelwright as project {res.get('project_id')}")
    except Exception as e:
        log(f"  ✗ {e}")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252: never crash on a ✓
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Send OBS clips from this PC to Reelwright on your network.")
    ap.add_argument("--token", default=os.environ.get("REELWRIGHT_TOKEN"), help="pairing token from Reelwright → OBS")
    ap.add_argument("--server", help="Reelwright receiver, e.g. http://192.168.1.20:8766 (found automatically if omitted)")
    ap.add_argument("--name", default=socket.gethostname(), help="how this PC is labelled in Reelwright")
    ap.add_argument("--obs", default="ws://127.0.0.1:4455", help="OBS websocket URL on this PC")
    ap.add_argument("--obs-password", default=os.environ.get("OBS_PASSWORD", ""))
    ap.add_argument("--recordings", action="store_true", help="also send full recordings when they stop (can be large)")
    ap.add_argument("--watch", help="folder mode: send new videos from this folder instead of listening to OBS")
    ap.add_argument("--send", nargs="+", help="send these files once and exit")
    args = ap.parse_args(argv)
    if not args.token:
        ap.error("--token is required (Reelwright → OBS → Network receiver)")
    if not args.server:
        print("Looking for Reelwright on the network…")
        args.server = discover()
        if not args.server:
            raise SystemExit("Couldn't find Reelwright. Turn on 'Receive clips from other PCs' there, allow it through the "
                             "firewall, or pass --server http://THAT-PC-IP:8766")
        print(f"Found {args.server} (it must prove it knows the token before anything is sent).")
    try:
        info = ping(args.server, args.token)
    except (RuntimeError, OSError) as e:
        raise SystemExit(str(e))
    print(f"Paired with Reelwright on {info.get('name')} ({args.server}).")
    if args.send:
        for f in args.send:
            send(args, f, "file")
    elif args.watch:
        run_watch(args)
    else:
        run_obs(args)


if __name__ == "__main__":
    sys.exit(main())
