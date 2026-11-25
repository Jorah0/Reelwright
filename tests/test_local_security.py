"""The main app's per-launch token and headers, settings/preset trust boundaries, the LAN receiver's caps, the phone
remote's AI budget, demo-mode guards."""
import socket
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from reelwright import config, lan, metrics, pipeline, platforms, providers, relay, remote, server, store
from reelwright import routes_presets as rp

BASE = f"http://127.0.0.1:{server.PORT}"


def anon(**k):
    return TestClient(server.app, base_url=BASE, headers={}, follow_redirects=False, **k)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- M1: the main API needs the launch token ----------

def test_api_and_files_refuse_a_local_process_without_the_token():
    c = anon()
    for method, path in (("GET", "/api/settings"), ("POST", "/api/remote/pair"), ("GET", "/api/obs"),
                         ("PATCH", "/api/accounts/x"), ("GET", "/files/p1/transcript.json")):
        assert c.request(method, path, json={}).status_code == 403, path
    assert c.get("/api/settings", headers={server.TOKEN_HEADER: "wrong"}).status_code == 403
    assert c.get("/api/settings", headers={server.TOKEN_HEADER: server.TOKEN}).status_code == 200


def test_ui_shell_and_the_tiktok_callback_need_no_token():
    c = anon()
    assert c.get("/").status_code == 200 and c.get("/app.js").status_code == 200
    r = c.get("/oauth/tiktok/callback?error=access_denied")  # opened by the system browser; its own state check applies
    assert r.status_code == 200 and "Not connected" in r.text


def test_launch_link_sets_a_strict_httponly_cookie_and_leaves_the_url():
    c = anon()
    r = c.get(f"/?rw_token={server.TOKEN}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert c.get("/api/settings").status_code == 200  # the cookie now authenticates
    assert anon().get("/?rw_token=guess").status_code == 303  # a bad code: the UI without it (see the stale-link test)


def test_desktop_opens_files_in_the_system_browser_with_a_one_time_ticket(monkeypatch):
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    c = TestClient(server.app, base_url=BASE)
    for bad in ("https://evil.example/x", "/api/settings", "//evil.example/files/x"):
        assert c.post("/api/app/open", json={"path": bad}).status_code == 400
    assert c.post("/api/app/open", json={"path": "/files/p1/cover.jpg?r=2"}).status_code == 200
    assert opened[0].startswith(f"{BASE}/files/p1/cover.jpg?r=2&rw_token=")
    link = opened[0][len(BASE):]
    r = anon().get(link)
    assert r.status_code == 303 and r.headers["location"] == "/files/p1/cover.jpg?r=2" and "httponly" in r.headers["set-cookie"].lower()
    assert anon().get(link).status_code == 403  # used once


def test_token_file_for_local_tools_is_written_at_startup():
    server._publish_token()
    assert config.app_token().split(",")[0] == server.TOKEN


# ---------- M2: no framing, no sniffing, a CSP ----------

def test_ui_cannot_be_framed_and_has_a_csp():
    for path in ("/", "/api/settings"):
        r = TestClient(server.app, base_url=BASE).get(path)
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        csp = r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp and "'unsafe-eval'" not in csp


# ---------- L7: /api/settings can't set sealed or listener keys ----------

def test_settings_route_refuses_sealed_and_listener_keys():
    c = TestClient(server.app, base_url=BASE)
    keys = {"lan_token": "evil", "remote_devices": [{"id": "x", "secret": "y"}], "gsi_token": "g", "vts_token": "v",
            "media_keys": {"giphy": "k"}, "lan_port": 1, "lan_receive": True, "remote_port": 2, "remote_enabled": True,
            "remote_dev_sim": True, "gsi_port": 3, "gsi_enabled": True, "vts_port": 4, "vts_enabled": True}
    before = config.load()
    assert c.put("/api/settings", json=keys).status_code == 200
    after = config.load()
    assert {k: after[k] for k in keys} == {k: before[k] for k in keys}
    try:  # the Claude key is still set here (Settings → AI)
        c.put("/api/settings", json={"anthropic_api_key": "sk-ant-test"})
        assert config.api_key() == "sk-ant-test"
    finally:
        config.save({"anthropic_api_key": ""})


# ---------- L4: an imported preset can't reroute the AI ----------

def test_imported_preset_drops_ai_routing():
    got = rp.import_preset({"reelwright_preset": 1, "name": "shared",
                            "parts": {"app": {"clip_min": 10, "clip_max": 40, "ai_routes": {"find": "openai"}}}})
    try:
        assert got["parts"]["app"] == {"clip_min": 10.0, "clip_max": 40.0}
    finally:
        store.delete("preset", got["id"])


# ---------- L9: a paired phone's paid AI calls are budgeted per device ----------

def test_phone_ai_calls_have_a_per_device_hourly_budget(monkeypatch):
    monkeypatch.setattr(server, "metadata", lambda cid: {"title": "t", "tags": []})
    monkeypatch.setattr(server, "find", lambda pid, body: {"id": "j"})
    monkeypatch.setattr(remote, "_ai_calls", {})
    a, b = {"id": "phoneA"}, {"id": "phoneB"}
    for i in range(remote.AI_PER_HOUR):
        assert remote._call(remote._meta if i % 2 else remote._find, a, {}, "x")[0] == 200
    assert remote._call(remote._meta, a, {}, "x")[0] == 429
    assert remote._call(remote._find, a, {}, "x")[0] == 429
    assert remote._call(remote._meta, b, {}, "x")[0] == 200  # another phone has its own budget


# ---------- M3 / L5 / L6: the LAN receiver ----------

@pytest.fixture
def receiver(monkeypatch):
    port = _free_port()
    config.save({"lan_receive": True, "lan_port": port, "lan_token": "secret-token"})
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: {"id": "j"})
    lan.apply()
    yield f"http://127.0.0.1:{port}"
    config.save({"lan_receive": False})
    lan.apply()


def test_lan_receiver_drops_strangers_and_caps_connections(receiver):
    srv = lan._state["http"]
    assert isinstance(srv, remote.Server)  # private-network filter + per-address connection caps before a byte is read
    assert not srv.verify_request(object(), ("8.8.8.8", 5000))
    if sys.platform == "win32":
        assert not srv.allow_reuse_address


def test_lan_receiver_strips_control_characters_from_the_source(receiver, tmp_path):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"x" * 2048)
    res = relay.upload(receiver, "secret-token", video, "PC\r\nfake log line\x1b[2J", "replay")
    src = store.get("project", res["project_id"])["origin"]["obs"]
    assert src.isprintable() and "fake log line" in src


# ---------- L13: demo mode ----------

def test_demo_mode_never_reaches_a_provider_or_platform(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    with pytest.raises(RuntimeError, match="Demo"):
        providers.ask("openai", {"model": "m", "base_url": "https://api.openai.com/v1"}, "s", "u", object)
    with pytest.raises(RuntimeError, match="Demo"):
        platforms.tiktok_creator("acc")
    with pytest.raises(RuntimeError, match="Demo"):
        metrics.fetch({"platform": "youtube"}, ["v1"])
    with pytest.raises(RuntimeError, match="Demo"):
        metrics.youtube_retention("acc", "v1", "2026-01-01", 30.0)


def test_demo_phone_listener_stays_on_this_pc(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    config.save({"remote_enabled": True, "remote_port": _free_port()})
    try:
        remote.apply()
        assert remote._state["http"].server_address[0] == "127.0.0.1"
    finally:
        config.save({"remote_enabled": False})
        remote.apply()


def test_ai_budget_raises_429():
    with pytest.raises(HTTPException) as e:
        remote._ai_budget({"id": "z"}, limit=0)
    assert e.value.status_code == 429


# ---------- review fixes: the asset library needs the token; a stale launch link; bad upload options ----------

def test_the_asset_library_needs_the_token_like_files_do():
    assert anon().get("/assets/anything.png").status_code == 403
    assert anon().get("/assets/fonts/x.ttf", headers={server.TOKEN_HEADER: "wrong"}).status_code == 403
    assert TestClient(server.app, base_url=BASE).get("/assets/nope.png").status_code == 404  # signed in: served (none here)


def test_a_stale_launch_link_on_the_ui_drops_the_code_instead_of_a_bare_403():
    r = anon().get("/?rw_token=stale&tab=lib")
    assert r.status_code == 303 and r.headers["location"] == "/?tab=lib" and "set-cookie" not in r.headers
    assert anon().get("/?rw_token=stale").headers["location"] == "/"
    assert anon().get(f"{BASE}//evil.example/x?rw_token=stale").headers["location"] == "/evil.example/x"  # never off-site
    for path in ("/api/settings", "/files/p1/cover.jpg", "/assets/x.png"):  # data stays a refusal
        assert anon().get(f"{path}?rw_token=stale").status_code == 403, path


def test_bad_autopilot_options_on_an_upload_are_a_400_before_the_body_is_read(monkeypatch):
    read = []
    monkeypatch.setattr(server, "_receive", lambda *a, **k: read.append(1))
    r = TestClient(server.app, base_url=BASE).post("/api/projects?name=a.mp4&autopilot={nope", content=b"x")
    assert r.status_code == 400 and not read
    r = TestClient(server.app, base_url=BASE).post("/api/projects?name=a.mp4&autopilot=[1]", content=b"x")
    assert r.status_code == 400 and not read
