"""Storage folder + space saver (storage.py): choosing and moving the projects folder, every path user following it, a
missing removable drive, retention (delete / archive) and each of its safety rules, re-download / re-import of a removed
source, and importing old shorts. No real waiting: a fixed clock (now=...), fake downloads and tiny fake video files,
each test on its own in-memory store, settings file and projects folder."""
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import config, longform, multicam, pipeline, server, storage, store  # noqa: E402

DAY = 86400.0
NOW = time.time()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def day(t: float) -> str:  # archive names carry the local date
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def jobs(tmp_path, monkeypatch):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("CREATE TABLE docs (kind TEXT, id TEXT, created REAL, data TEXT, PRIMARY KEY (kind, id))")
    monkeypatch.setattr(store, "_db", db)
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "DEFAULT_PROJECTS", tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    monkeypatch.setattr(storage, "_move", {**storage._move, "active": False, "error": "", "message": ""})
    seen = []

    def submit(kind, **kw):
        seen.append({"kind": kind, **kw})
        return {"id": f"j{len(seen)}", "kind": kind, "status": "queued", **kw}
    monkeypatch.setattr(pipeline, "submit", submit)
    return seen


@pytest.fixture
def api():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


def policy(mode="delete", days=14, since=NOW - 100 * DAY, **scope):
    config.save({"retention_mode": mode, "retention_days": days, "retention_since": iso(since),
                 "retention_scope": {**storage.SCOPE_DEFAULTS, **scope}})


def project(age_days=20, url="https://example.com/v", origin=None, name="Stream", **fields):
    p = store.put("project", {"name": name, "status": "ready", "source": "source.mp4", "created": NOW - age_days * DAY,
                              "meta": {"duration": 100.0, "width": 1920, "height": 1080, "has_audio": True},
                              "source_url": url, "origin": origin, **fields})
    d = pipeline.pdir(p["id"])
    d.mkdir(parents=True)
    (d / "source.mp4").write_bytes(b"v" * 1000)
    (d / "transcript.json").write_text('{"segments": []}')
    (d / "signals.json").write_text("{}")
    (d / "poster.jpg").write_bytes(b"j")
    return p


def clip(p, rendered=True, age_days=20, posted=False, **fields):
    c = pipeline.new_clip(p["id"], {"start": 0.0, "end": 30.0, "title": "Big play", "hook": "Watch this", "hashtags": ["#gg"],
                                    "score": 80}, source="ai")
    at = NOW - age_days * DAY
    patch = {"created": at, **fields}
    if rendered:
        ex = pipeline.pdir(p["id"]) / "exports"
        ex.mkdir(exist_ok=True)
        (ex / f"{c['id']}.mp4").write_bytes(b"r" * 500)
        (ex / f"{c['id']}.jpg").write_bytes(b"c")
        patch["export"] = {"status": "done", "rev": c["rev"], "file": f"exports/{c['id']}.mp4", "cover": f"exports/{c['id']}.jpg",
                           "at": iso(at)}
    c = store.update("clip", c["id"], patch)
    if posted:
        store.put("upload", {"clip_id": c["id"], "project_id": p["id"], "platform": "youtube", "status": "done", "account_id": None,
                             "uploaded_at": iso(at), "url": "https://youtu.be/abc", "caption": "Watch this #gg", "title": c["title"]})
    return c


def has_source(p):
    return (pipeline.pdir(p["id"]) / "source.mp4").exists()


def has_render(c):
    return (pipeline.pdir(c["project_id"]) / "exports" / f"{c['id']}.mp4").exists()


# ---------------- storage folder ----------------

def test_folder_validation(tmp_path, monkeypatch):
    target = tmp_path / "new drive" / "Reelwright"
    assert storage.validate(str(target)) == target.resolve() and not target.exists()  # checked without creating anything
    afile = tmp_path / "afile"
    afile.write_text("x")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "win"))
    for path, why in [("", "choose"), ("relative/dir", "full path"), (str(config.projects_dir()), "already"),
                      (str(config.projects_dir() / "sub"), "inside"), (str(afile), "not a folder"),
                      (str(tmp_path / "win" / "videos"), "system")]:
        with pytest.raises(ValueError, match=f"(?i){why}"):
            storage.validate(path)
    project()
    monkeypatch.setattr(storage, "_free", lambda p: 10)
    with pytest.raises(ValueError, match="(?i)space"):
        storage.validate(str(target))
    monkeypatch.setattr(storage, "_free", lambda p: 10 ** 15)
    monkeypatch.setattr(storage, "_writable", lambda p: False)
    with pytest.raises(ValueError, match="(?i)can't write"):
        storage.validate(str(target))


def test_usage_splits_sources_renders_and_other():
    p = project()
    clip(p)
    u = storage.usage()
    assert (u["sources"], u["renders"]) == (1000, 501) and u["other"] > 0 and u["total"] == sum(u[k] for k in ("sources", "renders", "other"))


def test_move_copies_verifies_then_deletes_and_switches(tmp_path):
    p = project()
    c = clip(p)
    old = pipeline.pdir(p["id"])
    dest = tmp_path / "big drive" / "Reelwright"
    storage.start_move(str(dest)).join(10)
    assert config.projects_dir() == dest.resolve() and pipeline.pdir(p["id"]) == dest.resolve() / p["id"]
    assert (dest / p["id"] / "source.mp4").read_bytes() == b"v" * 1000 and (dest / p["id"] / "exports" / f"{c['id']}.mp4").exists()
    assert not old.exists() and config.load()["storage_move"] == {}
    assert storage.status()["move"]["error"] == "" and not storage.status()["move"]["active"]
    storage.start_move(str(config.DEFAULT_PROJECTS)).join(10)  # and back to the default: stored as "" (follows DATA)
    assert config.load()["storage_dir"] == "" and has_source(p)


def test_move_is_refused_while_renders_or_uploads_run(tmp_path):
    project()
    store.put("job", {"kind": "export", "status": "running", "clip_id": "x"})
    with pytest.raises(RuntimeError, match="(?i)wait"):
        storage.start_move(str(tmp_path / "d"))
    assert config.projects_dir() == config.DEFAULT_PROJECTS and config.load()["storage_move"] == {}


def test_an_interrupted_move_loses_nothing_and_resumes(tmp_path, monkeypatch):
    p1, p2 = project(), project()
    dest = tmp_path / "dest"
    real, n = storage._copy_file, {"i": 0}

    def flaky(a, b):
        n["i"] += 1
        if n["i"] == 3:
            raise OSError("cable pulled")
        return real(a, b)
    monkeypatch.setattr(storage, "_copy_file", flaky)
    storage.start_move(str(dest)).join(10)
    assert config.projects_dir() == config.DEFAULT_PROJECTS and has_source(p1) and has_source(p2)  # still on the old folder
    assert "cable pulled" in storage.status()["move"]["error"] and config.load()["storage_move"] == {}
    # the app was closed mid-copy: the move stays recorded (old folder too) and finishes at the next start
    monkeypatch.setattr(storage, "_copy_file", real)
    config.save({"storage_move": {"from": str(config.DEFAULT_PROJECTS), "to": str(dest.resolve()), "phase": "copy"}})
    assert "moving" in storage.hold_reason().lower()  # jobs that need the files wait meanwhile
    storage.resume().join(10)
    assert config.projects_dir() == dest.resolve() and all((dest / p["id"] / "source.mp4").exists() for p in (p1, p2))
    assert not (config.DEFAULT_PROJECTS / p1["id"]).exists() and storage.hold_reason() == ""


def test_resume_after_the_switch_verifies_each_copy_before_deleting(tmp_path):
    p = project()
    old, dest = config.DEFAULT_PROJECTS, tmp_path / "dest"
    shutil.copytree(old / p["id"], dest / p["id"])
    (dest / p["id"] / "poster.jpg").unlink()  # the copy lacks a file
    (old / p["id"] / "music").mkdir()
    (old / p["id"] / "music" / "late.mp3").write_bytes(b"m")  # written to the old folder after the copy
    config.save({"storage_dir": str(dest), "storage_move": {"from": str(old), "to": str(dest), "phase": "cleanup"}})
    storage.resume().join(10)
    assert (dest / p["id"] / "poster.jpg").exists() and (dest / p["id"] / "music" / "late.mp3").exists()
    assert not (old / p["id"]).exists() and config.load()["storage_move"] == {}


def test_every_path_user_follows_the_new_root(api, tmp_path):
    p = project()
    other = tmp_path / "usb"
    other.mkdir()
    config.save({"storage_dir": str(other)})
    d = pipeline.pdir(p["id"])
    assert d == other.resolve() / p["id"]
    d.mkdir()
    (d / "poster.jpg").write_bytes(b"new")
    (other / "private.txt").write_text("not a project file")
    assert api.get(f"/files/{p['id']}/poster.jpg").content == b"new"  # no restart: /files follows the setting
    assert api.get("/files/private.txt").status_code == 404  # only project folders are served
    assert api.get(f"/files/{p['id']}/%2e%2e/private.txt").status_code == 404
    with pytest.raises(ValueError):
        pipeline.pdir("..")
    aid = "a" * 12
    assert multicam.angle_path(p["id"], {"id": aid, "file": f"angles/{aid}.mp4"}).parent == d / "angles"
    lf = longform.create(p["id"], "clips", "t", [[0, 10]], [])
    (d / "exports").mkdir()
    (d / "exports" / "lf.mp4").write_bytes(b"x")
    lf = store.update("longform", lf["id"], {"export": {"status": "done", "rev": 1, "file": "exports/lf.mp4"}})
    assert longform.export_current(lf)
    config.save({"storage_dir": ""})
    assert not longform.export_current(lf)  # back on the default folder, that render isn't there


def test_a_missing_folder_is_flagged_never_replaced(tmp_path, monkeypatch, jobs):
    gone = tmp_path / "unplugged"
    config.save({"storage_dir": str(gone)})
    assert storage.missing() and config.public()["storage_missing"]
    assert config.projects_dir() == gone.resolve()  # still points at the drive: no silent fallback
    with pytest.raises(RuntimeError, match="(?i)storage folder"):
        pipeline.new_project("x")
    assert not gone.exists() and store.find("project") == []
    # a job that needs the files waits (it doesn't fail) until the drive is back
    j = store.put("job", {"kind": "analyze", "status": "queued", "progress": 0, "message": "Queued", "project_id": "p"})
    ran, waits = [], []
    monkeypatch.setitem(pipeline.HANDLERS, "analyze", lambda job: ran.append(job["id"]))

    def sleep(s):
        waits.append(store.get("job", j["id"])["message"])
        gone.mkdir()
    monkeypatch.setattr(pipeline.time, "sleep", sleep)
    pipeline._run_job(j["id"])
    assert ran == [j["id"]] and "storage folder" in waits[0].lower()


def test_switching_away_from_a_missing_folder_moves_nothing(tmp_path):
    config.save({"storage_dir": str(tmp_path / "unplugged")})
    new = tmp_path / "fresh"
    assert storage.start_move(str(new)) is None
    assert config.projects_dir() == new.resolve() and new.is_dir() and not storage.missing()


# ---------------- space saver ----------------

def test_only_due_downloaded_sources_go_and_analysis_and_renders_stay(monkeypatch):
    events = []
    monkeypatch.setattr(storage.log, "event", lambda kind, msg: events.append((kind, msg)))
    policy(unfinished=False, uploaded=False, longform=False)
    old, fresh = project(age_days=20), project(age_days=5)
    obs = project(url=None, origin={"obs": "PC", "kind": "replay"})
    uploaded = project(url=None)
    linked, own = "b" * 12, "c" * 12
    store.update("project", old["id"], {"angles": [{"id": linked, "file": f"angles/{linked}.mp4", "status": "ready", "source_url": "https://x/y"},
                                                   {"id": own, "file": f"angles/{own}.mp4", "status": "ready"}]})
    (pipeline.pdir(old["id"]) / "angles").mkdir()
    for a in (linked, own):
        (pipeline.pdir(old["id"]) / "angles" / f"{a}.mp4").write_bytes(b"a")
    c = clip(old)
    storage.cleanup(now=NOW)
    assert not has_source(old) and has_source(fresh) and has_source(obs) and has_source(uploaded)
    assert not (pipeline.pdir(old["id"]) / "angles" / f"{linked}.mp4").exists()  # a downloaded camera angle follows its project
    assert (pipeline.pdir(old["id"]) / "angles" / f"{own}.mp4").exists()  # the creator's own recording is an original: kept
    d = pipeline.pdir(old["id"])
    assert all((d / f).exists() for f in ("transcript.json", "signals.json", "poster.jpg")) and has_render(c)
    assert store.get("project", old["id"])["source_removed"]["mode"] == "delete" and not store.get("clip", c["id"]).get("expired")
    assert any(k == "retention" and old["id"] in m for k, m in events)
    policy(local_sources=True, unfinished=False, uploaded=False, longform=False)  # opting in to local originals
    storage.cleanup(now=NOW)
    assert not has_source(obs) and not has_source(uploaded) and has_source(fresh)


def test_off_unchosen_and_zero_days_never_touch_anything(jobs, monkeypatch):
    p = project(age_days=400)
    c = clip(p, posted=True, age_days=400)
    monkeypatch.setattr(storage, "_boot", 0.0)
    for s in ({"retention_mode": ""}, {"retention_mode": "off"}, {"retention_mode": "delete", "retention_days": 0}):
        config.save({"retention_days": 14, **s, "retention_since": iso(NOW - 500 * DAY)})
        assert storage.preview(now=NOW)["items"] == [] and storage.cleanup(now=NOW)["done"] == []
        storage.tick()
    assert has_source(p) and has_render(c) and not [j for j in jobs if j["kind"] == "cleanup"]


def test_the_daily_tick_runs_once_after_startup_then_daily(jobs, monkeypatch):
    policy()
    monkeypatch.setattr(storage, "_boot", time.time())
    storage.tick()
    assert jobs == []  # not in the first minutes after a start
    monkeypatch.setattr(storage, "_boot", 0.0)
    monkeypatch.setattr(storage, "_ran", False)
    storage.tick()
    config.save({"retention_at": pipeline.now_iso()})
    storage.tick()
    assert [j["kind"] for j in jobs] == ["cleanup"]


def test_first_run_grace_nothing_already_on_disk_goes_on_day_one(api):
    p = project(age_days=400)
    c = clip(p, posted=True, age_days=400)
    assert api.put("/api/settings", json={"retention_mode": "delete", "retention_since": "2000-01-01T00:00:00+00:00"}).status_code == 200
    since = datetime.fromisoformat(config.load()["retention_since"]).timestamp()
    assert abs(since - time.time()) < 60  # the clock starts when the creator chose, never earlier
    assert storage.cleanup(now=time.time())["done"] == [] and has_source(p) and has_render(c)
    assert storage.cleanup(now=time.time() + 15 * DAY)["done"] and not has_source(p) and not has_render(c)


def test_keep_pins_exempt_clips_long_forms_and_whole_projects(api):
    policy()
    p = project()
    kept, other = clip(p), clip(p)
    lf = longform.create(p["id"], "clips", "Long", [[0, 10]], [])
    assert api.patch(f"/api/clips/{kept['id']}", json={"keep": True}).json()["keep"] is True
    assert api.patch(f"/api/longform/{lf['id']}", json={"keep": True}).json()["keep"] is True
    storage.cleanup(now=NOW)
    assert has_render(kept) and not has_render(other) and store.get("clip", other["id"])["expired"]
    assert not store.get("longform", lf["id"]).get("expired")
    q = project()
    qc = clip(q)
    assert api.patch(f"/api/projects/{q['id']}", json={"keep": True}).json()["keep"] is True
    storage.cleanup(now=NOW)
    assert has_source(q) and has_render(qc) and not store.get("clip", qc["id"]).get("expired")
    # "Keep" on an expired clip restores it (and pins it)
    r = api.patch(f"/api/clips/{other['id']}", json={"keep": True}).json()
    assert r["keep"] and not r.get("expired")


def test_pending_posts_and_running_jobs_are_never_touched():
    policy()
    p = project()
    sched, waiting = clip(p), clip(p)
    store.put("upload", {"clip_id": sched["id"], "project_id": p["id"], "status": "scheduled", "platform": "youtube"})
    store.put("upload", {"clip_id": waiting["id"], "project_id": p["id"], "status": "waiting_render", "platform": "youtube"})
    busy = project()
    bc = clip(busy)
    store.put("job", {"kind": "track", "status": "running", "clip_id": bc["id"]})
    pre = storage.preview(now=NOW)
    assert {b["id"] for b in pre["blocked"]} >= {sched["id"], waiting["id"], p["id"], busy["id"], bc["id"]}
    storage.cleanup(now=NOW)
    assert has_render(sched) and has_render(waiting) and has_source(p)  # a pending post holds its project's source too
    assert has_source(busy) and has_render(bc)


def test_archive_moves_files_with_readable_names_sidecars_and_a_folder_per_status(tmp_path):
    up, unf = tmp_path / "Uploaded", tmp_path / "Unfinished"
    policy(mode="archive")
    config.save({"archive_uploaded_dir": str(up), "archive_unfinished_dir": str(unf)})
    up.mkdir()
    unf.mkdir()
    p = project(name="My: Stream?")
    posted, draft, twin = clip(p, posted=True), clip(p), clip(p)
    storage.cleanup(now=NOW)
    d = day(NOW - 20 * DAY)
    f = up / f"{d} Big play [youtube].mp4"
    assert f.read_bytes() == b"r" * 500 and not has_render(posted)
    side = json.loads(f.with_suffix(".json").read_text("utf-8"))
    assert side["title"] == "Big play" and side["hashtags"] == ["#gg"] and "Watch this" in side["caption"]
    assert side["posts"][0]["url"] == "https://youtu.be/abc"
    assert (unf / f"{d} Big play.mp4").exists() and (unf / f"{d} Big play (2).mp4").exists()  # deduped, never overwritten
    assert not has_render(draft) and not has_render(twin) and store.get("clip", twin["id"])["expired"]["mode"] == "archive"
    assert store.get("clip", posted["id"])["expired"]["archive"] == str(f)
    assert (up / f"{d} My Stream [source].mp4").read_bytes() == b"v" * 1000  # sources default to the uploaded folder
    assert not has_source(p) and store.get("project", p["id"])["source_removed"]["mode"] == "archive"


def test_archive_verifies_the_copy_and_never_deletes_without_a_folder(tmp_path, monkeypatch):
    policy(mode="archive")
    p = project()
    c = clip(p, posted=True)
    storage.cleanup(now=NOW)  # no archive folder chosen: nothing is deleted instead
    assert has_render(c) and has_source(p) and not store.get("clip", c["id"]).get("expired")
    assert any("folder" in b["reason"].lower() for b in storage.preview(now=NOW)["blocked"])
    a = tmp_path / "A"
    a.mkdir()
    config.save({"archive_uploaded_dir": str(a)})
    monkeypatch.setattr(storage, "_copy_file", lambda src, dst: Path(dst).write_bytes(b"short"))
    storage.cleanup(now=NOW)
    assert has_render(c) and has_source(p) and not any(a.iterdir()) and not store.get("clip", c["id"]).get("expired")


def test_expired_clips_are_hidden_but_feedback_and_post_history_stay(api):
    policy()
    p = project()
    c = clip(p, posted=True)
    store.put("feedback", {"id": c["id"], "signal": 1, "why": "rated", "project_id": p["id"]})
    storage.cleanup(now=NOW)
    assert store.get("feedback", c["id"]) and store.find("upload", clip_id=c["id"], status="done")
    ids = lambda q="": [k["id"] for k in api.get(f"/api/projects/{p['id']}{q}").json()["clips"]]  # noqa: E731
    assert c["id"] not in ids() and c["id"] in ids("?expired=1")
    board = lambda q="": {it["id"] for st in api.get(f"/api/pipeline{q}").json()["stages"] for it in st["items"]}  # noqa: E731
    assert c["id"] not in board() and c["id"] in board("?expired=1")
    assert api.get("/api/projects").json()[0]["clip_count"] == 0
    assert api.post(f"/api/clips/{c['id']}/export").status_code == 409  # an expired clip is history, not editable


def test_due_dates_are_shown_on_projects_and_clips(api):
    policy(since=NOW - 30 * DAY)
    p = project(age_days=11)
    clip(p, age_days=11)
    view = api.get(f"/api/projects/{p['id']}").json()
    assert view["source_due"]["action"] == "delete" and view["clips"][0]["due"]["action"] == "delete"
    assert abs(datetime.fromisoformat(view["source_due"]["at"]).timestamp() - (NOW + 3 * DAY)) < 5


def test_redownload_puts_the_source_back_without_reanalysis(api, jobs, monkeypatch):
    policy(unfinished=False, uploaded=False, longform=False)
    p = project()
    c = clip(p)
    storage.cleanup(now=NOW)
    view = api.get(f"/api/projects/{p['id']}").json()
    assert view["source_state"] == "removed" and view["can_redownload"]
    assert api.post(f"/api/projects/{p['id']}/redownload").status_code == 200
    job = jobs[-1]
    assert job["kind"] == "import_url" and job["url"] == "https://example.com/v" and job["redownload"]

    def fake_dl(url, outtmpl, hook=None):
        Path(outtmpl.replace("%(ext)s", "mp4")).write_bytes(b"again")
        return {"title": "Another title"}
    monkeypatch.setattr(pipeline, "ytdlp_download", fake_dl)
    monkeypatch.setattr(pipeline.media, "probe", lambda f: {"duration": 100.0, "width": 1920, "height": 1080, "has_audio": True})
    pipeline._import_url({"id": "j", **job})
    proj = store.get("project", p["id"])
    assert proj["status"] == "ready" and not proj.get("source_removed") and proj["name"] == "Stream"
    assert (pipeline.pdir(p["id"]) / proj["source"]).read_bytes() == b"again"
    assert [j["kind"] for j in jobs] == ["import_url"] and store.get("clip", c["id"])  # no re-analysis: clips stay
    storage.cleanup(now=NOW + DAY)
    assert has_source(p)  # its clock restarted with the download


def test_reimport_a_local_file_checks_it_is_the_same_video(api, monkeypatch):
    policy(local_sources=True, unfinished=False, uploaded=False, longform=False)
    p = project(url=None)
    storage.cleanup(now=NOW)
    view = api.get(f"/api/projects/{p['id']}").json()
    assert view["source_state"] == "removed" and not view["can_redownload"]
    assert api.post(f"/api/projects/{p['id']}/redownload").status_code == 400
    lengths = iter([40.0, 100.0])
    monkeypatch.setattr(storage.media, "probe", lambda f: {"duration": next(lengths), "width": 1920, "height": 1080, "has_audio": True})
    r = api.post(f"/api/projects/{p['id']}/source", files={"file": ("other.mp4", b"x", "video/mp4")})
    assert r.status_code == 400 and "same video" in r.json()["detail"]
    r = api.post(f"/api/projects/{p['id']}/source", files={"file": ("orig.mp4", b"back", "video/mp4")})
    assert r.status_code == 200 and not r.json().get("source_removed") and has_source(p)


def test_render_and_edits_are_blocked_cleanly_when_the_source_is_gone(api):
    p = project()
    c = clip(p, rendered=False, age_days=0)
    lf = longform.create(p["id"], "clips", "Long", [[0, 10]], [])
    store.update("project", p["id"], {"source_removed": {"at": pipeline.now_iso(), "mode": "delete"}})
    (pipeline.pdir(p["id"]) / "source.mp4").unlink()
    for url in (f"/api/clips/{c['id']}/export", f"/api/longform/{lf['id']}/export", f"/api/projects/{p['id']}/analyze"):
        r = api.post(url)
        assert 400 <= r.status_code < 500 and "re-download" in r.json()["detail"].lower(), url
    with pytest.raises(RuntimeError, match="(?i)source video was removed"):
        pipeline._render_clip(store.get("clip", c["id"]), lambda f: None)
    with pytest.raises(RuntimeError, match="(?i)source video was removed"):
        pipeline.request_upload(c["id"], {"platform": "youtube"})
    assert store.find("upload") == []  # refused before a post was created


# ---------------- importing old shorts ----------------

def test_bulk_short_import_one_project_each_and_duplicates_skipped(api, tmp_path, jobs):
    folder = tmp_path / "old shorts"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.mp4").write_bytes(b"A" * 100)
    (folder / "sub" / "b.mov").write_bytes(b"B" * 100)
    (folder / "a again.mp4").write_bytes(b"A" * 100)  # the same short twice
    (folder / "notes.txt").write_text("x")
    r = api.post("/api/shorts/paths", json={"paths": [str(folder)]}).json()
    assert len(r["added"]) == 2 and len(r["duplicates"]) == 1
    projs = [store.get("project", a["id"]) for a in r["added"]]
    assert all(p["origin"]["kind"] == "imported_short" and p["source_hash"] for p in projs)
    assert [j["kind"] for j in jobs] == ["ingest_file", "ingest_file"] and all(j["stable"] for j in jobs)
    again = api.post("/api/shorts/paths", json={"paths": [str(folder)]}).json()
    assert again["added"] == [] and len(again["duplicates"]) == 3 and len(store.find("project")) == 2
    pipeline._ingest_file({"id": "j", **jobs[0]})  # copied in (no wait for a file that's already finished), then analyzed
    p = store.get("project", jobs[0]["project_id"])
    assert (pipeline.pdir(p["id"]) / p["source"]).exists() and jobs[-1] == {"kind": "analyze", "project_id": p["id"]}


def test_uploaded_short_is_deduplicated_by_content(api):
    r1 = api.post("/api/shorts", files={"file": ("clip.mp4", b"S" * 50, "video/mp4")}).json()
    r2 = api.post("/api/shorts", files={"file": ("renamed.mp4", b"S" * 50, "video/mp4")}).json()
    assert r1["project"]["id"] and r2["duplicate"]["id"] == r1["project"]["id"] and len(store.find("project")) == 1
    assert [f.name for f in config.projects_dir().iterdir()] == [r1["project"]["id"]]  # no temp file left behind


def test_an_imported_short_becomes_one_whole_clip(monkeypatch):
    monkeypatch.setattr(pipeline.ai, "ready", lambda task: False)
    monkeypatch.setattr(pipeline.ai, "consensus_pair", lambda: None)
    p = project(url=None, origin={"kind": "imported_short", "file": "a.mp4"})  # 100 s: longer than clip_max * 1.25
    pipeline._find(p["id"], "")
    [c] = store.find("clip", project_id=p["id"])
    assert (c["start"], c["end"]) == (0.0, 100.0)
    assert pipeline._find(p["id"], "") == []  # analysing again never adds a second clip


# ---------------- review findings (each failed before its fix) ----------------

def test_a_never_rendered_clip_or_long_form_is_never_expired():
    policy()
    p = project()
    c = clip(p, rendered=False)
    lf = longform.create(p["id"], "clips", "Long", [[0, 10]], [])
    store.update("longform", lf["id"], {"created": NOW - 20 * DAY})
    storage.cleanup(now=NOW)
    assert not store.get("clip", c["id"]).get("expired") and not store.get("longform", lf["id"]).get("expired")


def test_a_file_in_use_never_costs_both_the_original_and_its_archive(tmp_path):
    a = tmp_path / "A"
    a.mkdir()
    policy(mode="archive", unfinished=False, uploaded=False, longform=False)
    config.save({"archive_uploaded_dir": str(a)})
    p = project()
    aid = "d" * 12
    store.update("project", p["id"], {"angles": [{"id": aid, "name": "Cam", "file": f"angles/{aid}.mp4", "status": "ready", "source_url": "u"}]})
    (pipeline.pdir(p["id"]) / "angles").mkdir()
    angle = pipeline.pdir(p["id"]) / "angles" / f"{aid}.mp4"
    angle.write_bytes(b"a")
    with open(angle, "rb"):  # open in a player: Windows refuses to delete it
        storage.cleanup(now=NOW)
    archived = sorted(f.name for f in a.iterdir())
    if has_source(p):  # this OS let nothing be deleted: then nothing may be marked and no archive copy left
        assert archived == [] and not store.get("project", p["id"]).get("source_removed")
    else:  # the source went: its archive copy stays and the project says so
        assert any("[source]" in n for n in archived) and store.get("project", p["id"])["source_removed"]["archive"]


def test_resuming_a_move_whose_old_drive_is_gone_never_switches(tmp_path):
    gone, dest = tmp_path / "unplugged", tmp_path / "dest"
    config.save({"storage_dir": str(gone), "storage_move": {"from": str(gone), "to": str(dest), "phase": "copy"}})
    storage.resume().join(10)
    assert config.projects_dir() == gone.resolve() and config.load()["storage_move"] == {} and storage.status()["move"]["error"]


def test_concurrent_settings_saves_never_lose_each_other():
    import threading
    def save(k, v):
        for _ in range(40):
            config.save({k: v})
    ts = [threading.Thread(target=save, args=a) for a in (("retention_days", 9), ("archive_other_dir", "X:/a"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    s = config.load()
    assert (s["retention_days"], s["archive_other_dir"]) == (9, "X:/a")


def test_no_cleanup_is_queued_while_the_drive_is_away(jobs, tmp_path, monkeypatch, api):
    policy()
    config.save({"storage_dir": str(tmp_path / "unplugged")})
    monkeypatch.setattr(storage, "_boot", 0.0)
    monkeypatch.setattr(storage, "_ran", False)
    storage.tick()
    assert jobs == [] and api.post("/api/storage/cleanup").status_code == 409


def test_a_lost_policy_date_means_nothing_is_due_and_saving_restamps_it(api):
    p = project(age_days=400)
    config.save({"retention_mode": "delete", "retention_days": 14, "retention_since": ""})
    assert storage.preview(now=NOW)["items"] == [] and storage.cleanup(now=NOW)["done"] == [] and has_source(p)
    api.put("/api/settings", json={"retention_mode": "delete"})
    assert config.load()["retention_since"]


def test_an_expired_clip_cannot_be_posted_or_rendered():
    policy()
    p = project()
    c = clip(p)
    storage.cleanup(now=NOW)
    with pytest.raises(RuntimeError, match="(?i)expired"):
        pipeline.request_upload(c["id"], {"platform": "youtube"})
    with pytest.raises(RuntimeError, match="(?i)expired"):
        pipeline.request_export(c["id"])
    assert store.find("upload") == []


def test_projects_are_not_deleted_while_the_folder_moves_or_is_away(api, tmp_path):
    p = project()
    config.save({"storage_move": {"from": "a", "to": "b", "phase": "copy"}})
    assert not pipeline.delete_item("project", p["id"])["ok"] and api.delete(f"/api/projects/{p['id']}").status_code == 409
    assert store.get("project", p["id"]) and has_source(p)


def test_a_redownload_of_a_different_video_is_refused(monkeypatch):
    policy(unfinished=False, uploaded=False, longform=False)
    p = project()
    storage.cleanup(now=NOW)
    monkeypatch.setattr(pipeline, "ytdlp_download", lambda url, outtmpl, hook=None: Path(outtmpl.replace("%(ext)s", "mp4")).write_bytes(b"x") or {})
    monkeypatch.setattr(pipeline.media, "probe", lambda f: {"duration": 40.0, "width": 1920, "height": 1080, "has_audio": True})
    with pytest.raises(RuntimeError, match="(?i)different"):
        pipeline._import_url({"id": "j", "project_id": p["id"], "url": "https://example.com/v", "redownload": True})
    proj = store.get("project", p["id"])
    assert proj["source_removed"] and not any(pipeline.pdir(p["id"]).glob("source.*"))


def test_a_folder_inside_reelwrights_data_is_refused():
    with pytest.raises(ValueError, match="(?i)reelwright's own data"):
        storage.validate(str(config.DATA / "assets" / "videos"))


def test_a_removed_source_takes_its_preview_proxy_along():
    policy(unfinished=False, uploaded=False, longform=False)
    p = project(age_days=20, proxy="proxy.mp4")
    (pipeline.pdir(p["id"]) / "proxy.mp4").write_bytes(b"p" * 300)
    assert [i["bytes"] for i in storage.preview(now=NOW)["items"]] == [1300]
    storage.cleanup(now=NOW)
    assert not has_source(p) and not (pipeline.pdir(p["id"]) / "proxy.mp4").exists()
    assert not store.get("project", p["id"]).get("proxy")  # the players fall back instead of asking for a gone file


def test_chat_logs_are_pruned_by_the_retention_days_and_shown_in_usage(tmp_path, monkeypatch):
    from reelwright import chatlog
    monkeypatch.setattr(chatlog, "DIR", tmp_path / "chatlogs")
    d = tmp_path / "chatlogs" / "nightowl"
    d.mkdir(parents=True)
    utc = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")  # noqa: E731
    old, new, other = d / f"{utc(NOW - 30 * DAY)}.jsonl", d / f"{utc(NOW - 2 * DAY)}.jsonl", d / "notes.txt"
    old.write_text("o" * 10)
    new.write_text("n" * 20)
    other.write_text("keep")
    u = storage.usage()
    assert u["chatlogs"] == 34 and u["total"] == sum(u[k] for k in ("sources", "renders", "other"))  # not moved with projects
    storage.cleanup(now=NOW)  # space saver off: nothing goes
    assert old.exists()
    policy("archive")  # logs are deleted in either mode (no archive folder needed for them)
    storage.cleanup(now=NOW)
    assert not old.exists() and new.exists() and other.exists()
