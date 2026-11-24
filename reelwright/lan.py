"""Optional network receiver for OBS clips from other PCs (Reelwright Relay pushes files here).

Security model: the main app stays bound to 127.0.0.1 (it holds API keys and social tokens). This is a separate
listener that only does two things — GET /ping and PUT /ingest — and both require the pairing token. Nothing else
of the app is reachable from the network. A UDP responder on the same port number answers discovery broadcasts
so relays can find this PC; it reveals only that Reelwright is here and on which port.
"""
import hmac
import json
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, log, media, pipeline, store
from .relay import DISCOVER_MAGIC, server_proof, sign

MAX_BYTES = 64 * 1024 ** 3  # 64 GB: a long recording, not an attack on the disk
DISK_RESERVE = 5 * 1024 ** 3  # never let a received file eat the last 5 GB (projects + database live there)
CLOCK_SKEW_S = 600            # signed requests are valid for ±10 min (PCs on one LAN are rarely further apart)
_state: dict = {"http": None, "port": None, "stop": None, "error": ""}
_lock = threading.Lock()
_uploads = threading.BoundedSemaphore(3)  # parallel uploads; more get 503 and the relay retries
_nonces: dict[str, float] = {}
_nonce_lock = threading.Lock()


def _gen_token() -> str:
    # never starts with "-" or "_": a leading dash breaks `--token VALUE` on the relay's command line
    return "rw" + secrets.token_urlsafe(18)


def token() -> str:
    s = config.load()
    if not s["lan_token"]:
        s = config.save({"lan_token": _gen_token()})
    return s["lan_token"]


def new_token() -> str:
    config.save({"lan_token": _gen_token()})
    return token()


def verify(headers, method: str, path: str) -> str | None:
    """The request's nonce if it carries a fresh, unused, valid signature for this method+path; else None."""
    want = config.load()["lan_token"]
    nonce, sig = headers.get("X-Reelwright-Nonce", ""), headers.get("X-Reelwright-Sig", "")
    try:
        ts = int(headers.get("X-Reelwright-Time", ""))
    except ValueError:
        return None
    now = time.time()
    if not want or not nonce or len(nonce) > 64 or abs(now - ts) > CLOCK_SKEW_S:
        return None
    if not hmac.compare_digest(sig.encode(), sign(want, method, path, ts, nonce)["X-Reelwright-Sig"].encode()):
        return None
    with _nonce_lock:  # single use: a captured request can't be replayed
        for n, t in list(_nonces.items()):
            if now - t > 2 * CLOCK_SKEW_S:
                del _nonces[n]
        if nonce in _nonces:
            return None
        _nonces[nonce] = now
    return nonce


def local_ips() -> list[str]:
    """This PC's IPv4 addresses, the real LAN one first (virtual WSL/Hyper-V/VirtualBox adapters come after)."""
    primary = None
    try:  # the address used for outbound traffic is the one other PCs can reach (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            primary = s.getsockname()[0]
    except OSError:
        pass
    ips: set[str] = set()
    try:
        ips.update(str(i[4][0]) for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    rest = sorted(ip for ip in ips if not ip.startswith("127.") and ip != primary)
    return ([primary] if primary and not primary.startswith("127.") else []) + rest


def safe_name(raw: str) -> str:
    name = urllib.parse.unquote(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[^\w .()-]", "_", name).strip(" .")[:120] or "clip.mp4"


class Handler(BaseHTTPRequestHandler):
    server_version = "Reelwright"
    protocol_version = "HTTP/1.1"
    timeout = 30  # a peer that connects and goes quiet (or stalls mid-upload) is dropped, not kept forever

    def log_message(self, *a):  # quiet: the GUI app has no console
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/ping":
            return self._send(404, {"detail": "Not found"})
        nonce = verify(self.headers, "GET", "/ping")
        if not nonce:
            return self._send(401, {"detail": "Wrong token, reused request, or clocks too far apart"})
        room = max(0, min(MAX_BYTES, shutil.disk_usage(pipeline.PROJECTS).free - DISK_RESERVE))
        self._send(200, {"ok": True, "app": "reelwright", "name": socket.gethostname(), "max_bytes": room,
                         "proof": server_proof(config.load()["lan_token"], nonce)})

    def do_PUT(self):
        if self.path != "/ingest":
            return self._send(404, {"detail": "Not found"})
        if not verify(self.headers, "PUT", "/ingest"):
            return self._send(401, {"detail": "Wrong token, reused request, or clocks too far apart"})
        if not _uploads.acquire(blocking=False):
            return self._send(503, {"detail": "Busy receiving other clips; try again shortly"})
        try:
            self._ingest()
        finally:
            _uploads.release()

    def _ingest(self):
        name = safe_name(self.headers.get("X-Filename", ""))
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in media.VIDEO_EXT:
            return self._send(400, {"detail": f"Not a video file: {name}"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(411, {"detail": "Content-Length required"})
        if not 0 < length <= MAX_BYTES:
            return self._send(413, {"detail": "File is empty or too large"})
        if shutil.disk_usage(pipeline.PROJECTS).free < length + DISK_RESERVE:
            return self._send(507, {"detail": "Not enough disk space on the Reelwright PC for this clip"})
        source = urllib.parse.unquote(self.headers.get("X-Source", ""))[:60] or self.client_address[0]
        kind = self.headers.get("X-Kind", "file") if self.headers.get("X-Kind") in ("replay", "recording", "file") else "file"
        ap = config.load()["obs_autopilot"]
        # whoever holds the token can send clips; they must never be able to post publicly to the creator's channels,
        # so Autopilot on received clips always schedules them private for the creator to approve
        ap = {**ap, "privacy": "private"} if ap.get("enabled") else None
        p = pipeline.new_project(name.rsplit(".", 1)[0], ap,
                                 origin={"obs": source, "kind": kind, "via": "relay", "ip": self.client_address[0]})
        dst, left = pipeline.pdir(p["id"]) / f"source{ext}", length
        try:
            with open(dst, "wb") as f:
                while left:
                    chunk = self.rfile.read(min(left, 1024 * 1024))
                    if not chunk:
                        raise ConnectionError("upload cut off")
                    f.write(chunk)
                    left -= len(chunk)
        except (OSError, ConnectionError) as e:
            pipeline.delete_project(p["id"])  # never leave a half file that would analyze as garbage
            return self._send(400, {"detail": f"Upload incomplete: {e}"})
        store.update("project", p["id"], {"source": dst.name, "status": "queued", "stage": "Queued for analysis"})
        log.event("relay", f"received {name} ({length / 1e6:.1f} MB, {kind}) from {source} @ {self.client_address[0]}")
        pipeline.submit("analyze", project_id=p["id"])
        self._send(200, {"ok": True, "project_id": p["id"]})


def _udp_loop(port: int, stop: threading.Event):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:  # no SO_REUSEADDR: nobody else may answer for us
        try:
            s.bind(("", port))
        except OSError:
            return  # discovery is a convenience; --server still works
        s.settimeout(1.0)
        reply = json.dumps({"app": "reelwright", "port": port}).encode()  # no hostname: say as little as possible
        last: dict[str, float] = {}
        while not stop.is_set():
            try:
                data, addr = s.recvfrom(256)
            except (TimeoutError, OSError):
                continue
            now = time.time()
            if data.strip() != DISCOVER_MAGIC or now - last.get(addr[0], 0) < 1.0:
                continue  # one answer per second per address: not useful as a traffic amplifier
            last[addr[0]] = now
            if len(last) > 1024:
                last.clear()
            try:
                s.sendto(reply, addr)
            except OSError:
                pass


def apply() -> dict:
    """Start, stop or restart the receiver to match settings. Safe to call any time."""
    s = config.load()
    with _lock:
        want = int(s["lan_port"]) if s["lan_receive"] else None
        if _state["http"] and _state["port"] != want:
            _state["stop"].set()
            _state["http"].shutdown()
            _state["http"].server_close()
            _state.update(http=None, port=None, stop=None)
        if want and not _state["http"]:
            token()
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", want), Handler)
            except OSError as e:
                _state["error"] = f"Port {want} is in use or blocked: {e}"
                return status()
            srv.daemon_threads = True
            stop = threading.Event()
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            threading.Thread(target=_udp_loop, args=(want, stop), daemon=True).start()
            _state.update(http=srv, port=want, stop=stop, error="")
        if not want:
            _state["error"] = ""
    return status()


def status() -> dict:
    s = config.load()
    return {"enabled": s["lan_receive"], "running": bool(_state["http"]), "port": s["lan_port"], "token": s["lan_token"],
            "ips": local_ips(), "error": _state["error"], "autopilot": s["obs_autopilot"]}
