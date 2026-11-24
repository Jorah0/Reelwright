"""Direct OBS connections: Reelwright talks to OBS's built-in websocket (this PC or any PC on the network).

Each connection is watched by its own thread that reconnects on its own. When OBS saves a replay (or, optionally,
stops a recording) the file is imported. OBS reports the path on *its* disk, so for another PC the user maps that
folder to one this PC can read (a shared drive, e.g. D:/Clips → //GAMING-PC/Clips). Without a shared folder,
use the Relay instead. Connections also drive OBS remotely: save replay ("clip that"), replay buffer, recording.
"""
import socket
import threading
import time

from . import config, log, pipeline, store
from .relay import OBS, OBSError, saved_file

ACTIONS = {"save_replay": "SaveReplayBuffer", "start_replay": "StartReplayBuffer", "stop_replay": "StopReplayBuffer",
           "start_record": "StartRecord", "stop_record": "StopRecord"}
_threads: dict[str, threading.Thread] = {}
_started = False


def url(conn: dict) -> str:
    host = conn["host"].strip()
    return host if host.startswith(("ws://", "wss://")) else f"ws://{host}:{int(conn.get('port') or 4455)}"


def is_local(host: str) -> bool:
    h = host.split("://")[-1].split(":")[0].strip("[]").lower()
    if h in ("127.0.0.1", "localhost", "::1", socket.gethostname().lower()):
        return True
    try:
        return h in {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        return False


def map_path(path: str, rules: list, host: str) -> str | None:
    """Translate a path on the OBS PC to one this PC can open (longest matching rule wins); None if unreachable."""
    norm = path.replace("\\", "/")
    if ".." in norm.split("/"):
        return None  # a reported path must not climb out of the mapped folder
    for src, dst in sorted(rules or [], key=lambda r: -len(r[0])):
        s = src.replace("\\", "/").rstrip("/")
        if s and (norm.lower() == s.lower() or norm.lower().startswith(s.lower() + "/")):
            return dst.replace("\\", "/").rstrip("/") + norm[len(s):]
    return path if is_local(host) else None


def public(conn: dict) -> dict:
    """Safe for the browser: never send the OBS password back."""
    return {**{k: v for k, v in conn.items() if k != "password"}, "has_password": bool(conn.get("password"))}


def connect(conn: dict, events: bool = False) -> OBS:
    return OBS(url(conn), conn.get("password", ""), events=events).connect()


def probe(conn: dict) -> dict:
    with OBS(url(conn), conn.get("password", ""), events=False) as o:
        v = o.request("GetVersion")
        out = {"obs_version": v.get("obsVersion"), "websocket_version": v.get("obsWebSocketVersion"),
               "platform": v.get("platformDescription")}
        for key, req in (("replay_buffer", "GetReplayBufferStatus"), ("recording", "GetRecordStatus")):
            try:
                out[key] = bool(o.request(req).get("outputActive"))
            except OBSError:
                out[key] = None  # e.g. the replay buffer is disabled in OBS settings
        try:
            out["record_dir"] = o.request("GetRecordDirectory").get("recordDirectory")
        except OBSError:
            pass
    return out


def action(conn: dict, what: str) -> dict:
    if what not in ACTIONS:
        raise RuntimeError(f"Unknown OBS action {what}")
    with OBS(url(conn), conn.get("password", ""), events=False) as o:
        o.request(ACTIONS[what])
    return probe(conn)


def handle_file(conn: dict, path: str, kind: str) -> dict | None:
    local = map_path(path, conn.get("path_map"), conn["host"])
    if not local:
        store.update("obs", conn["id"], {"error": f"OBS saved {path} on its PC, but this PC can't read that folder. Add a folder "
                                                  "mapping to a shared drive, or run the Relay on that PC."})
        return None
    ap = conn.get("autopilot") or {}
    name = local.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    p = pipeline.ingest_file(local, name, {"obs": conn["name"], "kind": kind, "via": "direct", "path": path},
                             ap if ap.get("enabled") else None)
    store.update("obs", conn["id"], fn=lambda c: c.update(last_file=local, last_at=pipeline.now_iso(), error="",
                                                          imported=c.get("imported", 0) + 1))
    return p


def _watch(cid: str):
    backoff = 2
    while True:
        conn = store.get("obs", cid)
        if not conn or not conn.get("enabled"):
            break
        rev = conn.get("rev", 0)
        try:
            with OBS(url(conn), conn.get("password", ""), events=True) as o:
                store.update("obs", cid, {"status": "connected", "error": "", "connected_at": pipeline.now_iso()})
                backoff = 2
                while True:
                    cur = store.get("obs", cid)
                    if not cur or not cur.get("enabled") or cur.get("rev", 0) != rev:
                        break  # removed, disabled or edited: reconnect with the new settings
                    ev = o.next_event(timeout=2)
                    if ev:
                        path, kind = saved_file(*ev)
                        if path and (kind == "replay" and cur.get("import_replays", True)
                                     or kind == "recording" and cur.get("import_recordings")):
                            handle_file(cur, path, kind)
        except Exception as e:
            if not isinstance(e, OBSError):
                log.error("obs", f"watcher {cid}")
            elif store.get("obs", cid).get("status") != "offline":
                log.event("obs", f"{conn.get('name')}: {e}")
            store.update("obs", cid, {"status": "offline", "error": str(e)[:300]})
            for _ in range(backoff):  # wait, but notice quickly if the user disables or edits it
                time.sleep(1)
                cur = store.get("obs", cid)
                if not cur or not cur.get("enabled") or cur.get("rev", 0) != rev:
                    break
            backoff = min(backoff * 2, 60)
    if store.get("obs", cid):
        store.update("obs", cid, {"status": "off"})
    _threads.pop(cid, None)


def reconcile():
    for conn in store.find("obs"):
        t = _threads.get(conn["id"])
        if conn.get("enabled") and not (t and t.is_alive()):
            _threads[conn["id"]] = threading.Thread(target=_watch, args=(conn["id"],), daemon=True)
            _threads[conn["id"]].start()


def start():
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            try:
                reconcile()
            except Exception:
                log.error("obs", "reconcile failed")
            time.sleep(5)

    threading.Thread(target=loop, daemon=True).start()


def lan_status() -> dict:  # convenience for the UI: both ways of receiving OBS clips in one call
    from . import lan
    return {**lan.status(), "connections": [public(c) for c in store.find("obs")], "hostname": socket.gethostname(),
            "obs_autopilot": config.load()["obs_autopilot"]}
