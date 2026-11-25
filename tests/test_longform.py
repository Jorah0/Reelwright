"""Long-form edits: one or more per project, captured from the existing analysis (condensed / highlights / from clips),
edited as an ordered list of source segments, rendered segment-wise and published as a regular YouTube video.
No network: Claude is off and every platform call is a fake."""
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import accounts, longform, media, pipeline, platforms, server, store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "LEGACY_CONN", tmp_path / "connections.json")
    monkeypatch.setattr(accounts, "LEGACY_YT", tmp_path / "youtube_token.json")
    monkeypatch.setattr(platforms, "CONN_FILE", tmp_path / "connections.json")
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: {"id": "j", "kind": kind, **kw})
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "")


@pytest.fixture
def api():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


def project(duration=300.0, transcript=None, signals=None):
    p = store.put("project", {"name": "Stream", "status": "ready", "meta": {"duration": duration, "width": 1920, "height": 1080,
                                                                           "fps": 30, "has_audio": True}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    pipeline.save_json(p["id"], "transcript.json", transcript or {"segments": []})
    pipeline.save_json(p["id"], "signals.json", signals or {"silences": []})
    return p


def lf(p, segments=((0, 30), (60, 90), (120, 150)), **fields):
    return longform.create(p["id"], "clips", "Long cut", [list(s) for s in segments],
                           [{"t": 0, "title": "Intro"}, {"t": 30, "title": "Middle"}, {"t": 60, "title": "End"}], **fields)


def acct(platform, approved=True):
    a = accounts.add(platform, name=f"{platform} acct", access_token="T")
    return accounts.update(a["id"], {"approved": approved})


def rendered(d):
    folder = pipeline.pdir(d["project_id"]) / "exports"
    folder.mkdir(exist_ok=True)
    (folder / f"{d['id']}.mp4").write_bytes(b"x")
    (folder / f"{d['id']}.jpg").write_bytes(b"x")
    return store.update("longform", d["id"], {"export": {"status": "done", "rev": d["rev"], "file": f"exports/{d['id']}.mp4",
                                                           "cover": f"exports/{d['id']}.jpg", "cover_t": 1.0}})


# ---------------- capture modes ----------------

def test_condensed_cuts_silences_and_idle_screens_and_fillers_with_padding():
    words = [{"s": 1.0, "e": 1.5, "w": "hello"}, {"s": 21.0, "e": 21.4, "w": "um"}, {"s": 21.5, "e": 22.0, "w": "right"},
             {"s": 80.0, "e": 80.5, "w": "back"}]
    signals = {"silences": [[5.0, 20.0]],
               # seconds 30-69 are a static menu with no speech; 70+ moves again
               "visual": {"motion": [0.05] * 30 + [0.0] * 40 + [0.05] * 30}}
    segs = longform.condensed(signals, words, 100.0, pad=0.25)
    kept = lambda t: any(a <= t < b for a, b in segs)  # noqa: E731
    assert segs == sorted(segs) and all(b > a for a, b in segs)
    assert kept(1.2) and kept(21.7) and kept(80.2)          # speech stays
    assert not kept(12.0)                                   # the silence is gone
    assert kept(5.1) and kept(19.9)                         # ...but its padding stays
    assert not kept(21.2)                                   # the "um" is cut
    assert not kept(50.0)                                   # the idle menu is cut
    total = sum(b - a for a, b in segs)
    assert total < 60


def test_condensed_with_nothing_to_cut_keeps_the_whole_video():
    assert longform.condensed({"silences": []}, [], 42.0) == [[0.0, 42.0]]


def test_highlights_pick_the_best_moments_within_the_target_then_play_in_order():
    moments = [{"start": 300, "end": 420, "score": 90, "title": "Clutch"}, {"start": 10, "end": 100, "score": 70, "title": "Opener"},
               {"start": 500, "end": 640, "score": 95, "title": "Boss"}, {"start": 150, "end": 250, "score": 40, "title": "Meh"},
               {"start": 700, "end": 760, "score": 60, "title": "Outro"}]
    segs, chapters = longform.pick_highlights(moments, target=400)
    assert segs == sorted(segs)                                       # chronological
    assert sum(e - s for s, e in segs) <= 400                         # within the target length
    assert [500, 640] in segs and [300, 420] in segs                  # best first
    assert [150, 250] not in segs                                     # weakest left out
    assert [c["title"] for c in chapters][0] == "Opener" and chapters[0]["t"] == 0
    assert [c["t"] for c in chapters] == sorted(c["t"] for c in chapters)


def test_highlights_offline_capture_reuses_the_analysis(monkeypatch):
    segs = [{"start": float(i * 10), "end": float(i * 10 + 9), "text": f"line {i} with a few words in it",
             "words": [{"s": float(i * 10), "e": float(i * 10 + 1), "w": "line"}]} for i in range(60)]
    p = project(600.0, {"segments": segs}, {"silences": [], "loud": [{"t": 305.0, "energy": 3.0}]})
    doc = longform.create(p["id"], "highlights", "Best of", [], [], settings={"target": 120})
    longform.capture_job({"id": "j", "longform_id": doc["id"]})
    d = store.get("longform", doc["id"])
    assert d["status"] == "ready" and d["segments"]
    assert d["segments"] == sorted(d["segments"]) and sum(e - s for s, e in d["segments"]) <= 120 + 1e-6


def test_from_clips_keeps_the_chosen_order():
    p = project()
    a = pipeline.new_clip(p["id"], {"start": 100.0, "end": 130.0, "title": "B"})
    b = pipeline.new_clip(p["id"], {"start": 10.0, "end": 40.0, "title": "A"})
    segs, chapters = longform.from_clips([a, b])
    assert segs == [[100.0, 130.0], [10.0, 40.0]]                     # the order picked, not source order
    assert chapters == [{"t": 0.0, "title": "B"}, {"t": 30.0, "title": "A"}]


def test_capture_route_from_clips(api):
    p = project()
    a = pipeline.new_clip(p["id"], {"start": 100.0, "end": 130.0, "title": "B"})
    b = pipeline.new_clip(p["id"], {"start": 10.0, "end": 40.0, "title": "A"})
    r = api.post(f"/api/projects/{p['id']}/longform", json={"mode": "clips", "clip_ids": [a["id"], b["id"]]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["segments"] == [[100.0, 130.0], [10.0, 40.0]] and d["edit"]["aspect"] == "16:9" and d["status"] == "ready"
    assert [x["id"] for x in api.get(f"/api/projects/{p['id']}/longform").json()] == [d["id"]]


# ---------------- sequence time & captions ----------------

def test_captions_follow_the_sequence_across_reordered_segments():
    words = [{"s": 5.0, "e": 5.5, "w": "first"}, {"s": 15.0, "e": 15.5, "w": "cut"}, {"s": 52.0, "e": 52.4, "w": "second"}]
    segs = [[50.0, 60.0], [0.0, 10.0]]  # played out of source order
    out = longform.seq_words(words, segs)
    assert [w["w"] for w in out] == ["second", "first"]               # sequence order; "cut" is outside every segment
    assert out[0]["s"] == pytest.approx(2.0) and out[1]["s"] == pytest.approx(15.0)
    assert longform.to_seq(5.0, segs) == pytest.approx(15.0) and longform.to_seq(15.0, segs) is None


def test_render_three_segments_concatenates_to_the_right_length(tmp_path):
    p = project(20.0)
    src = pipeline.pdir(p["id"]) / "source.mp4"
    media._run([media.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=20",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=20", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-shortest", str(src)])
    store.update("project", p["id"], {"source": "source.mp4", "meta": media.probe(src)})
    pipeline.save_json(p["id"], "transcript.json", {"segments": [{"start": 1, "end": 2, "text": "hi",
                                                                  "words": [{"s": 1.0, "e": 1.5, "w": "hi"}]}]})
    d = lf(p, [(12, 15), (1, 4), (6, 8.5)], transition=0.2)  # reordered, so it can't be one select() pass
    longform.export_job({"id": "j", "longform_id": d["id"]})
    d = store.get("longform", d["id"])
    assert d["export"]["status"] == "done" and longform.export_current(d)
    out = media.probe(pipeline.pdir(p["id"]) / d["export"]["file"])
    assert out["duration"] == pytest.approx(8.5, abs=0.25) and out["width"] == 1920 and out["has_audio"]
    assert not list((pipeline.pdir(p["id"]) / "exports").glob(".lf_*"))  # the parts are cleaned up


def test_render_groups_contiguous_segments_and_splits_reordered_ones():
    groups = longform.groups([[0, 5], [6, 9], [100, 110], [2, 3], [3.5, 4]])
    assert groups == [[(0, 5), (6, 9)], [(100, 110)], [(2, 3), (3.5, 4)]]


# ---------------- chapters ----------------

def test_youtube_chapters_start_at_zero_have_three_and_each_lasts_ten_seconds():
    text = longform.youtube_chapters([{"t": 3, "title": "Opening"}, {"t": 40, "title": "Fight"}, {"t": 45, "title": "Too soon"},
                                      {"t": 120, "title": "Ending"}], 200)
    lines = text.splitlines()
    assert lines[0] == "00:00 Opening" and len(lines) == 3          # the 5 s chapter is merged away
    assert lines[1:] == ["00:40 Fight", "02:00 Ending"]
    assert longform.youtube_chapters([{"t": 0, "title": "A"}, {"t": 30, "title": "B"}], 100) == ""  # fewer than 3: none
    assert longform.youtube_chapters([{"t": 0, "title": "A"}, {"t": 30, "title": "B"}, {"t": 3500, "title": "C"}], 3590) \
        .splitlines()[2] == "58:20 C"
    assert longform.youtube_chapters([{"t": 0, "title": "A"}, {"t": 30, "title": "B"}, {"t": 3700, "title": "C"}], 4000) \
        .splitlines() == ["0:00:00 A", "0:00:30 B", "1:01:40 C"]


def test_last_chapter_shorter_than_ten_seconds_is_dropped():
    text = longform.youtube_chapters([{"t": 0, "title": "A"}, {"t": 20, "title": "B"}, {"t": 40, "title": "C"},
                                      {"t": 95, "title": "D"}], 100)
    assert text.splitlines() == ["00:00 A", "00:20 B", "00:40 C"]


# ---------------- publishing ----------------

def test_upload_docs_go_to_approved_accounts_only(api):
    p = project()
    d = rendered(lf(p))
    ok, no = acct("youtube"), acct("youtube", approved=False)
    r = api.post(f"/api/longform/{d['id']}/upload", json={"accounts": [ok["id"]], "title": "Full stream", "description": "x"})
    assert r.status_code == 200, r.text
    [u] = r.json()
    assert u["longform_id"] == d["id"] and u["clip_id"] is None and u["account_id"] == ok["id"] and u["status"] in ("queued", "scheduled")
    r = api.post(f"/api/longform/{d['id']}/upload", json={"accounts": [no["id"]], "title": "Full stream"})
    assert r.status_code == 400 and "approved" in r.json()["detail"]


def test_instagram_is_refused_for_a_long_form_over_fifteen_minutes(api):
    p = project(3000.0)
    d = rendered(lf(p, [(0, 1000)]))
    ig = acct("instagram")
    r = api.post(f"/api/longform/{d['id']}/upload", json={"accounts": [ig["id"]], "title": "Too long"})
    assert r.status_code == 400 and "15 minutes" in r.json()["detail"]


def test_upload_worker_posts_a_long_form_and_locks_it(api, monkeypatch):
    sent = []
    monkeypatch.setattr(platforms, "account_connected", lambda a: True)
    monkeypatch.setattr(pipeline.platforms, "publish",
                        lambda plat, path, u, *a, **k: sent.append((plat, str(path), u["account_id"])) or {"video_id": "v1", "url": "https://youtu.be/v1"})
    p = project()
    d = rendered(lf(p))
    yt = acct("youtube")
    u = pipeline.request_upload(None, {"platform": "youtube", "account_id": yt["id"], "title": "Full"}, longform_id=d["id"])
    pipeline._upload({"upload_id": u["id"]})
    assert store.get("upload", u["id"])["status"] == "done"
    assert sent and sent[0][1].endswith(f"{d['id']}.mp4") and sent[0][2] == yt["id"]
    assert store.get("longform", d["id"])["posts"]["youtube"]["video_id"] == "v1"
    # post-upload lock: the published edit is final; metadata stays editable; a new version is editable
    r = api.patch(f"/api/longform/{d['id']}", json={"segments": [[0, 10]]})
    assert r.status_code == 409
    assert api.patch(f"/api/longform/{d['id']}", json={"title": "Renamed"}).json()["title"] == "Renamed"
    v = api.post(f"/api/longform/{d['id']}/version").json()
    assert v["id"] != d["id"] and v["version_of"] == d["id"] and not v["posts"] and v["export"] is None
    assert api.patch(f"/api/longform/{v['id']}", json={"segments": [[0, 10]]}).status_code == 200


def test_upload_waits_for_the_render_when_the_long_form_changed(monkeypatch):
    p = project()
    d = lf(p)
    yt = acct("youtube")
    submitted = []
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: submitted.append(kind) or {"id": "j", "kind": kind, **kw})
    u = pipeline.request_upload(None, {"platform": "youtube", "account_id": yt["id"], "title": "x"}, longform_id=d["id"])
    assert u["status"] == "waiting_render" and "longform_export" in submitted


def test_upload_of_a_deleted_long_form_is_canceled():
    p = project()
    d = rendered(lf(p))
    yt = acct("youtube")
    u = pipeline.request_upload(None, {"platform": "youtube", "account_id": yt["id"], "title": "x"}, longform_id=d["id"])
    store.delete("longform", d["id"])
    pipeline._upload({"upload_id": u["id"]})
    assert store.get("upload", u["id"])["status"] == "canceled"


def test_clip_uploads_still_work_alongside(api, monkeypatch):
    monkeypatch.setattr(platforms, "account_connected", lambda a: True)
    monkeypatch.setattr(pipeline.platforms, "publish", lambda *a, **k: {"video_id": "c1", "url": None})
    monkeypatch.setattr(pipeline, "remember", lambda *a, **k: None)
    p = project()
    c = pipeline.new_clip(p["id"], {"start": 0.0, "end": 30.0})
    monkeypatch.setattr(pipeline, "clip_export_current", lambda c: True)
    store.update("clip", c["id"], {"export": {"file": "x.mp4", "status": "done", "rev": 1}})
    yt = acct("youtube")
    [u] = api.post(f"/api/clips/{c['id']}/upload", json={"accounts": [yt["id"]], "title": "c"}).json()
    pipeline._upload({"upload_id": u["id"]})
    assert store.get("upload", u["id"])["status"] == "done" and "longform_id" not in u
    assert api.get("/api/uploads").status_code == 200 and api.get("/api/schedule").status_code == 200


# ---------------- board & delete ----------------

def _stage(api, lid):
    return next((st["key"], it) for st in api.get("/api/pipeline").json()["stages"] for it in st["items"] if it["id"] == lid)


def test_board_moves_a_long_form_from_captured_to_uploaded(api):
    p = project()
    d = lf(p)
    stage, item = _stage(api, d["id"])
    assert (stage, item["type"], item["lf_stage"]) == ("preclipped", "longform", "captured") and item["edit_url"] == f"#/long/{d['id']}"
    d = store.update("longform", d["id"], {"rev": 2})
    assert _stage(api, d["id"])[1]["lf_stage"] == "edited" and _stage(api, d["id"])[0] == "longform"
    d = rendered(d)
    assert _stage(api, d["id"])[0] == "ready"
    store.put("upload", {"clip_id": None, "longform_id": d["id"], "project_id": p["id"], "platform": "youtube", "status": "done",
                         "url": "https://youtu.be/x", "video_id": "x"})
    stage, item = _stage(api, d["id"])
    assert stage == "uploaded" and item["locked"] and item["posts"][0]["url"] == "https://youtu.be/x"


def test_delete_long_form_cancels_pending_and_removes_files(api):
    p = project()
    d = rendered(lf(p))
    up = store.put("upload", {"clip_id": None, "longform_id": d["id"], "project_id": p["id"], "platform": "youtube",
                              "status": "scheduled"})
    r = api.post("/api/pipeline/delete", json={"items": [{"type": "longform", "id": d["id"]}], "options": {"remote": {}}})
    [res] = r.json()["results"]
    assert res["ok"] and res["canceled"] == 1 and not store.get("longform", d["id"])
    assert store.get("upload", up["id"])["status"] == "canceled"
    assert not (pipeline.pdir(p["id"]) / "exports" / f"{d['id']}.mp4").exists()


def test_delete_long_form_refuses_while_uploading_and_removes_youtube_only_when_ticked(api, monkeypatch):
    p = project()
    d = rendered(lf(p))
    up = store.put("upload", {"clip_id": None, "longform_id": d["id"], "project_id": p["id"], "platform": "youtube", "status": "uploading"})
    [res] = api.post("/api/pipeline/delete", json={"items": [{"type": "longform", "id": d["id"]}]}).json()["results"]
    assert not res["ok"] and "uploading" in res["error"] and store.get("longform", d["id"])
    yt = acct("youtube")
    store.update("upload", up["id"], {"status": "done", "video_id": "vid", "account_id": yt["id"]})
    gone = []
    monkeypatch.setattr(pipeline.youtube if hasattr(pipeline, "youtube") else __import__("reelwright.youtube").youtube,
                        "delete_video", lambda vid, aid: gone.append((vid, aid)))
    [res] = api.post("/api/pipeline/delete", json={"items": [{"type": "longform", "id": d["id"]}],
                                                   "options": {"remote": {up["id"]: True}}}).json()["results"]
    assert res["ok"] and gone == [("vid", yt["id"])] and res["remote"][0]["ok"]


def test_deleting_the_project_removes_its_long_forms():
    p = project()
    d = lf(p)
    pipeline.delete_project(p["id"])
    assert not store.get("longform", d["id"])


# ---------------- editing ----------------

def test_patch_validates_segments_and_chapters(api):
    p = project(100.0)
    d = lf(p)
    assert api.patch(f"/api/longform/{d['id']}", json={"segments": [[50, 40]]}).status_code == 400
    assert api.patch(f"/api/longform/{d['id']}", json={"segments": [[0, 500]]}).status_code == 400
    r = api.patch(f"/api/longform/{d['id']}", json={"segments": [[10, 20], [0, 5]], "chapters": [{"t": 0, "title": "A"}],
                                                    "edit": {"captions": False, "aspect": "9:16"}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["segments"] == [[10, 20], [0, 5]] and out["rev"] == 2 and out["edit"]["aspect"] == "9:16" and not out["edit"]["captions"]
    ov = api.patch(f"/api/longform/{d['id']}", json={"edit": {"overlays": [{"kind": "text", "text": "hi", "t": 12, "d": 2}]}})
    assert ov.status_code == 200 and ov.json()["edit"]["overlays"][0]["t"] == 12  # sequence time (15 s long now)
    assert api.patch(f"/api/longform/{d['id']}", json={"edit": {"overlays": [{"kind": "text", "text": "hi", "t": 20}]}}).status_code == 400


# ---------------- headline and camera angles reach every part ----------------

def _capture_render(monkeypatch):
    calls = []
    monkeypatch.setattr(media, "render", lambda src, out, start, end, e, meta, ass, *a, **kw: calls.append(
        {"start": start, "end": end, "e": e, "ass": ass, **kw}))
    return calls


def test_render_part_draws_the_headline_and_passes_it_to_the_renderer(monkeypatch):
    calls = _capture_render(monkeypatch)
    p = project(100.0)
    store.update("project", p["id"], {"source": "source.mp4"})
    d = lf(p)
    e = {**media.DEFAULT_EDIT, **d["edit"], "captions": False,
         "headline": {"on": True, "text": "Biggest clutch of the year", "theme": "bold_bar"}}
    longform._render_part(d, store.get("project", p["id"]), [(0.0, 30.0)], e, [], pipeline.pdir(p["id"]) / "x.mp4", 0.0, None)
    [c] = calls
    assert c["ass"] and "BIGGEST CLUTCH" in c["ass"].upper()
    assert c["headline"] and c["headline"]["text"] == "Biggest clutch of the year"


def test_render_part_reads_camera_angles_with_switches_retimed_to_the_part(monkeypatch):
    calls = _capture_render(monkeypatch)
    p = project(200.0)
    aid = "a1b2c3d4e5f6"
    store.update("project", p["id"], {"source": "source.mp4", "angles": [{"id": aid, "name": "B", "file": f"angles/{aid}.mp4", "offset_s": 2.0,
                                                  "meta": {"width": 160, "height": 90, "duration": 300, "fps": 30, "has_audio": True},
                                                  "status": "ready"}]})
    (pipeline.pdir(p["id"]) / "angles").mkdir(exist_ok=True)
    (pipeline.pdir(p["id"]) / f"angles/{aid}.mp4").write_bytes(b"x")
    d = lf(p, [(10, 20), (100, 120)])
    # sequence time: cut to the angle at 5 s (inside part 0), back to main at 15 s (5 s into part 1)
    e = {**media.DEFAULT_EDIT, **d["edit"], "captions": False,
         "switches": [{"t": 5.0, "angle": aid}, {"t": 15.0, "angle": "main"}, {"t": 25.0, "angle": aid}]}
    proj = store.get("project", p["id"])
    out = pipeline.pdir(p["id"]) / "x.mp4"
    n0 = longform._render_part(d, proj, [(10.0, 20.0)], e, [], out, 0.0, None)
    longform._render_part(d, proj, [(100.0, 120.0)], e, [], out, n0, None)
    a, b = calls
    assert aid in (a["angles"] or {}) and aid in (b["angles"] or {})
    assert a["angles"][aid]["offset_s"] == 2.0 and a["start"] == 10.0 and b["start"] == 100.0  # angle seeks start - offset
    assert a["e"]["switches"] == [{"t": 5.0, "angle": aid}]       # the later cuts belong to part 1
    # part 1 opens on the camera live at its start (the angle from 5 s), then cuts at its own clip times
    assert b["e"]["switches"] == [{"t": 0.0, "angle": aid}, {"t": 5.0, "angle": "main"}, {"t": 15.0, "angle": aid}]


# ---------------- chapters and overlays follow speed and cuts ----------------

def _sped_up_with_fillers():
    words = [{"s": 10.0, "e": 11.0, "w": "um"}, {"s": 20.0, "e": 21.0, "w": "uh"}, {"s": 30.0, "e": 30.5, "w": "hello"}]
    p = project(300.0, transcript={"segments": [{"start": 0, "end": 300, "text": "x", "words": words}]})
    store.update("project", p["id"], {"source": "source.mp4"})
    d = longform.create(p["id"], "clips", "LF", [[0, 60], [100, 160], [200, 260]],  # three groups (40 s gaps)
                        [{"t": 0, "title": "One"}, {"t": 60, "title": "Two"}, {"t": 120, "title": "Three"}])
    return p, store.update("longform", d["id"], fn=lambda x: x["edit"].update(speed=1.25, remove_fillers=True))


def test_youtube_chapters_follow_speed_and_removed_fillers():
    _, d = _sped_up_with_fillers()
    text = longform.chapters_text(d)
    # 2 s of fillers cut from the first 60 s, then everything at 1.25x: 58/1.25 = 46.4 s, 118/1.25 = 94.4 s, total 178/1.25 = 142.4 s
    assert text.splitlines() == ["00:00 One", "00:46 Two", "01:34 Three"]
    assert longform.output_length(d) == pytest.approx(142.4, abs=0.01)


def test_overlays_and_cuts_land_on_the_right_part_in_sequence_time(monkeypatch):
    p, d = _sped_up_with_fillers()
    asses = []
    monkeypatch.setattr(media, "build_ass", lambda *a, **k: asses.append(k.get("overlays")) or "ass")
    monkeypatch.setattr(media, "render", lambda *a, **k: None)
    monkeypatch.setattr(longform, "_join", lambda *a, **k: None)
    e = {**media.DEFAULT_EDIT, **d["edit"], "captions": False,
         "overlays": [{"kind": "text", "text": "hi", "t": 70.0, "d": 2.0}]}  # 10 s into the second segment
    longform.render({**d, "edit": e}, lambda f: None)
    texts = [o for o in asses if o]
    assert len(texts) == 1 and texts[0][0]["t"] == pytest.approx(10.0) and texts[0][0]["d"] == pytest.approx(2.0)


def test_render_part_bleeps_censors_and_animates_captions_like_a_clip(monkeypatch):
    calls = _capture_render(monkeypatch)
    p = project(100.0)
    store.update("project", p["id"], {"source": "source.mp4"})
    d = lf(p)
    words = [{"s": 1.0, "e": 1.4, "w": "oh"}, {"s": 2.0, "e": 2.5, "w": "shit"}, {"s": 40.0, "e": 40.4, "w": "fuck"}]
    e = {**media.DEFAULT_EDIT, **d["edit"], "captions": True, "bleep": "beep", "caption_anim": "pop", "caption_emphasis": True}
    longform._render_part(d, store.get("project", p["id"]), [(0.0, 30.0)], e, words, pipeline.pdir(p["id"]) / "x.mp4", 0.0, None)
    [c] = calls
    assert c["bleeps"] == [(2.0, 2.5)]  # source time inside the part; media.render maps them through the cuts
    assert "\\fscx125\\fscy125\\t(0,120,\\fscx100\\fscy100)" in c["ass"]  # the pop animation
    assert "S***" in c["ass"].upper() and "SHIT" not in c["ass"].upper()
