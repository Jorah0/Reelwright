"""Direct OBS connections: Reelwright talks to OBS's built-in websocket (this PC or any PC on the network).

Each connection is watched by its own thread that reconnects on its own. When OBS saves a replay (or, optionally,
stops a recording) the file is imported. OBS reports the path on *its* disk, so for another PC the user maps that
folder to one this PC can read (a shared drive, e.g. D:/Clips → //GAMING-PC/Clips). Without a shared folder,
use the Relay instead. Connections also drive OBS remotely: save replay ("clip that"), replay buffer, recording.
While OBS records, the watcher follows the recording's wall-clock start and pauses, so marks (the Mark button, the
global hotkey, the phone remote, game / VTube Studio telemetry) become seconds in the recorded file (markers.json).
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
    """Safe for the browser: never send the OBS password back (and a recording's marks only as a count)."""
    rec = conn.get("rec")
    return {**{k: v for k, v in conn.items() if k not in ("password", "rec")}, "has_password": bool(conn.get("password")),
            "recording": {"since": rec["start"], "marks": len(rec["marks"])} if rec else None}


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
    if what == "mark":  # Reelwright keeps the recording's clock itself: no request to OBS needed
        return {"ok": True, "marks": mark(source="ui", cid=conn["id"])}
    if what not in ACTIONS:
        raise RuntimeError(f"Unknown OBS action {what}")
    with OBS(url(conn), conn.get("password", ""), events=False) as o:
        o.request(ACTIONS[what])
    return probe(conn)


# ---------- marks: moments flagged while OBS records ----------
# The recording's wall-clock start, pauses and marks live on its connection doc ("rec") until it stops; then the marks
# become seconds in the recorded file and go to the import as markers.json: [{t, kind, label, strength, d?}].

MARK_LABEL = {"ui": "Marked in Reelwright", "hotkey": "Hotkey mark", "remote": "Marked from phone"}
MAX_MARKS = 5000  # per recording: a glitching integration can't grow the doc without bound


def file_seconds(rec: dict, at: float) -> float:
    """Seconds into the recorded file at wall-clock `at`: time since the start, minus the paused stretches before it."""
    paused = sum(max(0.0, min(at, at if b is None else b) - a) for a, b in rec.get("pauses") or [])
    return at - rec["start"] - paused


def rec_markers(rec: dict) -> list[dict]:
    """The recording's marks in file seconds, time-sorted; marks from before it started are dropped."""
    out = []
    for m in rec.get("marks") or []:
        t = round(file_seconds(rec, m["at"]), 2)
        if t >= 0:
            out.append({"t": t, **{k: m[k] for k in ("kind", "label", "strength", "d") if k in m}})
    return sorted(out, key=lambda m: m["t"])


def recording() -> list[dict]:
    """Connections whose OBS is recording right now: connected, and their watcher saw it recording (a stale "rec" left by
    a crash or an offline OBS doesn't count; the watcher re-checks it with OBS when it reconnects)."""
    return [c for c in store.find("obs") if c.get("enabled") and c.get("rec") and c.get("status") == "connected"]


def add_marker(kind: str, label: str, strength: float = 1.0, d: float | None = None, at: float | None = None,
               cid: str | None = None) -> list[dict]:
    """Mark wall-clock `at` (default now) on every recording connection, or only `cid`: [{connection, t, imports}].
    RuntimeError when nothing is recording (a mark needs a recording to belong to)."""
    at = time.time() if at is None else at
    m = {"at": at, "kind": kind, "label": str(label)[:80], "strength": round(min(max(float(strength), 0.0), 1.0), 2),
         **({"d": round(float(d), 2)} if d else {})}

    def add(doc):
        rec = doc.get("rec")
        if rec and file_seconds(rec, at) >= 0 and len(rec["marks"]) < MAX_MARKS:  # an event from before it started: not in the file
            rec["marks"].append(m)
            added.append(round(file_seconds(rec, at), 1))

    live, out = recording(), []
    if cid:
        live = [c for c in live if c["id"] == cid]
    if not live:
        raise RuntimeError("Nothing is recording in OBS right now (or its connection is off): start recording first.")
    for c in live:
        added: list[float] = []
        store.update("obs", c["id"], fn=add)
        out += [{"connection": c["name"], "t": t, "imports": bool(c.get("import_recordings"))} for t in added]
    return out


def mark(label: str = "", source: str = "ui", cid: str | None = None, at: float | None = None) -> list[dict]:
    """The creator's "this moment!": from the OBS page (ui), the global hotkey, or the phone remote (source="remote")."""
    return add_marker("obs", label.strip() or MARK_LABEL.get(source, "Mark"), 1.0, at=at, cid=cid)


def _record_event(cid: str, event: str, data: dict, now: float | None = None) -> list[dict] | None:
    """Follow the recording clock from OBS's RecordStateChanged events; on stop, its marks in file seconds."""
    if event != "RecordStateChanged":
        return None
    now = time.time() if now is None else now
    st, got = data.get("outputState"), {}

    def apply(c):
        rec = c.get("rec")
        if st == "OBS_WEBSOCKET_OUTPUT_STARTED":
            c["rec"] = {"start": now, "pauses": [], "marks": []}
        elif rec and st == "OBS_WEBSOCKET_OUTPUT_PAUSED":
            rec["pauses"].append([now, None])
        elif rec and st == "OBS_WEBSOCKET_OUTPUT_RESUMED" and rec["pauses"] and rec["pauses"][-1][1] is None:
            rec["pauses"][-1][1] = now
        elif st == "OBS_WEBSOCKET_OUTPUT_STOPPED":
            got["rec"], c["rec"] = rec, None

    store.update("obs", cid, fn=apply)
    if st != "OBS_WEBSOCKET_OUTPUT_STOPPED":
        return None
    rec = got.get("rec")
    if rec and rec["pauses"] and rec["pauses"][-1][1] is None:
        rec["pauses"][-1][1] = now
    return rec_markers(rec) if rec else []


def _sync_recording(cid: str, o, now: float | None = None) -> None:
    """After (re)connecting: is OBS already recording? One that started before Reelwright saw it gets its start from
    OBS's own duration (which leaves out pauses); the recording Reelwright was already following keeps its marks."""
    now = time.time() if now is None else now
    try:
        st = o.request("GetRecordStatus")
    except OBSError:
        return
    dropped = []

    def sync(c):
        rec = c.get("rec")
        if not st.get("outputActive"):
            if rec and rec["marks"]:
                dropped.append(len(rec["marks"]))
            c["rec"] = None
            return
        dur = float(st.get("outputDuration") or 0) / 1000
        if rec and abs(file_seconds(rec, now) - dur) < 10:
            return
        c["rec"] = {"start": now - dur, "pauses": [[now, None]] if st.get("outputPaused") else [], "marks": []}

    doc = store.update("obs", cid, fn=sync)
    if dropped:
        log.event("obs", f"{(doc or {}).get('name')}: the recording ended while Reelwright was disconnected; {dropped[0]} mark(s) dropped")


def handle_file(conn: dict, path: str, kind: str, markers: list | None = None) -> dict | None:
    from . import storage
    if why := storage.hold_reason():  # storage drive unplugged / folder moving: say so (the file stays where OBS saved it)
        store.update("obs", conn["id"], {"error": f"OBS saved {path}, but it wasn't imported: {why}"})
        return None
    local = map_path(path, conn.get("path_map"), conn["host"])
    if not local:
        store.update("obs", conn["id"], {"error": f"OBS saved {path} on its PC, but this PC can't read that folder. Add a folder "
                                                  "mapping to a shared drive, or run the Relay on that PC."})
        return None
    ap = conn.get("autopilot") or {}
    name = local.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    p = pipeline.ingest_file(local, name, {"obs": conn["name"], "kind": kind, "via": "direct", "path": path},
                             ap if ap.get("enabled") else None, markers)
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
                _sync_recording(cid, o)
                backoff = 2
                while True:
                    cur = store.get("obs", cid)
                    if not cur or not cur.get("enabled") or cur.get("rev", 0) != rev:
                        break  # removed, disabled or edited: reconnect with the new settings
                    ev = o.next_event(timeout=2)
                    if ev:
                        marks = _record_event(cid, *ev)  # the recording clock; on stop, its marks in file seconds
                        path, kind = saved_file(*ev)
                        if path and (kind == "replay" and cur.get("import_replays", True)
                                     or kind == "recording" and cur.get("import_recordings")):
                            handle_file(cur, path, kind, marks)
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
    from . import telemetry  # game / VTube Studio telemetry and the mark hotkey: they run only while OBS records
    telemetry.start()


def lan_status() -> dict:  # convenience for the UI: both ways of receiving OBS clips in one call
    from . import lan
    return {**lan.status(), "connections": [public(c) for c in store.find("obs")], "hostname": socket.gethostname(),
            "obs_autopilot": config.load()["obs_autopilot"]}
