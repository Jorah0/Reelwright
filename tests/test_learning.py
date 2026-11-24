"""Personal learning: features, reranker, house style, trim bias, and long-video scanning."""
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import numpy as np  # noqa: E402

from reelwright import ai, learn  # noqa: E402

SEGS = [{"start": float(i * 5), "end": float(i * 5 + 4.8), "text": f"sentence number {i} here", "words": []} for i in range(40)]


def _fb(signal, why="rated", **feat):
    base = {k: 0.5 for k in learn.FEATURES}
    return {"signal": signal, "why": why, "features": {**base, **feat}, "length": 30.0}


def test_features_capture_loudness_speech_and_question_hooks():
    wave = [0.1] * 2000
    wave[100:300] = [0.9] * 200  # loud from 10-30 s
    sig = {"waveform": wave, "loud": [{"t": 12.0, "energy": 1}], "scenes": [11.0, 20.0]}
    loud = learn.features({"start": 10.0, "end": 30.0, "score": 80}, SEGS, sig, 200.0)
    quiet = learn.features({"start": 100.0, "end": 120.0, "score": 80}, SEGS, sig, 200.0)
    assert set(loud) == set(learn.FEATURES)
    assert loud["loud"] > 3 * quiet["loud"] and loud["spikes"] > 0 and loud["cuts"] > 0
    q = learn.features({"start": 0.0, "end": 5.0}, [{"start": 0.5, "end": 4, "text": "Why does this work?"}], {}, 100.0)
    assert q["question"] == 1.0 and q["lead"] > 0


def test_reranker_learns_a_real_pattern_and_refuses_noise():
    rng = np.random.default_rng(3)
    fb = [_fb(1 if loud > 1.5 else -1, loud=loud) for loud in rng.uniform(0.5, 2.5, 60)]  # creator likes loud clips
    m = learn.train(fb)
    assert m and m["useful"] and m["cv_acc"] > m["baseline"] + 0.05
    assert learn.predict(m, {**fb[0]["features"], "loud": 2.4}) > learn.predict(m, {**fb[0]["features"], "loud": 0.6})
    assert max(m["weights"], key=lambda k: abs(m["weights"][k])) == "loud"
    noise = [_fb(int(rng.choice([1, -1])), loud=x) for x in rng.uniform(0.5, 2.5, 60)]
    assert not (learn.train(noise) or {}).get("useful")  # random labels must not steer picks
    assert learn.train(fb[:5]) is None  # too little data


def test_rerank_blends_and_reorders_only_when_useful():
    m = {"useful": True, "mu": [0.0] * len(learn.FEATURES), "sd": [1.0] * len(learn.FEATURES),
         "w": [0.0] * len(learn.FEATURES), "b": 0.0}
    m["w"][learn.FEATURES.index("loud")] = 5.0
    a = {"score": 80, "features": {k: 0.0 for k in learn.FEATURES}}
    b = {"score": 75, "features": {**{k: 0.0 for k in learn.FEATURES}, "loud": 2.0}}
    out = learn.rerank([a, b], m)
    assert out[0] is b and b["ai_score"] == 75 and b["fit"] > 90
    fresh = [{"score": 80, "features": a["features"]}, {"score": 75, "features": b["features"]}]
    assert learn.rerank(fresh, {**m, "useful": False})[0]["score"] == 80  # a model that can't beat baseline changes nothing


def test_views_relabel_published_clips():
    fb = [{**_fb(1, "published"), "views": v} for v in (100, 200, 5000, 8000)]
    X, y, w = learn.samples(fb)
    assert list(y) == [0.0, 0.0, 1.0, 1.0]  # posted but underperformed counts against


def test_house_style_needs_agreement():
    edits = [{"signal": 1, "edit": {"caption_style": "neon", "aspect": "9:16", "zoom": 1.2}},
             {"signal": 1, "edit": {"caption_style": "neon", "aspect": "1:1", "zoom": 1.2}},
             {"signal": 1, "edit": {"caption_style": "neon", "aspect": "16:9", "zoom": 1.25}},
             {"signal": -1, "edit": {"caption_style": "bold"}}]
    hs = learn.house_style(edits)
    assert hs["caption_style"] == "neon" and "aspect" not in hs and abs(hs["zoom"] - 1.2) < 0.06
    assert learn.house_style(edits[:2]) == {}


def test_trim_bias_shifts_future_picks():
    fb = [{"signal": 1, "trim": [1.5 + i * 0.1, -1.0]} for i in range(6)]
    bias = learn.trim_bias(fb)
    assert abs(bias["start"] - 1.75) < 0.01 and bias["end"] == -1.0
    shifted = learn.apply_trim([{"start": 10.0, "end": 40.0}], bias)
    assert shifted[0]["start"] == 11.75 and shifted[0]["end"] == 39.0 and shifted[0]["ai_bounds"] == [10.0, 40.0]
    assert learn.trim_bias(fb[:3]) is None


def test_tagged_lines_mark_loud_fast_and_chat_lines():
    wave = [0.1] * 400
    wave[50:100] = [0.8] * 50  # 5-10 s loud
    chat = [1, 1, 1, 1, 30, 1]  # counts per 10 s: spike at 40-50 s
    segs = [{"start": 5.0, "end": 9.8, "text": "huge play"}, {"start": 41.0, "end": 43.0, "text": "a b c d e f g h i j k"},
            {"start": 20.0, "end": 25.0, "text": "calm"}]
    lines = ai.tagged_lines(segs, {"waveform": wave, "chat": chat}).splitlines()
    assert "LOUD" in lines[0] and "fast" in lines[1] and "CHAT SPIKE" in lines[1] and "(" not in lines[2]


def test_long_videos_are_scanned_in_chunks_then_judged(monkeypatch):
    calls = []

    def fake_pass(segments, duration, signals, count, *a, **k):
        calls.append((segments[0]["start"], segments[-1]["end"], count))
        t = segments[len(segments) // 2]["start"]
        return [{"start": t, "end": t + 30, "title": f"at {t}", "hook": "", "reason": "", "score": 60, "hashtags": []}]

    monkeypatch.setattr(ai, "_find_pass", fake_pass)
    monkeypatch.setattr(ai, "rank_moments", lambda cands, segs, count, **k: sorted(cands, key=lambda c: -c["start"])[:count])
    segs = [{"start": float(t), "end": t + 9.0, "text": "words " * 20, "words": []} for t in range(0, 4 * 3600, 10)]
    out = ai.find_moments(segs, 4 * 3600.0, {}, 3, 20, 60)
    assert len(calls) >= 5 and max(c[1] for c in calls) > 3.9 * 3600  # the whole stream was scanned (chunks run in parallel)
    assert len(out) == 3 and out[0]["start"] > out[-1]["start"]  # judged across chunks
    calls.clear()
    ai.find_moments(segs[:300], 3000.0, {}, 3, 20, 60)  # short: one pass, no judge
    assert len(calls) == 1
