"""Live telemetry while OBS records: game events and VTube Studio reactions become marks on the recording
(obs.add_marker), so analysis knows when the kill, the death, the round win or the shocked expression happened.

Official local interfaces on this PC only, all off by default; nothing reads game memory or draws over a game:
- League of Legends: Riot's Live Client Data API, polled once a second while a game runs;
- CS2 / Dota 2: Valve's Game State Integration: the game posts its state to a listener on 127.0.0.1, with a token;
- VTube Studio: its public plugin API (the creator clicks Allow in VTS once): hotkeys, item drops, tracking loss.
Plus the global "mark this" hotkey (Windows RegisterHotKey). Each runs only while it is enabled AND an OBS connection is
recording: the recording's start time is the clock that places every event in the file.
"""
import hmac
import http.client
import json
import secrets
import ssl
import string
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, log, obs

_st = {"lol": {"active": False, "events": 0}, "gsi": {"error": "", "events": 0}, "vts": {"connected": False, "error": "", "events": 0},
       "hotkey": {"spec": "", "error": ""}}
_started = False


def _emit(kind: str, label: str, strength: float, at: float | None = None) -> bool:
    try:
        return bool(obs.add_marker(kind, label, strength, at=at))
    except RuntimeError:  # the recording stopped a moment ago
        return False


# ---------- League of Legends: Live Client Data ----------

LOL_BASE = "https://127.0.0.1:2999"  # the game's own endpoint on this PC; never any other host
_LOL_TLS = ssl.create_default_context()
_LOL_TLS.check_hostname = False
_LOL_TLS.verify_mode = ssl.CERT_NONE  # the game serves a self-signed certificate on loopback: this context is used for LOL_BASE only
# ponytail: unverified TLS to a fixed loopback address that receives nothing secret; pin Riot's root certificate if it ever matters
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # the unverified context must never follow the game's port anywhere else
        return None


_LOL = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_LOL_TLS), _NoRedirect())
MULTI = {2: "Double kill", 3: "Triple kill", 4: "Quadra kill", 5: "Penta kill"}
OBJECTIVES = {"BaronKill": "Baron", "DragonKill": "Dragon", "HeraldKill": "Rift Herald", "HordeKill": "Voidgrubs"}


def _lol_get(path: str):
    with _LOL.open(LOL_BASE + path, timeout=1.5) as r:
        return json.loads(r.read(4_000_000))


def _who(name) -> str:
    return str(name or "").split("#")[0].strip().lower()  # events name the player without the Riot ID's #tag


def lol_markers(events: list, me: str, game_time: float, now: float, state: dict) -> list[tuple]:
    """New Live Client events -> [(kind, label, strength, wall time)]. Event times are game seconds, placed on the wall
    clock through the current game time; events already seen (state["seen"] = last EventID) are skipped."""
    out, seen = [], state.get("seen", -1)
    for e in events:
        eid, t, name = e.get("EventID"), e.get("EventTime"), e.get("EventName")
        if not isinstance(eid, int) or eid <= state.get("seen", -1) or not isinstance(t, (int, float)):
            continue
        seen = max(seen, eid)
        killer, victim, hit = _who(e.get("KillerName")), _who(e.get("VictimName")), None
        if name == "ChampionKill":
            hit = ("game:kill", "Kill", 0.6) if killer == me else ("game:death", "Death", 0.5) if victim == me else None
        elif name == "Multikill" and killer == me:
            n = int(e.get("KillStreak") or 2)
            hit = ("game:multikill", MULTI.get(n, f"{n} kills"), min(0.6 + 0.1 * n, 1.0))
        elif name in OBJECTIVES:
            label = OBJECTIVES[name] + (f" ({e['DragonType']})" if e.get("DragonType") else "")
            stolen = str(e.get("Stolen")).lower() == "true"
            hit = ("game:objective", label + (" (stolen)" if stolen else ""), 1.0 if stolen else 0.6)
        elif name == "Ace":
            hit = ("game:ace", "Ace", 0.7)
        elif name == "GameEnd":
            hit = ("game:win", "Victory", 1.0) if e.get("Result") == "Win" else ("game:loss", "Defeat", 0.6)
        if hit:
            out.append((*hit, now - (game_time - t)))
    state["seen"] = seen
    return out


def lol_poll(state: dict, now: float | None = None) -> int:
    """One poll: new events become marks on the recording. No game running = nothing to do (state resets for the next one)."""
    now = time.time() if now is None else now
    try:
        if "me" not in state:
            state["me"] = _who(_lol_get("/liveclientdata/activeplayername"))
        events = (_lol_get("/liveclientdata/eventdata") or {}).get("Events") or []
        game_time = float((_lol_get("/liveclientdata/gamestats") or {}).get("gameTime") or 0)
    except ConnectionRefusedError:  # no game running: the next one starts fresh
        state.clear()
        _st["lol"]["active"] = False
        return 0
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return 0  # a stutter, a loading screen (404) or garbage: keep what was seen, or every event would be marked again
    if game_time < state.get("game_time", 0) - 5:  # the clock went back: a new game
        state["seen"] = -1
    state["game_time"] = game_time
    _st["lol"]["active"] = True
    n = sum(_emit(kind, label, strength, at) for kind, label, strength, at in lol_markers(events, state["me"], game_time, now, state))
    _st["lol"]["events"] += n
    return n


# ---------- CS2 / Dota 2: Game State Integration ----------

GSI_FILE = "gamestate_integration_reelwright.cfg"
GSI_DATA = {"cs2": ("provider", "map", "round", "player_id", "player_state", "player_match_stats"),
            "dota2": ("provider", "map", "player", "events")}
_gsi: dict = {}
_gsi_lock = threading.Lock()
_token_lock = threading.Lock()
_gsi_state: dict = {}


def gsi_token() -> str:
    with _token_lock:  # the supervisor and a download may both ask first: only one token is ever made
        if not config.load()["gsi_token"]:
            config.save({"gsi_token": "gsi" + secrets.token_urlsafe(18)})
        return config.load()["gsi_token"]


def gsi_config(game: str) -> str:
    """The .cfg the creator drops into the game's folder: where to post, the token, and which state to send."""
    if game not in GSI_DATA:
        raise ValueError(f"No Game State Integration for {game}.")
    data = "\n".join(f'    "{k}" "1"' for k in GSI_DATA[game])
    return (f'"Reelwright"\n{{\n  "uri" "http://127.0.0.1:{int(config.load()["gsi_port"])}/"\n  "timeout" "5.0"\n  "buffer" "0.1"\n'
            f'  "throttle" "0.5"\n  "heartbeat" "30.0"\n  "auth"\n  {{\n    "token" "{gsi_token()}"\n  }}\n'
            f'  "data"\n  {{\n{data}\n  }}\n}}\n')


def _delta(cur: dict, prev: dict, k: str) -> int:
    a, b = cur.get(k), prev.get(k)
    return a - b if isinstance(a, int) and isinstance(b, int) else 0


def _cs2(p: dict, st: dict) -> list[tuple]:
    me, pl, rnd = (p.get("provider") or {}).get("steamid"), p.get("player") or {}, p.get("round") or {}
    if not me or pl.get("steamid") != me:  # spectating someone else (or dead, watching a teammate)
        return []
    ms, ps = pl.get("match_stats") or {}, pl.get("state") or {}
    cur = {"kills": ms.get("kills"), "deaths": ms.get("deaths"), "mvps": ms.get("mvps"), "rk": ps.get("round_kills"), "phase": rnd.get("phase")}
    prev, st["cs2"] = st.get("cs2"), cur
    if not prev:
        return []  # the first state is a baseline: nothing happened "now"
    out = []
    if (dk := _delta(cur, prev, "kills")) > 0:
        out.append(("game:kill", "Kill" if dk == 1 else f"{dk} kills", 0.6))
    rk = cur["rk"] if isinstance(cur["rk"], int) else 0
    if rk >= 3 and rk > (prev["rk"] if isinstance(prev["rk"], int) else 0):
        out.append(("game:multikill", "ACE" if rk >= 5 else f"{rk}K", min(0.6 + 0.1 * rk, 1.0)))
    if _delta(cur, prev, "deaths") > 0:
        out.append(("game:death", "Death", 0.5))
    if _delta(cur, prev, "mvps") > 0:
        out.append(("game:mvp", "Round MVP", 0.7))
    if cur["phase"] == "over" and prev["phase"] != "over" and rnd.get("win_team") and rnd.get("win_team") == pl.get("team"):
        out.append(("game:round_win", "Round won", 0.6))
    return out


def _dota(p: dict, st: dict) -> list[tuple]:
    pl, mp = p.get("player") or {}, p.get("map") or {}
    cur = {"kills": pl.get("kills"), "deaths": pl.get("deaths"), "state": mp.get("game_state")}
    seen = st.setdefault("dota_events", set())
    new = [e for e in p.get("events") or [] if isinstance(e, dict) and (e.get("event_type"), e.get("game_time")) not in seen]
    seen.update((e.get("event_type"), e.get("game_time")) for e in new)
    prev, st["dota"] = st.get("dota"), cur
    if not prev:
        return []
    out = []
    if (dk := _delta(cur, prev, "kills")) > 0:
        out.append(("game:kill", "Kill" if dk == 1 else f"{dk} kills", 0.6))
    if _delta(cur, prev, "deaths") > 0:
        out.append(("game:death", "Death", 0.5))
    out += [("game:objective", "Roshan", 0.7) for e in new if e.get("event_type") == "roshan_killed"]
    if cur["state"] == "DOTA_GAMERULES_STATE_POST_GAME" and prev["state"] != cur["state"] and mp.get("win_team"):
        out.append(("game:win", "Victory", 1.0) if mp["win_team"] == pl.get("team_name") else ("game:loss", "Defeat", 0.6))
    return out


def gsi_markers(payload: dict, state: dict) -> list[tuple]:
    """A game state post -> [(kind, label, strength)] for what changed since the previous one."""
    appid = (payload.get("provider") or {}).get("appid")
    return _cs2(payload, state) if appid == 730 else _dota(payload, state) if appid == 570 else []


class _GSIHandler(BaseHTTPRequestHandler):
    server_version = "Reelwright"
    timeout = 10

    def log_message(self, *a):  # the GUI app has no console
        pass

    def _send(self, code: int):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(411)
        if not 0 < n <= 256_000:  # read before the token can be checked (it's in the body): keep it small
            return self._send(413)
        try:
            p = json.loads(self.rfile.read(n))
        except ValueError:
            return self._send(400)
        want = config.load()["gsi_token"]
        got = str(((p.get("auth") or {}) if isinstance(p, dict) else {}).get("token") or "")
        if not want or not hmac.compare_digest(got.encode(), want.encode()):
            return self._send(401)
        try:
            with _gsi_lock:  # one post at a time, answered after it's handled: each is a diff against the previous one
                _st["gsi"]["events"] += sum(_emit(*m) for m in gsi_markers(p, _gsi_state))
        except Exception:  # an odd payload: the game still gets its answer
            log.error("live", "GSI post")
        self._send(200)


class _GSIServer(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let another program share the port


def _gsi_start(port: int) -> None:
    srv = _GSIServer(("127.0.0.1", port), _GSIHandler)  # loopback only; OSError when the port is taken
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _gsi_state.clear()
    _gsi.update(srv=srv, port=port)


def _gsi_stop() -> None:
    srv = _gsi.pop("srv", None)
    _gsi.pop("port", None)
    if srv:
        srv.shutdown()
        srv.server_close()


# ---------- VTube Studio: public plugin API ----------

class VTSError(RuntimeError):
    pass


PLUGIN = {"pluginName": "Reelwright", "pluginDeveloper": "Reelwright"}
VTS_EVENTS = ("HotkeyTriggeredEvent", "ItemEvent", "TrackingStatusChangedEvent")
_vts = {"want": False, "thread": None}


def vts_markers(mtype: str, d: dict, track: dict) -> list[tuple]:
    """A VTS event -> [(kind, label, strength)]: expressions/animations a hotkey fired, an item dropped into the scene,
    the face tracker losing the creator (leaning out of frame is often a reaction)."""
    if mtype == "HotkeyTriggeredEvent":
        kind = {"ToggleExpression": "vts:expression", "TriggerAnimation": "vts:animation"}.get(d.get("hotkeyAction"), "vts:hotkey")
        return [(kind, str(d.get("hotkeyName") or d.get("hotkeyAction") or "Hotkey"), 0.5 if kind == "vts:hotkey" else 0.8)]
    if mtype == "ItemEvent" and d.get("itemEventType") == "Added":
        return [("vts:item", str(d.get("itemFileName") or "Item"), 0.6)]
    if mtype == "TrackingStatusChangedEvent":
        was, track["face"] = track.get("face", True), bool(d.get("faceFound"))
        if was and not track["face"]:
            return [("vts:tracking_lost", "Face tracking lost", 0.4)]
    return []


def _vts_request(ws, mtype: str, data: dict | None = None, timeout: float = 10.0, on_event=None) -> dict:
    rid = uuid.uuid4().hex[:16]
    ws.send(json.dumps({"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0", "requestID": rid, "messageType": mtype, "data": data or {}}))
    end = time.monotonic() + timeout
    while (left := end - time.monotonic()) > 0:
        msg = json.loads(ws.recv(timeout=left))
        if msg.get("messageType") in VTS_EVENTS:
            if on_event:
                on_event(msg)
        elif msg.get("requestID") == rid:
            if msg.get("messageType") == "APIError":
                raise VTSError((msg.get("data") or {}).get("message") or "VTube Studio refused the request.")
            return msg.get("data") or {}
    raise TimeoutError(f"VTube Studio didn't answer {mtype}")


def _vts_connect(port: int):
    from websockets.sync.client import connect
    return connect(f"ws://127.0.0.1:{int(port)}", open_timeout=3, max_size=2 ** 20, proxy=None)


def vts_authorize(port: int | None = None, timeout: float = 90.0) -> dict:
    """Ask VTube Studio for a plugin token: VTS shows a pop-up and the creator clicks Allow (we wait for it)."""
    from websockets.exceptions import WebSocketException
    port = int(port or config.load()["vts_port"])
    try:
        with _vts_connect(port) as ws:
            tok = _vts_request(ws, "AuthenticationTokenRequest", PLUGIN, timeout).get("authenticationToken")
    except (OSError, TimeoutError, WebSocketException) as e:
        raise VTSError(f"Can't reach VTube Studio on port {port}: is it running, with Settings → “Start API” on? ({e})") from None
    if not tok:
        raise VTSError("VTube Studio didn't give Reelwright access.")
    config.save({"vts_token": tok})
    return {"ok": True}


def _vts_session(port: int, token: str, keep_going) -> None:
    """Authenticate, subscribe, and turn events into marks until keep_going() is false or VTS closes the connection."""
    from websockets.exceptions import ConnectionClosed
    track: dict = {}

    def on_event(msg):
        _st["vts"]["events"] += sum(_emit(*m) for m in vts_markers(msg["messageType"], msg.get("data") or {}, track))

    with _vts_connect(port) as ws:
        if not _vts_request(ws, "AuthenticationRequest", {**PLUGIN, "authenticationToken": token}).get("authenticated"):
            raise VTSError("VTube Studio no longer accepts Reelwright: Authorize it again in Settings → Live integrations.")
        for ev in VTS_EVENTS:
            _vts_request(ws, "EventSubscriptionRequest", {"eventName": ev, "subscribe": True, "config": {}}, on_event=on_event)
        _st["vts"].update(connected=True, error="")
        try:
            while keep_going():
                try:
                    msg = json.loads(ws.recv(timeout=1))
                except TimeoutError:
                    continue
                if msg.get("messageType") in VTS_EVENTS:
                    on_event(msg)
        except ConnectionClosed:
            pass
        finally:
            _st["vts"]["connected"] = False


def _vts_run() -> None:
    backoff = 2
    while _vts["want"]:
        s = config.load()
        try:
            _vts_session(s["vts_port"], s["vts_token"], lambda: _vts["want"])
            backoff = 2
        except VTSError as e:
            _st["vts"]["error"] = str(e)
            if "Authorize" in str(e):
                config.save({"vts_token": ""})  # revoked in VTS: stop until the creator authorizes again
        except Exception as e:  # VTS closed, not running, or an odd message
            _st["vts"]["error"] = f"VTube Studio: {e}"
        _st["vts"]["connected"] = False
        for _ in range(backoff):
            if not _vts["want"]:
                break
            time.sleep(1)
        backoff = min(backoff * 2, 30)


# ---------- the global "mark this" hotkey ----------

MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
KEYS = {"pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "scrolllock": 0x91,
        "space": 0x20, **{f"num{i}": 0x60 + i for i in range(10)}}
_hk: dict = {"spec": "", "tid": None, "thread": None}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """"ctrl+alt+m" -> (RegisterHotKey modifiers, virtual-key code). ValueError for anything else."""
    mods, keys = 0, []
    for p in (x.strip().lower() for x in str(spec or "").split("+")):
        if p in MODS:
            mods |= MODS[p]
        elif len(p) == 1 and p in string.ascii_lowercase + string.digits:
            keys.append(ord(p.upper()))
        elif p[:1] == "f" and p[1:].isdigit() and 1 <= int(p[1:]) <= 24:
            keys.append(0x6F + int(p[1:]))
        elif p in KEYS:
            keys.append(KEYS[p])
        else:
            raise ValueError(f"Unknown key “{p}” in the hotkey: use e.g. ctrl+alt+m or ctrl+shift+f9.")
    if len(keys) != 1:
        raise ValueError("A hotkey is modifier keys plus exactly one key, e.g. ctrl+alt+m.")
    if not mods and not (0x70 <= keys[0] <= 0x87 or keys[0] in (0x13, 0x91)):  # a bare letter would be stolen from the game
        raise ValueError("Add ctrl, alt, shift or win to that key (only F-keys, Pause and Scroll Lock work alone).")
    return mods, keys[0]


def _hotkey_loop(spec: str, ready: threading.Event) -> None:
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    tid = ctypes.windll.kernel32.GetCurrentThreadId()
    try:
        mods, vk = parse_hotkey(spec)
        if not u32.RegisterHotKey(None, 1, mods | 0x4000, vk):  # MOD_NOREPEAT: holding the keys marks once
            _st["hotkey"]["error"] = f"{spec} is already used by another program: pick another hotkey."
            return
    except ValueError as e:
        _st["hotkey"]["error"] = str(e)
        return
    finally:
        ready.set()
    _hk["tid"], _st["hotkey"]["error"] = tid, ""
    msg = wintypes.MSG()
    try:
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # WM_QUIT (from _hotkey_apply) ends it
            if msg.message == 0x0312:  # WM_HOTKEY
                try:
                    obs.mark(source="hotkey")
                except RuntimeError as e:
                    log.event("live", f"hotkey mark: {e}")
    finally:
        u32.UnregisterHotKey(None, 1)
        if _hk.get("tid") == tid:  # a newer hotkey thread may have taken over already
            _hk["tid"] = None


def _hotkey_apply(spec: str) -> None:
    """Register the hotkey (only while recording, so it never steals the keys otherwise), or release it."""
    if spec == _hk["spec"]:
        return
    old = _hk.get("thread")
    if _hk.get("tid"):
        import ctypes
        ctypes.windll.user32.PostThreadMessageW(_hk["tid"], 0x0012, 0, 0)  # WM_QUIT
    if old:
        old.join(2)  # it unregisters first, so the new thread can register the same id
    _hk.update(spec=spec, thread=None, tid=None)
    _st["hotkey"].update(spec=spec, error="")
    if spec and sys.platform == "win32":
        ready = threading.Event()
        _hk["thread"] = threading.Thread(target=_hotkey_loop, args=(spec, ready), daemon=True, name="mark-hotkey")
        _hk["thread"].start()
        ready.wait(2)


# ---------- the supervisor: everything runs only while enabled and recording ----------

_lol_state: dict = {}


def _tick() -> None:
    s = config.load()
    rec = bool(obs.recording())
    if s["gsi_enabled"] and rec:
        if not _gsi.get("srv") or _gsi.get("port") != int(s["gsi_port"]):
            _gsi_stop()
            try:
                gsi_token()
                _gsi_start(int(s["gsi_port"]))
                _st["gsi"]["error"] = ""
            except OSError as e:
                _st["gsi"]["error"] = f"Port {s['gsi_port']} is in use by another program ({e}): pick another and regenerate the .cfg."
    elif _gsi.get("srv"):
        _gsi_stop()
    if s["lol_enabled"] and rec:
        lol_poll(_lol_state)
    else:
        _lol_state.clear()
        _st["lol"]["active"] = False
    _vts["want"] = bool(s["vts_enabled"] and rec and s["vts_token"])
    if _vts["want"] and not (_vts["thread"] and _vts["thread"].is_alive()):
        _vts["thread"] = threading.Thread(target=_vts_run, daemon=True, name="vts")
        _vts["thread"].start()
    _hotkey_apply(s["live_mark_hotkey"] if rec else "")


def start() -> None:
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            try:
                _tick()
            except Exception:
                log.error("live", "telemetry tick failed")
            time.sleep(1)

    threading.Thread(target=loop, daemon=True, name="telemetry").start()


def status() -> dict:
    s = config.load()
    return {"recording": [c["name"] for c in obs.recording()], "hotkey_spec": s["live_mark_hotkey"],
            "lol": {"enabled": s["lol_enabled"], **_st["lol"]},
            "gsi": {"enabled": s["gsi_enabled"], "running": bool(_gsi.get("srv")), "port": s["gsi_port"], "token": s["gsi_token"], **_st["gsi"]},
            "vts": {"enabled": s["vts_enabled"], "port": s["vts_port"], "authorized": bool(s["vts_token"]), **_st["vts"]},
            "hotkey": {"active": bool(_hk.get("tid")), **_st["hotkey"]}}
