"""Review fixes: the relay script in the frozen .exe (and every data file the code reads next to itself is bundled), the
desktop log's size cap and the silenced Windows connection-reset noise, training artifacts (checkpoint after export,
delete routes, Space saver pruning, never the active/rollback release), an old build still running on the 8765 block,
and a label for every job kind. Each test on its own in-memory store, settings file and folders."""
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import config, desktop, log, pipeline, remote, routes_training, server, storage, store  # noqa: E402
from reelwright.models import selections  # noqa: E402
from reelwright.training import dataset, runner  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "reelwright"
DAY = 86400.0
NOW = time.time()


@pytest.fixture
def mem(tmp_path, monkeypatch):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("CREATE TABLE docs (kind TEXT, id TEXT, created REAL, data TEXT, PRIMARY KEY (kind, id))")
    monkeypatch.setattr(store, "_db", db)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(runner, "jobs_dir", lambda: tmp_path / "training" / "jobs")
    monkeypatch.setattr(dataset, "root", lambda: tmp_path / "training" / "datasets")
    return tmp_path


@pytest.fixture
def api():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


# ---------- 1. the relay script ships in the .exe ----------

def test_relay_script_resolves_inside_the_frozen_bundle(tmp_path, monkeypatch, api):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert server.relay_file() == tmp_path / "reelwright" / "relay.py"
    r = api.get("/api/obs/relay.py")
    assert r.status_code == 404 and "relay" in r.json()["detail"].lower()  # a clear message, not a 500
    (tmp_path / "reelwright").mkdir()
    (tmp_path / "reelwright" / "relay.py").write_text("print('relay')", "utf-8")
    r = api.get("/api/obs/relay.py")
    assert r.status_code == 200 and r.text == "print('relay')" and "reelwright_relay.py" in r.headers["content-disposition"]


def test_relay_script_from_a_source_checkout(monkeypatch, api):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert server.relay_file() == PKG / "relay.py"
    assert api.get("/api/obs/relay.py").status_code == 200


# every module that reads a file next to its own source, and the package paths it reads: each must be an --add-data of
# build_exe.py (compiled modules only end up in the .exe). A new reader fails here until both are updated.
READERS = {"config.py": ["reelwright/static"], "server.py": ["reelwright/relay.py"], "runner.py": ["reelwright/training/worker.py"],
           "ai.py": [], "desktop.py": [], "mcp_server.py": []}  # site-packages scan / source-checkout paths: nothing bundled


def test_every_file_read_from_the_package_dir_is_bundled_into_the_exe():
    build = (ROOT / "scripts" / "build_exe.py").read_text("utf-8")
    bundled = set()
    for src, dest in re.findall(r'"--add-data", f"\{ROOT / (.+?)\}\{SEP\}([\w/]+)"', build):
        rel = "/".join(re.findall(r"'([^']+)'", src))
        assert (ROOT / rel).exists(), rel
        assert dest == str(Path(rel).parent if (ROOT / rel).is_file() else Path(rel)).replace("\\", "/"), (rel, dest)
        bundled.add(rel)
    readers = {p.name for p in PKG.rglob("*.py") if re.search(r"__file__|_MEIPASS", p.read_text("utf-8"))}
    assert readers <= set(READERS), f"new module reading files beside itself: add to build_exe --add-data and READERS: {readers - set(READERS)}"
    need = {p for v in READERS.values() for p in v}
    assert need <= bundled, f"not bundled into the .exe: {need - bundled}"


# ---------- 2. the desktop log stays small ----------

def test_the_desktop_log_rolls_over_at_its_cap_and_keeps_three(tmp_path):
    f = tmp_path / "reelwright.log"
    f.write_text("x" * 150, "utf-8")  # a log already over the cap from an earlier run rolls over on the first write
    s = log.RotatingStream(f, max_bytes=100, backups=3)
    s.write("first\n")
    assert (tmp_path / "reelwright.log.1").read_text("utf-8") == "x" * 150 and f.read_text("utf-8") == "first\n"
    for i in range(60):
        print(f"[download] {i:03d}% of 1.2GiB", file=s)
    s.flush()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["reelwright.log", "reelwright.log.1", "reelwright.log.2", "reelwright.log.3"]
    assert all(p.stat().st_size <= 100 for p in tmp_path.iterdir())  # the 150-byte file from before has rolled off
    assert "059%" in f.read_text("utf-8") and not s.isatty()
    s.close()


@pytest.mark.skipif(sys.platform != "win32", reason="WinError 10054 only exists on Windows")
def test_only_the_windows_connection_reset_noise_is_silenced():
    loop = asyncio.new_event_loop()
    try:
        seen = []
        loop.default_exception_handler = seen.append
        log.quiet_connection_resets(loop)
        reset = OSError(None, "An existing connection was forcibly closed by the remote host", None, 10054)
        assert isinstance(reset, ConnectionResetError) and reset.winerror == 10054
        loop.call_exception_handler({"message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
                                     "exception": reset})
        assert seen == []
        other = [{"message": "boom", "exception": ValueError("x")}, {"message": "no exception"},
                 {"message": "reset", "exception": ConnectionResetError(104, "posix reset")}]
        for ctx in other:
            loop.call_exception_handler(ctx)
        assert seen == other
    finally:
        loop.close()


def test_the_server_installs_the_reset_filter_at_startup():
    assert server._quiet_resets in server.app.router.on_startup
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(server._quiet_resets())  # inside the loop uvicorn serves on
        assert getattr(loop.get_exception_handler(), "__name__", "") == "_ignore_reset"
    finally:
        loop.close()


# ---------- 3. training artifacts ----------

def _job(kind="train", status="done", age=30, ckpt=True, gguf=True, **extra):
    j = store.put("train_job", {"kind": kind, "status": status, "spec": extra.pop("spec", {"mode": "lora"}), "gpu": True,
                                "created": NOW - age * DAY, "finished": NOW - age * DAY if status == "done" else None, **extra})
    d = runner.jobs_dir() / j["id"]
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    if ckpt:
        (d / "checkpoint.pt").write_bytes(b"c" * 1200)
    if gguf:
        (d / "model-q4_k_m.gguf").write_bytes(b"g" * 300)
    return store.update("train_job", j["id"], {"dir": str(d)})


def _release(rid, job, status="candidate", selection=None, dataset_hash=None):
    return store.put("train_release", {"id": rid, "status": status, "tag": f"reelwright-specialist:{rid}", "selection": selection,
                                       "hashes": {"dataset": dataset_hash}, "manifest": {"train_job": job["id"]}})


def test_export_frees_the_checkpoint_but_paused_or_interrupted_jobs_keep_theirs(mem, monkeypatch):
    src = _job(result={"dataset": "h", "dataset_dir": str(mem)})
    steps = routes_training._export_steps({"spec": {"train_job": src["id"]}})
    label, fn = steps[-1]
    assert "checkpoint" in label.lower() and [n for n, _ in steps].index("Record the release") < len(steps) - 1
    d = Path(src["dir"])
    (d / "merged").mkdir()
    (d / "merged" / "model.safetensors").write_bytes(b"w" * 500)  # full HF weights (LoRA: rebuilt from the adapter)
    (d / "model-f16.gguf").write_bytes(b"f" * 400)  # the converter's output, only the quantizer's input
    assert fn({}) == {"checkpoint_freed": 1200, "export_freed": 900} and not (d / "checkpoint.pt").exists()
    assert not (d / "merged").exists() and not (d / "model-f16.gguf").exists()
    assert (d / "model-q4_k_m.gguf").exists()  # the export itself stays (the release record hashes it)
    for status in ("paused", "failed", "canceled", "running"):
        j = _job(status=status)
        assert runner.drop_checkpoint(j) == {} and (Path(j["dir"]) / "checkpoint.pt").exists()


def test_delete_job_route_refuses_live_and_protected_jobs_and_stays_inside_the_jobs_folder(mem, api, monkeypatch):
    for status in ("running", "paused", "queued"):
        j = _job(status=status)
        r = api.delete(f"/api/training/jobs/{j['id']}")
        assert r.status_code == 400 and Path(j["dir"]).exists() and store.get("train_job", j["id"])
    src = _job()
    exp = _job(kind="export", status="running", spec={"train_job": src["id"]}, ckpt=False, gguf=False)
    assert api.delete(f"/api/training/jobs/{src['id']}").status_code == 400  # its export is still running
    store.update("train_job", exp["id"], {"status": "done"})
    _release("rc001", src, status="active")
    r = api.delete(f"/api/training/jobs/{src['id']}")
    assert r.status_code == 400 and "active" in r.json()["detail"] and Path(src["dir"]).exists()
    prev = _job()
    store.put(selections.REL, {"id": "find", "task": "find", "history": ["sel-old", "sel-new"], "active": "sel-new"})
    _release("rc000", prev, status="retired", selection="sel-old")  # Roll back returns to it
    assert api.delete(f"/api/training/jobs/{prev['id']}").status_code == 400 and Path(prev["dir"]).exists()
    outside = mem / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k")
    odd = _job()
    store.update("train_job", odd["id"], {"dir": str(outside)})  # a doc edited by hand must not steer the delete
    assert api.delete(f"/api/training/jobs/{odd['id']}").status_code == 200
    assert (outside / "keep.txt").exists() and not (runner.jobs_dir() / odd["id"]).exists() and not store.get("train_job", odd["id"])
    assert api.delete("/api/training/jobs/..%5C..%5Cx").status_code in (400, 404)
    assert api.delete("/api/training/jobs/nope00000000").status_code == 404
    gone = _job(status="failed")
    r = api.delete(f"/api/training/jobs/{gone['id']}")
    assert r.status_code == 200 and r.json()["bytes"] == 1200 + 300 + 2 and not Path(gone["dir"]).exists()


def test_delete_candidate_route_never_removes_the_active_or_rollback_release(mem, api):
    j = _job()
    (Path(j["dir"]) / "release-rc002.json").write_text("{}")
    store.put(selections.REL, {"id": "find", "task": "find", "history": ["s1", "s2"], "active": "s2"})
    _release("rc001", j, status="retired", selection="s1")
    _release("rc003", j, status="active", selection="s2")
    _release("rc002", j)
    for rid in ("rc001", "rc003"):
        assert api.delete(f"/api/training/releases/{rid}").status_code == 400 and store.get("train_release", rid)
    assert api.delete("/api/training/releases/rc002").status_code == 200
    assert not store.get("train_release", "rc002") and not (Path(j["dir"]) / "release-rc002.json").exists()
    assert api.delete("/api/training/releases/rc999").status_code == 404


def _ds(h, age, mem):
    d = dataset.root() / h[:16]
    d.mkdir(parents=True)
    (d / "train.jsonl").write_text("t" * 40)
    (d / "manifest.json").write_text("{}")
    os.utime(d / "manifest.json", (NOW - age * DAY, NOW - age * DAY))
    return store.put("train_dataset", {"id": h, "dir": str(d), "created": NOW - age * DAY})


def test_space_saver_prunes_old_training_files_but_never_live_protected_or_in_use_ones(mem, monkeypatch):
    config.save({"retention_mode": "delete", "retention_days": 14, "retention_since": "2020-01-01T00:00:00+00:00",
                 "retention_scope": {**storage.SCOPE_DEFAULTS, "training": True}})  # opt-in
    old, fresh, paused = _job(age=30), _job(age=2), _job(status="paused", age=30)
    kept = _job(age=30, result={"dataset": "b" * 64})
    store.put(selections.REL, {"id": "find", "task": "find", "history": ["s1"], "active": "s1"})
    _release("rc001", kept, status="active", selection="s1")
    stale, used, newest = _ds("a" * 64, 30, mem), _ds("b" * 64, 30, mem), _ds("c" * 64, 30, mem)
    store.update("train_dataset", newest["id"], {"created": NOW})  # the latest build stays (Model Lab shows it)
    u = storage.usage()["training"]
    assert u["jobs"] > 0 and u["exports"] == 4 * 300 and u["datasets"] > 0
    storage.cleanup(now=NOW)
    assert not Path(old["dir"]).exists() and not store.get("train_job", old["id"])
    assert all(Path(j["dir"]).exists() for j in (fresh, paused, kept))
    assert not (dataset.root() / stale["id"][:16]).exists() and store.get("train_dataset", stale["id"])  # lineage stays
    assert (dataset.root() / used["id"][:16]).exists() and (dataset.root() / newest["id"][:16]).exists()
    config.save({"retention_scope": {**storage.SCOPE_DEFAULTS, "training": False}})
    storage.cleanup(now=NOW + 100 * DAY)
    assert Path(fresh["dir"]).exists()  # unticked: training files stay


def test_training_files_stay_when_the_space_saver_is_off(mem):
    j = _job(age=300)
    storage.cleanup(now=NOW)
    assert Path(j["dir"]).exists()


# ---------- 4. an old build on the 8765 block ----------

def test_an_old_copy_on_the_old_ports_is_found_with_http_checks_only_on_ports_in_use(monkeypatch):
    checked = []
    monkeypatch.setattr(desktop, "_port_in_use", lambda p: p in (8765, 8771))
    monkeypatch.setattr(desktop, "_is_reelwright", lambda p: checked.append(p) or p == 8771)
    assert desktop.find_old_copy() == 8771 and checked == [8765, 8771]  # 8765: e.g. Lodestar, not us
    assert 8766 not in desktop.OLD_APP_PORTS and desktop.OLD_APP_PORTS == [8765, *range(8767, 8800)]
    monkeypatch.setattr(desktop, "_port_in_use", lambda p: False)
    assert desktop.find_old_copy() is None


def test_a_second_launch_shows_the_old_copy_alerts_and_starts_no_server(monkeypatch):
    calls, alerts = [], []
    monkeypatch.setattr(sys, "argv", ["Reelwright.exe"])
    monkeypatch.setenv("REELWRIGHT_PORT", "38765")  # main() writes the port it picked here
    monkeypatch.setattr(desktop, "pick_port", lambda want: (38765, False))
    monkeypatch.setattr(desktop, "find_old_copy", lambda: 8771)
    monkeypatch.setattr(desktop.urllib.request, "urlopen", lambda req, timeout=0: calls.append(req.full_url))
    monkeypatch.setattr(desktop, "_alert", alerts.append)
    monkeypatch.setattr(desktop.threading, "Thread", lambda *a, **k: pytest.fail("a second server was started"))
    with pytest.raises(SystemExit):
        desktop.main()
    assert calls == ["http://127.0.0.1:8771/api/app/show"] and "older" in alerts[0].lower() and "close" in alerts[0].lower()


def test_only_a_reelwright_settings_reply_counts_as_reelwright(monkeypatch):
    class Reply:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self, n=-1):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(desktop.urllib.request, "urlopen", lambda url, timeout=0: Reply(b'{"ok": true}'))
    assert not desktop._is_reelwright(8765)  # another local app answering /api/settings
    monkeypatch.setattr(desktop.urllib.request, "urlopen", lambda url, timeout=0: Reply(json.dumps(config.public()).encode()))
    assert desktop._is_reelwright(8765)


# ---------- 5. every job kind has a label ----------

def test_every_job_kind_has_a_label_in_the_ui_and_the_phone_remote():
    kinds = set(pipeline.HANDLERS)  # server imported: routes register theirs (hud_scan)
    assert {"angle", "hud_scan", "longform_capture", "longform_export", "cleanup", "proxy"} <= kinds
    js = (PKG / "static" / "app.js").read_text("utf-8")
    table = re.search(r"const JOB_LABEL = \{(.*?)\};", js, re.S).group(1)
    assert not kinds - set(re.findall(r"(\w+):\s*\"", table)), "app.js JOB_LABEL"
    assert not kinds - set(remote.JOB_LABEL), "remote.JOB_LABEL"


# ---------- review fixes: links open outside the job object; training files a candidate needs ----------

def test_links_open_outside_the_job_so_quitting_never_kills_the_browser(monkeypatch):
    import webbrowser
    assert desktop.JOB_LIMITS & 0x2000 and desktop.JOB_LIMITS & 0x800  # kill on close; an explicit breakaway may leave
    assert not desktop.JOB_LIMITS & 0x1000  # never SILENT_BREAKAWAY: ffmpeg & co. must stay in the job
    runs = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **k: runs.append((cmd, k)))
    assert desktop.open_url('https://example.com/a b"c&d=1')
    cmd, k = runs[0]
    assert k["creationflags"] & 0x01000000 and not k.get("shell")  # CREATE_BREAKAWAY_FROM_JOB, no shell
    assert desktop.RUNDLL.lower().endswith(r"\system32\rundll32.exe") and cmd.startswith(f'"{desktop.RUNDLL}" ')  # full path
    assert cmd.endswith(" url.dll,FileProtocolHandler https://example.com/a%20b%22c&d=1")  # one argument, nothing to split
    for bad in ("file:///C:/Windows/System32/calc.exe", r"C:\x.exe", "javascript:alert(1)", "", "ms-settings:"):
        assert not desktop.open_url(bad), bad
    assert len(runs) == 1
    monkeypatch.setattr(webbrowser, "_tryorder", None)  # every webbrowser.open (server, pywebview, the YouTube sign-in)
    monkeypatch.setattr(webbrowser, "_browsers", {})
    desktop.route_browser()
    assert webbrowser.open("http://127.0.0.1:9/files/x?rw_token=t") and runs[-1][0].endswith(" http://127.0.0.1:9/files/x?rw_token=t")


def test_space_saver_keeps_a_job_any_release_still_points_at_and_is_opt_in(mem):
    assert storage.SCOPE_DEFAULTS["training"] is False  # Model Lab files only go when the creator ticks them
    config.save({"retention_mode": "delete", "retention_days": 14, "retention_since": "2020-01-01T00:00:00+00:00",
                 "retention_scope": {**storage.SCOPE_DEFAULTS, "training": True}})
    cand, retired, old = _job(age=30, result={"dataset": "a" * 64}), _job(age=30), _job(age=30)
    _release("rc001", cand)  # exported, not yet released: its GGUF is what Release would load
    _release("rc002", retired, status="retired")
    ds = _ds("a" * 64, 30, mem)
    _ds("c" * 64, 0, mem)  # the newest build
    storage.cleanup(now=NOW)
    assert Path(cand["dir"]).exists() and Path(retired["dir"]).exists() and store.get("train_job", cand["id"])
    assert (dataset.root() / ds["id"][:16]).exists()
    assert not Path(old["dir"]).exists()
    config.save({"retention_mode": "archive", "archive_other_dir": str(mem / "arch")})  # archive mode deletes nothing
    j = _job(age=30)
    storage.cleanup(now=NOW)
    assert Path(j["dir"]).exists()
