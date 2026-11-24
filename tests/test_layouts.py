"""Multi-region split-screen layouts: the region model, the filter graph per preset, legacy "split" clips, validation,
auto-suggested regions, reuse (apply to all / default layout), and real renders."""
import os
import subprocess
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from reelwright import media, pipeline, server, store  # noqa: E402

LEGACY = {"aspect": "9:16", "layout": "split", "split": 0.4, "cam_x": 0.7, "cam_y": 0.05, "cam_w": 0.28, "cam_h": 0.32,
          "crop_x": 0.4, "zoom": 1.2}
CAM = {"id": "c", "role": "cam", "x": 0.7, "y": 0.05, "w": 0.28, "h": 0.32}
MAIN = {"id": "m", "role": "main", "x": 0.0, "y": 0.0, "w": 0.75, "h": 1.0}
CHAT = {"id": "t", "role": "chat", "x": 0.75, "y": 0.4, "w": 0.25, "h": 0.6}
GUEST = {"id": "g", "role": "guest", "x": 0.0, "y": 0.6, "w": 0.25, "h": 0.4}


def _g(edit, **kw):
    return media.build_filters({"aspect": "9:16", **edit}, 1920, 1080, 10, False, **kw)[0]


def _vf(edit, **kw):
    return _g(edit, **kw).split(";[0:a]")[0]


# ---------- old clips ----------

def test_legacy_split_graph_is_byte_identical():
    g, size = media.build_filters(LEGACY, 1920, 1080, 10, True, [(0, 2), (3, 8)])
    assert size == (1080, 1920)
    assert g == ("[0:v]select='between(t,0.000,2.000)+between(t,3.000,8.000)',setpts=N/FRAME_RATE/TB,split[c][m];"
                 "[c]crop=538:346:1344:54,scale=1080:768:force_original_aspect_ratio=increase,crop=1080:768[top];"
                 "[m]crop=844:900:346:90,scale=1080:1152[bot];[top][bot]vstack,setsar=1,subtitles=captions.ass,format=yuv420p[v];"
                 "[0:a]aselect='between(t,0.000,2.000)+between(t,3.000,8.000)',asetpts=N/SR/TB,loudnorm=I=-14:TP=-1.5:LRA=11[a]")


def test_single_graph_is_unchanged():
    assert media.build_filters({"aspect": "9:16"}, 1920, 1080, 10, True)[0] == (
        "[0:v]crop=608:1080:656:0,scale=1080:1920,setsar=1,subtitles=captions.ass,format=yuv420p[v];"
        "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]")


def test_legacy_clip_regions_keep_the_same_crops():
    """Moving an old split clip onto regions (what the region editor does on first touch) frames it the same way."""
    regs = media.regions_of({**media.DEFAULT_EDIT, **LEGACY})
    assert [r["role"] for r in regs] == ["cam", "main"]
    base, items = media.layout_slots({**media.DEFAULT_EDIT, **LEGACY}, 1080, 1920, 1920, 1080)
    by = {it["r"]["role"]: it for it in items}
    assert base is None
    assert by["cam"]["box"] == (0, 0, 1080, 768) and by["main"]["box"] == (0, 768, 1080, 1152)
    assert by["main"]["crop"] == (844, 900, 346, 90)  # exactly the old main-view crop (crop_x + zoom still apply)
    assert by["cam"]["crop"] == (486, 346, 1370, 54)  # the old cam box, centre-cropped to the panel's shape


def test_seam_follows_the_layout():
    e = {**media.DEFAULT_EDIT, "aspect": "9:16", "split": 0.4}
    assert media.seam({**e, "layout": "split"}) == 0.4
    assert media.seam({**e, "layout": "split_rev"}) == pytest.approx(0.6)
    assert media.seam({**e, "layout": "pip"}) is None and media.seam({**e, "layout": "split", "aspect": "source"}) is None


# ---------- presets ----------

def test_stack_with_regions_uses_xstack_and_covers_each_slot():
    vf = _vf({"layout": "split", "regions": [CAM, MAIN]})
    assert vf.startswith("[0:v]split=2[L0][L1];")
    assert "xstack=inputs=2:layout=0_0|0_768:fill=black,setsar=1" in vf and vf.endswith("[v]")
    assert "scale=1080:768:force_original_aspect_ratio=increase,crop=1080:768" in vf
    assert "scale=1080:1152:force_original_aspect_ratio=increase,crop=1080:1152" in vf


def test_stack_reversed_puts_main_on_top():
    vf = _vf({"layout": "split_rev", "regions": [CAM, MAIN]})
    assert "xstack=inputs=2:layout=0_0|0_1152" in vf
    first = vf.split(";")[1]
    assert "crop=1080:1152" in first  # the first tile is the main view


def test_three_tier_stack_with_chat_and_ratios():
    vf = _vf({"layout": "stack3", "regions": [CAM, MAIN, CHAT], "split": 0.3, "split2": 0.25})
    assert "split=3" in vf and "xstack=inputs=3:layout=0_0|0_576|0_1440" in vf
    assert "crop=1080:864" in vf and "crop=1080:480" in vf  # main gets what's left: 1920 - 576 - 480


def test_three_tier_keeps_main_room_when_ratios_overflow():
    _, items = media.layout_slots({**media.DEFAULT_EDIT, "aspect": "9:16", "layout": "stack3", "regions": [CAM, MAIN, CHAT],
                                   "split": 0.7, "split2": 0.5}, 1080, 1920, 1920, 1080)
    main = next(it for it in items if it["r"]["role"] == "main")
    assert main["box"][3] >= 0.15 * 1920 and sum(it["box"][3] for it in items) == 1920


def test_pip_corner_rounded_and_bordered():
    vf = _vf({"layout": "pip", "regions": [CAM, MAIN], "pip_corner": "bl", "pip_size": 0.4, "pip_radius": 0.2, "pip_border": 6})
    assert "overlay=" in vf and "xstack" not in vf
    assert "pad=" in vf and "color=white" in vf and "geq=" in vf and "format=rgba" in vf
    _, items = media.layout_slots({**media.DEFAULT_EDIT, "aspect": "9:16", "layout": "pip", "regions": [CAM, MAIN],
                                   "pip_corner": "bl", "pip_size": 0.4}, 1080, 1920, 1920, 1080)
    main, cam = items
    assert main["box"] == (0, 0, 1080, 1920)
    x, y, w, h = cam["box"]
    assert w == 432 and x < 100 and y + h > 1800  # bottom-left, 40% wide
    assert abs(w / h - (0.28 * 1920) / (0.32 * 1080)) < 0.02  # keeps the cam's own shape


def test_pip_without_radius_or_border_skips_the_mask():
    vf = _vf({"layout": "pip", "regions": [CAM, MAIN], "pip_radius": 0, "pip_border": 0})
    assert "geq" not in vf and "pad=" not in vf


def test_duo_puts_two_people_side_by_side_over_main():
    vf = _vf({"layout": "duo", "regions": [CAM, GUEST, MAIN]})
    assert "xstack=inputs=3:layout=0_0|540_0|0_768" in vf


def test_blurred_fit_with_cam():
    vf = _vf({"layout": "blurcam", "regions": [CAM, MAIN]})
    assert "boxblur" in vf and vf.count("overlay=") == 2 and "split=3" in vf


def test_free_layout_places_each_region_at_its_box():
    cam = {**CAM, "out": {"x": 0.1, "y": 0.05, "w": 0.5, "h": 0.2}}
    vf = _vf({"layout": "free", "regions": [MAIN, cam]})
    assert "boxblur" in vf and "overlay=108:96" in vf and "crop=540:384" in vf


def test_missing_cam_region_falls_back_to_main():
    _, items = media.layout_slots({**media.DEFAULT_EDIT, "aspect": "9:16", "layout": "split", "regions": [MAIN]},
                                  1080, 1920, 1920, 1080)
    assert [it["r"]["role"] for it in items] == ["main", "main"]


def test_cuts_speed_and_follow_keep_clip_time_per_region():
    track = {"points": [[0, 0.2, 0.3, 0.1, 0.1], [4, 0.8, 0.3, 0.1, 0.1]], "manual": []}
    vf = _vf({"layout": "split", "regions": [{**CAM, "follow": True}, MAIN], "track": track, "speed": 1.5},
             keeps_rel=[(0, 2), (3, 8)])
    assert vf.startswith("[0:v]split=2[L0][L1];")  # following: cut/speed run per branch, after the follow crop
    cam_branch, main_branch = vf.split(";")[1:3]
    assert "clip(t-" in cam_branch and cam_branch.index("crop=") < cam_branch.index("select=")
    assert "select=" in main_branch and "setpts=PTS/1.5" in main_branch


def test_without_follow_cuts_run_once_before_split():
    vf = _vf({"layout": "stack3", "regions": [CAM, MAIN, CHAT]}, keeps_rel=[(0, 2)])
    assert vf.startswith("[0:v]select=") and vf.count("select=") == 1


def test_captions_and_overlays_ride_on_the_composed_frame():
    g = media.build_filters({"aspect": "9:16", "layout": "pip", "regions": [CAM, MAIN], "fade_in": 1}, 1920, 1080, 10, True,
                            overlays=[{"kind": "image", "input": 1, "a": 1.0, "b": 2.0}])[0]
    assert "[b0]" in g and "subtitles=captions.ass" in g and "fade=t=in" in g
    assert g.index("overlay=x=") > g.index("geq=")  # media overlays come after the composition


# ---------- camera angles (ready for multicam; nothing imports angles yet) ----------

def test_source_main_renders_exactly_like_no_source():
    for lay in ("split", "stack3", "pip", "blurcam", "free"):
        plain = _g({"layout": lay, "regions": [CAM, MAIN, CHAT]}, keeps_rel=[(0, 2)])
        tagged = _g({"layout": lay, "regions": [{**r, "source": "main"} for r in (CAM, MAIN, CHAT)]}, keeps_rel=[(0, 2)])
        assert tagged == plain
    # and a source with no feed wired up (an angle that was removed) falls back to the main video
    assert _g({"layout": "split", "regions": [{**CAM, "source": "gone"}, MAIN]}) == _g({"layout": "split", "regions": [CAM, MAIN]})


def test_a_region_reads_its_own_angle_input_with_an_offset():
    vf = _vf({"layout": "split", "regions": [{**CAM, "source": "a1", "follow": True}, MAIN],
              "track": {"points": [[0, 0.2, 0.3, 0.1, 0.1], [4, 0.8, 0.3, 0.1, 0.1]]}},
             keeps_rel=[(0, 2)], feeds={"a1": (1, 0.5, 1280, 720)})
    heads = vf.split(";")[:2]
    assert heads[0] == "[1:v]setpts=PTS+0.500/TB,select='between(t,0.000,2.000)',setpts=N/FRAME_RATE/TB,null[L0]"
    assert heads[1].startswith("[0:v]select=") and heads[1].endswith("null[L1]")
    assert "clip(t-" not in vf  # the main video's tracked path never steers another angle
    # the cam box is a fraction of the 1280×720 angle (358×230 px at 896,36), cover-cropped to the 1080:768 panel
    assert "[L0]crop=322:230:914:36," in vf


def test_region_source_is_validated_against_the_project_angles():
    clip = _clip()
    with pytest.raises(HTTPException):
        server.clean_edit({"regions": [{**CAM, "source": "a1b2c3d4e5f6"}]}, clip)  # no angles on this project yet
    store.update("project", clip["project_id"], {"angles": [{"id": "a1b2c3d4e5f6", "file": "angles/a.mp4"}]})
    out = server.clean_edit({"regions": [{**CAM, "source": "a1b2c3d4e5f6"}, {**MAIN, "source": "main"}]}, clip)["regions"]
    assert out[0]["source"] == "a1b2c3d4e5f6" and "source" not in out[1]


# ---------- validation ----------

def _clip():
    p = pipeline.new_project("lay")
    store.update("project", p["id"], {"meta": {"width": 1920, "height": 1080, "duration": 60.0, "fps": 30, "has_audio": True},
                                      "status": "ready", "source": "source.mp4"})
    return pipeline.new_clip(p["id"], {"start": 5.0, "end": 15.0})


def test_regions_are_clamped_and_cleaned():
    out = server.clean_edit({"regions": [{"role": "cam", "x": 0.9, "y": -1, "w": 0.5, "h": 3, "follow": 1, "junk": 1},
                                         {"id": "x" * 99, "role": "main", "x": 0, "y": 0, "w": 0.001, "h": 1,
                                          "out": {"x": 2, "y": 0, "w": 0.5, "h": 0.5}}]}, {"start": 0, "end": 10})
    a, b = out["regions"]
    assert a == {"id": a["id"], "role": "cam", "x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0, "follow": True}
    assert len(b["id"]) <= 24 and b["w"] == 0.02 and b["out"] == {"x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5}
    assert a["id"] != b["id"]
    assert server.clean_edit({"regions": None}, {"start": 0, "end": 10}) == {"regions": None}


@pytest.mark.parametrize("bad", [
    [{"role": "boss", "x": 0, "y": 0, "w": 1, "h": 1}],
    [{"role": "cam", "x": "a", "y": 0, "w": 1, "h": 1}],
    [{"role": "cam", "x": float("nan"), "y": 0, "w": 1, "h": 1}],
    [{"role": "cam", "x": 0, "y": 0, "w": 1, "h": 1}] * 5,
    "cam",
])
def test_bad_regions_are_rejected(bad):
    with pytest.raises(HTTPException):
        server.clean_edit({"regions": bad}, {"start": 0, "end": 10})


def test_layout_names_and_options_are_validated():
    ok = server.clean_edit({"layout": "stack3", "split2": 0.9, "pip_corner": "br", "pip_size": 5, "pip_border": -3},
                           {"start": 0, "end": 10})
    assert ok == {"layout": "stack3", "split2": 0.5, "pip_corner": "br", "pip_size": 0.6, "pip_border": 0}
    assert server.clean_edit({"layout": "split"}, {"start": 0, "end": 10}) == {"layout": "split"}  # old clips keep working
    for bad in ({"layout": "mosaic"}, {"pip_corner": "middle"}):
        with pytest.raises(HTTPException):
            server.clean_edit(bad, {"start": 0, "end": 10})


# ---------- suggestions + reuse ----------

def test_suggested_regions_from_face_and_text_column():
    frames = np.zeros((4, 180, 320), np.uint8)
    frames[:, 20:170:6, 250:315] = 255  # dense "text" lines in a right-hand column
    regs = media.suggest_regions((0.15, 0.8, 0.06, 0.1), frames, 1920, 1080)
    by = {r["role"]: r for r in regs}
    assert set(by) == {"cam", "main", "chat"}
    cam, chat, main = by["cam"], by["chat"], by["main"]
    assert cam["x"] < 0.15 < cam["x"] + cam["w"] and cam["y"] < 0.8 < cam["y"] + cam["h"]
    assert chat["x"] >= 0.7 and chat["w"] <= 0.3
    assert main["x"] + main["w"] <= chat["x"] + 1e-6 and main["w"] * main["h"] >= 0.4  # the rest, clear of chat


def test_no_text_column_means_no_chat_region():
    regs = media.suggest_regions(None, np.zeros((3, 180, 320), np.uint8), 1920, 1080)
    assert [r["role"] for r in regs] == ["main"] and regs[0]["w"] == 1.0


def test_detect_route_sets_regions_and_a_layout(monkeypatch):
    clip = _clip()
    monkeypatch.setattr(media, "detect_regions", lambda *a, **k: [CAM, MAIN, CHAT])
    out = server.detect_regions(clip["id"])
    assert out["edit"]["layout"] == "stack3" and [r["role"] for r in out["edit"]["regions"]] == ["cam", "main", "chat"]


def test_facecam_updates_the_cam_region_when_the_clip_has_regions(monkeypatch):
    clip = _clip()
    server.patch_clip(clip["id"], {"edit": {"layout": "pip", "regions": [CAM, MAIN]}})
    monkeypatch.setattr(media, "face_center", lambda *a, **k: (0.2, 0.3, 0.1, 0.1))
    out = server.facecam(clip["id"])
    cam = next(r for r in out["edit"]["regions"] if r["role"] == "cam")
    assert out["edit"]["layout"] == "pip" and cam["x"] < 0.2 < cam["x"] + cam["w"]


def test_apply_to_all_and_default_layout():
    a = _clip()
    b = pipeline.new_clip(a["project_id"], {"start": 20.0, "end": 30.0})
    server.patch_clip(a["id"], {"edit": {"layout": "stack3", "regions": [CAM, MAIN, CHAT], "split2": 0.3}})
    assert server.apply_layout_all(a["id"])["updated"] == 1
    got = store.get("clip", b["id"])["edit"]
    assert got["layout"] == "stack3" and got["split2"] == 0.3 and len(got["regions"]) == 3
    try:
        server.save_layout_default(a["id"])
        c = pipeline.new_clip(a["project_id"], {"start": 40.0, "end": 50.0})
        assert c["edit"]["layout"] == "stack3" and len(c["edit"]["regions"]) == 3
    finally:
        store.delete("pref", "layout")  # never leak a default layout into other tests' new clips


# ---------- real renders ----------

def need_ffmpeg():
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")


@pytest.mark.parametrize("edit", [
    {"layout": "stack3", "regions": [CAM, MAIN, CHAT]},
    {"layout": "pip", "regions": [CAM, MAIN], "pip_radius": 0.2, "pip_border": 6},
])
def test_real_render_composes_regions(tmp_path, edit):
    need_ffmpeg()
    src = tmp_path / "src.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=10",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    out = tmp_path / "out.mp4"
    meta = {"width": 640, "height": 360, "fps": 10, "has_audio": False}
    media.render(src, out, 0.0, 2.0, {"aspect": "9:16", "loudnorm": False, **edit}, meta, None)
    m = media.probe(out)
    assert (m["width"], m["height"]) == (1080, 1920) and m["duration"] > 1.5


def test_default_layout_never_carries_another_projects_camera_angles():
    from reelwright import longform
    aid = "a1b2c3d4e5f6"
    a = _clip()
    store.update("project", a["project_id"], {"angles": [{"id": aid, "name": "B", "status": "ready"}]})
    server.patch_clip(a["id"], {"edit": {"layout": "duo", "regions": [{**CAM, "source": aid}, MAIN, GUEST]}})
    try:
        server.save_layout_default(a["id"])
        assert all("source" not in r for r in store.get("pref", "layout")["edit"]["regions"])  # saved without angle ids
        # a default saved before this fix still holds one: it is dropped where the project has no such angle
        store.put("pref", {"id": "layout", "edit": {"layout": "duo", "regions": [{**CAM, "source": aid}, MAIN, GUEST]}})
        b = _clip()  # another project, no angles
        assert b["edit"]["layout"] == "duo" and all("source" not in r for r in b["edit"]["regions"])
        assert server.patch_clip(b["id"], {"edit": {"regions": b["edit"]["regions"]}})  # no 400 on the next edit
        lf = longform.create(b["project_id"], "clips", "LF", [[0, 5]], [])
        assert all("source" not in r for r in lf["edit"]["regions"])
        same = pipeline.new_clip(a["project_id"], {"start": 20.0, "end": 30.0})  # the project that has it keeps it
        assert any(r.get("source") == aid for r in same["edit"]["regions"])
    finally:
        store.delete("pref", "layout")
