"""Phone remote: a separate, opt-in LAN listener for the phone web app. Every API call is HMAC-signed per paired device
(never the token itself), media is served only by doc id through signed short-lived URLs, and every action goes through
the same server/pipeline functions as the desktop app (ratings → learning memory, publish → account approval)."""
import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from reelwright import accounts, config, pipeline, platforms, remote, store  # noqa: E402

STATIC = Path(remote.__file__).parent / "static" / "remote"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    """Own accounts file, no real jobs, a fresh rate limiter, and the listener off and unpaired afterwards."""
    monkeypatch.setattr(accounts, "FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "LEGACY_CONN", tmp_path / "connections.json")
    monkeypatch.setattr(accounts, "LEGACY_YT", tmp_path / "youtube_token.json")
    monkeypatch.setattr(platforms, "CONN_FILE", tmp_path / "connections.json")
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: {"id": "j", "kind": kind, **kw})
    remote._hits.clear()
    remote._fails.clear()
    yield
    config.save({"remote_enabled": False, "remote_devices": []})
    remote.apply()


@pytest.fixture
def phone():
    """A running listener and one paired device: (base host, port, device id, secret)."""
    port = _free_port()
    config.save({"remote_enabled": True, "remote_port": port})
    assert remote.apply()["running"]
    dev = remote.add_device()
    pn = "5a" * 16
    st, js, _ = call(port, dev["id"], dev["secret"], "POST", "/remote/api/pair", {"name": "Test phone", "pn": pn})
    assert st == 200
    return port, dev["id"], remote.derive_key(dev["secret"], pn, js["sn"])  # the phone's key: not the QR code


def call(port, did, secret, method, path, body=None, headers=None, raw_body=None):
    """One signed request; returns (status, json, response headers). Checks the server proof on every 2xx."""
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
    h = remote.sign(did, secret, method, path, data) if secret else {}
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=data or None, headers=h)
    r = c.getresponse()
    out = r.read()
    if r.status < 300 and secret:
        assert r.getheader("X-Reelwright-Proof") == remote.proof(secret, h["X-Reelwright-Nonce"], r.status, out)
    try:
        js = json.loads(out)
    except ValueError:
        js = None
    return r.status, js, dict(r.getheaders())


def get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, r.read(), dict(r.getheaders())


def ai_clip(rendered=b""):
    p = store.put("project", {"name": "Stream", "status": "ready", "meta": {"duration": 100.0, "width": 1920, "height": 1080}})
    folder = pipeline.pdir(p["id"])
    folder.mkdir(parents=True, exist_ok=True)
    pipeline.save_json(p["id"], "transcript.json", {"segments": [{"start": 1.0, "end": 3.0, "text": "what a play"}]})
    c = pipeline.new_clip(p["id"], {"start": 0.0, "end": 30.0, "score": 80, "title": "Big play"}, source="ai")
    if rendered:
        (folder / "exports").mkdir(exist_ok=True)
        (folder / "exports" / f"{c['id']}.mp4").write_bytes(rendered)
        c = store.update("clip", c["id"], {"export": {"status": "done", "rev": c["rev"], "file": f"exports/{c['id']}.mp4"}})
    return c


# ---------------- listener rules ----------------

def test_listener_is_off_by_default_and_its_port_is_outside_the_main_range():
    assert config.DEFAULTS["remote_enabled"] is False and config.DEFAULTS["remote_devices"] == []
    port = config.DEFAULTS["remote_port"]
    assert port == 8810 and port not in range(8765, 8800) and port != 8766
    config.save({"remote_enabled": False})
    assert not remote.apply()["running"]


def test_port_must_stay_clear_of_the_main_app_and_the_lan_receiver():
    for bad in (8765, 8766, 8780, 8799, 80, 70000, config.load()["lan_port"]):
        with pytest.raises(ValueError):
            remote.check_port(bad)
    assert remote.check_port(8810) == 8810 and remote.check_port(8800) == 8800
    with pytest.raises(Exception) as e:
        remote.put_remote({"port": 8770})
    assert getattr(e.value, "status_code", None) == 400
    assert config.load()["remote_port"] != 8770


def test_only_private_and_link_local_clients_are_served():
    for ip in ("192.168.1.5", "10.0.0.9", "172.16.4.2", "172.31.255.1", "169.254.10.1", "127.0.0.1", "100.101.102.103", "fd00::1", "fe80::1"):
        assert remote.client_allowed(ip), ip
    for ip in ("8.8.8.8", "1.1.1.1", "172.32.0.1", "203.0.113.9", "::ffff:8.8.8.8", "2001:4860::8888", "not-an-ip", ""):
        assert not remote.client_allowed(ip), ip



def test_connections_are_capped_per_address(monkeypatch):
    """A LAN host opening (or trickling) many connections can't take every thread: strangers are dropped before a byte is read."""
    monkeypatch.setattr(remote, "MAX_CONNS_PER_IP", 2)
    srv = remote.Server(("127.0.0.1", 0), remote.Handler)
    socks = [socket.socket() for _ in range(5)]
    try:
        assert srv.verify_request(socks[0], ("8.8.8.8", 1)) is False
        assert [srv.verify_request(s, ("192.168.0.20", 1)) for s in socks[1:4]] == [True, True, False]
        srv.shutdown_request(socks[3])  # a refused connection never held a slot
        assert srv.verify_request(socks[4], ("192.168.0.21", 1))  # another phone isn't affected
        assert not srv.verify_request(socket.socket(), ("192.168.0.20", 1))
        srv.shutdown_request(socks[1])  # a finished connection frees its slot
        assert srv.verify_request(socket.socket(), ("192.168.0.20", 1))
    finally:
        srv.server_close()
        for s in socks:
            s.close()


# ---------------- signatures ----------------

def test_signed_requests_wrong_token_replay_expired_and_tampered_body_are_refused(phone):
    port, did, secret = phone
    st, js, _ = call(port, did, secret, "GET", "/remote/api/dashboard")
    assert st == 200 and "paused" in js
    assert call(port, did, "wrong-secret", "GET", "/remote/api/dashboard")[0] == 401
    assert call(port, "nosuchdev", secret, "GET", "/remote/api/dashboard")[0] == 401
    assert get(port, "/remote/api/dashboard")[0] == 401  # unsigned
    # a replay of a captured request is refused
    h = remote.sign(did, secret, "GET", "/remote/api/dashboard", b"")
    assert get(port, "/remote/api/dashboard", h)[0] == 200
    assert get(port, "/remote/api/dashboard", h)[0] == 401
    # expired: the 401 says the server time so a phone with a wrong clock can correct itself
    old = remote.sign(did, secret, "GET", "/remote/api/dashboard", b"", ts=int(time.time()) - 3600)
    st, body, _ = get(port, "/remote/api/dashboard", old)
    assert st == 401 and abs(json.loads(body)["time"] - time.time()) < 5
    # tampered body: signed for one rating, sent with another
    c = ai_clip()
    signed = remote.sign(did, secret, "POST", f"/remote/api/clips/{c['id']}/rate", json.dumps({"rating": 1}).encode())
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", f"/remote/api/clips/{c['id']}/rate", body=json.dumps({"rating": -1}).encode(), headers=signed)
    assert conn.getresponse().status == 401 and store.get("clip", c["id"])["rating"] == 0
    # tampered path: a signature for one clip doesn't work on another
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/remote/api/clips/other/rate", body=json.dumps({"rating": 1}).encode(), headers=signed)
    assert conn.getresponse().status == 401


def test_an_unpaired_device_can_only_pair_and_its_code_expires(phone, monkeypatch):
    port, _, _ = phone
    dev = remote.add_device()
    assert call(port, dev["id"], dev["secret"], "GET", "/remote/api/dashboard")[0] == 401  # not paired yet
    later = remote.add_device()
    assert remote.verify_device(later["id"])
    real = time.time
    monkeypatch.setattr(remote.time, "time", lambda: real() + remote.PAIR_TTL_S + 60)
    assert remote.verify_device(later["id"]) is None  # never paired within 15 min: the code is dead


def test_repeated_bad_signatures_get_rate_limited(phone, monkeypatch):
    port, did, secret = phone
    monkeypatch.setattr(remote, "MAX_FAILS", 3)
    for _ in range(3):
        assert call(port, did, "nope", "GET", "/remote/api/dashboard")[0] == 401
    assert call(port, did, secret, "GET", "/remote/api/dashboard")[0] == 429


def test_body_size_is_capped_and_there_is_no_cors(phone):
    port, did, secret = phone
    big = b"{" + b" " * (remote.MAX_BODY + 10) + b"}"
    assert call(port, did, secret, "POST", "/remote/api/import", raw_body=big)[0] == 413
    st, _, hdrs = call(port, did, secret, "GET", "/remote/api/dashboard", headers={"Origin": "http://evil.example"})
    assert "Access-Control-Allow-Origin" not in hdrs
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("OPTIONS", "/remote/api/dashboard", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    r = c.getresponse()
    assert r.status >= 400 and r.getheader("Access-Control-Allow-Origin") is None


# ---------------- the phone's JS crypto matches Python ----------------

@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_hmac_matches_python_for_test_vectors():
    import hashlib
    import hmac
    vectors = [  # (key bytes, message bytes): RFC 4231 cases, empty, multi-block, long keys and UTF-8
        (b"\x0b" * 20, b"Hi There"), (b"Jefe", b"what do ya want for nothing?"), (b"\xaa" * 20, b"\xdd" * 50),
        (bytes(range(1, 26)), b"\xcd" * 50), (b"\xaa" * 131, b"Test Using Larger Than Block-Size Key - Hash Key First"),
        (b"\xaa" * 131, b"This is a test using a larger than block-size key and a larger than block-size data. The key needs to be hashed before being used by the HMAC algorithm."),
        (b"", b""), (b"k", b"a" * 55), (b"k", b"a" * 56), (b"k", b"a" * 64), (b"k" * 64, b"b" * 1000),
        ("clé🔑".encode(), "naïve message — 😀 emoji".encode()),
    ]
    script = r"""
const C = require(process.argv[1]);
const vs = JSON.parse(process.argv[2]);
const out = vs.map(([k, m]) => [C.sha256Hex(Buffer.from(m, "hex")), C.hmacHex(Buffer.from(k, "hex"), Buffer.from(m, "hex"))]);
out.push(C.hmacHex("clé🔑", "naïve message — 😀 emoji"));
const s = C.signature("s3cret", "POST", "/remote/api/clips/ab12/rate", 1700000000, "n0nce", '{"rating":1}');
out.push(C.deriveKey("code-from-qr", "aa11", "bb22"));
const p = C.proofOk("s3cret", "n0nce", 200, Buffer.from('{"ok":true}'), process.argv[3]);
const bad = C.proofOk("s3cret", "n0nce", 200, Buffer.from('{"ok":false}'), process.argv[3]);
console.log(JSON.stringify({out, s, p, bad}));
"""
    good_proof = remote.proof("s3cret", "n0nce", 200, b'{"ok":true}')
    res = subprocess.run(["node", "-e", script, str(STATIC / "hmac.js"), json.dumps([[k.hex(), m.hex()] for k, m in vectors]), good_proof],
                         capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    for (k, m), (sha, mac) in zip(vectors, got["out"]):
        assert sha == hashlib.sha256(m).hexdigest() and mac == hmac.new(k, m, hashlib.sha256).hexdigest()
    assert got["out"][-2] == hmac.new("clé🔑".encode(), "naïve message — 😀 emoji".encode(), hashlib.sha256).hexdigest()
    py = remote.sign("dev", "s3cret", "POST", "/remote/api/clips/ab12/rate", b'{"rating":1}', ts=1700000000, nonce="n0nce")
    assert got["s"] == py["X-Reelwright-Sig"] and got["p"] is True and got["bad"] is False
    assert got["out"][-1] == remote.derive_key("code-from-qr", "aa11", "bb22")


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_phone_scripts_parse():
    for f in ("app.js", "hmac.js", "sw.js"):
        assert subprocess.run(["node", "--check", str(STATIC / f)], capture_output=True).returncode == 0, f


# ---------------- media ----------------

def test_signed_media_urls_expire_bind_the_id_and_support_ranges(phone):
    port, did, secret = phone
    data = bytes(range(256)) * 40
    c = ai_clip(rendered=data)
    url = remote.media_url(did, "video", c["id"])
    assert "/remote/media/video/" in url and secret not in url
    st, body, hdrs = get(port, url)
    assert st == 200 and body == data and hdrs["Accept-Ranges"] == "bytes" and hdrs["Content-Type"] == "video/mp4"
    st, body, hdrs = get(port, url, {"Range": "bytes=10-19"})
    assert st == 206 and body == data[10:20] and hdrs["Content-Range"] == f"bytes 10-19/{len(data)}"
    st, body, _ = get(port, url, {"Range": "bytes=-5"})
    assert st == 206 and body == data[-5:]
    st, body, _ = get(port, url, {"Range": "bytes=100-"})
    assert st == 206 and body == data[100:]
    assert get(port, url, {"Range": f"bytes={len(data) + 5}-"})[0] == 416
    other = ai_clip(rendered=b"other clip")
    assert get(port, url.replace(c["id"], other["id"]))[0] == 403  # the signature is for one id only
    assert get(port, url.replace("/video/", "/cover/"))[0] == 403
    expired = remote.media_url(did, "video", c["id"], ttl=-1)
    fails = len(remote._fails.get("127.0.0.1", []))
    assert get(port, expired)[0] == 403
    assert len(remote._fails.get("127.0.0.1", [])) == fails  # a genuine link that merely expired isn't an attack
    assert get(port, url.replace("e=", "e=9"))[0] == 403  # a stretched expiry breaks the signature
    assert get(port, remote.media_url(did, "poster", c["project_id"]))[0] == 404  # no poster on disk: nothing else served
    assert get(port, "/remote/media/video/" + c["id"])[0] == 403  # unsigned


# ---------------- actions go through the desktop's own paths ----------------

def test_review_keep_and_skip_write_the_same_feedback_as_desktop_rating(phone):
    port, did, secret = phone
    keep, skip = ai_clip(), ai_clip()
    st, js, _ = call(port, did, secret, "GET", "/remote/api/review")
    ids = [x["id"] for x in js["clips"]]
    assert st == 200 and keep["id"] in ids and skip["id"] in ids
    card = next(x for x in js["clips"] if x["id"] == keep["id"])
    assert "what a play" in card["snippet"] and card["video"] is None  # not rendered: poster + transcript instead
    assert call(port, did, secret, "POST", f"/remote/api/clips/{keep['id']}/rate", {"rating": 1})[0] == 200
    assert call(port, did, secret, "POST", f"/remote/api/clips/{skip['id']}/rate", {"rating": -1})[0] == 200
    fk, fs = store.get("feedback", keep["id"]), store.get("feedback", skip["id"])
    assert (fk["signal"], fk["why"]) == (1, "rated") and (fs["signal"], fs["why"]) == (-1, "rated")
    assert store.get("clip", keep["id"])["rating"] == 1
    ids = [x["id"] for x in call(port, did, secret, "GET", "/remote/api/review")[1]["clips"]]
    assert keep["id"] not in ids and skip["id"] not in ids
    assert call(port, did, secret, "POST", f"/remote/api/clips/{keep['id']}/rate", {"rating": 0})[0] == 200  # undo
    assert store.get("feedback", keep["id"]) is None
    assert call(port, did, secret, "POST", f"/remote/api/clips/{keep['id']}/rate", {"rating": 5})[0] == 400


def test_publish_goes_only_to_approved_accounts(phone):
    port, did, secret = phone
    c = ai_clip()
    unapproved = accounts.add("youtube", name="Second channel", access_token="T")
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/publish", {"accounts": [unapproved["id"]], "title": "t"})
    assert st == 400 and "approved" in js["detail"] and not store.find("upload", clip_id=c["id"])
    ok = accounts.update(accounts.add("youtube", name="Main", access_token="T")["id"], {"approved": True})
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/publish",
                     {"accounts": [ok["id"]], "title": "Big play", "privacy": "private"})
    assert st == 200 and [u["account_id"] for u in js["uploads"]] == [ok["id"]]
    assert store.find("upload", clip_id=c["id"])[0]["status"] in ("waiting_render", "queued")
    tt = accounts.update(accounts.add("tiktok", name="TT", access_token="T")["id"], {"approved": True})
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/publish", {"accounts": [tt["id"]], "title": "t"})
    assert st == 400 and "TikTok" in js["detail"]  # TikTok needs its own posting choices: the PC's Publish dialog


def test_delete_follows_the_pipeline_delete_rules(phone):
    port, did, secret = phone
    c = ai_clip()
    store.put("upload", {"clip_id": c["id"], "project_id": c["project_id"], "platform": "youtube", "status": "uploading"})
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/delete", {})
    assert st == 200 and js["ok"] is False and store.get("clip", c["id"])  # an upload in flight refuses the delete
    for u in store.find("upload", clip_id=c["id"]):
        store.delete("upload", u["id"])
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/delete", {})
    assert st == 200 and js["ok"] is True and store.get("clip", c["id"]) is None


# ---------------- devices ----------------

def test_revoking_a_device_ends_its_api_and_media_access(phone):
    port, did, secret = phone
    c = ai_clip(rendered=b"video bytes")
    url = remote.media_url(did, "video", c["id"])
    assert get(port, url)[0] == 200
    st = remote.local_status()  # the PC's card lists it with name and last-seen time, never the secret
    dev = next(d for d in st["devices"] if d["id"] == did)
    assert dev["name"] == "Test phone" and dev["paired"] and dev["last_seen"] and secret not in json.dumps(st)
    remote.revoke(did)
    assert call(port, did, secret, "GET", "/remote/api/dashboard")[0] == 401
    assert get(port, url)[0] == 403
    assert not [d for d in remote.local_status()["devices"] if d["id"] == did]


def test_pairing_code_is_single_use_and_stops_being_the_key(phone):
    """Whoever photographs the QR later is too late: pairing turns the code into a different key, and the code is dead."""
    port, _, _ = phone
    dev = remote.add_device()
    assert call(port, dev["id"], dev["secret"], "POST", "/remote/api/pair", {"name": "No nonce"})[0] == 400
    st, js, _ = call(port, dev["id"], dev["secret"], "POST", "/remote/api/pair", {"name": "Mine", "pn": "ab" * 16})
    key = remote.derive_key(dev["secret"], "ab" * 16, js["sn"])
    assert st == 200 and key != dev["secret"] and key not in json.dumps(js)
    assert call(port, dev["id"], key, "GET", "/remote/api/dashboard")[0] == 200
    assert call(port, dev["id"], dev["secret"], "POST", "/remote/api/pair", {"name": "Thief", "pn": "cd" * 16})[0] == 401
    assert call(port, dev["id"], dev["secret"], "GET", "/remote/api/dashboard")[0] == 401
    assert call(port, dev["id"], key, "POST", "/remote/api/pair", {"name": "Renamed", "pn": "ef" * 16})[0] == 409
    assert next(d for d in remote.local_status()["devices"] if d["id"] == dev["id"])["name"] == "Mine"


def test_import_refuses_links_to_this_network_in_any_spelling(phone, monkeypatch):
    port, did, key = phone
    real, fake = socket.getaddrinfo, {"evil.example": "192.168.1.1", "videos.example": "93.184.216.34"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (fake[h], 0))] if h in fake else real(h, *a, **k))
    for bad in ("http://127.0.0.1:8765/api/settings", "http://localhost./x", "http://0.0.0.0:8765/", "http://evil.example/v", "http://[::1]/", "no link"):
        assert call(port, did, key, "POST", "/remote/api/import", {"url": bad})[0] == 400, bad
    st, js, _ = call(port, did, key, "POST", "/remote/api/import", {"url": "Watch this https://videos.example/v/1"})
    assert st == 200 and store.get("project", js["project_id"])


def test_pairing_link_carries_the_code_only_in_the_fragment():
    st = remote.pair_link(None, "192.168.1.20")
    assert st["url"].startswith(f"http://192.168.1.20:{config.load()['remote_port']}/#pair=")
    assert st["svg"].startswith("<svg") and st["device_id"]
    again = remote.pair_link(st["device_id"], "192.168.1.20")
    assert again["device_id"] == st["device_id"] and again["url"] == st["url"]  # same pending device, not a new one
    with pytest.raises(ValueError):
        remote.pair_link(None, "8.8.8.8")


# ---------------- nothing sensitive reaches the phone ----------------

@pytest.fixture
def secrets_set():
    """A key, a LAN token and an OBS password on this PC; all put back afterwards (other tests must never see a key)."""
    old = {k: config.load()[k] for k in ("anthropic_api_key", "lan_token")}
    config.save({"anthropic_api_key": "sk-ant-SUPERSECRET", "lan_token": "LANSECRET123"})
    conn = store.put("obs", {"name": "Stream PC", "host": "192.168.1.30", "port": 4455, "password": "OBSPASSWORD",
                             "enabled": False, "status": "connected"})
    job = store.put("job", {"kind": "export", "status": "failed", "finished": time.time(), "message": "Failed",
                            "error": f"ffmpeg: {config.DATA}\\projects\\abc\\source.mp4: Invalid data; also \\\\NAS\\share\\clips\\x.mp4"})
    yield
    store.delete("job", job["id"])
    store.delete("obs", conn["id"])
    config.save(old)


def test_no_settings_keys_tokens_or_files_are_exposed(phone, secrets_set):
    port, did, secret = phone
    acc = accounts.update(accounts.add("youtube", name="Main", token={"token": "YTTOKEN", "refresh_token": "YTREFRESH"})["id"], {"approved": True})
    c = ai_clip(rendered=b"x")
    store.put("upload", {"clip_id": c["id"], "project_id": c["project_id"], "platform": "youtube", "status": "done",
                         "account_id": acc["id"], "url": "https://youtu.be/abc", "uploaded_at": pipeline.now_iso()})
    blobs = []
    for path in ("/remote/api/dashboard", "/remote/api/review", "/remote/api/projects", f"/remote/api/projects/{c['project_id']}",
                 "/remote/api/board", "/remote/api/obs", "/remote/api/activity", f"/remote/api/clips/{c['id']}"):
        st, js, _ = call(port, did, secret, "GET", path)
        assert st == 200, path
        blobs.append(json.dumps(js))
    blob = "".join(blobs)
    for secret_value in ("SUPERSECRET", "LANSECRET123", "OBSPASSWORD", "YTTOKEN", "YTREFRESH", secret, json.dumps(str(config.DATA))[1:-1],
                         "NAS", "source.mp4"):
        assert secret_value not in blob, secret_value
    assert "Stream PC" in blob and "Invalid data" in blob  # listed, minus password, address and file paths
    assert "192.168.1.30" not in blob
    assert "/files/" not in blob and "youtu.be/abc" in blob  # posted links yes; raw file paths no
    for path in ("/api/settings", "/remote/api/settings", "/files/x", "/settings.json", "/../settings.json", "/%2e%2e/settings.json",
                 "/remote/..%2Fapp.js", "/legal/privacy.html", "/app.js/../../config.py"):
        assert get(port, path)[0] in (401, 404), path
        assert call(port, did, secret, "GET", path)[0] in (401, 404), path
    st, body, hdrs = get(port, "/")
    assert st == 200 and b"<html" in body and "frame-ancestors 'none'" in hdrs["Content-Security-Policy"]
    assert get(port, "/manifest.webmanifest")[0] == 200 and get(port, "/hmac.js")[0] == 200
    assert "remote_devices" not in config.public()  # the PC's settings page never carries the phones' secrets
