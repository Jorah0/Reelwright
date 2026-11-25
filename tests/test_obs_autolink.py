"""OBS auto-link: an enabled connection reconnects by itself whenever Reelwright or OBS (re)starts, says clearly why it
isn't connected (waiting for OBS vs wrong password), and keeps its password sealed at rest. Runs against the in-process
fake obs-websocket v5 server from test_obs; retry waits are recorded (obs._pause patched), never really slept."""
import os
import tempfile
import threading
import time

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from websockets.sync.server import serve  # noqa: E402

from reelwright import obs, store, vault  # noqa: E402
from test_obs import PASSWORD, _free_port, obs_handler  # noqa: E402


class FakeOBS:
    """The fake OBS on a fixed port that can be started and stopped (= OBS opened / closed) any number of times."""

    def __init__(self, password=PASSWORD):
        self.port, self.password, self.state, self.seen, self.conns, self.srv = _free_port(), password, {"replay_event": False}, [], [], None

    def start(self):
        inner = obs_handler(self.seen, self.state, self.password)

        def handler(ws):
            self.conns.append(ws)
            inner(ws)
        self.srv = serve(handler, "127.0.0.1", self.port)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):  # OBS closed: listener gone and every live connection dropped
        if self.srv:
            self.srv.shutdown()
            for ws in self.conns:
                ws.close()
            self.srv = None


def until(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def waits(monkeypatch):
    """Retry waits are recorded instead of slept (a 10 ms nap keeps the watcher from spinning)."""
    got = []

    def pause(cid, rev, seconds):
        doc = store.get("obs", cid) or {}
        if doc.get("name") == "Test OBS":  # only this file's connections (a stray watcher must not skew the counts)
            got.append(seconds)
        time.sleep(0.01)
    monkeypatch.setattr(obs, "_pause", pause)
    return got


@pytest.fixture
def made():
    """Connection docs a test creates: turned off, their watchers ended, and deleted afterwards (the store is restored)."""
    ids = []
    yield ids
    for i in ids:
        store.update("obs", i, {"enabled": False})
    until(lambda: not any(obs._threads.get(i) for i in ids), 10)
    for i in ids:
        store.delete("obs", i)


@pytest.fixture
def fake():
    f = FakeOBS()
    yield f
    f.stop()


def conn(made, port, **kw):
    doc = store.put("obs", {"name": "Test OBS", "host": "127.0.0.1", "port": port, "password": vault.seal(PASSWORD),
                            "enabled": True, "import_replays": False, "import_recordings": False, "path_map": [],
                            "autopilot": {"enabled": False}, "status": "connecting", "error": "", "rev": 1, **kw})
    made.append(doc["id"])
    return doc["id"]


def status(cid):
    return store.get("obs", cid)["status"]


def test_stale_connected_status_is_reset_at_startup(made):
    """A crash or kill leaves "connected" (and a recording) on the doc: until the watcher has really reconnected, marks
    must not land on that stale recording and the UI must not claim a live link."""
    on = conn(made, 1, status="connected", rec={"start": time.time() - 60, "pauses": [], "marks": []})
    off = conn(made, 1, enabled=False, status="connected")
    obs.reset_status()
    assert status(on) == "connecting" and status(off) == "off"
    assert not [c for c in obs.recording() if c["id"] in (on, off)]


def test_enabled_link_reconnects_at_startup_with_no_user_action(made, fake, waits):
    fake.start()
    cid = conn(made, fake.port, status="connected")  # left over from the last run
    obs.reset_status()
    obs.reconcile()
    assert until(lambda: status(cid) == "connected" and "GetRecordStatus" in fake.seen)
    assert store.get("obs", cid)["error"] == ""


def test_obs_started_after_the_app_is_picked_up_within_the_local_cap(made, fake, waits):
    cid = conn(made, fake.port)
    obs.reconcile()
    assert until(lambda: len(waits) >= 6)  # several retries while OBS is closed
    doc = store.get("obs", cid)
    assert doc["status"] == "waiting" and doc["enabled"]
    assert max(waits) <= obs.LOCAL_CAP <= 10  # this PC: never more than a few seconds between tries
    fake.start()  # OBS opens
    assert until(lambda: status(cid) == "connected")


def test_retrying_while_obs_is_closed_writes_the_status_only_when_it_changes(made, waits, monkeypatch):
    cid = conn(made, _free_port())  # nothing listens there: OBS is closed
    writes, real = [], store.update
    monkeypatch.setattr(store, "update", lambda kind, id, *a, **k: (writes.append(a) if id == cid else None) or real(kind, id, *a, **k))
    obs.reconcile()
    assert until(lambda: len(waits) >= 6)  # several retries, every 5 s in real life
    assert status(cid) == "waiting" and len(writes) == 1  # connecting -> waiting once, not once per retry


def test_remote_hosts_back_off_longer():
    assert obs._delay("192.168.1.50", "offline", 64) == obs.REMOTE_CAP == 60
    assert obs._delay("127.0.0.1", "waiting", 64) == obs.LOCAL_CAP
    assert obs._delay("127.0.0.1", "auth", 2) >= 30  # a wrong password is retried slowly, wherever OBS is


def test_obs_restart_reconnects_and_keeps_the_recording_being_followed(made, fake, waits):
    """OBS (or Reelwright) restarts while OBS records: the reconnect keeps the recording's clock and its marks."""
    fake.state.update(recording=True, duration_ms=100_000)
    mark = {"at": time.time() - 50, "kind": "obs", "label": "m", "strength": 1.0}
    cid = conn(made, fake.port, rec={"start": time.time() - 100, "pauses": [], "marks": [mark]})
    fake.start()
    obs.reconcile()
    assert until(lambda: status(cid) == "connected")
    assert store.get("obs", cid)["rec"]["marks"] == [mark]
    fake.stop()  # OBS closed (or crashed)
    assert until(lambda: status(cid) == "waiting")
    fake.start()
    assert until(lambda: status(cid) == "connected")
    rec = store.get("obs", cid)["rec"]
    assert rec["marks"] == [mark] and abs(rec["start"] - (time.time() - 100)) < 15


def test_wrong_password_says_so_and_retries_slowly(made, waits):
    f = FakeOBS(password="the-real-one").start()
    try:
        cid = conn(made, f.port)  # sealed PASSWORD, which this OBS doesn't accept
        obs.reconcile()
        assert until(lambda: status(cid) == "auth" and waits)
        doc = store.get("obs", cid)
        assert "password" in doc["error"].lower() and doc["enabled"] and min(waits) >= 30
    finally:
        f.stop()


def test_obs_closing_mid_request_reads_as_waiting_not_a_crash(fake):
    """OBS shutting down right as Reelwright asks it something (e.g. _sync_recording after connecting)."""
    from reelwright.relay import OBS, OBSNotRunning
    fake.start()
    o = OBS(f"ws://127.0.0.1:{fake.port}", PASSWORD, events=False).connect()
    try:
        fake.stop()
        with pytest.raises(OBSNotRunning):
            o.request("GetRecordStatus")
    finally:
        o.close()


def test_turning_a_link_back_on_shows_connecting_not_paused(made):
    from fastapi.testclient import TestClient

    from reelwright.server import PORT, app
    cid = conn(made, 1, enabled=False, status="off")
    r = TestClient(app, base_url=f"http://127.0.0.1:{PORT}").patch(f"/api/obs/{cid}", json={"enabled": True})
    assert r.json()["status"] == "connecting"
    store.update("obs", cid, {"enabled": False})


def test_a_disabled_link_never_connects(made, fake, waits):
    fake.start()
    cid = conn(made, fake.port, enabled=False, status="off")
    obs.reset_status()
    obs.reconcile()
    time.sleep(0.5)
    assert not obs._threads.get(cid) and not fake.conns and status(cid) == "off"


def test_password_sealed_at_rest_and_never_in_api_output(made, monkeypatch):
    from fastapi.testclient import TestClient

    from reelwright.server import PORT, app
    c = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    secret = "s3cret-obs-pass"
    r = c.post("/api/obs", json={"host": "127.0.0.1", "password": secret, "enabled": False})
    assert r.status_code == 200
    cid = r.json()["id"]
    made.append(cid)
    raw = store.get("obs", cid)["password"]
    assert raw != secret and (vault.is_sealed(raw) or not vault.on()) and obs.password(store.get("obs", cid)) == secret
    r2 = c.patch(f"/api/obs/{cid}", json={"name": "Renamed", "password": ""})  # blank on edit = keep the saved one
    assert obs.password(store.get("obs", cid)) == secret and r2.json()["has_password"]
    for out in (r.text, r2.text, c.get("/api/obs").text, c.get("/api/connections").text):
        assert secret not in out and raw not in out and '"password"' not in out
    # migration: a plain-text password saved by an older Reelwright is sealed by the vault's start-up pass
    old = conn(made, 1, password="plain-old", enabled=False)
    obs.reseal()
    assert obs.password(store.get("obs", old)) == "plain-old"
    assert vault.is_sealed(store.get("obs", old)["password"]) or not vault.on()


def test_a_reconcile_exception_does_not_kill_the_loop(monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("store hiccup")
    monkeypatch.setattr(obs, "reconcile", boom)
    obs._tick()
    obs._tick()  # still alive: the next tick tries again
    assert len(calls) == 2


def test_startup_continues_to_obs_when_an_earlier_step_fails(monkeypatch):
    """server._start: a failing pipeline/storage start-up step must not keep OBS from linking."""
    from reelwright import config, lan, log, pipeline, remote, server, storage
    started = []
    monkeypatch.setattr(log, "setup", lambda: None)
    monkeypatch.setattr(config, "migrate_ports", lambda: [])
    monkeypatch.setattr(pipeline, "start", lambda: (_ for _ in ()).throw(RuntimeError("bad job doc")))
    monkeypatch.setattr(storage, "start", lambda: (_ for _ in ()).throw(OSError("drive gone")))
    monkeypatch.setattr(obs, "start", lambda: started.append("obs"))
    monkeypatch.setattr(lan, "apply", lambda: started.append("lan"))
    monkeypatch.setattr(remote, "apply", lambda: started.append("remote"))
    server._start()
    assert started == ["obs", "lan", "remote"]
