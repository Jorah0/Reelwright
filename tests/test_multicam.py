"""Multi-camera: camera angles per project, audio sync, angle regions and switches in the render, auto-switch."""
import os
import subprocess
import tempfile
import time
import wave

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import media, multicam, pipeline, server, store  # noqa: E402

FPS = 30
SR = 16000


def need_ffmpeg():
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")


def client():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


# ---------- synthetic media ----------

def speech_like(seconds: float, seed: int) -> np.ndarray:
    """Noise bursts with a random on/off rhythm: enough structure for an onset envelope to lock onto."""
    rng = np.random.default_rng(seed)
    env, i, n = np.zeros(int(seconds * SR), np.float32), 0, int(seconds * SR)
    while i < n:
        on, off = int(rng.uniform(0.08, 0.5) * SR), int(rng.uniform(0.05, 0.4) * SR)
        env[i:i + on] = rng.uniform(0.2, 1.0)
        i += on + off
    return (env * rng.standard_normal(n)).astype(np.float32) * 0.5


def write_wav(path, x: np.ndarray):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def make_video(path, seconds: float, lum: str, size="160x90", audio=None):
    """A flat-colour video whose luma is `lum` (an ffmpeg geq expression of the frame number N), optional mono audio."""
    args = [media.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
            f"nullsrc=s={size}:r={FPS}:d={seconds},format=yuv420p,geq=lum='{lum}':cb=128:cr=128"]
    if audio is not None:
        wav = path.with_suffix(".wav")
        write_wav(wav, audio)
        args += ["-i", str(wav), "-c:a", "aac", "-shortest"]
    subprocess.run(args + ["-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def luma_at(path, t: float, box=None) -> float:
    """Mean luma (the stored Y values, 16 = black) of the frame at t, optionally of a pixel box x, y, w, h."""
    m = media.probe(path)
    raw = subprocess.run([media.FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"], capture_output=True, check=True).stdout
    img = np.frombuffer(raw[: m["width"] * m["height"]], np.uint8).reshape(m["height"], m["width"])  # the Y plane
    if box:
        x, y, w, h = box
        img = img[y:y + h, x:x + w]
    return float(img.mean())


def frame_no(y: float) -> float:  # inverse of the angle's lum = 40 + 2*N
    return (y - 40) / 2


# ---------- sync ----------

@pytest.mark.parametrize("offset", [3.5, -2.0, 0.0])
def test_sync_finds_a_known_delay_within_one_frame(tmp_path, offset):
    need_ffmpeg()
    main_a = speech_like(40, 1)
    rng = np.random.default_rng(7)
    if offset >= 0:  # the angle started recording `offset` s after the main camera
        ang_a = main_a[int(offset * SR):]
    else:  # ... or -offset s before it (other sound first)
        ang_a = np.concatenate([speech_like(-offset, 9), main_a])
    ang_a = 0.6 * ang_a[: 30 * SR] + 0.03 * rng.standard_normal(min(len(ang_a), 30 * SR)).astype(np.float32)
    main = make_video(tmp_path / "main.mp4", 40, "235", audio=main_a)
    ang = make_video(tmp_path / "ang.mp4", 30, "60", audio=ang_a)
    t0 = time.perf_counter()
    r = multicam.sync(main, ang)
    took = time.perf_counter() - t0
    print(f"sync offset={offset} found={r['offset_s']:.4f} conf={r['confidence']:.1f} in {took:.2f}s")
    assert r["method"] == "audio"
    assert abs(r["offset_s"] - offset) <= 1 / FPS
    assert r["confidence"] >= multicam.MIN_CONFIDENCE


def test_sync_is_exact_when_the_overlap_starts_at_the_main_videos_start(tmp_path):
    """A short angle that began before the main camera: the fine excerpt sits at main 0 (no negative seeks)."""
    need_ffmpeg()
    main_a = speech_like(30, 4)
    main = make_video(tmp_path / "main.mp4", 30, "235", audio=main_a)
    ang = make_video(tmp_path / "ang.mp4", 20, "60", audio=np.concatenate([speech_like(2, 9), 0.6 * main_a[: 18 * SR]]))
    r = multicam.sync(main, ang)
    assert r["method"] == "audio" and abs(r["offset_s"] + 2.0) <= 1 / FPS / 3, r


def test_sync_falls_back_to_zero_on_unrelated_audio_and_says_so(tmp_path):
    need_ffmpeg()
    main = make_video(tmp_path / "main.mp4", 30, "235", audio=speech_like(30, 1))
    other = make_video(tmp_path / "other.mp4", 30, "60", audio=speech_like(30, 2))
    r = multicam.sync(main, other)
    assert r["offset_s"] == 0.0 and r["method"] == "none" and r["confidence"] < multicam.MIN_CONFIDENCE
    assert "low" in r["note"].lower()


def test_sync_without_audio_falls_back_to_zero(tmp_path):
    need_ffmpeg()
    main = make_video(tmp_path / "main.mp4", 10, "235", audio=speech_like(10, 1))
    silent = make_video(tmp_path / "silent.mp4", 10, "60")
    r = multicam.sync(main, silent)
    assert r["offset_s"] == 0.0 and r["method"] == "none" and "audio" in r["note"].lower()


# ---------- angles on a project ----------

def _project(tmp_path, seconds=12):
    need_ffmpeg()
    p = pipeline.new_project("mc")
    src = pipeline.pdir(p["id"]) / "source.mp4"
    make_video(src, seconds, "235", audio=speech_like(seconds, 3))
    store.update("project", p["id"], {"source": "source.mp4", "status": "ready", "meta": media.probe(src)})
    return p["id"]


def _upload(pid, path, name="Cam B"):
    with open(path, "rb") as f:
        return client().post(f"/api/projects/{pid}/angles", files={"file": (path.name, f, "video/mp4")}, data={"name": name})


def _run_jobs(pid):
    for j in store.find("job", project_id=pid):
        if j["kind"] == "angle" and j["status"] == "queued":
            multicam.angle_job(j)
            store.update("job", j["id"], {"status": "done"})


def test_upload_adds_a_synced_angle_inside_the_project(tmp_path):
    pid = _project(tmp_path)
    main_a = speech_like(12, 3)
    ang = make_video(tmp_path / "b.mp4", 8, "60", audio=0.5 * main_a[2 * SR: 10 * SR])
    r = _upload(pid, ang)
    assert r.status_code == 200, r.text
    a = r.json()
    assert len(a["id"]) == 12 and int(a["id"], 16) >= 0
    assert a["file"] == f"angles/{a['id']}.mp4" and (pipeline.pdir(pid) / a["file"]).is_file()
    assert a["name"] == "Cam B" and a["status"] == "syncing"
    _run_jobs(pid)
    a = store.get("project", pid)["angles"][0]
    assert a["status"] == "ready" and a["sync"]["method"] == "audio"
    assert abs(a["offset_s"] - 2.0) <= 1 / FPS
    assert a["meta"]["width"] == 160 and a["meta"]["duration"] > 7
    assert (pipeline.pdir(pid) / a["thumb"]).is_file()


def test_angle_limits_and_file_types(tmp_path):
    pid = _project(tmp_path, 4)
    bad = tmp_path / "x.txt"
    bad.write_text("nope")
    with open(bad, "rb") as f:
        assert client().post(f"/api/projects/{pid}/angles", files={"file": ("x.txt", f)}).status_code == 400
    store.update("project", pid, {"angles": [{"id": f"{i:012x}", "name": f"A{i}", "file": f"angles/{i:012x}.mp4"}
                                             for i in range(multicam.MAX_ANGLES)]})
    ang = make_video(tmp_path / "b.mp4", 2, "60")
    assert _upload(pid, ang).status_code == 409
    assert client().post(f"/api/projects/{pid}/angles/url", json={"url": "https://example.com/v"}).status_code == 409


def test_angle_paths_never_leave_the_project_folder(tmp_path):
    pid = _project(tmp_path, 4)
    assert multicam.angle_path(pid, {"file": "../../evil.mp4"}) is None
    assert multicam.angle_path(pid, {"file": "C:/Windows/win.ini"}) is None
    assert multicam.angle_path(pid, {"file": "source.mp4"}) is None  # only files under angles/
    # OBS picks: only a plain file name that is listed in the recordings folder
    rec = tmp_path / "rec"
    rec.mkdir()
    make_video(rec / "take1.mp4", 2, "60")
    (tmp_path / "secret.mp4").write_bytes(b"x")
    conn = store.put("obs", {"name": "OBS", "host": "127.0.0.1", "port": 4455, "enabled": False, "last_file": str(rec / "take1.mp4")})
    c = client()
    listed = c.get(f"/api/projects/{pid}/angles/obs").json()
    assert any(f["name"] == "take1.mp4" for s in listed for f in s["files"])
    for name in ("../secret.mp4", "..\\secret.mp4", str(tmp_path / "secret.mp4"), "missing.mp4"):
        assert c.post(f"/api/projects/{pid}/angles/obs", json={"obs_id": conn["id"], "file": name}).status_code in (400, 404)
    ok = c.post(f"/api/projects/{pid}/angles/obs", json={"obs_id": conn["id"], "file": "take1.mp4"})
    assert ok.status_code == 200, ok.text
    _run_jobs(pid)
    a = store.get("project", pid)["angles"][0]
    assert a["status"] == "ready" and (pipeline.pdir(pid) / a["file"]).is_file() and (rec / "take1.mp4").is_file()


def _clip_with_angle(pid, aid="a1b2c3d4e5f6"):
    store.update("project", pid, {"angles": [{"id": aid, "name": "B", "file": f"angles/{aid}.mp4", "offset_s": 0.0,
                                              "meta": {"width": 160, "height": 90, "duration": 12, "fps": 30, "has_audio": True},
                                              "status": "ready", "sync": {"method": "manual", "confidence": 0}}]})
    (pipeline.pdir(pid) / "angles").mkdir(exist_ok=True)
    (pipeline.pdir(pid) / f"angles/{aid}.mp4").write_bytes(b"x")
    return pipeline.new_clip(pid, {"start": 1.0, "end": 9.0})


def test_switches_and_audio_source_are_validated(tmp_path):
    pid = _project(tmp_path, 4)
    clip = _clip_with_angle(pid)
    c = client()
    ok = c.patch(f"/api/clips/{clip['id']}", json={"edit": {"switches": [{"t": 5, "angle": "main"}, {"t": 2, "angle": "a1b2c3d4e5f6"}],
                                                            "audio_source": "a1b2c3d4e5f6"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["edit"]["switches"] == [{"t": 2.0, "angle": "a1b2c3d4e5f6"}, {"t": 5.0, "angle": "main"}]  # sorted
    assert ok.json()["edit"]["audio_source"] == "a1b2c3d4e5f6"
    for bad in ({"switches": [{"t": 1, "angle": "nope"}]}, {"switches": [{"t": "x", "angle": "main"}]},
                {"switches": "main"}, {"audio_source": "nope"}):
        assert c.patch(f"/api/clips/{clip['id']}", json={"edit": bad}).status_code == 400
    # a cut beyond the clip's end is clamped onto it
    r = c.patch(f"/api/clips/{clip['id']}", json={"edit": {"switches": [{"t": 99, "angle": "main"}]}}).json()
    assert r["edit"]["switches"] == [{"t": 8.0, "angle": "main"}]


def test_removing_an_angle_puts_its_regions_back_on_main(tmp_path):
    pid = _project(tmp_path, 4)
    clip = _clip_with_angle(pid)
    aid = "a1b2c3d4e5f6"
    store.update("clip", clip["id"], fn=lambda c: c["edit"].update(
        layout="split", regions=[{"id": "c", "role": "cam", "x": 0, "y": 0, "w": 1, "h": 1, "source": aid},
                                 {"id": "m", "role": "main", "x": 0, "y": 0, "w": 1, "h": 1}],
        switches=[{"t": 2.0, "angle": aid}, {"t": 4.0, "angle": "main"}], audio_source=aid))
    r = client().delete(f"/api/projects/{pid}/angles/{aid}")
    assert r.status_code == 200
    e = store.get("clip", clip["id"])["edit"]
    assert all("source" not in rg for rg in e["regions"])
    assert e["switches"] == [] and e["audio_source"] == "main"
    assert store.get("project", pid)["angles"] == []
    assert not (pipeline.pdir(pid) / f"angles/{aid}.mp4").exists()


def test_manual_nudge_and_project_delete(tmp_path):
    pid = _project(tmp_path, 4)
    _clip_with_angle(pid)
    c = client()
    a = c.patch(f"/api/projects/{pid}/angles/a1b2c3d4e5f6", json={"offset_s": 1.234, "name": "Wide"}).json()
    assert a["offset_s"] == 1.234 and a["name"] == "Wide" and a["sync"]["method"] == "manual"
    assert c.patch(f"/api/projects/{pid}/angles/a1b2c3d4e5f6", json={"offset_s": "x"}).status_code == 400
    folder = pipeline.pdir(pid)
    assert (folder / "angles/a1b2c3d4e5f6.mp4").exists()
    assert c.delete(f"/api/projects/{pid}").status_code == 200
    assert not folder.exists()


# ---------- render ----------

def _render_setup(tmp_path, offset):
    """Main: 10 s of luma 235. Angle: 3 s whose luma counts frames (40 + 2N), recorded from main time `offset`."""
    need_ffmpeg()
    main = make_video(tmp_path / "main.mp4", 10, "235", size="640x360")
    ang = make_video(tmp_path / "ang.mp4", 3, "40+2*N", size="320x180")
    meta = {**media.probe(main), "has_audio": False}
    angles = {"a1": {"path": ang, "offset_s": offset, **media.probe(ang)}}
    return main, meta, angles


def test_angle_region_renders_at_the_right_time_and_black_outside_it(tmp_path):
    main, meta, angles = _render_setup(tmp_path, 4.0)
    out = tmp_path / "out.mp4"
    edit = {"aspect": "9:16", "loudnorm": False, "layout": "split", "split": 0.4,
            "regions": [{"id": "c", "role": "cam", "x": 0, "y": 0, "w": 1, "h": 1, "source": "a1"},
                        {"id": "m", "role": "main", "x": 0, "y": 0, "w": 1, "h": 1}]}
    media.render(main, out, 3.0, 8.0, edit, meta, None, angles=angles)
    m = media.probe(out)
    assert (m["width"], m["height"]) == (1080, 1920) and abs(m["duration"] - 5.0) < 0.15
    top, bottom = (200, 200, 600, 300), (200, 1200, 600, 300)
    assert luma_at(out, 0.5, top) < 25                      # clip 0.5 = main 3.5: the angle hasn't started -> black
    assert abs(frame_no(luma_at(out, 2.0, top)) - 30) <= 1.5  # clip 2.0 = main 5.0 = angle 1.0 s = frame 30
    assert luma_at(out, 4.5, top) < 25                      # main 7.5: the 3 s angle has ended -> black
    for t in (0.5, 2.0, 4.5):
        assert luma_at(out, t, bottom) > 220                # the main view is never touched


def test_switches_cut_between_angles_at_the_right_times(tmp_path):
    main, meta, angles = _render_setup(tmp_path, 2.0)
    out = tmp_path / "sw.mp4"
    edit = {"aspect": "source", "loudnorm": False, "layout": "single",
            "switches": [{"t": 1.5, "angle": "a1"}, {"t": 3.0, "angle": "main"}]}
    media.render(main, out, 2.5, 7.5, edit, meta, None, angles=angles)
    m = media.probe(out)
    assert (m["width"], m["height"]) == (640, 360)
    assert luma_at(out, 1.0) > 220                            # before the first cut: main
    assert abs(frame_no(luma_at(out, 2.0)) - 75) <= 1.5       # clip 2.0 = main 4.5 = angle 2.5 s = frame 75
    assert luma_at(out, 3.5) > 220                            # cut back to main at 3.0


def test_switch_times_follow_jump_cuts_and_speed():
    e = {"aspect": "source", "layout": "single", "speed": 2.0, "switches": [{"t": 4.0, "angle": "a1"}]}
    g, _ = media.build_filters(e, 640, 360, 4.0, False, keeps_rel=[(0, 2), (3, 9)], has_audio=False,
                               feeds={"a1": (1, 0.0, 320, 180, 9.0)})
    # clip 4.0 s sits 3.0 s into the kept footage, then 2x speed -> output 1.5 s
    assert "enable='between(t,1.500," in g


def test_audio_can_come_from_an_angle():
    e = {"aspect": "source", "layout": "single", "audio_source": "a1", "loudnorm": False}
    g, _ = media.build_filters(e, 640, 360, 5.0, False, has_audio=True, feeds={"a1": (1, 0.5, 320, 180, 5.0)})
    assert "[1:a]" in g and "[0:a]" not in g and "adelay=500" in g


def test_main_only_graphs_are_unchanged():
    base = {"aspect": "9:16", "layout": "split", "regions": [{"id": "c", "role": "cam", "x": 0.7, "y": 0, "w": 0.3, "h": 0.3},
                                                             {"id": "m", "role": "main", "x": 0, "y": 0, "w": 1, "h": 1}]}
    for e in (base, {"aspect": "9:16"}, {"aspect": "source", "zoom": 1.5}):
        plain = media.build_filters(e, 1920, 1080, 10, True, [(0, 2), (3, 8)])
        tagged = media.build_filters({**e, "switches": [], "audio_source": "main", "switch_region": ""}, 1920, 1080, 10, True,
                                     [(0, 2), (3, 8)])
        assert plain == tagged
    # angles that no region / switch / audio uses add no ffmpeg inputs at all
    assert media.angle_inputs({"a1": {"path": "x.mp4", "offset_s": 0, "width": 2, "height": 2, "duration": 9}},
                              {**media.DEFAULT_EDIT, **base}, 0, 5, 1) == ([], {})


# ---------- auto-switch ----------

def test_auto_switch_follows_the_loudest_with_a_minimum_shot_length():
    step = 0.1
    n = 120  # 12 s
    main, a, b = np.full(n, -30.0), np.full(n, -40.0), np.full(n, -40.0)
    a[50:] = -20.0           # A takes over at 5 s
    b[70:75] = -5.0          # a 0.5 s blip on B at 7 s: shorter than a shot, ignored
    main[100:] = -10.0       # main back at 10 s
    cuts = multicam.suggest({"main": main, "A": a, "B": b}, step, min_shot=2.0)
    assert cuts == [{"t": 5.0, "angle": "A"}, {"t": 10.0, "angle": "main"}]
    # flicker between two nearly equal speakers stays on one shot (hysteresis)
    wob = np.where(np.arange(n) % 6 < 3, -20.0, -21.0)
    assert multicam.suggest({"main": wob, "A": np.full(n, -20.5)}, step, min_shot=2.0) == []
    # every suggested shot is at least min_shot long
    rng = np.random.default_rng(0)
    cuts = multicam.suggest({k: rng.normal(-20, 6, n) for k in ("main", "A", "B")}, step, min_shot=2.0)
    assert cuts  # random scores do cut somewhere
    times = sorted({0.0, *(c["t"] for c in cuts), n * step})  # a cut at 0 just opens on another camera
    assert all(b - a >= 2.0 - 1e-9 for a, b in zip(times, times[1:]))


def test_changing_an_angles_sync_makes_renders_that_use_it_stale(monkeypatch):
    from reelwright import longform
    p = pipeline.new_project("mc-stale")
    pid = p["id"]
    store.update("project", pid, {"source": "source.mp4", "status": "ready",
                                  "meta": {"duration": 60.0, "width": 320, "height": 180, "fps": 30, "has_audio": True}})
    aid = "a1b2c3d4e5f6"
    using = _clip_with_angle(pid, aid)
    store.update("clip", using["id"], fn=lambda c: c["edit"].update(audio_source=aid))
    other = pipeline.new_clip(pid, {"start": 1.0, "end": 9.0})
    posted = pipeline.new_clip(pid, {"start": 1.0, "end": 9.0})
    store.update("clip", posted["id"], fn=lambda c: c["edit"].update(audio_source=aid))
    store.put("upload", {"clip_id": posted["id"], "project_id": pid, "platform": "youtube", "status": "done"})
    lf = longform.create(pid, "clips", "LF", [[0, 5]], [])
    store.update("longform", lf["id"], fn=lambda d: d["edit"].update(switches=[{"t": 1.0, "angle": aid}]))
    revs = lambda: (store.get("clip", using["id"])["rev"], store.get("clip", other["id"])["rev"],  # noqa: E731
                    store.get("clip", posted["id"])["rev"], store.get("longform", lf["id"])["rev"])
    before = revs()
    multicam.update(pid, aid, {"offset_s": 1.5})
    assert revs() == (before[0] + 1, before[1], before[2], before[3] + 1)  # posted clips keep their render
    multicam.update(pid, aid, {"name": "Renamed"})  # a rename changes no picture
    assert revs() == (before[0] + 1, before[1], before[2], before[3] + 1)
    # a finished re-sync is the same as a new offset
    monkeypatch.setattr(media, "probe", lambda p: {"width": 160, "height": 90, "duration": 12, "fps": 30, "has_audio": True})
    monkeypatch.setattr(media, "poster", lambda *a, **k: None)
    monkeypatch.setattr(multicam, "sync", lambda m, a: {"offset_s": 0.25, "method": "audio", "confidence": 20.0, "note": ""})
    (pipeline.pdir(pid) / "source.mp4").write_bytes(b"x")
    multicam.angle_job({"id": "j", "project_id": pid, "angle_id": aid, "resync": True})
    assert revs() == (before[0] + 2, before[1], before[2], before[3] + 2)
