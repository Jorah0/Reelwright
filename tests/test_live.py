"""WP6 live & integrations: OBS marks -> markers.json, game telemetry (LoL Live Client, CS2/Dota 2 GSI), VTube Studio,
the teach-by-example HUD detector and the local MCP server. No network, no real games: every endpoint is faked in-process."""
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from websockets.sync.server import serve  # noqa: E402

from reelwright import accounts, config, hud, mcp_server, media, obs, pipeline, signals, store, telemetry, vision  # noqa: E402

STARTED, STOPPED = "OBS_WEBSOCKET_OUTPUT_STARTED", "OBS_WEBSOCKET_OUTPUT_STOPPED"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def rec():
    """An OBS connection that is recording: started at wall-clock 1000.0."""
    c = store.put("obs", {"name": "Rig", "host": "127.0.0.1", "enabled": True, "status": "connected"})
    obs._record_event(c["id"], "RecordStateChanged", {"outputState": STARTED}, now=1000.0)
    yield c["id"]
    store.delete("obs", c["id"])


def _marks(cid):
    return (store.get("obs", cid).get("rec") or {}).get("marks", [])


# ---------- everything is off by default ----------

def test_everything_is_off_by_default():
    d = config.DEFAULTS
    assert d["live_mark_hotkey"] == "" and not d["lol_enabled"] and not d["gsi_enabled"] and not d["vts_enabled"]
    assert not obs.recording()  # no OBS connection recording: telemetry has no clock, so nothing runs
    telemetry._tick()
    st = telemetry.status()
    assert not st["gsi"]["running"] and not st["vts"]["connected"] and not st["hotkey"]["active"] and not st["lol"]["active"]
    with pytest.raises(RuntimeError, match="recording"):
        obs.mark("x")


# ---------- OBS marks: wall clock -> seconds in the recorded file ----------

def test_mark_times_map_to_file_seconds_with_pauses(rec):
    obs.mark("clutch", source="ui", cid=rec, at=1012.5)
    obs._record_event(rec, "RecordStateChanged", {"outputState": "OBS_WEBSOCKET_OUTPUT_PAUSED"}, now=1050.0)
    obs._record_event(rec, "RecordStateChanged", {"outputState": "OBS_WEBSOCKET_OUTPUT_RESUMED"}, now=1060.0)
    obs.mark(source="hotkey", cid=rec, at=1070.0)  # 10 s paused don't exist in the file
    obs.add_marker("game:kill", "Kill", 0.6, at=1080.0)  # telemetry lands on whatever is recording
    obs.mark(source="remote", cid=rec, at=990.0)  # before the recording started: dropped
    out = obs._record_event(rec, "RecordStateChanged", {"outputState": STOPPED, "outputPath": "C:/rec.mkv"}, now=1100.0)
    assert out == [{"t": 12.5, "kind": "obs", "label": "clutch", "strength": 1.0},
                   {"t": 60.0, "kind": "obs", "label": "Hotkey mark", "strength": 1.0},
                   {"t": 70.0, "kind": "game:kill", "label": "Kill", "strength": 0.6}]
    assert store.get("obs", rec)["rec"] is None
    with pytest.raises(RuntimeError, match="recording"):
        obs.mark("late", cid=rec)


def test_a_recording_already_running_when_reelwright_connects(rec):
    class FakeOBS:
        def __init__(self, ms):
            self.ms = ms

        def request(self, kind):
            assert kind == "GetRecordStatus"
            return {"outputActive": True, "outputPaused": False, "outputDuration": self.ms}

    obs._sync_recording(rec, FakeOBS(95_000), now=2000.0)  # started 95 s ago, before Reelwright saw it start
    assert store.get("obs", rec)["rec"]["start"] == 1905.0
    obs.mark("x", cid=rec, at=2010.0)
    assert obs.rec_markers(store.get("obs", rec)["rec"])[0]["t"] == 105.0
    obs._sync_recording(rec, FakeOBS(115_000), now=2020.0)  # a reconnect mid-recording keeps the marks
    assert len(_marks(rec)) == 1


def test_a_stale_recording_of_an_offline_obs_runs_nothing(rec):
    store.update("obs", rec, {"status": "offline"})  # OBS crashed mid-recording: "rec" is still on the doc
    assert not obs.recording()
    with pytest.raises(RuntimeError, match="recording"):
        obs.mark("x", cid=rec)


def test_obs_mark_action_needs_a_recording(rec):
    r = obs.action(store.get("obs", rec), "mark")  # the OBS page's Mark button: no websocket round-trip needed
    assert r["ok"] and r["marks"][0]["connection"] == "Rig" and _marks(rec)[0]["label"] == "Marked in Reelwright"


def test_watcher_passes_marks_of_a_finished_recording_to_ingest(monkeypatch, tmp_path):
    """Fake OBS: the recording starts, the creator marks it, it stops -> the import gets the marks in file seconds."""
    go = threading.Event()

    def handler(ws):
        ws.send(json.dumps({"op": 0, "d": {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}}))
        ws.recv()
        ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        for raw in ws:
            d = json.loads(raw)["d"]
            ws.send(json.dumps({"op": 7, "d": {"requestType": d["requestType"], "requestId": d["requestId"],
                                               "requestStatus": {"result": True, "code": 100}, "responseData": {"outputActive": False}}}))
            ws.send(json.dumps({"op": 5, "d": {"eventType": "RecordStateChanged", "eventData": {"outputState": STARTED}}}))
            go.wait(10)
            ws.send(json.dumps({"op": 5, "d": {"eventType": "RecordStateChanged",
                                               "eventData": {"outputState": STOPPED, "outputPath": str(tmp_path / "rec.mkv")}}}))

    port = _free_port()
    srv = serve(handler, "127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    got = []
    monkeypatch.setattr(pipeline, "ingest_file", lambda path, name, origin, ap=None, markers=None: got.append(markers) or {"id": "p"})
    c = store.put("obs", {"name": "Local", "host": "127.0.0.1", "port": port, "enabled": True, "import_recordings": True, "rev": 1})
    t = threading.Thread(target=obs._watch, args=(c["id"],), daemon=True)
    t.start()
    try:
        for _ in range(100):
            if (store.get("obs", c["id"]).get("rec") or {}).get("start"):
                break
            time.sleep(0.05)
        start = store.get("obs", c["id"])["rec"]["start"]
        obs.mark("boom", cid=c["id"], at=start + 7.25)
        go.set()
        for _ in range(100):
            if got:
                break
            time.sleep(0.05)
        assert got == [[{"t": 7.25, "kind": "obs", "label": "boom", "strength": 1.0}]]
    finally:
        store.update("obs", c["id"], {"enabled": False})
        t.join(10)
        srv.shutdown()
        store.delete("obs", c["id"])


def test_markers_json_is_written_at_ingest_in_the_contract_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: {"id": "j"})
    marks = [{"t": 12.5, "kind": "obs", "label": "clutch", "strength": 1.0}, {"t": 70.0, "kind": "game:kill", "label": "Kill", "strength": 0.6, "d": 2.0}]
    p = pipeline.ingest_file(tmp_path / "rec.mkv", "rec", {"obs": "Rig", "kind": "recording"}, None, marks)
    saved = json.loads((pipeline.pdir(p["id"]) / "markers.json").read_text("utf-8"))
    assert saved == {"markers": marks}
    assert signals.read_markers(pipeline.pdir(p["id"])) == marks  # WP1's reader takes it unchanged
    q = pipeline.ingest_file(tmp_path / "b.mkv", "b", {"obs": "Rig", "kind": "replay"})
    assert not (pipeline.pdir(q["id"]) / "markers.json").exists()


def test_hotkey_spec_parsing():
    assert telemetry.parse_hotkey("Ctrl+Alt+M") == (0x2 | 0x1, ord("M"))
    assert telemetry.parse_hotkey("shift+f9") == (0x4, 0x78)
    assert telemetry.parse_hotkey("f13") == (0, 0x7C)  # an F-key may stand alone; a letter may not
    for bad in ("", "ctrl+alt", "ctrl+m+k", "hyper+m", "m"):
        with pytest.raises(ValueError):
            telemetry.parse_hotkey(bad)


# ---------- League of Legends Live Client Data ----------

LOL_EVENTS = {"Events": [
    {"EventID": 0, "EventName": "GameStart", "EventTime": 0.05},
    {"EventID": 1, "EventName": "ChampionKill", "EventTime": 230.0, "KillerName": "Faker", "VictimName": "Foe"},  # before recording
    {"EventID": 2, "EventName": "ChampionKill", "EventTime": 300.0, "KillerName": "Faker", "VictimName": "Foe", "Assisters": []},
    {"EventID": 3, "EventName": "Multikill", "EventTime": 302.0, "KillerName": "Faker", "KillStreak": 3},
    {"EventID": 4, "EventName": "ChampionKill", "EventTime": 310.0, "KillerName": "Foe", "VictimName": "Faker"},
    {"EventID": 5, "EventName": "BaronKill", "EventTime": 320.0, "KillerName": "Faker", "Stolen": "True"},
    {"EventID": 6, "EventName": "ChampionKill", "EventTime": 330.0, "KillerName": "Someone", "VictimName": "Other"},
]}


@pytest.fixture
def fake_lol(monkeypatch):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = {"/liveclientdata/eventdata": LOL_EVENTS, "/liveclientdata/gamestats": {"gameTime": 340.0},
                    "/liveclientdata/activeplayername": "Faker#KR1"}.get(self.path)
            raw = json.dumps(body).encode()
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(telemetry, "LOL_BASE", f"http://127.0.0.1:{srv.server_address[1]}")
    yield
    srv.shutdown()


def test_lol_events_become_game_markers(rec, fake_lol):
    # wall 1340 = game time 340, so the game started at wall 1000 - 0 ... game time 230 is wall 1230
    obs._record_event(rec, "RecordStateChanged", {"outputState": STARTED}, now=1250.0)  # recording from game time 250
    state = {}
    assert telemetry.lol_poll(state, now=1340.0) == 4
    assert telemetry.lol_poll(state, now=1341.0) == 0  # seen events are never added twice
    base = telemetry.LOL_BASE
    telemetry.LOL_BASE = base + "/missing"  # a 404 blip (loading screen) keeps what was seen
    assert telemetry.lol_poll(state, now=1342.0) == 0 and state["seen"] == 6
    telemetry.LOL_BASE = base
    assert telemetry.lol_poll(state, now=1343.0) == 0
    out = obs.rec_markers(store.get("obs", rec)["rec"])
    assert [(m["t"], m["kind"], m["label"]) for m in out] == [
        (50.0, "game:kill", "Kill"), (52.0, "game:multikill", "Triple kill"), (60.0, "game:death", "Death"),
        (70.0, "game:objective", "Baron (stolen)")]


# ---------- CS2 / Dota 2 Game State Integration ----------

def _post(port, body: dict):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _cs(kills, deaths, rk, phase="live", win=None, token="tok-cs"):
    return {"provider": {"appid": 730, "steamid": "765"}, "auth": {"token": token},
            "player": {"steamid": "765", "team": "CT", "state": {"round_kills": rk}, "match_stats": {"kills": kills, "deaths": deaths, "mvps": 0}},
            "round": {"phase": phase, **({"win_team": win} if win else {})}}


def test_gsi_rejects_a_wrong_token_and_turns_cs2_events_into_markers(rec):
    config.save({"gsi_token": "tok-cs"})
    port = _free_port()
    telemetry._gsi_start(port)
    try:
        assert _post(port, _cs(0, 0, 0, token="WRONG")) == 401
        assert _post(port, {"provider": {"appid": 730}}) == 401  # no token at all
        assert _post(port, _cs(3, 1, 0)) == 200  # first state: a baseline, no events
        assert _post(port, _cs(4, 1, 1)) == 200
        assert _post(port, _cs(6, 1, 3)) == 200
        assert _post(port, _cs(6, 2, 3)) == 200
        assert _post(port, _cs(6, 2, 0, phase="over", win="CT")) == 200
        spec = _cs(9, 2, 0)
        spec["player"]["steamid"] = "999"  # spectating someone else: not the creator's kills
        assert _post(port, spec) == 200
    finally:
        telemetry._gsi_stop()
        config.save({"gsi_token": ""})
    kinds = [(m["kind"], m["label"]) for m in _marks(rec)]
    assert kinds == [("game:kill", "Kill"), ("game:kill", "2 kills"), ("game:multikill", "3K"), ("game:death", "Death"),
                     ("game:round_win", "Round won")]


def test_dota_gsi_kills_roshan_and_win():
    st = {}
    base = {"provider": {"appid": 570}, "player": {"kills": 2, "deaths": 0, "team_name": "radiant"},
            "map": {"game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"}}
    assert telemetry.gsi_markers(base, st) == []
    nxt = {**base, "player": {**base["player"], "kills": 3}, "events": [{"event_type": "roshan_killed", "game_time": 1200}]}
    assert telemetry.gsi_markers(nxt, st) == [("game:kill", "Kill", 0.6), ("game:objective", "Roshan", 0.7)]
    assert telemetry.gsi_markers(nxt, st) == []  # the same event list again
    end = {**nxt, "map": {"game_state": "DOTA_GAMERULES_STATE_POST_GAME", "win_team": "radiant"}}
    assert telemetry.gsi_markers(end, st) == [("game:win", "Victory", 1.0)]


def test_gsi_config_files_carry_the_token_and_port():
    config.save({"gsi_token": "tok-x", "gsi_port": 3010})
    cs = telemetry.gsi_config("cs2")
    assert '"token" "tok-x"' in cs and '"uri" "http://127.0.0.1:3010/"' in cs and '"player_match_stats" "1"' in cs
    assert '"token" "tok-x"' in telemetry.gsi_config("dota2")
    with pytest.raises(ValueError):
        telemetry.gsi_config("quake")
    config.save({"gsi_token": ""})


# ---------- VTube Studio ----------

@pytest.fixture
def fake_vts():
    state = {"subs": [], "token": "vts-tok"}

    def handler(ws):
        for raw in ws:
            m = json.loads(raw)
            reply = lambda t, d: ws.send(json.dumps({"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",  # noqa: E731
                                                     "requestID": m["requestID"], "messageType": t, "data": d}))
            if m["messageType"] == "AuthenticationTokenRequest":
                assert m["data"]["pluginName"] == "Reelwright"
                reply("AuthenticationTokenResponse", {"authenticationToken": state["token"]})
            elif m["messageType"] == "AuthenticationRequest":
                reply("AuthenticationResponse", {"authenticated": m["data"]["authenticationToken"] == state["token"], "reason": "x"})
            elif m["messageType"] == "EventSubscriptionRequest":
                state["subs"].append(m["data"]["eventName"])
                reply("EventSubscriptionResponse", {"subscribedEventCount": len(state["subs"])})
                if len(state["subs"]) == 3:
                    for t, d in (("HotkeyTriggeredEvent", {"hotkeyName": "Shocked", "hotkeyAction": "ToggleExpression"}),
                                 ("HotkeyTriggeredEvent", {"hotkeyName": "Wave", "hotkeyAction": "TriggerAnimation"}),
                                 ("ItemEvent", {"itemEventType": "Added", "itemFileName": "pizza.png"}),
                                 ("TrackingStatusChangedEvent", {"faceFound": False}),
                                 ("TrackingStatusChangedEvent", {"faceFound": False})):
                        ws.send(json.dumps({"apiName": "VTubeStudioPublicAPI", "messageType": t, "data": d}))
                    ws.close()
                    return

    port = _free_port()
    srv = serve(handler, "127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port, state
    srv.shutdown()


def test_vts_plugin_auth_and_events_become_markers(rec, fake_vts):
    port, state = fake_vts
    assert telemetry.vts_authorize(port)["ok"] and config.load()["vts_token"] == "vts-tok"
    telemetry._vts_session(port, "vts-tok", lambda: True)  # returns when VTS closes the connection
    assert sorted(state["subs"]) == ["HotkeyTriggeredEvent", "ItemEvent", "TrackingStatusChangedEvent"]
    assert [(m["kind"], m["label"]) for m in _marks(rec)] == [
        ("vts:expression", "Shocked"), ("vts:animation", "Wave"), ("vts:item", "pizza.png"), ("vts:tracking_lost", "Face tracking lost")]
    config.save({"vts_token": ""})


def test_vts_rejected_token_says_to_authorize_again(fake_vts):
    port, _ = fake_vts
    with pytest.raises(telemetry.VTSError, match="Authorize"):
        telemetry._vts_session(port, "stale", lambda: True)


# ---------- teach-by-example HUD detector ----------

@pytest.fixture(scope="module")
def banner_video(tmp_path_factory):
    """A busy test pattern with a patterned red banner shown at 5-7 s and 13-15 s."""
    path = tmp_path_factory.mktemp("hud") / "v.mp4"
    subprocess.run([media.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=20",
                    "-f", "lavfi", "-i", "color=c=0xD02020:s=240x60:r=10:d=20", "-filter_complex",
                    "[1]drawbox=x=20:y=15:w=40:h=30:c=black:t=fill,drawbox=x=100:y=10:w=20:h=40:c=white:t=fill,"
                    "drawbox=x=160:y=20:w=60:h=20:c=black:t=fill[b];"
                    "[0][b]overlay=x=200:y=150:enable='between(t,5,7)+between(t,13,15)'",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_hud_template_matching_finds_the_banner_at_known_times(banner_video):
    meta = media.probe(banner_video)
    doc = hud.teach(banner_video, meta, 6.0, [200 / 640, 150 / 360, 240 / 640, 60 / 360], "VICTORY!", "Test game")
    try:
        assert doc["kind"] == "hud:victory" and (config.DATA / doc["file"]).exists()
        plain = vision.analyze(banner_video, meta, faces=False)
        assert "hud" not in plain  # default behaviour unchanged
        out = vision.analyze(banner_video, meta, faces=False, hud=hud.active())
        starts = [m["t"] for m in out["hud"]]
        assert len(starts) == 2 and abs(starts[0] - 5) <= 1 and abs(starts[1] - 13) <= 1, out["hud"]
        assert all(m["kind"] == "hud:victory" and m["label"] == "VICTORY!" and 1 <= m["d"] <= 3 for m in out["hud"])
        with pytest.raises(ValueError, match="detail"):  # a flat area can't be matched
            hud.teach(banner_video, meta, 6.0, [272 / 640, 152 / 360, 0.03, 0.03], "flat", "")
    finally:
        hud.remove(doc["id"])


def test_hud_matches_replace_earlier_ones_in_markers_and_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: {"id": "j"})
    p = pipeline.ingest_file(tmp_path / "a.mkv", "a", {"obs": "Rig"}, None, [{"t": 1.0, "kind": "obs", "label": "m", "strength": 1.0}])
    pipeline.save_json(p["id"], "signals.json", {"markers": [{"t": 9.0, "kind": "hud:died", "label": "old", "strength": 1.0}]})
    hud.save_markers(p["id"], [{"t": 4.0, "kind": "hud:died", "label": "YOU DIED", "strength": 0.9, "d": 2.0}], [{"kind": "hud:died"}], analyzed=True)
    assert [m["kind"] for m in signals.read_markers(pipeline.pdir(p["id"]))] == ["obs", "hud:died"]
    assert [m["t"] for m in pipeline.load_json(p["id"], "signals.json")["markers"]] == [4.0]


# ---------- local MCP server ----------

@pytest.fixture
def app_api(monkeypatch, tmp_path):
    """The MCP tools talk to the real app routes (in-process), with their own accounts file."""
    from fastapi.testclient import TestClient

    from reelwright import server
    monkeypatch.setattr(accounts, "FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "LEGACY_CONN", tmp_path / "connections.json")
    monkeypatch.setattr(accounts, "LEGACY_YT", tmp_path / "youtube_token.json")
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: store.put("job", {"kind": kind, "status": "queued", **kw}))
    client = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    calls = []

    def send(method, path, body=None):
        calls.append((method, path))
        r = client.request(method, path, json=body)
        return r.status_code, r.json()

    monkeypatch.setattr(mcp_server, "_send", send)
    p = store.put("project", {"name": "Stream", "status": "ready", "meta": {"duration": 100.0, "width": 1920, "height": 1080},
                              "origin": {"path": "D:/OBS/secret-folder/rec.mkv"}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    c = pipeline.new_clip(p["id"], {"start": 0.0, "end": 30.0, "score": 80, "title": "Clutch"}, source="ai")
    yield {"pid": p["id"], "cid": c["id"], "calls": calls}
    store.delete("clip", c["id"])
    store.delete("project", p["id"])


def _acct(platform, approved, autopilot):
    a = accounts.add(platform, name=f"{platform} main", access_token="TOKEN-123", refresh_token="REFRESH-456")
    return accounts.update(a["id"], {"approved": approved, "autopilot": autopilot})


def test_mcp_publish_refuses_unapproved_accounts(app_api):
    cid = app_api["cid"]
    unapproved, manual_only = _acct("youtube", False, False), _acct("youtube", True, False)
    for a in (unapproved, manual_only):
        with pytest.raises(RuntimeError, match="approved|Autopilot"):
            mcp_server.schedule_post(cid, a["id"], "My title")
    assert not store.find("upload", clip_id=cid)
    tt = _acct("tiktok", True, True)
    with pytest.raises(RuntimeError, match="TikTok"):
        mcp_server.schedule_post(cid, tt["id"], "My title")
    ok = _acct("youtube", True, True)
    with pytest.raises(ValueError, match="future"):
        mcp_server.schedule_post(cid, ok["id"], "My title", when="2001-01-01T00:00:00Z")
    res = mcp_server.schedule_post(cid, ok["id"], "My title")
    ups = store.find("upload", clip_id=cid)
    assert len(ups) == 1 and ups[0]["auto"] and ups[0]["account_id"] == ok["id"] and ups[0]["privacy"] == config.load()["default_privacy"]
    assert res["uploads"][0]["account_id"] == ok["id"]
    for u in ups:
        store.delete("upload", u["id"])


def test_mcp_tools_never_expose_settings_or_keys(app_api):
    config.save({"anthropic_api_key": "sk-ant-SECRET-KEY", "lan_token": "LAN-SECRET"})
    _acct("youtube", True, True)
    try:
        pid, cid = app_api["pid"], app_api["cid"]
        outs = [mcp_server.list_projects(), mcp_server.list_clips(pid), mcp_server.clip_status(cid), mcp_server.review_queue(),
                mcp_server.list_accounts(), mcp_server.list_jobs(), mcp_server.render_clip(cid), mcp_server.find_clips(pid, "funny"),
                mcp_server.review_clip(cid, "keep"), mcp_server.pause_autopilot()]
        blob = json.dumps(outs)
        for secret in ("SECRET", "TOKEN-123", "REFRESH-456", "LAN-SECRET", "secret-folder"):
            assert secret not in blob
        assert store.get("clip", cid)["rating"] == 1 and config.load()["autopilot_paused"]
        assert all(not p.startswith(("/api/settings", "/api/accounts", "/api/connections")) for _, p in app_api["calls"])
        for method, path in (("GET", "/api/settings"), ("PUT", "/api/settings"), ("PATCH", "/api/accounts/x"), ("PUT", "/api/autopilot/../settings"),
                             ("GET", "/api/projects/../settings"), ("POST", "/api/youtube/connect")):
            with pytest.raises(PermissionError):
                mcp_server._api(method, path)
        with pytest.raises(ValueError):
            mcp_server.clip_status("../../api/settings")
        with pytest.raises(ValueError):
            mcp_server.review_clip(cid, "publish")
    finally:
        config.save({"anthropic_api_key": "", "lan_token": "", "autopilot_paused": False})


def test_mcp_tool_list_has_no_resume_or_settings_tool():
    names = {f.__name__ for f in mcp_server.TOOLS}
    assert names == {"list_projects", "list_clips", "find_clips", "clip_status", "render_clip", "review_queue", "review_clip",
                     "list_accounts", "schedule_post", "pause_autopilot", "list_jobs"}
    assert all(f.__doc__ for f in mcp_server.TOOLS)


def test_mcp_config_snippet_points_at_this_install():
    cfg = mcp_server.claude_config()["mcpServers"]["reelwright"]
    assert cfg["args"][-1] in ("--mcp", "mcp") and cfg["command"]

