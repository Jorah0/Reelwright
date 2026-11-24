"""Clip length targeting: min / target / max settings, per-search overrides, and how clips are fitted to them."""
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

from reelwright import ai, pipeline  # noqa: E402

SEGS = [{"start": float(i * 5), "end": float(i * 5 + 4.8), "text": f"sentence {i}"} for i in range(60)]


def test_lengths_are_clamped_and_target_sits_between_min_and_max():
    assert pipeline.lengths({"clip_min": 20, "clip_target": 35, "clip_max": 60}) == (20, 35, 60)
    assert pipeline.lengths({"clip_min": 20, "clip_target": 99, "clip_max": 60}) == (20, 60, 60)
    assert pipeline.lengths({"clip_min": 20, "clip_target": 0, "clip_max": 60}) == (20, 40, 60)  # 0 = middle
    assert pipeline.lengths({"clip_min": 20, "clip_target": 35, "clip_max": 60}, max_len=30) == (20, 30, 30)  # override
    assert pipeline.lengths({"clip_min": 20, "clip_target": 35, "clip_max": 60}, target=15, max_len=25) == (12, 15, 25)
    assert pipeline.lengths({"clip_min": 20, "clip_target": 35, "clip_max": 60}, max_len="x") == (20, 35, 60)  # junk ignored


def test_an_overlong_pick_is_cut_at_the_sentence_end_nearest_the_target():
    out = ai.refine([{"start": 0.0, "end": 120.0, "score": 90}], SEGS, 300.0, 10, 60, target=30)
    assert out[0]["start"] == 0.0 and abs(out[0]["end"] - 29.8) < 0.01
    no_target = ai.refine([{"start": 0.0, "end": 120.0, "score": 90}], SEGS, 300.0, 10, 60)
    assert no_target[0]["end"] <= 60  # never over the max, with or without a target


def test_a_long_pick_far_over_the_target_is_pulled_back_toward_it():
    out = ai.refine([{"start": 0.0, "end": 58.0, "score": 90}], SEGS, 300.0, 10, 60, target=25)
    assert out[0]["end"] - out[0]["start"] <= 25 * 1.5  # aims for the target instead of filling the max
    ok = ai.refine([{"start": 0.0, "end": 29.8, "score": 90}], SEGS, 300.0, 10, 60, target=25)
    assert ok[0]["end"] == 29.8  # close enough to the target: the AI's ending is kept


def test_the_finder_is_told_the_target_and_the_hard_max(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_ask", lambda system, user, schema, effort="high": seen.update(user=user) or ai.MomentList(clips=[]))
    ai.find_moments(SEGS, 300.0, {}, 3, 15, 45, target=30)
    task = seen["user"][1]
    assert "30" in task and "45" in task and "never" in task.lower()


def test_a_search_can_override_the_lengths(monkeypatch):
    from tests.test_core import _clip_in_new_project
    pid = _clip_in_new_project()["project_id"]
    seen = {}

    def fake(segs, dur, signals, count, lo, hi, query="", guidance="", taste="", avoid=(), progress=None, target=None):
        seen.update(lo=lo, hi=hi, target=target)
        return [{"start": 100.0, "end": 140.0, "score": 80, "title": "x"}]

    monkeypatch.setattr(ai, "find_moments", fake)
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "k")
    found = pipeline._find(pid, "", count=1, target=15, max_len=20)
    assert seen["hi"] == 20 and seen["target"] == 15
    assert found and all(f["end"] - f["start"] <= 20 for f in found)


def test_settings_reject_lengths_that_cant_work():
    from fastapi.testclient import TestClient
    from reelwright.server import app
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.put("/api/settings", json={"clip_min": 50, "clip_max": 40}).status_code == 400
    r = c.put("/api/settings", json={"clip_min": 15, "clip_target": 25, "clip_max": 45})
    assert r.status_code == 200 and r.json()["clip_target"] == 25
    assert c.put("/api/settings", json={"clip_target": 90}).status_code == 400  # above the saved max


def test_a_search_with_a_picked_length_is_told_that_length(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_ask", lambda system, user, schema, effort="high": seen.update(user=user) or ai.MomentList(clips=[]))
    ai.find_moments(SEGS, 300.0, {}, 3, 12, 20, query="funny", target=15)
    task = seen["user"][1]
    assert "15" in task and "20" in task and "40" not in task  # not the wide 2x-max search range


def test_an_old_max_below_the_default_target_doesnt_block_saving_settings():
    from fastapi.testclient import TestClient
    from reelwright import config
    from reelwright.server import app
    config.SETTINGS_FILE.write_text('{"clip_min": 10, "clip_max": 30}', "utf-8")  # saved before clip_target existed
    assert config.load()["clip_target"] == 0  # out of range -> automatic, not the 35 s default
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    s = c.get("/api/settings").json()
    assert c.put("/api/settings", json={"clip_min": s["clip_min"], "clip_target": s["clip_target"], "clip_max": s["clip_max"],
                                        "model": s["model"]}).status_code == 200
