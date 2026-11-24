"""Supporting tools: cover frames, filler-word cuts, Claude usage tracking, learning export/import, event log."""
import os
import subprocess
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from reelwright import ai, covers, log, media, pipeline, store, usage  # noqa: E402


def test_cover_score_prefers_sharp_well_exposed_frames():
    rng = np.random.default_rng(0)
    sharp = (rng.random((180, 320, 3)) * 255).astype(np.uint8)
    import cv2
    blurred = cv2.GaussianBlur(sharp, (31, 31), 0)
    dark = (sharp * 0.05).astype(np.uint8)
    assert covers.frame_score(sharp) > covers.frame_score(blurred)
    assert covers.frame_score(sharp) > covers.frame_score(dark)
    assert covers.frame_score(sharp, faces=1, face_area=0.1) > covers.frame_score(sharp)


def test_cover_is_written_from_a_real_video(tmp_path):
    video = tmp_path / "v.mp4"
    r = subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
                        "-pix_fmt", "yuv420p", str(video)], capture_output=True)
    if r.returncode:
        pytest.skip("ffmpeg can't make a test video here")
    out = tmp_path / "cover.jpg"
    t = covers.make(video, out, samples=6, faces=False)
    assert t is not None and 0 <= t <= 3 and out.stat().st_size > 1000


def test_filler_words_are_cut_and_captions_follow():
    words = [{"s": 10.0, "e": 10.4, "w": " So"}, {"s": 10.5, "e": 10.9, "w": " um,"}, {"s": 11.0, "e": 11.5, "w": " this"},
             {"s": 11.6, "e": 12.0, "w": " Uh"}, {"s": 12.1, "e": 12.6, "w": " works"}]
    spans = media.filler_spans(words, 10.0, 13.0)
    assert spans == [(10.5, 10.9), (11.6, 12.0)]
    keeps = media.cut_spans([(10.0, 13.0)], spans)
    assert keeps == [(10.0, 10.5), (10.9, 11.6), (12.0, 13.0)]
    ass = media.build_ass(words, 10.0, 13.0, 1.0, (1080, 1920), keeps=keeps)
    assert "UM" not in ass.upper().replace("NUMBER", "") and "WORKS" in ass.upper()
    assert media.cut_spans([(0.0, 5.0)], []) == [(0.0, 5.0)]


def test_usage_records_tokens_cache_and_cost(monkeypatch):
    import anthropic
    for d in store.find("usage"):
        store.delete("usage", d["id"])

    class U:
        input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens = 1000, 500, 9000, 0

    class Fake:
        def __init__(self, **kw):
            parse = staticmethod(lambda **kw: type("R", (), {"stop_reason": "end_turn", "usage": U(), "model": "claude-opus-5-5",
                                                              "parsed_output": ai.Query(query="q")})())
            self.beta = type("B", (), {"messages": type("M", (), {"parse": parse})()})()

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    monkeypatch.setattr(ai.config, "api_key", lambda: "k")
    ai._ask("sys", ("CTX", "TASK"), ai.Query)
    s = usage.summary(days=30)
    assert s["calls"] == 1 and s["by_kind"]["Query"]["calls"] == 1
    assert s["cache_hit_ratio"] == 0.9  # 9000 of 10000 input tokens came from cache
    expected = (1000 * 4 + 500 * 20 + 9000 * 0.2) / 1e6
    assert abs(s["cost_usd"] - expected) < 1e-9
    assert usage.cost("some-future-model", {"input_tokens": 1}) is None  # unknown price: say so, don't guess


def test_learning_export_import_roundtrip_and_validation():
    for d in store.find("feedback"):
        store.delete("feedback", d["id"])
    store.put("feedback", {"id": "a1", "created": 1.0, "project_id": "p", "signal": 1, "why": "rated", "title": "T",
                           "text": "x", "length": 30.0, "features": {"loud": 2.0}, "edit": {"zoom": 1.2}, "trim": [1.0, 0.0]})
    data = pipeline.export_learning()
    assert data["format"] == "reelwright-learning" and len(data["feedback"]) == 1
    store.delete("feedback", "a1")
    bad = [{"id": "b", "signal": 5, "why": "rated"}, {"id": "c", "signal": 1, "why": "hack", "created": 1},
           {"id": "../x", "signal": 1, "why": "rated", "created": 1}, "nope"]
    res = pipeline.import_learning({**data, "feedback": data["feedback"] + bad})
    assert res == {"added": 1, "skipped": 4}
    assert store.get("feedback", "a1")["features"] == {"loud": 2.0}
    assert pipeline.import_learning(data) == {"added": 0, "skipped": 1}  # already known
    with pytest.raises(RuntimeError):
        pipeline.import_learning({"format": "something-else"})


def test_hostile_import_rows_are_sanitized_and_cant_break_learning():
    from reelwright import learn
    for d in store.find("feedback"):
        store.delete("feedback", d["id"])
    rows = [{"id": f"h{i}", "created": 1.0, "signal": 1, "why": "published"} for i in range(6)]
    rows[0].update(length="long", views="lots", trim=[1], edit="9:16", features={"loud": float("inf")})
    rows[1].update(trim={"a": 1}, edit=["x"], likes=[1])
    rows[2].update(edit={"aspect": "evil", "zoom": 1e9, "caption_style": ["x"], "nope": 1}, trim=[1.0, "x"])
    rows[3].update(edit={"aspect": "1:1", "zoom": 1.2}, trim=[1.0, -0.5], length=30, views=10)
    pipeline.import_learning({"format": "reelwright-learning", "feedback": rows})
    stored = {d["id"]: d for d in store.find("feedback")}
    assert "length" not in stored["h0"] or isinstance(stored["h0"]["length"], float)
    assert "views" not in stored["h0"] and "trim" not in stored["h0"] and "edit" not in stored["h0"]
    assert stored["h0"]["features"] == {}  # non-finite dropped
    assert stored["h2"]["edit"] == {}      # invalid enum / out-of-range / unknown keys dropped
    assert stored["h3"]["edit"] == {"aspect": "1:1", "zoom": 1.2} and stored["h3"]["trim"] == [1.0, -0.5]
    fb = pipeline.feedback()
    pipeline.taste_examples(), learn.hints(fb, learn.train(fb)), learn.house_style(fb), learn.trim_bias(fb)  # no crash
    store.put("feedback", {"id": "legacy", "created": 2.0, "signal": -1, "why": "rated"})  # an old row missing fields
    assert "REJECTED" in pipeline.taste_examples()


def test_filler_cut_with_no_fillers_is_a_noop():
    keeps = [(10.123456, 20.987654)]
    assert media.cut_spans(keeps, [(30.0, 31.0)]) is keeps  # nothing overlaps: the exact input comes back


def test_privacy_policy_discloses_every_service_the_code_contacts():
    """Adding a new outside service without updating the privacy policy fails here."""
    import re
    from pathlib import Path
    root = Path(media.__file__).parent
    code = "\n".join(p.read_text("utf-8") for p in root.glob("*.py"))
    policy = (root / "static" / "legal" / "privacy.html").read_text("utf-8").lower()
    disclosed = {"api.anthropic.com": "anthropic", "anthropic": "anthropic", "googleapiclient": "google", "open.tiktokapis.com": "tiktok",
                 "graph.facebook.com": "instagram", "github.com": "github", "kick.com": "kick", "yt_dlp": "yt-dlp",
                 "127.0.0.1": "your own computer", "0.0.0.0": "obs", "www.twitch.tv": "twitch", "youtu.be": "youtube",
                 "www.youtube.com": "youtube", "www.googleapis.com": "google", "rupload.facebook.com": "instagram", "graph.instagram.com": "instagram",
                 "raw.githubusercontent.com": "raw.githubusercontent.com"}
    hosts = set(re.findall(r"https?://([a-z0-9.-]+\.[a-z]{2,})", code)) | {k for k in ("googleapiclient", "yt_dlp", "anthropic") if k in code}
    hosts -= {"localhost", "console.anthropic.com", "www.python.org"}  # links shown to the user, not data flows
    missing = [h for h in hosts if disclosed.get(h, h) not in policy]
    assert not missing, f"privacy.html doesn't disclose: {missing}"
    terms = (root / "static" / "legal" / "terms.html").read_text("utf-8")
    assert "privacy.html" in terms and "terms.html" in (root / "static" / "legal" / "privacy.html").read_text("utf-8")


def test_connection_guide_lists_every_permission_the_code_requests():
    """If the code starts asking for a new scope, the setup guide must say so (and why), or verify fails."""
    from pathlib import Path
    from reelwright import platforms, youtube
    guide = (Path(media.__file__).parent / "static" / "guide" / "connect.html").read_text("utf-8")
    wanted = [*youtube.SCOPES, *platforms.TT_SCOPES.split(","), "instagram_business_basic", "instagram_business_content_publish",
              "instagram_basic", "instagram_content_publish", "pages_read_engagement", platforms.tiktok_redirect_uri(8765)]
    assert [w for w in wanted if w not in guide] == []


def test_old_youtube_sign_ins_are_asked_to_reconnect_for_new_permissions(tmp_path, monkeypatch):
    import json
    from reelwright import youtube
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({"scopes": youtube.SCOPES[:2]}), "utf-8")
    monkeypatch.setattr(youtube, "YT_TOKEN", tok)
    assert youtube.missing_scopes() == ["https://www.googleapis.com/auth/youtube.force-ssl",
                                        "https://www.googleapis.com/auth/yt-analytics.readonly"]
    with pytest.raises(RuntimeError, match="reconnect YouTube"):
        youtube.set_publish_at("vid", "2031-01-01T10:00:00+00:00")
    tok.write_text(json.dumps({"scopes": youtube.SCOPES}), "utf-8")
    assert youtube.missing_scopes() == []


def test_update_exe_rebuilds_only_when_the_source_is_newer(tmp_path):
    import importlib.util
    import time
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("update_exe", Path(media.__file__).parent.parent / "scripts" / "update_exe.py")
    up = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(up)
    src, exe = tmp_path / "src", tmp_path / "Reelwright.exe"
    src.mkdir()
    (src / "a.py").write_text("x")
    assert up.outdated(exe, [src])  # never built
    exe.write_text("exe")
    now = time.time()
    os.utime(src / "a.py", (now - 100, now - 100))
    os.utime(exe, (now, now))
    assert not up.outdated(exe, [src])  # built after the last change
    os.utime(src / "a.py", (now + 50, now + 50))
    assert up.outdated(exe, [src])  # source changed since the build


def test_ui_scale_is_saved_and_clamped():
    from fastapi.testclient import TestClient
    from reelwright.server import app
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    r = c.put("/api/settings", json={"ui_scale": {"app": 1.25, "text": 9, "timeline": 0.1, "evil": 2, "preview": "big", "panels": True}})
    assert r.json()["ui_scale"] == {"app": 1.25, "text": 2.0, "timeline": 0.6}
    assert c.get("/api/settings").json()["ui_scale"]["app"] == 1.25  # persisted


def test_event_log_rotates_and_tails(tmp_path):
    log.setup(tmp_path / "events.log", max_bytes=2000, backups=1)
    for i in range(200):
        log.event("test", f"line {i}")
    lines = log.tail(5)
    assert len(lines) == 5 and "line 199" in lines[-1]
    assert (tmp_path / "events.log.1").exists()  # rotated instead of growing forever


def test_project_paths_cannot_escape_the_projects_folder():
    for bad in ("..", "../x", "a/b", "", ".", "..\\x"):
        with pytest.raises(ValueError):
            pipeline.pdir(bad)
    assert pipeline.pdir("abc123def456").parent == pipeline.config.projects_dir()
    from fastapi.testclient import TestClient
    from reelwright.server import app
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.delete("/api/projects/%2E%2E").status_code in (400, 404)
    from fastapi import HTTPException
    from reelwright import server
    with pytest.raises(HTTPException) as e:  # straight at the route: URL normalization can't hide the bad id
        server.delete_project("..")
    assert e.value.status_code == 404
    assert pipeline.config.DATA.exists()


def test_app_shell_loads_nothing_from_the_internet():
    """Fonts/scripts/styles must be local: works offline and nothing phones home on launch."""
    import re
    from pathlib import Path
    static = Path(media.__file__).parent / "static"
    for name in ("index.html", "styles.css"):
        assert not re.findall(r"https?://", (static / name).read_text("utf-8")), name


def test_desktop_moves_off_a_port_another_program_holds(monkeypatch):
    from reelwright import desktop
    busy = {8765, 8767}  # another app (e.g. a different local tool) has the default port
    mine = set()
    monkeypatch.setattr(desktop, "_port_in_use", lambda p: p in busy or p in mine)
    monkeypatch.setattr(desktop, "_is_reelwright", lambda p: p in mine)
    assert desktop.pick_port(8765) == (8768, False)  # skips 8766 (the LAN receiver's default) and the busy 8767
    mine.add(8768)
    assert desktop.pick_port(8765) == (8768, True)   # a second launch finds the running copy instead of starting another
    mine.clear()
    mine.add(8765)
    assert desktop.pick_port(8765) == (8765, True)
