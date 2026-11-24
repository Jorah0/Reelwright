"""Checks for the pure logic: crop math, filter graphs, captions, clip refinement, scheduling."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["REELWRIGHT_DATA"] = tempfile.mkdtemp()  # always a throwaway store: some tests wipe docs

from reelwright import ai, media, pipeline, youtube  # noqa: E402

SEGS = [{"start": float(i * 5), "end": float(i * 5 + 4.8), "text": f"sentence number {i} here",
         "words": [{"s": i * 5 + j * 1.2, "e": i * 5 + j * 1.2 + 1.0, "w": f" w{i}_{j}"} for j in range(4)]} for i in range(40)]


def test_crop_box_vertical_from_landscape_is_clamped_and_even():
    assert media.crop_box(1920, 1080, 9 / 16, 0.5, 0.5) == (608, 1080, 656, 0)
    w, h, x, y = media.crop_box(1920, 1080, 9 / 16, 1.0, 0.5)
    assert x + w == 1920 and w % 2 == 0
    assert media.crop_box(1920, 1080, 9 / 16, 0.0, 0.5)[2] == 0


def test_crop_box_landscape_from_vertical_crops_height():
    w, h, x, y = media.crop_box(1080, 1920, 16 / 9, 0.5, 0.5)
    assert (w, x) == (1080, 0) and h == 608


def test_build_filters_speed_captions_fades():
    g, size = media.build_filters({"aspect": "9:16", "speed": 1.5, "fade_out": 1, "contrast": 1.2}, 1920, 1080, 20, True)
    vf, af = g.split(";[0:a]")
    assert size == (1080, 1920)
    assert vf.startswith("[0:v]setpts=PTS/1.5,crop=") and vf.endswith("[v]")
    assert "subtitles=captions.ass" in vf and "fade=t=out:st=19.00" in vf and "eq=" in vf
    assert "atempo=1.5000" in af and "loudnorm" in af and af.endswith("[a]")


def test_atempo_chains_out_of_range_speeds():
    import math
    for speed in (4.0, 0.25, 3.0):
        factors = [float(f.split("=")[1]) for f in media._atempo(speed)]
        assert all(0.5 <= f <= 2.0 for f in factors) and math.isclose(math.prod(factors), speed, rel_tol=1e-3)


def test_blur_fit_graph():
    vf, _ = media.build_filters({"aspect": "9:16", "fit": "blur"}, 1920, 1080, 10, False)
    assert "boxblur" in vf and "overlay" in vf and "subtitles" not in vf


def test_ass_highlights_each_word_and_offsets_time():
    ass = media.build_ass(SEGS[2]["words"], 10.0, 15.0, 1.0, (1080, 1920), title="Hi {there}")
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue: 0")]
    assert len(lines) == 4 and lines[0].startswith("Dialogue: 0,0:00:00.00")
    assert "Title,,0,0,0,,Hi (there)" in ass
    assert "W2_0" in lines[0]


def test_refine_snaps_and_enforces_length_and_overlap():
    clips = [{"start": 11.0, "end": 31.0, "score": 90}, {"start": 12.0, "end": 30.0, "score": 50}, {"start": 100.3, "end": 300.0, "score": 70}]
    out = ai.refine(clips, SEGS, 200.0, 15, 40)
    assert out[0]["start"] == 10.0 and out[0]["end"] == 29.8
    assert len(out) == 2  # overlapping lower-score clip dropped
    assert out[1]["end"] - out[1]["start"] <= 40


def test_heuristic_moments_without_api_key():
    loud = [{"t": 52.0, "energy": 1.0}]
    out = ai.heuristic_moments(SEGS, loud, 200.0, 3, 15, 40)
    assert 1 <= len(out) <= 3
    assert all(15 <= c["end"] - c["start"] <= 40 for c in out)


def test_stagger_and_publish_at_guard():
    times = pipeline.stagger("2030-01-01T09:00:00+00:00", 12, 3)
    assert times[2].startswith("2030-01-02T09:00")
    assert youtube.effective_publish_at("2030-01-01T09:00:00Z") == "2030-01-01T09:00:00.000Z"
    soon = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    assert youtube.effective_publish_at(soon) is None


def test_autopilot_never_schedules_in_the_past():
    past = (datetime.now(timezone.utc) - timedelta(days=9, hours=3)).replace(microsecond=0)
    nxt = datetime.fromisoformat(pipeline.next_slot(past.isoformat()))
    assert nxt > datetime.now(timezone.utc) and nxt.time() == past.time()  # same time of day, rolled forward
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    assert pipeline.next_slot(future) == datetime.fromisoformat(future).isoformat()


def test_jump_cuts_keep_speech_and_remap_times():
    keeps = media.keep_segments([[12.0, 14.0], [20.0, 20.2], [25.0, 40.0]], 10.0, 30.0)
    assert keeps == [(10.0, 12.12), (13.88, 25.12)]  # short 0.2 s pause kept, trailing silence trimmed
    assert media.remap(10.0, keeps) == 0 and abs(media.remap(14.0, keeps) - 2.24) < 1e-6
    assert abs(media.remap(13.0, keeps) - 2.12) < 1e-6  # inside a removed gap snaps forward
    g, _ = media.build_filters({"aspect": "9:16"}, 1920, 1080, 13.36, False, [(0, 2.12), (3.88, 15.12)])
    assert "select='between(t,0.000,2.120)+between(t,3.880,15.120)'" in g and "aselect=" in g


def test_split_layout_and_music_mix():
    g, size = media.build_filters({"aspect": "9:16", "layout": "split", "split": 0.4}, 1920, 1080, 10, False, music=True)
    assert size == (1080, 1920) and "vstack" in g and "scale=1080:768" in g and "scale=1080:1152" in g
    assert "amix=inputs=2" in g and "[1:a]volume=0.150" in g


def test_zoom_shrinks_crop():
    assert media.crop_box(1920, 1080, 16 / 9, 0.5, 0.5, 2.0)[:2] == (960, 540)


def test_tiktok_chunking_rules():
    from reelwright import platforms
    assert platforms.tiktok_chunks(1000) == [(0, 999)]
    big = 150 * 1024 * 1024
    ch = platforms.tiktok_chunks(big)
    assert ch[0][0] == 0 and ch[-1][1] == big - 1 and all(b - a + 1 >= 10 * 1024 * 1024 for a, b in ch)


def _clip_in_new_project(**kw):
    from reelwright import store
    p = store.put("project", {"name": "t", "meta": {"duration": 200.0, "width": 1920, "height": 1080}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    pipeline.save_json(p["id"], "transcript.json", {"segments": SEGS})
    return pipeline.new_clip(p["id"], {"start": 10.0, "end": 30.0, "title": "A pick", "score": 70, **kw}, source="ai")


def test_feedback_memory_survives_and_explicit_beats_implicit():
    from reelwright import store
    clip = _clip_in_new_project()
    pipeline.remember(clip, "published")
    assert store.get("feedback", clip["id"])["signal"] == 1
    pipeline.remember({**clip, "rating": -1}, "rated")  # explicit rating overrides the implicit one
    pipeline.remember(clip, "dismissed")                 # ...and implicit signals never overwrite it
    fb = store.get("feedback", clip["id"])
    assert (fb["signal"], fb["why"]) == (-1, "rated") and "sentence number 2" in fb["text"]
    store.delete("clip", clip["id"])
    assert store.get("feedback", clip["id"])  # memory outlives the clip
    pipeline.remember({**clip, "rating": 0}, "rated")    # un-rating forgets it
    assert store.get("feedback", clip["id"]) is None


def test_taste_examples_are_bounded_and_split_by_signal():
    from reelwright import store
    for d in store.find("feedback"):
        store.delete("feedback", d["id"])
    for i in range(20):
        pipeline.remember({**_clip_in_new_project(title=f"pick {i}"), "rating": 1 if i % 2 else -1}, "rated")
    lines = pipeline.taste_examples(per_side=4).splitlines()
    assert sum(l.startswith("+ ") for l in lines) == 4 and sum(l.startswith("- ") for l in lines) == 4
    assert any("LIKED" in l for l in lines) and any("REJECTED" in l for l in lines)


def test_reanalysis_keeps_clips_the_user_touched():
    assert pipeline.replaceable({"source": "ai", "rating": 0, "rev": 1, "posts": {}, "export": None})
    for touched in ({"rating": 1}, {"rev": 2}, {"posts": {"youtube": {}}}, {"export": {"status": "done"}}, {"source": "manual"}):
        assert not pipeline.replaceable({"source": "ai", "rating": 0, "rev": 1, "posts": {}, "export": None, **touched})


def test_energy_spikes_scale_with_video_length():
    import numpy as np
    rng = np.random.default_rng(0)
    smooth = rng.random(3 * 3600 * 10)  # 3 h of 0.1 s frames
    for k in range(0, len(smooth), 3000):  # a burst every 5 min
        smooth[k] = 5.0
    spikes = media.energy_spikes(smooth)
    assert len(spikes) > 25 and spikes[-1]["t"] > 2.5 * 3600  # the back half of a long VOD is covered


def test_even_sample_covers_whole_range():
    s = ai.even_sample(list(range(1000)), 10)
    assert len(s) == 10 and s[0] == 0 and s[-1] == 999
    assert ai.even_sample([1, 2], 10) == [1, 2]


def test_find_moments_caches_transcript_and_puts_volatile_text_last(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_ask", lambda system, user, schema, effort="high": seen.update(system=system, user=user)
                        or ai.MomentList(clips=[]))
    ai.find_moments(SEGS, 200.0, {"scenes": list(range(2000))}, 3, 15, 40, query="funny", taste="+ liked: x")
    ctx, task = seen["user"]
    assert "TRANSCRIPT" in ctx and "funny" not in ctx and "liked" not in ctx  # stable prefix → cacheable across searches
    assert "funny" in task and "liked" in task and "liked" not in seen["system"]
    assert "1999" in ctx  # late scene cuts reach the model


def test_ask_marks_context_block_cacheable(monkeypatch):
    import anthropic
    sent = {}

    class Fake:
        def __init__(self, **kw):
            parse = staticmethod(lambda **kw: sent.update(kw) or type(
                "R", (), {"stop_reason": "end_turn", "parsed_output": ai.Query(query="q")})())
            self.beta = type("B", (), {"messages": type("M", (), {"parse": parse})()})()

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    monkeypatch.setattr(ai.config, "api_key", lambda: "k")
    ai._ask("sys", ("CONTEXT", "TASK"), ai.Query)
    blocks = sent["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "CONTEXT", "cache_control": {"type": "ephemeral"}}
    assert blocks[1] == {"type": "text", "text": "TASK"}


def test_remember_is_safe_and_ignores_worked_on_clips():
    from reelwright import store
    pipeline.remember(None, "published")  # clip deleted mid-upload: must not raise (would fail a finished post)
    edited = {**_clip_in_new_project(), "rev": 3}
    pipeline.remember(edited, "dismissed")  # deleting a clip you worked on isn't a "bad pick" lesson
    assert store.get("feedback", edited["id"]) is None
    posted = {**_clip_in_new_project(), "rating": 1, "posts": {"youtube": {}}}
    pipeline.remember(posted, "rated")
    pipeline.remember({**posted, "rating": 0}, "rated")  # un-rating a posted clip falls back to "published"
    assert store.get("feedback", posted["id"])["why"] == "published"


def test_find_more_skips_existing_clips_but_still_fills_the_count(monkeypatch):
    clip = _clip_in_new_project()  # existing clip at 10-30 s
    pid = clip["project_id"]
    seen = {}

    def fake(segs, dur, signals, count, lo, hi, query="", guidance="", taste="", avoid=(), progress=None, target=None):
        seen.update(count=count, avoid=avoid)
        return [{"start": 10.0, "end": 29.8, "score": 90, "title": "dup"}] + [
            {"start": 40.0 + 25 * i, "end": 59.8 + 25 * i, "score": 80 - i, "title": f"new {i}"} for i in range(5)]

    monkeypatch.setattr(ai, "find_moments", fake)
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "k")
    found = pipeline._find(pid, "", count=3)
    assert [f["title"] for f in found] == ["new 0", "new 1", "new 2"]
    assert seen["count"] > 3 and (10.0, 30.0) in seen["avoid"]


def test_energy_spikes_on_busy_long_audio_respect_spacing_and_budget():
    # deterministic (no wall-clock assert: a timing bound flaked under load); the O(1) spacing check is in the code
    import numpy as np
    smooth = np.random.default_rng(1).random(6 * 3600 * 10)  # 6 h where most frames are "loud"
    spikes = media.energy_spikes(smooth)
    assert len(spikes) == 6 * 3600 * 10 // 900  # budget filled: ~1 per 90 s
    assert all(b["t"] - a["t"] > 10 for a, b in zip(spikes, spikes[1:]))


def test_ass_sticker_and_jumpcut_captions():
    keeps = [(10.0, 11.5), (13.0, 15.0)]
    ass = media.build_ass(SEGS[2]["words"], 10.0, 15.0, 1.0, (1080, 1920), sticker="@me", keeps=keeps, style="boxed")
    assert "Sticker,,0,0,0,,@me" in ass
    assert "W2_1" not in ass  # word at 11.2-12.2 has its midpoint in the removed gap
