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
        assert TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}").get("/api/_scope_probe").status_code == 200
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


# ---------- perf/resource pass 2: transcript memo, slim polls, bounded growth, Whisper release ----------

def _count_parses(monkeypatch):
    calls = []
    real = json.loads
    monkeypatch.setattr(pipeline, "json", type("J", (), {"loads": staticmethod(lambda s: calls.append(1) or real(s)),
                                                         "dumps": staticmethod(json.dumps)}))
    return calls


def test_transcript_segments_parse_once_per_file_version_and_copies_are_private(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "pdir", lambda pid: tmp_path)
    pipeline._segs_memo.clear()
    f = tmp_path / "transcript.json"
    f.write_text(json.dumps({"segments": [{"start": i, "end": i + 1, "text": f"t{i}", "words": [{"s": i, "e": i + 1, "w": f"w{i}"}]}
                                          for i in range(50)]}), "utf-8")
    parses = _count_parses(monkeypatch)
    texts = [pipeline.clip_text({"project_id": "p", "start": i, "end": i + 2}) for i in range(50)]  # Triage: 50 cards
    assert texts[0] == "t0 t1" and len(parses) == 1
    own = pipeline.transcript_segments("p", copy=True)
    own[0]["text"], own[0]["words"][0]["w"] = "CHANGED", "CHANGED"
    assert pipeline.transcript_segments("p")[0]["text"] == "t0" and pipeline.transcript_words("p")[0]["w"] == "w0"
    st = f.stat()
    f.write_text(f.read_text("utf-8").replace('"t0"', '"x0"'), "utf-8")  # same size, new text
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert pipeline.clip_text({"project_id": "p", "start": 0, "end": 1}) == "x0" and len(parses) == 2
    f.unlink()
    assert pipeline.transcript_segments("p") == [] and pipeline.clip_text({"project_id": "p", "start": 0, "end": 9}) == ""


def test_remember_reads_files_outside_the_store_lock(db, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "pdir", lambda pid: tmp_path)
    (tmp_path / "transcript.json").write_text(json.dumps({"segments": [{"start": 0, "end": 2, "text": "hello there"}]}), "utf-8")
    store.put("project", {"id": "p", "meta": {"duration": 60}})
    held = []
    real = pipeline.transcript_segments
    monkeypatch.setattr(pipeline, "transcript_segments", lambda *a, **k: held.append(store._lock._is_owned()) or real(*a, **k))
    clip = {"id": "c", "project_id": "p", "start": 0.0, "end": 5.0, "rating": 1, "score": 50}
    pipeline.remember(clip, "rated")
    fb = store.get("feedback", "c")
    assert fb["signal"] == 1 and fb["text"] == "hello there" and held and not any(held)
    pipeline.remember({**clip, "rating": 0}, "rated")  # un-rated, never posted: the lesson goes
    assert store.get("feedback", "c") is None


def _old_items(pol, project_ids=None):  # the whole-doc version storage._items replaced
    from reelwright import storage
    DAY, _ts, scope, out = storage.DAY, storage._ts, pol["scope"], []
    projects = {p["id"]: p for p in store.find("project") if not project_ids or p["id"] in project_ids}
    by_item: dict = {}
    for u in store.find("upload"):
        by_item.setdefault(u.get("longform_id") or u.get("clip_id"), []).append(u)

    def add(kind, doc, proj, bucket, clock):
        out.append({"kind": kind, "id": doc["id"], "project_id": proj["id"], "bucket": bucket,
                    "title": doc.get("title") or doc.get("name") or "Untitled", "due": max(clock, pol["since"]) + pol["days"] * DAY})
    for p in projects.values():
        if p.get("keep") or not p.get("source") or p.get("source_removed") or p.get("status") not in ("ready", "failed"):
            continue
        if scope["sources" if storage.source_url(p) else "local_sources"]:
            add("source", p, p, "other", max(_ts(p.get("created")), _ts(p.get("source_at"))))
    for kind in ("clip", "longform"):
        for d in store.find(kind):
            p = projects.get(d["project_id"])
            if not p or p.get("keep") or d.get("keep") or d.get("expired") or not (d.get("export") or {}).get("file"):
                continue
            done = [u for u in by_item.get(d["id"], []) if u.get("status") == "done"]
            made = max(_ts(d.get("created")), _ts((d.get("export") or {}).get("at")))
            if kind == "longform":
                if scope["longform"]:
                    add(kind, d, p, "other", max([_ts(u.get("uploaded_at")) for u in done] or [made]))
            elif done and scope["uploaded"]:
                add(kind, d, p, "uploaded", max(_ts(u.get("uploaded_at")) for u in done) or made)
            elif not done and scope["unfinished"]:
                add(kind, d, p, "unfinished", made)
    return out


def test_due_dates_match_the_whole_doc_version_and_one_project_reads_only_its_own(db, monkeypatch):
    from reelwright import storage
    rnd = random.Random(3)
    for i in range(8):
        store.put("project", {"id": f"p{i}", "created": 100.0 + i, "status": rnd.choice(["ready", "failed", "analyzing"]),
                              "source": rnd.choice(["source.mp4", None]), "keep": rnd.random() < 0.15,
                              "source_url": rnd.choice([None, "https://x"]), "source_at": rnd.choice([None, 500.0, "2026-01-02T00:00:00Z"])})
    for kind in ("clip", "longform"):
        for i in range(120):
            store.put(kind, {"id": f"{kind}{i}", "project_id": f"p{rnd.randrange(9)}", "created": 1000.0 + i, "title": rnd.choice(["T", "", None]),
                             "name": rnd.choice(["N", None]), "keep": rnd.random() < 0.1, "expired": rnd.choice([None, None, {"at": 1}]),
                             "export": rnd.choice([None, {"file": "a.mp4", "at": 2000.0 + i}, {"file": "", "at": 1}, {"file": "b.mp4"}]),
                             "edit": {"words": [1] * 50}})
    for i in range(150):
        store.put("upload", {"id": f"u{i}", "created": 5000.0 + i, "status": rnd.choice(["done", "failed", "scheduled"]),
                             "clip_id": f"clip{rnd.randrange(130)}", "longform_id": rnd.choice([None, None, f"longform{rnd.randrange(130)}"]),
                             "uploaded_at": rnd.choice([None, 9000.0 + i, "2026-03-01T00:00:00+00:00"])})
    key = lambda it: (it["kind"], it["id"])  # noqa: E731
    for scope in ({}, {"uploaded": False}, {"unfinished": False, "longform": False}, {"sources": False, "local_sources": True}):
        pol = {"mode": "delete", "days": 3.0, "since": 50.0, "scope": {**storage.SCOPE_DEFAULTS, **scope}}
        want = sorted(_old_items(pol), key=key)
        assert sorted(storage._items(pol), key=key) == want and len(want) > 3
        for ids in ({"p1"}, {"p2", "p5"}, {"nope"}):
            assert sorted(storage._items(pol, ids), key=key) == sorted(_old_items(pol, ids), key=key)
    whole = []
    real_find = store.find
    monkeypatch.setattr(store, "find", lambda kind, /, **m: whole.append(kind) or real_find(kind, **m))
    storage._items({"mode": "delete", "days": 3.0, "since": 50.0, "scope": dict(storage.SCOPE_DEFAULTS)}, {"p1"})
    assert not {"clip", "longform", "upload", "project"} & set(whole)  # the Editor's poll parses no table whole


def test_live_clips_and_slim_agree_with_whole_docs(db):
    db.execute(f"CREATE INDEX docs_clip_live ON docs (kind, {store._CLIP_PID}, {store._CLIP_EXP}, created, id)")
    rnd = random.Random(5)
    for i in range(300):
        store.put("clip", {"id": f"c{i}", "project_id": f"p{rnd.randrange(12)}", "created": 10.0 + i,
                           "expired": rnd.choice([None, None, {"at": "x"}, False, 0, "", [], {}]),
                           "edit": {"aspect": rnd.choice(["9:16", None]), "overlays": [{"asset_id": "a"}] * rnd.randrange(2)},
                           **({"title": "t"} if rnd.random() < 0.5 else {})})
    old: dict = {}
    for c in store.find("clip"):
        if not c.get("expired"):
            old.setdefault(c["project_id"], []).append(c["id"])
    assert store.live_clips() == {pid: (len(ids), ids[0]) for pid, ids in old.items()}
    every: dict = {}
    for c in store.find("clip"):
        every[c["project_id"]] = every.get(c["project_id"], 0) + 1
    assert {k: v[0] for k, v in store.live_clips(expired=True).items()} == every
    plan = db.execute(f"EXPLAIN QUERY PLAN SELECT {store._CLIP_PID}, {store._CLIP_EXP}, id FROM docs "
                      "WHERE kind='clip' ORDER BY created DESC").fetchall()
    assert "COVERING INDEX docs_clip_live" in str(plan)  # no clip JSON parsed
    slim = store.slim("clip", "title", "edit.aspect", "edit.overlays", "project_id")
    for s, c in zip(slim, store.find("clip"), strict=True):
        assert s["id"] == c["id"] and s.get("title") == c.get("title") and s["project_id"] == c["project_id"]
        assert s["edit"].get("aspect") == c["edit"].get("aspect") and s["edit"].get("overlays") == c["edit"].get("overlays")
    assert [r[0] for r in store.fields("clip", project_id="p3")] == [c["id"] for c in store.find("clip", project_id="p3")]


def test_plans_go_with_their_clip_reports_and_usage_stay_bounded(db, monkeypatch):
    from reelwright import usage
    store.put("plan", {"id": "pc", "clip_id": "c1"})
    store.put("plan", {"id": "pl", "longform_id": "l1"})
    store.put("plan", {"id": "other", "clip_id": "c2"})
    store.delete("clip", "c1")
    store.delete("longform", "l1")
    assert store.get("plan", "pc") is None and store.get("plan", "pl") is None and store.get("plan", "other")
    store.put("plan", {"id": "old", "clip_id": "c2", "created": time.time() - 30 * 86400})
    store.prune_jobs()
    assert store.get("plan", "old") is None and store.get("plan", "other")
    for i in range(12):
        store.put("report", {"id": f"r{i}", "created": 100.0 + i})
    assert store.keep_newest("report", 5) == 7 and [r["id"] for r in store.find("report")] == [f"r{i}" for i in range(11, 6, -1)]
    monkeypatch.setattr(usage, "KEEP", 10)
    monkeypatch.setattr(usage, "_calls", 0)
    finds = []
    real_find = store.find
    monkeypatch.setattr(store, "find", lambda kind, /, **m: finds.append(kind) or real_find(kind, **m))
    for _ in range(usage.PRUNE_EVERY + 1):
        usage.record("find", "claude-opus-5-5", {"input_tokens": 1})
    assert "usage" not in finds and len(real_find("usage")) == 10  # one SQL prune on the 1st and the 101st call


def test_backfill_runs_once_and_enqueued_ids_are_released(db, monkeypatch):
    monkeypatch.setattr(pipeline, "_started", False)
    monkeypatch.setattr(pipeline.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(pipeline, "_recover_uploads", lambda: None)
    seen = []
    monkeypatch.setattr(pipeline, "remember", lambda c, why: seen.append((c["id"], why)))
    store.put("clip", {"id": "rated", "project_id": "p", "rating": 1})
    store.put("clip", {"id": "posted", "project_id": "p", "posts": [1]})
    store.put("clip", {"id": "plain", "project_id": "p"})
    pipeline.start()
    assert sorted(seen) == [("posted", "published"), ("rated", "rated")]
    monkeypatch.setattr(pipeline, "_started", False)
    seen.clear()
    pipeline.start()
    assert seen == []  # the marker: later starts skip the scan
    job = store.put("job", {"kind": "zz_test", "status": "canceled"})
    pipeline._enqueued.add(job["id"])
    pipeline._run_job(job["id"])
    assert job["id"] not in pipeline._enqueued


def test_failed_url_import_drops_its_partials_but_a_redownload_keeps_the_source(db, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "pdir", lambda pid: tmp_path)
    monkeypatch.setattr(pipeline, "_analyze_failed", lambda job, err: None)
    for n in ("source.mp4.part", "source.f137.mp4", "source.mp4.ytdl", "poster.jpg"):
        (tmp_path / n).write_bytes(b"x")
    store.put("project", {"id": "p"})
    pipeline._import_failed({"project_id": "p"}, "boom")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["poster.jpg"]
    (tmp_path / "source.mp4").write_bytes(b"x")
    store.put("project", {"id": "p", "source": "source.mp4"})
    pipeline._import_failed({"project_id": "p", "redownload": True}, "boom")
    pipeline._import_failed({"project_id": "p"}, "boom")  # the project's own source is never a partial
    assert (tmp_path / "source.mp4").exists()


def test_idle_whisper_is_released_but_never_mid_transcription(monkeypatch):
    from reelwright import ai
    monkeypatch.setattr(ai, "_whisper", {("small", "cuda"): object()})
    monkeypatch.setattr(ai, "_whisper_used", time.monotonic())
    assert not ai.release_whisper() and ai._whisper  # just used
    monkeypatch.setattr(ai, "_whisper_used", time.monotonic() - ai.WHISPER_IDLE_S - 1)
    with ai._whisper_lock:  # a transcription holds it
        assert not ai.release_whisper() and ai._whisper
    assert ai.release_whisper() and not ai._whisper


def test_training_waits_for_an_analysis_too(db):
    from reelwright.training import runner
    store.put("job", {"kind": "analyze", "status": "running"})
    assert runner._rendering() == "analyze" and "analysed" in runner.block_reason()


def _analyze_harness(monkeypatch, order, vision_wait=None):
    p = pipeline.new_project("x")
    (pipeline.pdir(p["id"]) / "source.mp4").write_bytes(b"x")
    store.update("project", p["id"], {"source": "source.mp4"})
    monkeypatch.setattr(pipeline.media, "probe", lambda src: {"duration": 60.0, "width": 1, "height": 1, "has_audio": True})
    monkeypatch.setattr(pipeline.media, "poster", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.media, "sprite", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.media, "audio_signals", lambda src, out=None: ([], []))
    monkeypatch.setattr(pipeline, "_video_type", lambda *a, **k: {})

    def vision(*a, **k):
        order.append("vision start")
        if vision_wait:
            assert vision_wait.wait(5), "transcription never started beside the on-screen pass"
        order.append("vision end")
        return {"scenes": []}
    monkeypatch.setattr(pipeline.vision, "analyze", vision)
    monkeypatch.setattr(pipeline.sig, "enrich", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_find", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_autopilot", lambda pid: None)
    return p["id"]


def test_whisper_on_cuda_runs_beside_the_on_screen_pass(monkeypatch):
    import threading
    order, started = [], threading.Event()
    pid = _analyze_harness(monkeypatch, order, started)
    monkeypatch.setattr(pipeline.ai, "whisper_on_cuda", lambda: True)
    monkeypatch.setattr(pipeline.ai, "transcribe", lambda *a, **k: started.set() or order.append("transcribe") or
                        {"segments": [{"start": 0, "end": 1, "text": "hi"}], "language": "en"})
    pipeline._analyze({"id": "j", "project_id": pid})
    assert order.index("transcribe") < order.index("vision end")
    assert pipeline.transcript_segments(pid)[0]["text"] == "hi"


def test_cpu_whisper_stays_serial_and_a_gpu_failure_fails_the_job(monkeypatch):
    order = []
    pid = _analyze_harness(monkeypatch, order)
    monkeypatch.setattr(pipeline.ai, "whisper_on_cuda", lambda: False)
    monkeypatch.setattr(pipeline.ai, "transcribe", lambda *a, **k: order.append("transcribe") or {"segments": [], "language": None})
    pipeline._analyze({"id": "j", "project_id": pid})
    assert order == ["vision start", "vision end", "transcribe"]

    def boom(*a, **k):
        raise RuntimeError("Transcription failed: x")
    monkeypatch.setattr(pipeline.ai, "whisper_on_cuda", lambda: True)
    monkeypatch.setattr(pipeline.ai, "transcribe", boom)
    with pytest.raises(RuntimeError, match="Transcription failed"):
        pipeline._analyze({"id": "j", "project_id": pid})


def test_a_failed_analysis_never_leaves_the_gpu_whisper_running(monkeypatch):
    import threading
    order = []
    pid = _analyze_harness(monkeypatch, order)
    monkeypatch.setattr(pipeline.ai, "whisper_on_cuda", lambda: True)
    started = threading.Event()

    def transcribe(src, dur, progress, **k):  # holds the lock like ai.transcribe; reports progress per segment
        with pipeline.ai._whisper_lock:
            started.set()
            for i in range(1000):
                progress(i / 1000)
                time.sleep(0.01)
        return {"segments": [], "language": None}
    monkeypatch.setattr(pipeline.ai, "transcribe", transcribe)
    real_save = pipeline.save_json

    def save(p, name, data):
        if name == "signals.json":
            assert started.wait(5)
            raise OSError("disk full")
        return real_save(p, name, data)
    monkeypatch.setattr(pipeline, "save_json", save)
    t0 = time.monotonic()
    with pytest.raises(OSError, match="disk full"):
        pipeline._analyze({"id": "j", "project_id": pid})
    assert not [t for t in threading.enumerate() if t.name.startswith("whisper-") and t.is_alive()]
    assert pipeline.ai._whisper_lock.acquire(blocking=False)  # a retry isn't blocked behind the orphan
    pipeline.ai._whisper_lock.release()
    assert time.monotonic() - t0 < 5  # stopped at its next segment, not run to the end
