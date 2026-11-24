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


def test_event_log_rotates_and_tails(tmp_path):
    log.setup(tmp_path / "events.log", max_bytes=2000, backups=1)
    for i in range(200):
        log.event("test", f"line {i}")
    lines = log.tail(5)
    assert len(lines) == 5 and "line 199" in lines[-1]
    assert (tmp_path / "events.log.1").exists()  # rotated instead of growing forever
