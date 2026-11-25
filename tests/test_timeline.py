"""Multi-layer timeline: edit.effects / edit.markers validation, keyframed zoom + opacity in a real render (sampled at a
frame), frame-exact splits at 29.97 and 60 fps, byte-identical graphs without effects, the pure timeline maths
(static/timeline.js under node) and the status indicator's endpoints (training jobs, cancel a queued job)."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import media, pipeline, server, store  # noqa: E402

STATIC = Path(server.__file__).parent / "static"
CLIP = {"start": 10.0, "end": 20.0}
KINDS = ("project", "clip", "job", "train_job")


def _client():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


@pytest.fixture(autouse=True)
def clean():
    before = {k: {d["id"] for d in store.find(k)} for k in KINDS}
    yield
    for k in KINDS:
        for d in store.find(k):
            if d["id"] not in before[k]:
                store.delete(k, d["id"])
                if k == "project":
                    shutil.rmtree(pipeline.pdir(d["id"]), ignore_errors=True)


def _need_ffmpeg():
    if not shutil.which(media.FFMPEG) and not Path(media.FFMPEG).exists():
        pytest.skip("no ffmpeg")


# ---------- validation ----------

def test_effects_validate_clamp_and_sort():
    assert server.clean_edit({"effects": None}, CLIP) == {"effects": None}
    assert server.clean_edit({"effects": []}, CLIP) == {"effects": None}
    out = server.clean_edit({"effects": [
        {"id": "z1", "kind": "zoom", "t": 2, "d": 30, "x": 0.4, "y": 0.6,
         "keys": [[1.5, 2.0], [0, 1], [0.5, 9, 0.2, 0.8]]},  # unsorted, zoom clamped to 4, d clamped to the clip
        {"kind": "opacity", "t": 0, "d": 1, "keys": [[0, 1], [1, -3]]},
        {"kind": "blur", "t": 5, "d": 2, "amount": 99, "junk": 1},
    ]}, CLIP)["effects"]
    z, o, b = out
    assert z == {"id": "z1", "kind": "zoom", "t": 2.0, "d": 8.0, "x": 0.4, "y": 0.6,
                 "keys": [[0.0, 1.0, 0.4, 0.6], [0.5, 4.0, 0.2, 0.8], [1.5, 2.0, 0.4, 0.6]]}
    assert o["kind"] == "opacity" and o["keys"] == [[0.0, 1.0], [1.0, 0.0]] and o["id"]
    assert b == {"id": b["id"], "kind": "blur", "t": 5.0, "d": 2.0, "amount": 40}
    for bad in ("x", [{"kind": "warp", "t": 0, "d": 1}], [{"kind": "zoom", "t": 11, "d": 1, "keys": [[0, 2]]}],
                [{"kind": "zoom", "t": 0, "d": 1, "keys": []}], [{"kind": "opacity", "t": 0, "d": 0, "keys": [[0, 1]]}],
                [{"kind": "zoom", "t": 0, "d": 1, "keys": [["a", 2]]}], [{"kind": "zoom", "t": 0, "d": 1, "keys": [[0, 2]] * 33}],
                [{"kind": "blur", "t": 0, "d": 1}] * 61):
        with pytest.raises(HTTPException):
            server.clean_edit({"effects": bad}, CLIP)


def test_markers_validate():
    assert server.clean_edit({"markers": []}, CLIP) == {"markers": None}
    got = server.clean_edit({"markers": [{"t": 3, "label": "  punchline "}, {"t": 1}, {"t": 50, "label": "x" * 200}]}, CLIP)["markers"]
    assert got == [{"t": 1.0, "label": "Marker"}, {"t": 3.0, "label": "punchline"}, {"t": 10.0, "label": "x" * 60}]
    with pytest.raises(HTTPException):
        server.clean_edit({"markers": [{"t": "soon"}]}, CLIP)


# ---------- frame-exact splits ----------

@pytest.mark.parametrize("fps,exact", [(29.97, 30000 / 1001), (60.0, 60.0)])
def test_split_at_a_frame_stores_a_cut_on_that_frame(fps, exact):
    clip = {"start": 0.0, "end": 60.0}
    for n0, n1 in ((31, 62), (1001, 1500), (299, 300)):
        a, b = n0 / exact, n1 / exact  # what timeline.js frameTime() sends
        [[s, e]] = server.clean_edit({"cuts": [[a, b]]}, clip)["cuts"]
        assert round(s * exact) == n0 and round(e * exact) == n1  # the stored (ms-rounded) times land on the same frames
        assert abs(s - a) <= 0.0005 and abs(e - b) <= 0.0005


# ---------- render ----------

def test_effect_windows_follow_cuts_and_speed():
    keeps = [(10.0, 12.0), (13.0, 20.0)]  # 1 s cut at 12..13
    [w] = media.effect_windows([{"kind": "zoom", "t": 1.0, "d": 4.0, "x": 0.5, "y": 0.5,
                                 "keys": [[0, 1, 0.5, 0.5], [3.0, 2, 0.5, 0.5]]}], 10.0, keeps, 2.0)
    assert (w["a"], w["b"]) == (0.5, 2.0)  # 11 s → 1 s out /2; 15 s → 4 s out /2
    assert w["keys"] == [[0.5, 1, 0.5, 0.5], [1.5, 2, 0.5, 0.5]]  # key at 14 s → 3 s out /2


def test_graphs_without_effects_are_byte_identical():
    base = media.build_filters({"aspect": "9:16", "fade_out": 1.0}, 1920, 1080, 10, True)
    assert base[0] == ("[0:v]crop=608:1080:656:0,scale=1080:1920,setsar=1,fade=t=out:st=9.00:d=1.00,subtitles=captions.ass,"
                       "format=yuv420p[v];[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st=9.00:d=1.00[a]")
    for none in (None, []):
        assert media.build_filters({"aspect": "9:16", "fade_out": 1.0}, 1920, 1080, 10, True, effects=none) == base
    assert media.effect_windows(None, 0, None, 1) == []


def test_effects_go_into_the_graph():
    fx = media.effect_windows([{"kind": "zoom", "t": 1, "d": 2, "x": 0.5, "y": 0.5, "keys": [[0, 1, 0.5, 0.5], [1, 2, 0.5, 0.5]]},
                               {"kind": "opacity", "t": 0, "d": 1, "keys": [[0, 0], [1, 1]]},
                               {"kind": "blur", "t": 2, "d": 1, "amount": 8}], 0, None, 1)
    g, _ = media.build_filters({"aspect": "source"}, 320, 240, 3, False, has_audio=False, fps=30, effects=fx)
    assert "zoompan=" in g and ":s=320x240:fps=30" in g and "enable='between(t,2.000,3.000)'" in g
    assert g.index("zoompan") < g.index("format=yuv420p[v]")


def _px(path: Path, t: float, x: int, y: int) -> int:
    out = subprocess.run([media.FFMPEG, "-v", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
                          "-vf", f"format=gray,crop=1:1:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return out[0]


def test_real_render_with_keyframed_zoom_and_opacity(tmp_path):
    _need_ffmpeg()
    src = tmp_path / "src.mp4"  # black frame with a white box in the middle half
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=black:s=320x240:r=30:d=4,drawbox=x=80:y=60:w=160:h=120:c=white:t=fill",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    meta = {"width": 320, "height": 240, "fps": 30, "duration": 4.0, "has_audio": False}
    edit = {"aspect": "source", "loudnorm": False, "effects": [
        {"kind": "zoom", "t": 1.0, "d": 3.0, "x": 0.5, "y": 0.5, "keys": [[0, 1, 0.5, 0.5], [0.5, 2, 0.5, 0.5]]},  # 2× by 1.5 s
        {"kind": "opacity", "t": 2.0, "d": 1.0, "keys": [[0, 1], [0.3, 0], [0.7, 0], [1.0, 1]]},  # black 2.3..2.7 s
    ]}
    out = tmp_path / "out.mp4"
    media.render(src, out, 0.0, 4.0, edit, meta, None, None, None, None, [], [])
    assert abs(media.probe(out)["duration"] - 4.0) < 0.1
    assert _px(out, 0.5, 5, 5) < 30 and _px(out, 0.5, 160, 120) > 220  # before the zoom: corner black, centre white
    assert _px(out, 1.9, 5, 5) > 220  # zoomed 2× on the centre: the white box fills the frame
    assert _px(out, 2.5, 160, 120) < 40  # faded to black
    assert _px(out, 3.5, 160, 120) > 220 and _px(out, 3.5, 5, 5) > 220  # back to full opacity, still zoomed


# ---------- pure timeline maths (static/timeline.js) ----------

@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_timeline_logic_under_node():
    script = r"""
const T = require(process.argv[1]);
const o = {};
o.f2997 = T.fpsExact(29.97); o.f60 = T.fpsExact(60); o.f25 = T.fpsExact(25);
o.ft = T.frameTime(31, 29.97); o.back = T.toFrame(T.frameTime(31, 29.97), 29.97);
o.snapF = T.snapFrame(1.016, 60);
o.tc = T.timecode(62.5, 30); o.tcH = T.timecode(3725.0, 25);
o.snapPt = T.snap(5.03, { fps: 30, points: [5.0, 9], tol: 0.05 }); o.snapPtNtsc = T.snap(3.01, { fps: 29.97, points: [3.0], tol: 0.05 }); o.snapNo = T.snap(5.03, { fps: 30, points: [9], tol: 0.05 });
o.snapOff = T.snap(5.03, { fps: 30, points: [5.0], tol: 0.05, on: false });
o.split = T.splitItem({ id: "a", t: 1, d: 4, keys: [[0, 1], [3, 2]] }, 3, "b");
o.splitOut = T.splitItem({ id: "a", t: 1, d: 4 }, 9, "b");
o.addCut = T.addCut([[12, 13], [15, 16]], 12.5, 15.2, 10, 20);
o.ripple = T.rippleDelete([[12, 13]], 14, 15, 10, 20);
o.restore = T.removeCutAt([[12, 13], [15, 16]], 15.5);
o.segs = T.segments(10, 20, [[12, 13]], [11, 16]);
o.nudge = T.nudge([{ t: 1, d: 2 }, { t: 7.99, d: 2 }], 1, 30, 10);
o.nudgeBack = T.nudge([{ t: 0.01, d: 2 }], -10, 30, 10);
o.lanes = T.packLanes([{ t: 0, d: 2 }, { t: 1, d: 2 }, { t: 2.5, d: 1 }, { t: 1.5, d: 0.2 }]);
o.interp = [T.interp([[0, 1], [2, 3]], -1), T.interp([[0, 1], [2, 3]], 1), T.interp([[0, 1], [2, 3]], 5)];
o.ticks = T.ticks(0, 2, 1000, 30); o.ticksWide = T.ticks(0, 600, 1000, 30);
o.marquee = T.inRange([{ id: 1, t: 0, d: 1 }, { id: 2, t: 3, d: 1 }, { id: 3, t: 0.5, d: 3 }], 0.8, 2.9);
console.log(JSON.stringify(o));
"""
    res = subprocess.run(["node", "-e", script, str(STATIC / "timeline.js")], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    o = json.loads(res.stdout)
    assert o["f2997"] == 30000 / 1001 and o["f60"] == 60 and o["f25"] == 25
    assert o["ft"] == 31 * 1001 / 30000 and o["back"] == 31
    assert o["snapF"] == 61 / 60
    assert o["tc"] == "0:01:02:15 · f1875" and o["tcH"] == "1:02:05:00 · f93125"
    assert o["snapPtNtsc"] == 90 * 1001 / 30000  # a word edge at 3.0 s lands on frame 90
    assert o["snapPt"] == 5.0 and o["snapNo"] == 151 / 30 and o["snapOff"] == 151 / 30
    left, right = o["split"]
    assert left == {"id": "a", "t": 1, "d": 2, "keys": [[0, 1], [2, 1 + 2 / 3]]}
    assert right == {"id": "b", "t": 3, "d": 2, "keys": [[0, 1 + 2 / 3], [1, 2]]}
    assert o["splitOut"] is None
    assert o["addCut"] == [[12, 16]] and o["ripple"] == [[12, 13], [14, 15]] and o["restore"] == [[12, 13]]
    assert o["segs"] == [{"a": 10, "b": 11, "cut": False}, {"a": 11, "b": 12, "cut": False}, {"a": 12, "b": 13, "cut": True},
                         {"a": 13, "b": 16, "cut": False}, {"a": 16, "b": 20, "cut": False}]
    assert o["nudge"] == [{"t": 1 + 1 / 30, "d": 2}, {"t": 8, "d": 2}]  # the second can't pass the clip's end
    assert o["nudgeBack"] == [{"t": 0, "d": 2}]
    assert o["lanes"] == [0, 1, 0, 2]
    assert o["interp"] == [1, 2, 3]
    assert o["ticks"]["frames"] is True and o["ticksWide"]["frames"] is False and o["ticksWide"]["major"] >= 30
    assert o["marquee"] == [1, 3]


# ---------- status indicator ----------

def test_training_active_lists_only_running_work():
    a = store.put("train_job", {"kind": "train", "status": "running", "message": "Step 5", "last": {"step": 5},
                                "spec": {"train": {"max_steps": 20}}})
    store.put("train_job", {"kind": "pilot", "status": "done", "message": "Done"})
    got = _client().get("/api/training/active").json()
    assert [j["id"] for j in got] == [a["id"]] and got[0]["progress"] == 0.25 and "spec" not in got[0]


def test_cancel_a_queued_job_only():
    q = store.put("job", {"kind": "export", "status": "queued", "progress": 0, "message": "Queued", "error": ""})
    r = store.put("job", {"kind": "export", "status": "running", "progress": 0.5, "message": "Rendering", "error": ""})
    assert _client().post(f"/api/jobs/{q['id']}/cancel").status_code == 200
    assert store.get("job", q["id"])["status"] == "canceled"
    assert _client().post(f"/api/jobs/{r['id']}/cancel").status_code == 409
    assert _client().post("/api/jobs/nope/cancel").status_code == 404


# ---------- integration with the existing paths ----------

def _project_clip(edit):
    p = pipeline.new_project("tl")
    pipeline.save_json(p["id"], "transcript.json", {"segments": [{"start": 0, "end": 60, "text": "", "words": []}]})
    store.update("project", p["id"], {"source": "source.mp4", "status": "ready",
                                      "meta": {"width": 1920, "height": 1080, "fps": 30, "duration": 60.0, "has_audio": True}})
    c = pipeline.new_clip(p["id"], {"start": 10.0, "end": 20.0, "title": "Clip"})
    return store.update("clip", c["id"], fn=lambda d: d["edit"].update(edit))


def test_keeps_endpoint_and_markers_reach_the_nle_export():
    from reelwright import nle
    c = _project_clip({"cuts": [[12.0, 13.0]], "markers": [{"t": 4.0, "label": "laugh"}, {"t": 2.5, "label": "gone"}]})
    assert _client().get(f"/api/clips/{c['id']}/keeps").json() == {"keeps": [[10.0, 12.0], [13.0, 20.0]]}
    assert ("laugh" in [m[1] for m in nle.clip_timeline(c)["markers"]]
            and (3.0, "laugh", "marker") in nle.clip_timeline(c)["markers"])  # 14 s source = 3 s after the 1 s cut
    assert "gone" not in [m[1] for m in nle.clip_timeline(c)["markers"]]  # inside the cut


def test_effects_and_markers_go_through_patch_and_undo_to_none():
    c = _project_clip({})
    fx = [{"kind": "blur", "t": 1, "d": 2, "amount": 5}]
    r = _client().patch(f"/api/clips/{c['id']}", json={"edit": {"effects": fx, "markers": [{"t": 1}]}})
    assert r.status_code == 200 and r.json()["edit"]["effects"][0]["amount"] == 5 and r.json()["rev"] == c["rev"] + 1
    if shutil.which("node"):
        res = subprocess.run(["node", "-e", "const U=require(process.argv[1]);console.log(JSON.stringify(U.inverse("
                              "{edit:{effects:[1],markers:[1],layout_segments:[1]}},{edit:{}})))", str(STATIC / "undo.js")],
                             capture_output=True, text=True, timeout=30)
        assert json.loads(res.stdout) == {"edit": {"effects": None, "markers": None, "layout_segments": None}}
    r = _client().patch(f"/api/clips/{c['id']}", json={"edit": {"effects": None, "markers": None}})
    assert r.json()["edit"]["effects"] is None and r.json()["edit"]["markers"] is None


# ---------- per-section layouts (edit.layout_segments) ----------

def test_layout_segments_validate():
    clip = {"start": 10.0, "end": 20.0}
    assert server.clean_edit({"layout_segments": []}, clip) == {"layout_segments": None}
    got = server.clean_edit({"layout_segments": [
        {"s": 6, "e": 30, "layout": "pip", "pip_corner": "tl", "junk": 1},  # e clamped to the clip's 10 s
        {"s": 2, "e": 4, "layout": "split", "split": 0.9, "crop_x": 0.25},  # split clamped to 0.7
    ]}, clip)["layout_segments"]
    assert got == [{"s": 2.0, "e": 4.0, "layout": "split", "split": 0.7, "crop_x": 0.25},
                   {"s": 6.0, "e": 10.0, "layout": "pip", "pip_corner": "tl"}]
    for bad in ("x", [{"s": 0, "e": 1}], [{"s": 0, "e": 1, "layout": "warp"}], [{"s": 3, "e": 2, "layout": "split"}],
                [{"s": 0, "e": 2, "layout": "split"}, {"s": 1, "e": 3, "layout": "pip"}],  # overlap
                [{"s": 11, "e": 12, "layout": "split"}], [{"s": i, "e": i + 0.1, "layout": "split"} for i in range(51)],
                [{"s": 0, "e": 1, "layout": "split", "regions": "nope"}]):
        with pytest.raises(HTTPException):
            server.clean_edit({"layout_segments": bad}, clip)


def test_layout_section_windows_follow_cuts_and_speed():
    keeps = [(10.0, 11.0), (11.5, 20.0)]  # 0.5 s cut at clip 1..1.5
    [w] = media.section_windows([{"s": 2.0, "e": 4.0, "layout": "split"}], 10.0, keeps, 1.0)
    assert (w["a"], w["b"]) == (1.5, 3.5) and w["layout"] == "split"
    assert media.section_windows([{"s": 1.1, "e": 1.4, "layout": "pip"}], 10.0, keeps, 1.0) == []  # all inside the cut
    [w] = media.section_windows([{"s": 2.0, "e": 4.0, "layout": "split"}], 10.0, None, 2.0)
    assert (w["a"], w["b"]) == (1.0, 2.0)


def test_graphs_without_sections_are_byte_identical():
    for e in ({"aspect": "9:16"}, {"aspect": "9:16", "layout": "split", "regions": [{"id": "cam", "role": "cam", "x": 0.7, "y": 0, "w": 0.3, "h": 0.3}]}):
        base = media.build_filters(e, 1920, 1080, 10, True)
        assert media.build_filters(e, 1920, 1080, 10, True, sections=None) == base
        assert media.build_filters(e, 1920, 1080, 10, True, sections=[]) == base


def test_section_graph_overlays_its_own_layout_under_the_captions():
    wins = media.section_windows([{"s": 2.0, "e": 4.0, "layout": "split"}], 0.0, [(0.0, 1.0), (1.5, 6.0)], 1.0)
    g, size = media.build_filters({"aspect": "9:16"}, 1920, 1080, 5.5, True, keeps_rel=[(0.0, 1.0), (1.5, 6.0)], sections=wins)
    assert size == (1080, 1920) and "overlay=0:0:enable='between(t,1.500,3.500)'" in g
    assert g.count("subtitles=captions.ass") == 1 and g.index("enable='between(t,1.500") < g.index("subtitles=")
    assert g.count("[0:v]select='") == 2 and g.count("select='") == 3  # both pictures (and the audio) go through the same jump cuts


def test_real_render_switches_layout_for_a_section(tmp_path):
    _need_ffmpeg()
    src = tmp_path / "src.mp4"  # black frame, a white box where the default cam region is (top right)
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=black:s=320x240:r=30:d=5,drawbox=x=224:y=12:w=90:h=77:c=white:t=fill",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    meta = {"width": 320, "height": 240, "fps": 30, "duration": 5.0, "has_audio": False}
    edit = {"aspect": "1:1", "loudnorm": False, "layout": "single", "layout_segments": [{"s": 2.0, "e": 4.0, "layout": "split"}]}
    out = tmp_path / "out.mp4"
    media.render(src, out, 0.0, 5.0, edit, meta, None, None, [(0.0, 1.0), (1.5, 5.0)], None, [], [])  # 0.5 s cut before the section
    p = media.probe(out)
    assert abs(p["duration"] - 4.5) < 0.1
    W, H = p["width"], p["height"]
    assert _px(out, 0.75, W // 2, H // 12) < 40   # single: the top middle of the centre crop is black
    assert _px(out, 2.5, W // 2, H // 12) > 200   # split (1.5..3.5 s out): the cam box fills the top tier
    assert _px(out, 4.0, W // 2, H // 12) < 40    # back to single after the section
