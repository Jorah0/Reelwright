"""OBS link: obs-websocket v5 client against a fake OBS, path mapping, the LAN receiver + relay end to end."""
import base64
import hashlib
import json
import os
import socket
import tempfile
import threading

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from websockets.sync.server import serve  # noqa: E402

from reelwright import config, lan, obs, pipeline, relay, store  # noqa: E402

PASSWORD, SALT, CHALLENGE = "hunter2", "c2FsdA==", "Y2hhbGxlbmdl"


def _free_port() -> int:
    """A port free for TCP *and* UDP: the receiver needs both, and a busy PC (or a parallel test run) often has the
    UDP side of a TCP-free number taken, which silently turned discovery off and made the relay test flaky."""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.bind(("", port))
            return port
        except OSError:
            continue
    raise RuntimeError("no port free for both TCP and UDP")


@pytest.fixture
def fake_obs():
    """A tiny obs-websocket v5 server: checks auth per the spec, answers requests, emits one replay-saved event."""
    seen = []

    def handler(ws):
        ws.send(json.dumps({"op": 0, "d": {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1,
                                           "authentication": {"salt": SALT, "challenge": CHALLENGE}}}))
        ident = json.loads(ws.recv())["d"]
        secret = base64.b64encode(hashlib.sha256((PASSWORD + SALT).encode()).digest()).decode()
        if ident.get("authentication") != base64.b64encode(hashlib.sha256((secret + CHALLENGE).encode()).digest()).decode():
            ws.close(4009, "Authentication failed")
            return
        ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        if ident.get("eventSubscriptions"):
            ws.send(json.dumps({"op": 5, "d": {"eventType": "ReplayBufferSaved", "eventIntent": 64,
                                               "eventData": {"savedReplayPath": "D:\\OBS\\Replays\\Replay 1.mkv"}}}))
        for raw in ws:
            d = json.loads(raw)["d"]
            seen.append(d["requestType"])
            data = {"GetVersion": {"obsVersion": "31.0.0", "obsWebSocketVersion": "5.5.0"},
                    "GetReplayBufferStatus": {"outputActive": True}, "GetRecordStatus": {"outputActive": False}}
            ok = d["requestType"] in data or d["requestType"] in obs.ACTIONS.values()
            ws.send(json.dumps({"op": 7, "d": {"requestType": d["requestType"], "requestId": d["requestId"],
                                               "requestStatus": {"result": ok, "code": 100 if ok else 204},
                                               "responseData": data.get(d["requestType"], {})}}))

    port = _free_port()
    srv = serve(handler, "127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"ws://127.0.0.1:{port}", seen
    srv.shutdown()


def test_obs_client_auth_requests_and_events(fake_obs):
    url, seen = fake_obs
    with relay.OBS(url, PASSWORD) as o:
        assert o.request("GetVersion")["obsVersion"] == "31.0.0"  # the event that arrived first is kept, not lost
        ev = o.next_event(timeout=2)
    assert relay.saved_file(*ev) == ("D:\\OBS\\Replays\\Replay 1.mkv", "replay")
    with pytest.raises(relay.OBSError, match="Wrong OBS websocket password"):
        relay.OBS(url, "nope").connect()
    with pytest.raises(relay.OBSError, match="requires a password"):
        relay.OBS(url, "").connect()


def test_obs_remote_actions_and_probe(fake_obs):
    url, seen = fake_obs
    conn = {"host": url, "password": PASSWORD}
    info = obs.action(conn, "save_replay")
    assert "SaveReplayBuffer" in seen and info["obs_version"] == "31.0.0" and info["replay_buffer"] is True
    with pytest.raises(RuntimeError):
        obs.action(conn, "format_c")


def test_map_path_rules():
    rules = [["D:\\OBS", "//GAMEPC/OBS"], ["D:\\OBS\\Replays", "Z:/replays"]]
    assert obs.map_path("D:\\OBS\\Replays\\a b.mkv", rules, "192.168.1.9") == "Z:/replays/a b.mkv"  # longest rule wins
    assert obs.map_path("d:/obs/x.mp4", rules, "192.168.1.9") == "//GAMEPC/OBS/x.mp4"  # case/slash-insensitive
    assert obs.map_path("E:/other.mp4", rules, "192.168.1.9") is None  # unreadable from here
    assert obs.map_path("E:/other.mp4", [], "127.0.0.1") == "E:/other.mp4"  # same PC: path works as-is
    assert obs.map_path("D:/OBSX/y.mp4", rules, "10.0.0.2") is None  # prefix must end at a folder boundary


def test_public_view_hides_password():
    assert "password" not in obs.public({"id": "x", "password": "p"}) and obs.public({"password": "p"})["has_password"]


def test_obs_event_imports_through_folder_mapping(tmp_path, monkeypatch):
    clip = tmp_path / "Replay 2.mkv"
    clip.write_bytes(b"x" * 100)
    calls = []
    monkeypatch.setattr(pipeline, "ingest_file", lambda path, name, origin, ap=None, markers=None: calls.append((path, name, origin)) or {"id": "p"})
    conn = store.put("obs", {"name": "Gaming PC", "host": "192.168.1.9", "enabled": False,
                             "path_map": [["D:/OBS", str(tmp_path)]], "autopilot": {"enabled": False}})
    obs.handle_file(conn, "D:\\OBS\\Replay 2.mkv", "replay")
    assert calls[0][0].endswith("Replay 2.mkv") and calls[0][1] == "Replay 2" and calls[0][2]["obs"] == "Gaming PC"
    assert store.get("obs", conn["id"])["imported"] == 1
    obs.handle_file(store.get("obs", conn["id"]), "C:/elsewhere/a.mkv", "replay")
    assert "can't read that folder" in store.get("obs", conn["id"])["error"] and len(calls) == 1


def test_wait_stable(tmp_path):
    f = tmp_path / "a.mp4"
    assert not relay.wait_stable(f, interval=0.05, timeout=0.3)  # missing
    f.write_bytes(b"data")
    assert relay.wait_stable(f, checks=2, interval=0.05, timeout=2)


@pytest.fixture
def receiver(monkeypatch):
    port = _free_port()
    config.save({"lan_receive": True, "lan_port": port, "lan_token": "secret-token"})
    submitted = []
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: submitted.append((kind, kw)) or {"id": "j"})
    lan.apply()
    yield f"http://127.0.0.1:{port}", port, submitted
    config.save({"lan_receive": False})
    lan.apply()


def test_relay_to_receiver_end_to_end(receiver, tmp_path):
    server, port, submitted = receiver
    assert lan.status()["running"]
    # UDP discovery answers on the same port number, via whichever NIC; 5 s because a loaded test run can miss a 2 s window
    # (discover returns as soon as it gets an answer, so this only costs time when it fails)
    found = relay.discover(timeout=5, port=port)
    assert found and found.endswith(f":{port}") and relay.ping(found, "secret-token")["ok"]
    assert relay.ping(server, "secret-token")["app"] == "reelwright"
    with pytest.raises(RuntimeError, match="rejected the token"):
        relay.ping(server, "wrong")
    video = tmp_path / "My clip (1).mp4"
    video.write_bytes(os.urandom(3 * 1024 * 1024 + 7))  # several chunks plus a remainder
    res = relay.upload(server, "secret-token", video, "Stream PC", "replay")
    proj = store.get("project", res["project_id"])
    stored = pipeline.pdir(proj["id"]) / proj["source"]
    assert stored.read_bytes() == video.read_bytes() and proj["origin"]["obs"] == "Stream PC" and proj["name"] == "My clip (1)"
    assert submitted[-1] == ("analyze", {"project_id": proj["id"]})
    with pytest.raises(PermissionError):
        relay.upload(server, "wrong", video, "x", retries=1)
    bad = tmp_path / "evil.exe"
    bad.write_bytes(b"MZ")
    with pytest.raises(PermissionError, match="Not a video"):
        relay.upload(server, "secret-token", bad, "x", retries=1)


def test_token_never_travels_and_signatures_cant_be_replayed(receiver):
    import http.client
    server, port, _ = receiver
    raw = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    raw.request("GET", "/ping", headers={"X-Reelwright-Token": "secret-token"})  # the old plain-token header no longer works
    assert raw.getresponse().status == 401
    hdrs = relay.sign("secret-token", "GET", "/ping")
    assert "secret-token" not in "".join(hdrs.values())
    for expect in (200, 401):  # the same signed request a second time (a replay) is refused
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/ping", headers=hdrs)
        r = c.getresponse()
        r.read()
        assert r.status == expect
    stale = relay.sign("secret-token", "GET", "/ping", ts=int(__import__("time").time()) - 3600)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/ping", headers=stale)
    assert c.getresponse().status == 401


def test_relay_refuses_an_impostor_receiver(monkeypatch):
    """A rogue PC answering discovery can't prove it knows the token, so the relay sends it nothing."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"ok": True, "app": "reelwright", "proof": "made-up"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with pytest.raises(RuntimeError, match="isn't the Reelwright"):
        relay.ping(f"http://127.0.0.1:{srv.server_address[1]}", "secret-token")
    srv.shutdown()


def test_received_clips_never_auto_publish_publicly(receiver, tmp_path):
    server, port, _ = receiver
    config.save({"obs_autopilot": {"enabled": True, "privacy": "public", "top_n": 2}})
    video = tmp_path / "r.mp4"
    video.write_bytes(b"x" * 2048)
    res = relay.upload(server, "secret-token", video, "PC", "replay")
    assert store.get("project", res["project_id"])["autopilot"]["privacy"] == "private"
    config.save({"obs_autopilot": {"enabled": False}})


def test_receiver_guards_disk_space(receiver, tmp_path, monkeypatch):
    server, port, _ = receiver
    from collections import namedtuple
    monkeypatch.setattr(lan.shutil, "disk_usage", lambda p: namedtuple("U", "total used free")(1, 1, 10))
    video = tmp_path / "big.mp4"
    video.write_bytes(b"x" * 4096)
    with pytest.raises(PermissionError, match="disk"):
        relay.upload(server, "secret-token", video, "PC", retries=1)


def test_map_path_blocks_dotdot_escape():
    assert obs.map_path("D:/Clips/../../Users/me/secret.mp4", [["D:/Clips", "//PC/Clips"]], "10.0.0.2") is None
    assert obs.map_path("C:/Rec/../x.mp4", [], "127.0.0.1") is None


def test_tokens_never_start_with_a_dash():
    assert all(not lan.new_token().startswith(("-", "_")) for _ in range(200))


def test_receiver_sanitizes_names_and_stops():
    assert lan.safe_name("..%2F..%2Fwindows%2Fevil.mp4") == "evil.mp4"
    assert lan.safe_name("C:\\a\\b<c>.mkv") == "b_c_.mkv"
    config.save({"lan_receive": False})
    assert not lan.apply()["running"]


def test_short_source_becomes_one_whole_clip(monkeypatch):
    p = store.put("project", {"name": "Replay 9", "meta": {"duration": 30.0, "width": 1920, "height": 1080}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    pipeline.save_json(p["id"], "transcript.json", {"segments": []})
    pipeline.save_json(p["id"], "signals.json", {"scenes": [], "loud": []})
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "")
    found = pipeline._find(p["id"], "")
    assert len(found) == 1 and found[0]["start"] == 0.0 and found[0]["end"] == 30.0
    assert pipeline._find(p["id"], "") == []  # a second scan doesn't duplicate it


def test_chat_counts(tmp_path):
    f = tmp_path / "source.live_chat.json"
    lines = [{"replayChatItemAction": {"videoOffsetTimeMsec": str(ms)}} for ms in (1000, 2000, 15000, 15500, 16000)]
    f.write_text("\n".join(map(json.dumps, lines)) + "\nnot json\n", "utf-8")
    assert pipeline.chat_counts(f, 25.0) == [2, 3, 0]


def test_lessons_record_trim_and_edits_and_feed_house_style(monkeypatch):
    p = store.put("project", {"name": "t", "meta": {"duration": 200.0, "width": 1920, "height": 1080}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    pipeline.save_json(p["id"], "transcript.json", {"segments": []})
    for d in store.find("feedback"):
        store.delete("feedback", d["id"])
    for i in range(3):
        c = pipeline.new_clip(p["id"], {"start": 10.0, "end": 40.0, "score": 70}, source="ai")
        c = store.update("clip", c["id"], {"start": 11.5, "end": 39.0, "rating": 1,
                                           "edit": {**c["edit"], "caption_style": "neon", "zoom": 1.3}})
        pipeline.remember(c, "rated")
    fb = store.get("feedback", c["id"])
    assert fb["trim"] == [1.5, -1.0] and fb["edit"]["caption_style"] == "neon" and set(fb["features"]) >= {"loud", "wps"}
    fresh = pipeline.new_clip(p["id"], {"start": 50.0, "end": 80.0, "score": 60}, source="ai")
    assert fresh["edit"]["caption_style"] == "neon" and fresh["edit"]["zoom"] == 1.3  # learned house style
    store.update("clip", c["id"], {"title": "renamed later"})
    pipeline.remember(store.get("clip", c["id"]), "refresh")
    assert store.get("feedback", c["id"])["title"] == "renamed later" and store.get("feedback", c["id"])["signal"] == 1


def test_receiver_says_when_discovery_cant_listen(monkeypatch):
    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("", port))  # something else already holds the discovery (UDP) port
    try:
        config.save({"lan_receive": True, "lan_port": port, "lan_token": "secret-token"})
        st = lan.apply()
        assert st["running"] and st["discovery"] is False and "discovery" in st["error"].lower()
    finally:
        blocker.close()
        config.save({"lan_receive": False})
        lan.apply()
