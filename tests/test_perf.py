"""Performance pass: indexed store.find, job pruning, progress throttling, and the settings / transcript caches must behave
exactly like the slow versions they replace."""
import json
import math
import os
import random
import sqlite3
import tempfile
import time

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from reelwright import config, pipeline, server, store  # noqa: E402


@pytest.fixture
def db(monkeypatch):
    """A private store table with the real indexes, so tests never touch the shared one."""
    d = sqlite3.connect(":memory:", check_same_thread=False)
    d.execute("CREATE TABLE docs (kind TEXT, id TEXT, created REAL, data TEXT, PRIMARY KEY (kind, id))")
    for f in store.INDEXED:
        d.execute(f"CREATE INDEX docs_{f} ON docs (kind, json_extract(data, '$.{f}'))")
    monkeypatch.setattr(store, "_db", d)
    return d


def old_find(kind, /, **match):  # the full scan find() replaced
    rows = store._db.execute("SELECT data FROM docs WHERE kind=? ORDER BY created DESC", (kind,)).fetchall()
    return [d for d in (json.loads(r[0]) for r in rows) if all(d.get(k) == v for k, v in match.items())]


VALUES = ["a", "b", "1", "", "é ü", "a.b", 0, 1, 2, -1, 2 ** 70, 1.0, 0.5, float("nan"), float("inf"), True, False, None,
          [1], ["a"], {"a": 1}, {}, []]
KEYS = list(store.INDEXED) + ["source", "x", "a.b"]


def test_find_matches_the_old_scan_on_random_docs(db):
    rnd = random.Random(7)
    for i in range(600):
        doc = {k: rnd.choice(VALUES) for k in KEYS if rnd.random() < 0.6}
        store.put(rnd.choice(["job", "clip"]), {**doc, "id": f"d{i}", "created": 1000.0 + i})  # distinct times: one order
    checked = 0
    for _ in range(800):
        match = {k: rnd.choice(VALUES) for k in rnd.sample(KEYS, rnd.randint(0, 3))}
        kind = rnd.choice(["job", "clip"])
        got, want = store.find(kind, **match), old_find(kind, **match)
        assert [d["id"] for d in got] == [d["id"] for d in want], match
        checked += bool(want)
    assert checked > 100  # plenty of the queries matched something


def test_find_keeps_python_equality_for_mixed_types(db):
    store.put("job", {"id": "t", "status": True, "n": 1.0, "s": "1"})
    assert [d["id"] for d in store.find("job", status=1)] == ["t"]  # True == 1
    assert [d["id"] for d in store.find("job", n=1)] == ["t"]  # 1.0 == 1
    assert store.find("job", s=1) == [] and store.find("job", n="1") == []
    assert [d["id"] for d in store.find("job", missing=None)] == ["t"]  # an absent field matches None, as d.get() did
    assert store.find("job", status=None) == []
    nan = {"id": "n", "x": float("nan")}
    store.put("job", nan)
    assert store.find("job", x=float("nan")) == []  # nan != nan in Python, so nothing matches
    assert math.isnan(store.get("job", "n")["x"])


def test_find_uses_the_field_indexes(db):
    plan = db.execute("EXPLAIN QUERY PLAN SELECT data FROM docs WHERE kind=? AND json_extract(data, '$.project_id') = ? "
                      "ORDER BY created DESC", ("clip", "p")).fetchall()
    assert "docs_project_id" in str(plan)


def test_fields_and_newest_agree_with_whole_docs(db):
    for i, v in enumerate([{"expired": {"at": "x"}}, {"expired": None}, {}, {"expired": "s"}, {"expired": 0}, {"expired": [1]}]):
        store.put("clip", {"id": f"c{i}", "project_id": f"p{i % 2}", "created": 10.0 + i, **v})
    docs = store.find("clip")
    assert store.fields("clip", "project_id", "expired") == [(d["id"], d["project_id"], d.get("expired")) for d in docs]
    assert store.newest("clip", 2) == docs[:2]


def test_prune_drops_only_old_finished_jobs(db):
    now, day = time.time(), 86400
    jobs = {
        "old_done": {"status": "done", "kind": "export", "created": now - 30 * day, "finished": now - 30 * day},
        "old_failed": {"status": "failed", "kind": "find", "created": now - 9 * day},
        "old_canceled": {"status": "canceled", "kind": "track", "created": now - 8 * day},
        "old_running": {"status": "running", "kind": "analyze", "created": now - 60 * day},
        "old_queued": {"status": "queued", "kind": "export", "created": now - 60 * day},
        "recent_done": {"status": "done", "kind": "export", "created": now - 1 * day},
        "long_job_just_finished": {"status": "done", "kind": "analyze", "created": now - 20 * day, "finished": now - 60},
        "old_import_url": {"status": "done", "kind": "import_url", "created": now - 90 * day, "url": "https://x"},
        "old_failed_proxy": {"status": "failed", "kind": "proxy", "created": now - 90 * day},
    }
    for jid, j in jobs.items():
        store.put("job", {"id": jid, **j})
    store.put("train_job", {"id": "other_kind", "status": "done", "created": now - 90 * day})
    assert store.prune_jobs() == 3
    left = {j["id"] for j in store.find("job")}
    assert left == set(jobs) - {"old_done", "old_failed", "old_canceled"}
    assert store.get("train_job", "other_kind")


def test_scheduler_prunes_at_start_then_daily(db, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "prune_jobs", lambda: calls.append(1) or 0)
    monkeypatch.setattr(pipeline, "_last_prune", 0.0)
    monkeypatch.setattr(pipeline, "_last_source_check", time.time())  # skip the minute-ly part of the tick
    pipeline._scheduler_tick()
    pipeline._scheduler_tick()
    assert calls == [1]
    monkeypatch.setattr(pipeline, "_last_prune", time.time() - 86401)
    pipeline._scheduler_tick()
    assert calls == [1, 1]


def test_progress_writes_at_most_twice_a_second_per_job(monkeypatch):
    writes, clock = [], [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(store, "update", lambda kind, id, patch=None, fn=None: writes.append((kind, id, dict(patch))))
    pipeline._progress("j1", 0.1, "Transcribing", project_id="p")
    pipeline._progress("j1", 0.2)  # same instant: dropped
    pipeline._progress("j2", 0.3)  # another job has its own clock
    clock[0] += 0.3
    pipeline._progress("j1", 0.4, "Transcribing")  # same message: still throttled
    pipeline._progress("j1", 0.45, "Finding clips")  # a new stage is never dropped
    clock[0] += 0.6
    pipeline._progress("j1", 0.9)
    pipeline._progress("j1", 1.0)  # a step's end is never dropped (a quiet step may follow it)
    assert writes == [("job", "j1", {"progress": 0.1, "message": "Transcribing"}), ("project", "p", {"progress": 0.1, "stage": "Transcribing"}),
                      ("job", "j2", {"progress": 0.3}),
                      ("job", "j1", {"progress": 0.45, "message": "Finding clips"}),
                      ("job", "j1", {"progress": 0.9}), ("job", "j1", {"progress": 1.0})]
    pipeline._progress_at.clear()


def test_settings_are_read_once_per_request_and_saves_show_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    reads = []
    raw = config._raw
    monkeypatch.setattr(config, "_raw", lambda: reads.append(1) or raw())
    token = config._request.set({})
    try:
        a = config.load()
        a["clip_count"] = 99  # a caller's copy: never leaks into the next read
        assert config.load()["clip_count"] == config.DEFAULTS["clip_count"]
        config.projects_dir()
        assert len(reads) == 1
        config.save({"clip_count": 5})  # a save inside the request is seen by the next read
        assert config.load()["clip_count"] == 5
        (tmp_path / "settings.json").write_text(json.dumps({"clip_count": 12, "caption_color": "#000000"}), "utf-8")
        assert config.load()["clip_count"] == 12  # another writer: the file's size / mtime changed
    finally:
        config._request.reset(token)
    n = len(reads)
    config.load(), config.load()  # outside a request: no cache at all
    assert len(reads) == n + 2


def test_request_scope_reaches_sync_endpoints():
    from fastapi.testclient import TestClient
    seen = []
    server.app.add_api_route("/api/_scope_probe", lambda: seen.append(config._request.get()) or {})
    routes = server.app.router.routes
    routes.insert(0, routes.pop())  # ahead of the UI's catch-all routes
    try:
        assert TestClient(server.app, base_url="http://127.0.0.1:8765").get("/api/_scope_probe").status_code == 200
    finally:
        routes.pop(0)
    assert seen == [{}] and config._request.get() is None


def test_transcript_words_follow_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "pdir", lambda pid: tmp_path)
    f = tmp_path / "transcript.json"
    assert pipeline.transcript_words("p") == []
    f.write_text(json.dumps({"segments": [{"words": [{"s": 0, "e": 1, "w": "hi"}]}, {"text": "no words"}]}), "utf-8")
    first = pipeline.transcript_words("p")
    assert first == [{"s": 0, "e": 1, "w": "hi"}]
    first[0]["w"] = "CHANGED"  # a render's own copy
    assert pipeline.transcript_words("p")[0]["w"] == "hi"
    st = f.stat()
    f.write_text(json.dumps({"segments": [{"words": [{"s": 0, "e": 1, "w": "yo"}]}]}), "utf-8")  # same size, new text
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert pipeline.transcript_words("p")[0]["w"] == "yo"
    f.write_text(json.dumps({"segments": [{"words": [{"s": 0, "e": 1, "w": "longer"}]}]}), "utf-8")
    assert pipeline.transcript_words("p")[0]["w"] == "longer"


def _old_audio_signals(src):  # the whole-file version audio_signals replaced
    import subprocess

    import numpy as np

    from reelwright import media
    r = subprocess.run([media.FFMPEG, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "4000", "-f", "s16le", "-"],
                       capture_output=True, creationflags=media.NO_WINDOW)
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = len(x) // 400
    if n == 0:
        return [], []
    x = x[: n * 400].reshape(n, 400)
    peaks = np.abs(x).max(axis=1)
    peaks = peaks / (peaks.max() or 1)
    rms = np.sqrt((x ** 2).mean(axis=1))
    return [round(float(p), 3) for p in peaks], media.energy_spikes(np.convolve(rms, np.ones(20) / 20, mode="same"))


def test_streamed_audio_and_shared_silences_equal_the_separate_passes(tmp_path):
    import subprocess

    from reelwright import media
    src = tmp_path / "a.mkv"  # 150.37 s stereo 44.1 kHz (two read blocks + a part window), levels around the -35 dB line
    vol = "if(lt(mod(t,9),2),0.004,if(lt(mod(t,9),3),0.0175,if(lt(mod(t,9),3.6),0.019,0.4*abs(sin(t)))))"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=a=1:c=pink:r=44100:d=150.37",
                    "-af", f"volume='{vol}':eval=frame,aformat=channel_layouts=stereo", "-c:a", "pcm_s16le", str(src)], check=True)
    out = []
    assert media.audio_signals(src, out) == _old_audio_signals(src)
    assert out == media.silences(src) and len(out) > 5
    assert media.audio_signals(src) == _old_audio_signals(src)
    empty = tmp_path / "none.mkv"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=d=0.05", "-c:a", "pcm_s16le", str(empty)], check=True)
    assert media.audio_signals(empty, []) == ([], [])


def test_sprite_from_the_vision_frames_matches_the_sprite_decode(tmp_path):
    import subprocess

    import cv2
    import numpy as np

    from reelwright import media, vision
    src = tmp_path / "v.mp4"  # 20 s whose colour steps every 2 s: each of the 10 tiles must show its own slot's colour
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:d=20",
                    "-vf", "hue=h=floor(t/2)*70", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)],
                   check=True)
    meta = media.probe(src)
    count = 10
    media.sprite(src, meta["duration"], tmp_path / "ffmpeg.jpg", count)
    out = vision.analyze(src, meta, faces=False, scenes=True, sprite=(count, tmp_path / "vision.jpg"))
    assert out["sprite"] is True and "scenes" in out
    a, b = (cv2.imread(str(tmp_path / n)).astype(np.float32) for n in ("ffmpeg.jpg", "vision.jpg"))
    assert a.shape == b.shape == (90, 160 * count, 3)
    tile = lambda img, k: img[:, k * 160:(k + 1) * 160]  # noqa: E731
    for k in range(count):
        same = np.abs(tile(a, k) - tile(b, k)).mean()
        assert same < 12, (k, same)  # the same picture (JPEG and a ≤ 0.25 s sample offset apart)
        if k:
            assert np.abs(tile(a, k) - tile(b, k - 1)).mean() > 2 * same  # ...and a neighbour's would not pass
    assert "sprite" not in vision.analyze(src, meta, faces=False)  # without the argument nothing changes
