"""On-screen analysis: the visual activity analyzer, its tags/features for the finder, and the multimodal judge."""
import base64
import os
import subprocess
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from reelwright import ai, config, learn, media, vision  # noqa: E402


def _synthetic(path, dur=30):
    """Dark static frame; a white flash at 10.0-10.6 s; a fast-moving box from 20 to 23 s."""
    lav = lambda spec: ["-f", "lavfi", "-i", spec]  # noqa: E731
    subprocess.run([media.FFMPEG, "-y", "-v", "error", *lav(f"color=c=0x202020:s=320x240:r=10:d={dur}"),
                    *lav(f"color=c=white:s=320x240:r=10:d={dur}"), *lav(f"color=c=yellow:s=80x80:r=10:d={dur}"),
                    "-filter_complex", "[0][1]overlay=enable='between(t,10,10.6)'[a];"
                    "[a][2]overlay=x='mod(t*500,240)':y='mod(t*300,160)':enable='between(t,20,23)'",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-t", str(dur), str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    return _synthetic(tmp_path_factory.mktemp("vis") / "v.mp4")


def test_analyzer_finds_the_flash_and_the_motion_burst(clip):
    v = vision.analyze(clip, media.probe(clip), faces=False)
    assert 29 <= len(v["motion"]) <= 31  # one value per second
    kinds = {}
    for s in v["spikes"]:
        kinds.setdefault(s["kind"], []).append(s["t"])
    assert any(abs(t - 10) <= 1 for t in kinds.get("flash", [])), v["spikes"]
    assert any(19 <= t <= 24 for t in kinds.get("action", [])), v["spikes"]
    assert not any(t < 19 or t > 24 for t in kinds.get("action", [])), v["spikes"]  # the flash (a cut-like jump) isn't action
    assert max(v["motion"][20:23]) > 3 * (max(v["motion"][:9]) + 1e-3)


def test_tagged_lines_mark_on_screen_action_flash_and_cam():
    vis = {"motion": [0.01] * 60, "face": [0.0] * 60,
           "spikes": [{"t": 12.0, "kind": "flash", "strength": 0.8}, {"t": 41.0, "kind": "face_reaction", "strength": 0.9}]}
    vis["motion"][20:26] = [0.08] * 6
    segs = [{"start": 10.0, "end": 14.0, "text": "a b"}, {"start": 20.0, "end": 25.0, "text": "c d"},
            {"start": 30.0, "end": 34.0, "text": "e f"}, {"start": 40.0, "end": 43.0, "text": "g h"}]
    lines = ai.tagged_lines(segs, {"visual": vis}).splitlines()
    assert "FLASH" in lines[0] and "ACTION" not in lines[0]
    assert "ACTION" in lines[1]
    assert "(" not in lines[2]  # a calm line stays untagged
    assert "ON-CAM" in lines[3]
    assert ai.tagged_lines(segs, {}).count("(") == 0  # old projects without visual signals still work


def test_visual_features_and_old_feedback_rows():
    vis = {"motion": [0.01] * 100, "face": [0.0] * 50 + [1.0] * 50,
           "spikes": [{"t": 70.0, "kind": "flash", "strength": 1.0}]}
    vis["motion"][60:80] = [0.1] * 20
    hot = learn.features({"start": 60.0, "end": 80.0}, [], {"visual": vis}, 100.0)
    calm = learn.features({"start": 10.0, "end": 30.0}, [], {"visual": vis}, 100.0)
    assert {"motion", "flashes", "face_share"} <= set(learn.FEATURES) and set(hot) == set(learn.FEATURES)
    assert hot["motion"] > 3 * calm["motion"] and hot["flashes"] > 0 == calm["flashes"]
    assert hot["face_share"] == 1.0 and calm["face_share"] == 0.0
    none = learn.features({"start": 10.0, "end": 30.0}, [], {}, 100.0)  # no visual signals: neutral values
    assert none["motion"] == 1.0 and none["flashes"] == 0 and none["face_share"] == 0
    # feedback recorded before these features existed: same neutral values, so "old row" can't become a learned signal
    old = {k: 0.5 for k in learn.FEATURES if k not in ("motion", "flashes", "face_share")}
    X, _, _ = learn.samples([{"signal": 1, "why": "rated", "features": old}])
    row = dict(zip(learn.FEATURES, X[0]))
    assert row["motion"] == 1.0 and row["flashes"] == 0.0 and row["face_share"] == 0.0
    fb = [{"signal": 1 if i % 2 else -1, "why": "rated", "features": {**old, "loud": 2.0 if i % 2 else 0.5}} for i in range(16)]
    m = learn.train(fb)
    assert m and len(m["w"]) == len(learn.FEATURES) and 0 <= learn.predict(m, old) <= 1


def _fake_anthropic(monkeypatch, sent):
    import anthropic

    class Fake:
        def __init__(self, **kw):
            def parse(**kw):
                sent.update(kw)
                return type("R", (), {"stop_reason": "end_turn", "usage": {}, "model": "claude-opus-5-5",
                                      "parsed_output": ai.Ranking(ranked=[ai.Ranked(index=1, score=90, reason="visual payoff")])})()
            self.beta = type("B", (), {"messages": type("M", (), {"parse": staticmethod(parse)})()})()

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    monkeypatch.setattr(ai.config, "api_key", lambda: "k")


def test_judge_attaches_small_frames_per_candidate_offline(clip, monkeypatch):
    sent = {}
    _fake_anthropic(monkeypatch, sent)
    cands = [{"start": float(t), "end": t + 5.0, "title": f"c{t}", "hook": "", "score": 50} for t in range(0, 30, 2)]  # 15
    out = ai.rank_moments(cands, [], 2, video=clip)
    assert out[0]["start"] == 2.0 and out[0]["reason"] == "visual payoff"
    blocks = sent["messages"][0]["content"]
    imgs = [b for b in blocks if b["type"] == "image"]
    assert 12 * 3 <= len(imgs) <= 12 * 4  # 3-4 frames each, capped at 12 candidates
    assert imgs[0]["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(imgs[0]["source"]["data"])[:2] == b"\xff\xd8"
    assert any(b["type"] == "text" and "#0 " in b["text"] for b in blocks)
    sent.clear()
    config.save({"visual_judge": False})
    try:
        ai.rank_moments(cands, [], 2, video=clip)
    finally:
        config.save({"visual_judge": True})
    assert not any(b.get("type") == "image" for b in sent["messages"][0]["content"] if isinstance(b, dict))


def test_ask_keeps_cache_breakpoint_with_image_blocks(monkeypatch):
    import anthropic
    sent = {}

    class Fake:
        def __init__(self, **kw):
            parse = staticmethod(lambda **kw: sent.update(kw) or type(
                "R", (), {"stop_reason": "end_turn", "parsed_output": ai.Query(query="q")})())
            self.beta = type("B", (), {"messages": type("M", (), {"parse": parse})()})()

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    monkeypatch.setattr(ai.config, "api_key", lambda: "k")
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AA=="}}
    ai._ask("sys", ("CONTEXT", [{"type": "text", "text": "TASK"}, img]), ai.Query)
    blocks = sent["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "CONTEXT", "cache_control": {"type": "ephemeral"}}
    assert blocks[1:] == [{"type": "text", "text": "TASK"}, img]


def test_no_speech_clips_are_cut_around_visual_spikes():
    from reelwright import pipeline
    spikes = [{"t": 100.0, "kind": "action", "strength": 1.0}, {"t": 110.0, "kind": "action", "strength": 0.5},
              {"t": 300.0, "kind": "flash", "strength": 0.8}]
    picks = pipeline.visual_picks(spikes, 30, 400.0)
    assert [(p["start"], p["end"]) for p in picks] == [(95.0, 125.0), (295.0, 325.0)]  # the overlapping weaker one is dropped
    assert picks[0]["score"] > picks[1]["score"] and "Action" in picks[0]["title"] and "flash" in picks[1]["reason"]
