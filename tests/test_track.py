"""Subject tracking: association, camera-operator smoothing, manual keyframes, the ffmpeg crop expression, API validation."""
import os
import subprocess
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from reelwright import media, server, track  # noqa: E402


def c(cx, cy, w=0.1, score=0.9, src="face"):
    return {"cx": cx, "cy": cy, "w": w, "h": w, "score": score, "source": src}


def evaluate(expr: str, t: float) -> float:
    """Tiny evaluator for the generated expression: numbers, + - *, clip(), and ffmpeg's t."""
    assert set(expr) <= set("0123456789.+-*(),tclipg")  # our own output, charset-checked before eval
    return eval(expr, {"__builtins__": {}}, {"clip": lambda v, a, b: min(max(v, a), b), "gte": lambda a, b: float(a >= b), "t": t})


def test_association_keeps_the_same_subject_when_a_bigger_distractor_appears():
    prev = (0.3, 0.5)
    me, other = c(0.32, 0.5, 0.08), c(0.75, 0.45, 0.2)  # the distractor is bigger and as confident
    assert track.associate([other, me], prev) is me
    assert track.associate([other, me], None) is other  # no lock yet: the most prominent subject wins


def test_lock_survives_a_frame_where_only_the_distractor_is_seen():
    frames = [(0.0, [c(0.3, 0.5)]), (0.25, [c(0.31, 0.5), c(0.8, 0.5, 0.2)]), (0.5, [c(0.8, 0.5, 0.2)]),
              (0.75, [c(0.32, 0.5), c(0.8, 0.5, 0.2)])]
    raw = track.follow(frames, cuts=[])
    assert [round(p[1], 2) for p in raw] == [0.3, 0.31, 0.32]  # the lone far detection at 0.5 s is ignored, not jumped to


def test_dead_zone_holds_still_on_jitter():
    raw = [[i * 0.25, 0.5 + (0.01 if i % 2 else -0.01), 0.5, 0.1, 0.1] for i in range(40)]
    pts = track.smooth(raw, [], 10.0, crop_w=0.3164, crop_h=1.0)
    assert len({round(p[1], 4) for p in pts}) == 1  # camera never moved
    assert len(pts) <= 2 * 10 + 1


def test_large_move_eases_with_capped_speed_and_settles():
    raw = [[i * 0.25, 0.2 if i < 8 else 0.8, 0.5, 0.1, 0.1] for i in range(40)]
    pts = track.smooth(raw, [], 10.0, crop_w=0.3164, crop_h=1.0)
    xs = [track.at(pts, t)[0] for t in [i * 0.05 for i in range(200)]]
    steps = [abs(b - a) / 0.05 for a, b in zip(xs, xs[1:])]
    assert max(steps) <= track.MAX_SPEED + 1e-6  # never whips faster than an operator can pan
    assert abs(xs[-1] - 0.8) < 0.3164 * 0.08  # ends with the subject inside the dead zone


def test_gap_is_filled_by_interpolation_not_a_jump():
    raw = [[0.0, 0.3, 0.5, 0.1, 0.1], [0.25, 0.3, 0.5, 0.1, 0.1], [3.0, 0.45, 0.5, 0.1, 0.1], [3.25, 0.45, 0.5, 0.1, 0.1]]
    pts = track.smooth(raw, [], 4.0, crop_w=0.1, crop_h=1.0, dead=0.0)
    mid = track.at(pts, 1.6)[0]
    assert 0.32 < mid < 0.43  # moving through the gap, not holding then jumping


def test_scene_cut_resets_instantly():
    raw = [[i * 0.25, 0.2 if i * 0.25 < 5 else 0.8, 0.5, 0.1, 0.1] for i in range(40)]
    pts = track.smooth(raw, [5.0], 10.0, crop_w=0.3164, crop_h=1.0)
    assert abs(track.at(pts, 4.9)[0] - 0.2) < 0.03 and abs(track.at(pts, 5.05)[0] - 0.8) < 0.03
    # and association restarts at the cut (no lock carried into the new shot)
    raw2 = track.follow([(4.75, [c(0.2, 0.5)]), (5.0, [c(0.8, 0.5, 0.2)])], cuts=[5.0])
    assert round(raw2[-1][1], 2) == 0.8


def test_manual_keyframe_wins_at_its_time_and_blends_nearby():
    pts = [[0.0, 0.5, 0.5, 0.1, 0.1], [10.0, 0.5, 0.5, 0.1, 0.1]]
    merged = track.merge(pts, [[4.0, 0.2, 0.4]])
    assert track.at(merged, 4.0)[:2] == pytest.approx((0.2, 0.4))
    assert 0.2 < track.at(merged, 4.75)[0] < 0.5  # pulled toward it within 1.5 s
    assert track.at(merged, 6.0)[0] == pytest.approx(0.5)  # untouched beyond the blend window
    assert track.at(track.merge([], [[2.0, 0.3, 0.5]], base=(0.5, 0.5)), 2.0)[0] == pytest.approx(0.3)


def test_crop_path_and_expression_match_the_piecewise_path():
    pts = [[0.0, 0.2, 0.5, 0.1, 0.1], [2.0, 0.8, 0.5, 0.1, 0.1], [3.0, 0.8, 0.5, 0.1, 0.1], [5.0, 0.4, 0.5, 0.1, 0.1]]
    w, h, keys = track.crop_path(pts, 1920, 1080, 9 / 16, 1.0)
    assert (w, h) == (608, 1080)
    xs = [(t, x) for t, x, _ in keys]
    expr = track.ffmpeg_expr(xs)
    for t in (0.0, 0.5, 1.0, 2.0, 2.5, 3.7, 5.0, 9.0, -1.0):
        want = media.crop_box(1920, 1080, 9 / 16, track.at(pts, t)[0], 0.5)[2]
        assert abs(evaluate(expr, t) - want) <= 1.0, t
    assert track.ffmpeg_expr([(0, 100), (5, 100)]) == "100"


def test_expression_length_is_bounded():
    keys = [(i * 0.1, (i * 37) % 900) for i in range(3000)]
    assert len(track.ffmpeg_expr(keys)) < 9000


def _clip(**edit):
    return {"start": 10.0, "end": 20.0, "edit": {**media.DEFAULT_EDIT, **edit}}


def test_clean_edit_validates_track_manual_and_words():
    clip = _clip()
    ok = server.clean_edit({"follow": 1, "track_manual": [[1, 0.2, 0.3]], "words": [{"s": 11, "e": 11.4, "w": "hey"}]}, clip)
    assert ok == {"follow": True, "track_manual": [[1.0, 0.2, 0.3]], "words": [{"s": 11.0, "e": 11.4, "w": "hey"}]}
    assert server.clean_edit({"words": None}, clip) == {"words": None}
    for bad in ({"track_manual": [[11, 0.2, 0.3]]}, {"track_manual": [[1, 1.2, 0.3]]}, {"track_manual": [["x", 0.2, 0.3]]},
                {"track_manual": [[1, 0.2]]}, {"track_manual": "nope"}, {"track_manual": [[1, 0.5, 0.5]] * 501},
                {"words": [{"s": 5, "e": 6, "w": "x"}]}, {"words": [{"s": 12, "e": 11, "w": "x"}]},
                {"words": [{"s": 11, "e": 12, "w": "x" * 41}]}, {"words": "hi"},
                {"words": [{"s": 11, "e": 11.1, "w": "a"}] * 2001}):
        with pytest.raises(HTTPException):
            server.clean_edit(bad, clip)


def test_build_filters_uses_the_expression_only_when_following():
    tr = {"points": [[0.0, 0.2, 0.5, 0.1, 0.1], [5.0, 0.8, 0.5, 0.1, 0.1]], "manual": []}
    on, _ = media.build_filters({"aspect": "9:16", "follow": True, "track": tr}, 1920, 1080, 10, False)
    off, _ = media.build_filters({"aspect": "9:16", "follow": False, "track": tr}, 1920, 1080, 10, False)
    assert "clip(t" in on and "clip(t" not in off and "crop=608:1080:656:0" in off
    for edit in ({"aspect": "9:16", "fit": "blur"}, {"aspect": "source"}, {"aspect": "9:16", "layout": "split"}):
        g, _ = media.build_filters({**edit, "follow": True, "track": tr}, 1920, 1080, 10, False)
        assert "clip(t" not in g
    # the crop runs before jump cuts / speed so its t stays clip time
    sped, _ = media.build_filters({"aspect": "9:16", "speed": 2, "follow": True, "track": tr}, 1920, 1080, 10, False)
    assert sped.startswith("[0:v]crop=608:1080:'") and "setpts=PTS/2" in sped


def test_tracking_follows_a_moving_box(tmp_path, monkeypatch):
    video = tmp_path / "box.mp4"
    r = subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=4:r=25",
                        "-f", "lavfi", "-i", "color=c=white:s=40x40:d=4:r=25", "-filter_complex", "[0][1]overlay=x='20+60*t':y=70",
                        "-pix_fmt", "yuv420p", str(video)], capture_output=True)
    if r.returncode:
        pytest.skip("ffmpeg can't make a test video here")
    monkeypatch.setattr(track, "_detectors", lambda w, h: (None, None))  # offline: motion fallback only
    out = track.track(video, 0.0, 4.0, {"width": 320, "height": 180}, [])
    counts = out["source_counts"]
    assert out["points"] and counts.get("salient", 0) + counts.get("motion", 0) > 5 and out["updated"]
    xs = [p[1] for p in out["points"]]
    assert xs[-1] > xs[0] + 0.2  # followed the box left to right
    assert all(0 <= p[0] <= 4.0 for p in out["points"])
    # ffmpeg accepts the follow expression (with a manual keyframe and a speed change on top)
    rendered = tmp_path / "out.mp4"
    edit = {"aspect": "9:16", "follow": True, "speed": 1.25, "loudnorm": False,
            "track": {"points": out["points"], "manual": [[1.0, 0.9, 0.5]]}}
    media.render(video, rendered, 0.0, 4.0, edit, {"width": 320, "height": 180, "has_audio": False}, None)
    assert media.probe(rendered)["width"] == 1080


# ---------- anime faces, saliency, picked targets ----------

def test_anime_cascade_loads_when_available():
    import cv2
    if not hasattr(cv2, "CascadeClassifier"):
        pytest.skip("this OpenCV build has no cascade classifier")
    try:
        media.fetch_model("lbpcascade_animeface.xml", track.ANIME_URL)
    except Exception:
        pytest.skip("can't download the anime face model offline")
    _, anime = track._detectors(320, 180)
    assert anime is not None and not anime.empty()


def _frame_with_square(x=200, y=60, size=40):
    import numpy as np
    rng = np.random.default_rng(1)
    f = (rng.random((180, 320, 3)) * 30).astype(np.uint8)  # dim noise
    f[y:y + size, x:x + size] = (40, 200, 255)
    return f


def test_salient_object_is_a_candidate_and_face_mode_ignores_it():
    import cv2
    if not hasattr(cv2, "saliency"):
        pytest.skip("no cv2.saliency in this build")
    best = max(track.salient(_frame_with_square()), key=lambda c: c["score"])
    assert best["source"] == "salient" and abs(best["cx"] - 220 / 320) < 0.06 and abs(best["cy"] - 80 / 180) < 0.1
    cands, _ = track.candidates(_frame_with_square(), None, None, None, mode="auto")
    assert cands and {c["source"] for c in cands} == {"salient"}  # no faces: salient comes before motion
    assert track.candidates(_frame_with_square(), None, None, None, mode="face")[0] == []


def _object_video(tmp_path):
    """A textured object moving left to right (x = 20 + 60 t on 320 px) and a static look-alike distractor."""
    video = tmp_path / "obj.mp4"
    r = subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=0x202020:s=320x180:d=4:r=25",
                        "-f", "lavfi", "-i", "testsrc=s=40x40:d=4:r=25", "-f", "lavfi", "-i", "smptebars=s=40x40:d=4:r=25",
                        "-filter_complex", "[0][1]overlay=x='20+60*t':y=30[a];[a][2]overlay=x=140:y=120",
                        "-pix_fmt", "yuv420p", str(video)], capture_output=True)
    if r.returncode:
        pytest.skip("ffmpeg can't make a test video here")
    return video


@pytest.mark.parametrize("use_tracker", [True, False])
def test_target_mode_follows_the_picked_object_both_ways(tmp_path, monkeypatch, use_tracker):
    video = _object_video(tmp_path)
    if not use_tracker:
        monkeypatch.setattr(track, "_new_tracker", lambda frame, box: None)  # template-matching fallback
    target = {"t": 2.0, "x": 140 / 320, "y": 30 / 180, "w": 40 / 320, "h": 40 / 180}
    out = track.track(video, 0.0, 4.0, {"width": 320, "height": 180}, [], mode="target", target=target)
    assert set(out["source_counts"]) == {"target"} and out["source_counts"]["target"] > 10
    raw = track.follow(track.target_frames(track.decode(video, 0.0, 4.0, 320, 180, 4.0), 4.0, target), [])
    for t, cx, cy, *_ in raw:  # every tracked point sits on the object, never on the distractor
        assert abs(cx - (40 + 60 * t) / 320) < 0.06 and abs(cy - 50 / 180) < 0.08, (t, cx, cy)
    assert raw[0][0] <= 0.5 and raw[-1][0] >= 3.5  # followed backward to the start and forward to the end


def test_track_request_validation_and_repeat():
    clip = _clip()
    assert server.track_request(None, clip) == ("auto", None)
    tgt = {"t": 2, "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    assert server.track_request({"mode": "target", "target": tgt}, clip) == ("target", {k: float(v) for k, v in tgt.items()})
    assert server.track_request({"target": tgt}, clip)[0] == "target"
    assert server.track_request({"mode": "face"}, clip) == ("face", None)
    clip["edit"]["track"] = {"mode": "target", "target": {**tgt}}
    assert server.track_request({}, clip) == ("target", tgt)  # a re-track repeats the user's choice
    for bad in ({"mode": "people"}, {"mode": "target", "target": None}, {"mode": "target", "target": {**tgt, "t": 11}},
                {"mode": "target", "target": {**tgt, "w": 0.005}}, {"mode": "target", "target": {**tgt, "x": 0.9}},
                {"mode": "target", "target": {**tgt, "y": "x"}}, {"mode": "target", "target": {"t": 1}}, "nope"):
        with pytest.raises(HTTPException):
            server.track_request(bad, _clip())


# ---------- adjustable box: zoom keyframes + framing ----------

def test_zoom_keyframes_and_framing_validate():
    clip = _clip()
    ok = server.clean_edit({"track_manual": [[1, 0.2, 0.3, 2], [2, 0.5, 0.5]], "framing": {"offset_y": 0.9, "zoom": 1.5}}, clip)
    assert ok["track_manual"] == [[1.0, 0.2, 0.3, 2.0], [2.0, 0.5, 0.5]]  # 4-tuples kept as 4-tuples
    assert ok["framing"] == {"offset_y": 0.4, "zoom": 1.5}  # clamped like the other edit numbers
    for bad in ({"track_manual": [[1, 0.2, 0.3, 5]]}, {"track_manual": [[1, 0.2, 0.3, 2, 9]]}, {"framing": "x"},
                {"framing": {"zoom": "a"}}):
        with pytest.raises(HTTPException):
            server.clean_edit(bad, clip)


def test_manual_zoom_keyframes_blend_like_position():
    pts = [[0.0, 0.5, 0.5, 0.1, 0.1], [10.0, 0.5, 0.5, 0.1, 0.1]]
    merged = track.merge(pts, [[4.0, 0.5, 0.5, 2.0], [6.0, 0.3, 0.5]], zoom=1.2)
    z = lambda t: float(__import__("numpy").interp(t, [p[0] for p in merged], [p[3] for p in merged]))  # noqa: E731
    assert z(4.0) == pytest.approx(2.0) and 1.2 < z(4.75) < 2.0 and z(9.0) == pytest.approx(1.2)
    assert track.at(merged, 6.0)[0] == pytest.approx(0.3)


def test_headroom_offset_moves_the_crop_vertically():
    tr = {"points": [[0.0, 0.5, 0.5, 0.1, 0.1], [5.0, 0.5, 0.5, 0.1, 0.1]], "manual": []}
    base = track.crop_filter({"follow": True, "track": tr, "framing": {"zoom": 2.0, "offset_y": 0.0}}, 1920, 1080, 9 / 16)
    up = track.crop_filter({"follow": True, "track": tr, "framing": {"zoom": 2.0, "offset_y": 0.2}}, 1920, 1080, 9 / 16)
    assert base == "crop=304:540:808:270" and up == "crop=304:540:808:378"  # crop centre 0.5 + 0.2 × crop height


def test_varying_zoom_uses_zoompan_inside_a_following_window():
    tr = {"points": [[0.0, 0.3, 0.5, 0.1, 0.1], [5.0, 0.7, 0.5, 0.1, 0.1]], "manual": [[2.0, 0.5, 0.5, 2.0]]}
    g, _ = media.build_filters({"aspect": "9:16", "follow": True, "track": tr}, 1920, 1080, 10, False, fps=30)
    assert g.startswith("[0:v]crop=608:1080:'") and "zoompan=z='" in g and ":d=1:s=1080x1920:fps=30" in g
    flat, _ = media.build_filters({"aspect": "9:16", "follow": True, "track": {**tr, "manual": []}}, 1920, 1080, 10, False)
    assert "zoompan" not in flat


def test_render_follows_keyframed_zoom(tmp_path):
    video = tmp_path / "still.mp4"
    r = subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=4:r=25",
                        "-f", "lavfi", "-i", "color=c=white:s=20x20:d=4:r=25", "-filter_complex", "[0][1]overlay=x=150:y=80",
                        "-pix_fmt", "yuv420p", str(video)], capture_output=True)
    if r.returncode:
        pytest.skip("ffmpeg can't make a test video here")
    import cv2
    tr = {"points": [[0.0, 0.5, 0.5, 0.1, 0.1], [4.0, 0.5, 0.5, 0.1, 0.1]], "manual": [[0.0, 0.5, 0.5, 1.0], [3.0, 0.5, 0.5, 2.0]]}
    out = tmp_path / "zoom.mp4"
    media.render(video, out, 0.0, 4.0, {"aspect": "9:16", "follow": True, "track": tr, "loudnorm": False},
                 {"width": 320, "height": 180, "fps": 25, "has_audio": False}, None)
    cap = cv2.VideoCapture(str(out))

    def width_at(t):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        _, f = cap.read()
        cols = (f[:, :, 0] > 128).any(axis=0).nonzero()[0]
        return cols.max() - cols.min()

    early, late = width_at(0.1), width_at(3.0)  # exactly 2x at the keyframe (it blends back over ±1.5 s after)
    assert 1.7 < late / early < 2.3, (early, late)
