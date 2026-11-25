"""Presets: a named bundle of the parts of a clip's look and settings the user picked (layout, captions, headline,
card, effects, audio, publish defaults, app settings…). Saving keeps only the chosen parts; applying merges only them,
through the same validators as clean_edit, remapping what belongs to one project. Also the month view's date maths
(static/calendar.js under node) and the remembered tool-panel layout."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from reelwright import config, media, pipeline, routes_presets as rp, server, store  # noqa: E402

STATIC = Path(media.__file__).parent / "static"
CAM = {"id": "c", "role": "cam", "x": 0.7, "y": 0.05, "w": 0.28, "h": 0.32}
MAIN = {"id": "m", "role": "main", "x": 0.0, "y": 0.0, "w": 0.75, "h": 1.0}


def _proj(**kw):
    p = pipeline.new_project("presets")
    return store.update("project", p["id"], {"meta": {"width": 1920, "height": 1080, "duration": 120.0, "fps": 30, "has_audio": True},
                                             "status": "ready", "source": "source.mp4", **kw})


def _clip(pid=None, start=5.0, end=25.0, **kw):
    pid = pid or _proj()["id"]
    return pipeline.new_clip(pid, {"start": start, "end": end, "title": "t", **kw})


@pytest.fixture(autouse=True)
def _clean():
    """Every preset, default and pref a test makes is removed again (they would leak into other tests' new clips)."""
    before = {p["id"] for p in store.find("preset")}
    yield
    for p in store.find("preset"):
        if p["id"] not in before:
            store.delete("preset", p["id"])
    for k in ("preset_default", "layout", "headline", "headline_theme", "streamer_card", "ui_layout"):
        store.delete("pref", k)


def _styled(c):
    server.patch_clip(c["id"], {"edit": {
        "layout": "split", "regions": [CAM, MAIN], "split": 0.35, "aspect": "4:5", "caption_style": "neon", "caption_pos": "high",
        "caption_color": "#00FF00", "headline": {"on": True, "text": "Clip text", "theme": "gradient", "position": "bottom"},
        "streamer_card": {"on": True, "style": "kick_card", "name": "Me", "handle": "me"}, "bleep": "beep", "remove_fillers": True,
        "effects": [{"kind": "zoom", "t": 1.0, "d": 2.0, "keys": [[0, 1.5]]}, {"kind": "blur", "t": 15.0, "d": 3.0, "amount": 8}],
        "volume": 1.4, "music_volume": 0.3, "follow": True, "brightness": 0.1}})
    return store.get("clip", c["id"])


# ---------- save / apply ----------

def test_save_stores_only_the_chosen_parts():
    c = _styled(_clip())
    p = rp.create_preset({"name": "Neon split", "clip_id": c["id"], "parts": ["layout", "captions", "nonsense"], "color": "#ff00aa"})
    assert set(p["parts"]) == {"layout", "captions"}  # unknown parts are ignored
    assert p["parts"]["layout"]["layout"] == "split" and p["parts"]["layout"]["split"] == 0.35
    assert p["parts"]["captions"]["caption_style"] == "neon" and p["parts"]["captions"]["caption_color"] == "#00FF00"
    assert "aspect" not in json.dumps(p["parts"]) and "bleep" not in json.dumps(p["parts"])  # nothing it wasn't asked for
    assert p["color"] == "#FF00AA" and p["name"] == "Neon split"
    hl = rp.create_preset({"name": "HL", "clip_id": c["id"], "parts": ["headline"]})["parts"]["headline"]["headline"]
    assert hl["theme"] == "gradient" and "text" not in hl  # the look, not this clip's words
    with pytest.raises(HTTPException):
        rp.create_preset({"name": "x", "clip_id": c["id"], "parts": []})  # nothing chosen


def test_apply_merges_only_the_preset_parts():
    src = _styled(_clip())
    p = rp.create_preset({"name": "Caps", "clip_id": src["id"], "parts": ["captions", "bleep"]})
    dst = _clip(start=30.0, end=40.0)
    server.patch_clip(dst["id"], {"edit": {"layout": "pip", "brightness": -0.2, "caption_pos": "low"}})
    body = rp.preset_edit(p["id"], dst["id"])["edit"]
    assert set(body) <= set(rp.PARTS["captions"]) | {"bleep"}
    out = server.patch_clip(dst["id"], {"edit": body})["edit"]  # the same PATCH the UI sends (undo records its inverse)
    assert out["caption_style"] == "neon" and out["caption_pos"] == "high" and out["bleep"] == "beep"
    assert out["layout"] == "pip" and out["brightness"] == -0.2  # untouched: not in the preset


def test_effects_keep_relative_timing_and_fall_off_a_shorter_clip():
    src = _styled(_clip())
    p = rp.create_preset({"name": "FX", "clip_id": src["id"], "parts": ["effects"]})
    short = _clip(start=50.0, end=60.0)  # 10 s: the blur at 15 s can't fit
    fx = rp.preset_edit(p["id"], short["id"])["edit"]["effects"]
    assert [e["kind"] for e in fx] == ["zoom"] and fx[0]["t"] == 1.0  # clip time: 1 s after the start, wherever it starts


def test_every_part_is_validated():
    bad = {
        "layout": {"layout": "nope"}, "frame": {"aspect": "3:7"}, "captions": {"caption_style": "comic"},
        "headline": {"headline": {"theme": "no-such-theme"}}, "card": {"streamer_card": {"handle": "bad handle!"}},
        "title": {"sticker_pos": "middle"}, "look": {"brightness": "bright"}, "effects": {"effects": [{"kind": "warp", "t": 0, "d": 1}]},
        "overlays": {"overlays": [{"kind": "laser", "t": 0}]}, "audio": {"volume": "loud"},
        "bleep": {"bleep": "loud"}, "tracking": {"framing": {"zoom": "x"}}, "sections": {"layout_segments": [{"layout": "nope", "s": 0, "e": 1}]},
        "publish": {"privacy": "everyone"}, "app": {"clip_min": 50, "clip_max": 10},
    }
    for part, values in bad.items():
        with pytest.raises(HTTPException) as e:
            rp.import_preset({"reelwright_preset": 1, "name": part, "len": 20, "parts": {part: values}})
        assert e.value.status_code == 400, part
    regions = {"layout": {"layout": "split", "regions": [{**CAM, "role": "boss"}]}}
    with pytest.raises(HTTPException):
        rp.import_preset({"reelwright_preset": 1, "name": "r", "len": 20, "parts": regions})
    ok = rp.import_preset({"reelwright_preset": 1, "name": "ok", "len": 20, "parts": {
        "look": {"brightness": 9}, "publish": {"privacy": "unlisted", "ai_content": 1}, "app": {"clip_min": 10, "clip_target": 0, "clip_max": 40, "clip_count": 500}}})
    assert ok["parts"]["look"]["brightness"] == 1  # clamped like clean_edit
    assert ok["parts"]["publish"] == {"privacy": "unlisted", "ai_content": True}
    assert ok["parts"]["app"]["clip_count"] == 50


def test_cross_project_remap_drops_foreign_angles_and_missing_assets_and_copies_music():
    aid = "a1b2c3d4e5f6"
    a = _proj(angles=[{"id": aid, "name": "B", "status": "ready"}])
    src = _clip(a["id"])
    asset = store.put("asset", {"kind": "image", "name": "logo", "file": "logo.png", "tags": []})
    gone = store.put("asset", {"kind": "image", "name": "old", "file": "old.png", "tags": []})
    (pipeline.pdir(a["id"]) / "music").mkdir(parents=True, exist_ok=True)
    (pipeline.pdir(a["id"]) / "music" / "beat.mp3").write_bytes(b"ID3")
    server.patch_clip(src["id"], {"edit": {"layout": "split", "regions": [{**CAM, "source": aid}, MAIN], "music": "music/beat.mp3",
                                           "overlays": [{"kind": "image", "asset_id": asset["id"], "t": 1}, {"kind": "image", "asset_id": gone["id"], "t": 2}]}})
    p = rp.create_preset({"name": "All", "clip_id": src["id"], "parts": ["layout", "overlays", "audio"]})
    store.delete("asset", gone["id"])  # deleted from the library after the save
    other = _clip()
    body = rp.preset_edit(p["id"], other["id"])["edit"]
    assert all("source" not in r for r in body["regions"])  # the angle belongs to project A only
    assert [o["asset_id"] for o in body["overlays"]] == [asset["id"]]
    assert body["music"] == "music/beat.mp3" and (pipeline.pdir(other["project_id"]) / "music" / "beat.mp3").exists()  # remapped
    assert server.patch_clip(other["id"], {"edit": body})  # and it passes clean_edit there
    same = rp.preset_edit(p["id"], _clip(a["id"], start=40.0, end=60.0)["id"])["edit"]
    assert any(r.get("source") == aid for r in same["regions"])  # its own project keeps the angle
    store.delete("asset", asset["id"])


def test_default_preset_goes_on_new_clips_and_wraps_the_single_purpose_prefs():
    base = _clip()["edit"]["caption_style"]  # whatever the learned house style gives today
    src = _styled(_clip(hook="Hook one"))
    server.save_layout_default(src["id"])  # an older single-purpose default
    p = rp.create_preset({"name": "Default", "clip_id": src["id"], "parts": ["captions", "headline", "card"]})
    rp.set_default({"preset_id": p["id"]})
    assert rp.list_presets()["default"] == p["id"]
    n = _clip(hook="A brand new hook")
    e = n["edit"]
    assert e["caption_style"] == "neon" and e["headline"]["on"] and e["headline"]["theme"] == "gradient"
    assert e["headline"]["text"] == "A brand new hook"  # its own hook, like "headline on new clips"
    assert e["streamer_card"]["on"] and e["streamer_card"]["style"] == "kick_card"
    assert e["layout"] == "split" and len(e["regions"]) == 2  # the saved default layout still applies (not in this preset)
    rp.set_default({"preset_id": None})
    assert _clip()["edit"]["caption_style"] == base
    rp.set_default({"preset_id": p["id"]})
    rp.delete_preset(p["id"])  # deleting the default stops it
    assert rp.list_presets()["default"] is None and _clip()["edit"]["caption_style"] == base


def test_a_default_layout_preset_is_kept_on_a_vertical_source():
    """The vertical-source rule flattens the guessed defaults, never a layout the creator put in a preset."""
    v = _proj(meta={"width": 1080, "height": 1920, "duration": 60.0, "fps": 30, "has_audio": True})
    assert _clip(v["id"])["edit"]["layout"] == "single"  # no preset: still flattened
    src = _styled(_clip())
    rp.set_default({"preset_id": rp.create_preset({"name": "L", "clip_id": src["id"], "parts": ["layout"]})["id"]})
    e = _clip(v["id"])["edit"]
    assert e["layout"] == "split" and e["aspect"] == "9:16"


def test_concurrent_ui_layout_writes_keep_every_editor():
    import threading
    start = threading.Barrier(3)

    def put(ed):
        start.wait()
        for _ in range(20):
            rp.put_ui_layout({ed: {"min": True, "col": [ed]}})
    ts = [threading.Thread(target=put, args=(ed,)) for ed in rp.UI_EDITORS]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert set(rp.get_ui_layout()) == set(rp.UI_EDITORS)


def test_bulk_apply_skips_posted_clips_and_montages():
    pid = _proj()["id"]
    src = _styled(_clip(pid))
    a, b = _clip(pid, 30.0, 40.0), _clip(pid, 50.0, 60.0)
    store.put("upload", {"clip_id": b["id"], "status": "done", "platform": "youtube"})
    m = pipeline.make_montage(pid, [a["id"], src["id"]], "m")
    p = rp.create_preset({"name": "B", "clip_id": src["id"], "parts": ["bleep", "publish"], "publish": {"privacy": "unlisted", "template": False}})
    r = rp.apply_preset(p["id"], {"project_id": pid})
    assert r["updated"] == 2 and b["id"] in r["skipped"] and m["id"] in r["skipped"]  # src + a
    assert store.get("clip", a["id"])["edit"]["bleep"] == "beep"
    assert store.get("clip", a["id"])["publish_defaults"] == {"privacy": "unlisted", "template": False}
    assert store.get("clip", b["id"])["edit"]["bleep"] == "off"  # posted: the published render is final
    r = rp.apply_preset(p["id"], {"clip_ids": [a["id"], b["id"]]})
    assert r["updated"] == 1 and r["skipped"] == [b["id"]]


def test_app_settings_part_applies_and_is_restored():
    keys = ("clip_min", "clip_target", "clip_max", "clip_count", "ai_routes")
    before = {k: config.load()[k] for k in keys}
    try:
        p = rp.import_preset({"reelwright_preset": 1, "name": "Long", "parts": {"app": {"clip_min": 30, "clip_target": 45, "clip_max": 90,
                                                                                             "clip_count": 4, "ai_routes": {"find": "ollama"}}}})
        rp.apply_preset(p["id"], {"clip_ids": [], "parts": ["app"]})
        s = config.load()
        # an imported file never picks the AI provider (whose bill runs the searches); the lengths still apply
        assert (s["clip_min"], s["clip_target"], s["clip_max"], s["clip_count"]) == (30, 45, 90, 4) and s["ai_routes"] == before["ai_routes"]
        config.save({"ai_routes": {**before["ai_routes"], "find": "ollama"}})  # a preset saved on this PC keeps its routing
        mine = rp.create_preset({"name": "Mine", "clip_id": _clip(_proj()["id"])["id"], "parts": ["app"]})
        config.save({"ai_routes": before["ai_routes"]})
        rp.apply_preset(mine["id"], {"clip_ids": [], "parts": ["app"]})
        assert config.load()["ai_routes"]["find"] == "ollama"
    finally:
        config.save(before)


def test_autopilot_can_use_a_preset():
    pid = _proj()["id"]
    src = _styled(_clip(pid))
    p = rp.create_preset({"name": "AP", "clip_id": src["id"], "parts": ["captions", "bleep"]})
    c = _clip(pid, 40.0, 60.0)
    clip, _ = pipeline._autopilot_prepare(c, {"aspect": "9:16", "captions": True, "preset_id": p["id"]}, [])
    assert clip["edit"]["caption_style"] == "neon" and clip["edit"]["bleep"] == "beep" and clip["edit"]["aspect"] == "9:16"
    clip, _ = pipeline._autopilot_prepare(c, {"aspect": "9:16", "preset_id": "gone"}, [])  # a deleted preset is ignored
    assert clip["edit"]["aspect"] == "9:16"


# ---------- manage / export / import ----------

def test_manage_rename_duplicate_reorder_favourite_delete():
    c = _styled(_clip())
    a = rp.create_preset({"name": "A", "clip_id": c["id"], "parts": ["captions"]})
    b = rp.create_preset({"name": "B", "clip_id": c["id"], "parts": ["bleep"]})
    rp.patch_preset(a["id"], {"name": "  Renamed  ", "fav": True, "color": "#123456"})
    d = rp.duplicate_preset(b["id"])
    assert d["id"] != b["id"] and d["name"] == "B copy" and d["parts"] == b["parts"]
    rp.reorder_presets({"ids": [d["id"], b["id"], a["id"]]})
    names = [p["name"] for p in rp.list_presets()["presets"]]
    assert names[0] == "Renamed" and names[1:3] == ["B copy", "B"]  # favourites first, then the order given
    with pytest.raises(HTTPException):
        rp.patch_preset(a["id"], {"name": ""})
    rp.delete_preset(b["id"])
    assert store.get("preset", b["id"]) is None


def test_export_import_round_trip_without_paths_or_ids_and_hostile_input_rejected():
    pid = _proj()["id"]
    (pipeline.pdir(pid) / "music").mkdir(parents=True, exist_ok=True)
    (pipeline.pdir(pid) / "music" / "beat.mp3").write_bytes(b"ID3")
    c = _styled(_clip(pid))
    server.patch_clip(c["id"], {"edit": {"music": "music/beat.mp3"}})
    p = rp.create_preset({"name": "Share me", "clip_id": c["id"], "parts": ["layout", "captions", "audio", "effects", "headline"]})
    raw = rp.export_preset(p["id"]).body.decode()
    data = json.loads(raw)
    assert "music/" not in raw and pid not in raw and "src_project" not in raw and p["id"] not in raw  # no paths, no local ids
    assert data["reelwright_preset"] == 1 and data["name"] == "Share me"
    back = rp.import_preset(data)
    assert back["id"] != p["id"] and back["parts"]["captions"] == p["parts"]["captions"]
    assert back["parts"]["effects"] == p["parts"]["effects"] and back["parts"]["layout"] == p["parts"]["layout"]
    for hostile in ([], "x", {"name": "x"}, {"reelwright_preset": 1, "name": "x", "parts": "all"},
                    {"reelwright_preset": 1, "name": "x" * 500, "parts": {"bleep": {"bleep": "beep"}}},
                    {"reelwright_preset": 1, "name": "x", "parts": {"effects": {"effects": [{"kind": "blur", "t": 0, "d": 1}] * 61}}},
                    {"reelwright_preset": 1, "name": "x", "parts": {"look": {"brightness": float("nan")}}},
                    {"reelwright_preset": 1, "name": "x", "parts": {"bleep": {"bleep": "beep"}}, "pad": "y" * 600_000},
                    {"reelwright_preset": 1, "name": "x", "color": "red;}", "parts": {"bleep": {"bleep": "beep"}}}):
        with pytest.raises(HTTPException):
            rp.import_preset(hostile)
    junk = rp.import_preset({"reelwright_preset": 1, "name": "j", "parts": {"bleep": {"bleep": "beep", "__proto__": 1, "evil": "C:\\x"},
                                                                               "secrets": {"anthropic_api_key": "sk-1"}}})
    assert junk["parts"] == {"bleep": {"bleep": "beep"}}  # unknown keys and parts are dropped


# ---------- the single-purpose prefs keep working ----------

def test_layout_headline_and_card_prefs_still_work_without_presets():
    p = _proj(source_url="https://www.twitch.tv/videos/1", source_info={"channel": "NightOwl"})
    c = _styled(_clip(p["id"], hook="old"))
    server.save_layout_default(c["id"])
    server.save_headline_theme(c["id"])
    server.save_headline_default(c["id"], {"on": True})
    server.save_streamer_card(c["id"], {"auto": True})
    n = _clip(p["id"], 60.0, 70.0, hook="New hook")["edit"]
    assert n["layout"] == "split" and n["headline"]["text"] == "New hook" and n["headline"]["theme"] == "gradient"
    assert n["streamer_card"]["on"] and n["streamer_card"]["style"] == "kick_card"
    assert server.headline_themes()["custom"]["name"] == "My theme"


# ---------- tool panel layout (remembered across reinstalls) ----------

def test_ui_layout_is_validated_and_kept():
    rp.put_ui_layout({"studio": {"min": True, "col": ["captions", "bad id!"], "hide": ["qa"], "order": ["layout", "captions"]},
                      "nope": {"min": True}, "editor": "x"})
    got = rp.get_ui_layout()
    assert got == {"studio": {"min": True, "col": ["captions"], "hide": ["qa"], "order": ["layout", "captions"]}}
    with pytest.raises(HTTPException):
        rp.put_ui_layout({"studio": {"order": ["a"] * 100}})


# ---------- month view date maths (static/calendar.js) ----------

def _node(script, tz):
    res = subprocess.run(["node", "-e", script, str(STATIC / "calendar.js")], capture_output=True, text=True, encoding="utf-8",
                         timeout=30, env={**os.environ, "TZ": tz})
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_month_grid_maths_across_month_lengths_and_dst():
    o = _node(r"""
const C = require(process.argv[1]);
const flat = (g) => g.flat().map((c) => c.key);
const o = {};
o.feb24 = C.monthGrid(2024, 1); o.feb21 = C.monthGrid(2021, 1); o.mar26 = C.monthGrid(2026, 2); o.nov26 = C.monthGrid(2026, 10);
o.firsts = [o.feb24, o.feb21, o.mar26, o.nov26].map((g) => [g.length, g[0][0].key, g[g.length - 1][6].key, g.flat().filter((c) => c.inMonth).length]);
o.sunFirst = C.monthGrid(2026, 10, 0)[0][0].key;
o.add = [C.addMonths(2026, 11, 1), C.addMonths(2026, 0, -1), C.addMonths(2026, 0, -13), C.addMonths(2024, 1, 12)];
o.between = [C.daysBetween("2026-03-07", "2026-03-09"), C.daysBetween("2026-10-31", "2026-11-02"), C.daysBetween("2026-12-31", "2027-01-01")];
o.weekOff = [C.weekOffset("2026-03-09", new Date(2026, 2, 7, 23, 30)), C.weekOffset("2026-03-21", new Date(2026, 2, 7, 1)), C.weekOffset("2026-03-01", new Date(2026, 2, 7))];
o.monthOff = C.monthOffset(2027, 1, new Date(2026, 10, 24));
const keep = (iso, key) => { const d = new Date(C.moveToDay(iso, key)); return [C.dayKey(d), d.getHours(), d.getMinutes()]; };
o.moves = [keep(new Date(2026, 2, 6, 18, 45).toISOString(), "2026-03-09"),  // across spring-forward (US 8 Mar)
           keep(new Date(2026, 9, 30, 7, 15).toISOString(), "2026-11-02"),  // across fall-back (US 1 Nov)
           keep(new Date(2026, 0, 31, 23, 59).toISOString(), "2026-02-28"), // to a shorter month's end
           keep(new Date(2026, 1, 28, 0, 0).toISOString(), "2026-03-31")];
o.utcHours = new Date(C.moveToDay(new Date(2026, 2, 6, 18, 45).toISOString(), "2026-03-09")).getUTCHours();
o.byDay = C.byDay([{ id: "b", when: new Date(2026, 2, 8, 12).toISOString() }, { id: "a", when: new Date(2026, 2, 8, 9).toISOString() }, { id: "x", when: null }]);
o.key = C.dayKey(new Date(2026, 2, 8, 23, 59));
console.log(JSON.stringify(o));
""", "America/New_York")
    assert o["firsts"] == [[5, "2024-01-29", "2024-03-03", 29], [4, "2021-02-01", "2021-02-28", 28],
                           [6, "2026-02-23", "2026-04-05", 31], [6, "2026-10-26", "2026-12-06", 30]]
    assert all(len(w) == 7 for w in o["mar26"])
    assert o["sunFirst"] == "2026-11-01"  # weeks may start on Sunday
    assert o["add"] == [{"y": 2027, "m": 0}, {"y": 2025, "m": 11}, {"y": 2024, "m": 11}, {"y": 2025, "m": 1}]
    assert o["between"] == [2, 2, 1]  # a 23 h and a 25 h day still count as one day each
    assert o["weekOff"] == [0, 2, -1]
    assert o["monthOff"] == 3
    assert o["moves"] == [["2026-03-09", 18, 45], ["2026-11-02", 7, 15], ["2026-02-28", 23, 59], ["2026-03-31", 0, 0]]
    assert o["utcHours"] == 22  # 18:45 EDT (UTC-4) after the switch, not 23:45 (the old EST offset)
    assert list(o["byDay"]) == ["2026-03-08"] and [i["id"] for i in o["byDay"]["2026-03-08"]] == ["a", "b"]
    assert o["key"] == "2026-03-08"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_drag_to_another_day_keeps_the_time_of_day_and_reschedules():
    """The month view's drop: calendar.moveToDay gives the new time, which goes through the existing reschedule route."""
    o = _node(r"""
const C = require(process.argv[1]);
const from = new Date(2026, 11, 3, 16, 20);
console.log(JSON.stringify({ to: C.moveToDay(from.toISOString(), "2026-12-17"), want: new Date(2026, 11, 17, 16, 20).toISOString() }));
""", "Europe/London")
    assert o["to"] == o["want"]
    c = _clip()
    up = store.put("upload", {"clip_id": c["id"], "status": "queued", "platform": "youtube", "publish_at": "2099-12-03T16:20:00+00:00"})
    moved = server.move_upload(up["id"], {"when": "2099-12-17T16:20:00+00:00"})
    assert moved["publish_at"] == "2099-12-17T16:20:00+00:00"


def test_tracking_part_saves_from_a_clip_with_default_framing():
    c = _clip()
    p = rp.create_preset({"name": "T", "clip_id": c["id"], "parts": ["tracking"]})
    assert p["parts"]["tracking"]["follow"] is False and "framing" not in p["parts"]["tracking"]


def test_export_carries_no_library_asset_ids():
    c = _clip()
    a = store.put("asset", {"kind": "image", "name": "logo", "file": "logo.png", "tags": []})
    server.patch_clip(c["id"], {"edit": {"overlays": [{"kind": "image", "asset_id": a["id"], "t": 1}, {"kind": "text", "text": "hi", "t": 2}]}})
    p = rp.create_preset({"name": "O", "clip_id": c["id"], "parts": ["overlays"]})
    raw = rp.export_preset(p["id"]).body.decode()
    assert a["id"] not in raw and '"hi"' in raw
    store.delete("asset", a["id"])


def test_one_failing_clip_is_skipped_not_fatal(monkeypatch):
    pid = _proj()["id"]
    src = _styled(_clip(pid))
    a, b = _clip(pid, 30.0, 40.0), _clip(pid, 50.0, 60.0)
    p = rp.create_preset({"name": "B", "clip_id": src["id"], "parts": ["bleep"]})
    real = server.patch_clip

    def flaky(cid, body):
        if cid == a["id"]:
            raise HTTPException(409, server.POSTED)  # posted between the check and the patch
        return real(cid, body)
    monkeypatch.setattr(server, "patch_clip", flaky)
    r = rp.apply_preset(p["id"], {"clip_ids": [a["id"], b["id"]]})
    assert r == {"updated": 1, "skipped": [a["id"]]} and store.get("clip", b["id"])["edit"]["bleep"] == "beep"
