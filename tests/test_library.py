"""Media library (uploads + built-in sound effects), clip overlays, and the AI plan → approve → apply flow."""
import os
import subprocess
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import ai, assets, media, pipeline, server, store  # noqa: E402


def need_ffmpeg():
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")


@pytest.fixture
def api():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


def _png(path, color=(0, 0, 255)):
    import cv2
    import numpy as np
    img = np.zeros((60, 60, 3), np.uint8)
    img[:] = color
    cv2.imwrite(str(path), img)
    return path


def _project(duration=30.0):
    p = pipeline.new_project("lib")
    store.update("project", p["id"], {"meta": {"width": 1920, "height": 1080, "duration": duration, "fps": 30, "has_audio": True},
                                      "status": "ready", "source": "source.mp4"})
    return p


def _clip(start=10.0, end=20.0):
    return pipeline.new_clip(_project()["id"], {"start": start, "end": end})


def _sound(duration=1.5):
    return store.put("asset", {"kind": "sound", "name": "Test hit", "file": "x.wav", "duration": duration, "width": 0,
                               "height": 0, "builtin": False, "tags": ["hit"]})


# ---------- library ----------

def test_builtin_sound_effects_are_made_locally():
    need_ffmpeg()
    docs = assets.ensure_builtins()
    names = {d["name"].lower() for d in docs}
    assert len(docs) >= 10 and {"whoosh", "boom", "ding", "pop", "riser", "bass drop", "rimshot", "glitch",
                                "record scratch", "applause"} <= names
    for d in docs:
        assert d["builtin"] and d["kind"] == "sound" and d["tags"] and d["duration"] > 0
        assert (assets.ASSETS / d["file"]).stat().st_size > 1000
    assert [d["id"] for d in assets.ensure_builtins()] == [d["id"] for d in docs]  # made once, not again


def test_asset_upload_list_serve_and_delete(api, tmp_path):
    need_ffmpeg()
    r = api.post("/api/assets", files={"file": ("logo.png", _png(tmp_path / "l.png").read_bytes(), "image/png")},
                 data={"name": "Logo", "tags": "brand, logo ,"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert (a["kind"], a["name"], a["width"], a["height"], a["builtin"], a["tags"]) == ("image", "Logo", 60, 60, False, ["brand", "logo"])
    listed = api.get("/api/assets").json()
    mine = next(x for x in listed if x["id"] == a["id"])
    assert mine["used_by"] == 0 and any(x["builtin"] for x in listed)
    assert api.get(f"/assets/{a['file']}").status_code == 200
    assert api.post("/api/assets", files={"file": ("evil.exe", b"MZ", "application/octet-stream")}).status_code == 400
    builtin = next(x for x in listed if x["builtin"])
    assert api.delete(f"/api/assets/{builtin['id']}").status_code == 400
    assert api.delete(f"/api/assets/{a['id']}").status_code == 200
    assert not (assets.ASSETS / a["file"]).exists() and store.get("asset", a["id"]) is None


# ---------- overlays ----------

def test_overlays_validate_against_the_library_and_the_clip():
    clip, snd = _clip(), _sound(1.5)
    ok = server.clean_edit({"overlays": [{"kind": "sound", "asset_id": snd["id"], "t": 2},
                                         {"kind": "text", "text": "NO WAY", "t": 1, "d": 2, "x": 0.5, "y": 0.2, "anim": "pop"}]}, clip)
    s, t = ok["overlays"]
    assert s["d"] == 1.5 and s["volume"] == 1.0 and s["id"] and t["anim"] == "pop" and t["w"] > 0
    assert server.clean_edit({"overlays": [{"kind": "sound", "asset_id": snd["id"], "t": 9.5}]}, clip)["overlays"][0]["d"] == 0.5
    for bad in ([{"kind": "image", "asset_id": "nope", "t": 1, "d": 1}], [{"kind": "image", "asset_id": snd["id"], "t": 1, "d": 1}],
                [{"kind": "text", "text": "x", "t": 11, "d": 1}], [{"kind": "text", "text": "x" * 81, "t": 1, "d": 1}],
                [{"kind": "laser", "t": 1}], [{"kind": "text", "text": "x", "t": 1, "d": 1}] * 61, "nope"):
        with pytest.raises(HTTPException):
            server.clean_edit({"overlays": bad}, clip)


def test_overlay_times_follow_speed_and_jump_cuts():
    keeps = [(10.0, 12.0), (14.0, 20.0)]
    assert media.overlay_window(5.0, 2.0, 10.0, keeps, 2.0) == pytest.approx((1.5, 2.5))
    assert media.overlay_window(1.0, 1.0, 10.0, None, 1.0) == pytest.approx((1.0, 2.0))


def test_render_with_image_overlay_sound_effect_and_text(tmp_path):
    need_ffmpeg()
    import cv2
    src = tmp_path / "src.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=4:r=25", "-f", "lavfi",
                    "-i", "sine=f=220:d=4", "-shortest", "-pix_fmt", "yuv420p", str(src)], check=True)
    img = _png(tmp_path / "red.png", (0, 0, 255))
    ding = next(d for d in assets.ensure_builtins() if d["name"] == "Ding")
    overlays = [{"kind": "image", "path": str(img), "t": 1.0, "d": 2.0, "x": 0.5, "y": 0.5, "w": 0.4, "anim": "fade", "layer": 0},
                {"kind": "sound", "path": str(assets.ASSETS / ding["file"]), "t": 0.5, "d": ding["duration"], "volume": 1.0}]
    texts = [{"kind": "text", "text": "HELLO", "t": 0.0, "d": 3.0, "x": 0.5, "y": 0.2, "w": 0.6, "anim": "pop", "layer": 0}]
    ass = media.build_ass([], 0.0, 4.0, 1.0, (1080, 1920), overlays=texts)
    assert "HELLO" in ass
    out = tmp_path / "out.mp4"
    media.render(src, out, 0.0, 4.0, {"aspect": "9:16", "speed": 1.0}, {"width": 320, "height": 180, "fps": 25, "has_audio": True},
                 ass, overlays=overlays)
    m = media.probe(out)
    assert m["has_audio"] and abs(m["duration"] - 4.0) < 0.3
    cap = cv2.VideoCapture(str(out))

    def red_at(t):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        _, f = cap.read()
        b, g, r = (int(v) for v in f[960, 540])
        return r > 150 and g < 100

    assert not red_at(0.5) and red_at(2.0) and not red_at(3.6)


# ---------- AI plan → approve → apply ----------

def test_plan_drops_invented_assets_and_apply_applies_only_the_picked_items(monkeypatch):
    clip, snd = _clip(), _sound(1.0)
    seen = {}

    def fake_ask(system, user, schema, effort="high"):
        seen["user"] = user if isinstance(user, str) else "".join(user)
        return ai.Plan(summary="Punch it up", items=[
            ai.PlanItem(kind="edit", title="Faster", why="tighter", edit=ai.PlanEdit(speed=1.1)),
            ai.PlanItem(kind="overlay", title="Hit", why="impact", overlay=ai.PlanOverlay(kind="sound", asset_id=snd["id"], t=2.0)),
            ai.PlanItem(kind="overlay", title="Fake", why="made up", overlay=ai.PlanOverlay(kind="image", asset_id="invented", t=1.0, d=1.0)),
            ai.PlanItem(kind="overlay", title="Caption pop", why="hook", overlay=ai.PlanOverlay(kind="text", text="WAIT", t=0.5, d=1.5)),
            ai.PlanItem(kind="track", title="Follow face", why="talking head", track=ai.PlanTrack(mode="face", offset_y=0.1)),
            ai.PlanItem(kind="captions", title="Neon", why="energy", captions=ai.PlanCaptions(caption_style="neon")),
        ])

    monkeypatch.setattr(ai, "_ask", fake_ask)
    plan = server.plan_clip(clip["id"], {"prompt": ""})
    assert plan["dropped"] == 1 and len(plan["items"]) == 5 and plan["summary"] == "Punch it up"
    assert snd["id"] in seen["user"]  # Claude sees the real library
    assert store.get("plan", plan["plan_id"])["clip_id"] == clip["id"]
    by = {i["title"]: i for i in plan["items"]}
    assert by["Faster"]["patch"] == {"speed": 1.1} and by["Hit"]["patch"]["asset_id"] == snd["id"]

    picked = [by[k]["id"] for k in ("Faster", "Hit", "Follow face")]
    out = server.apply_plan(clip["id"], {"plan_id": plan["plan_id"], "item_ids": picked})
    assert out["edit"]["speed"] == 1.1 and out["edit"]["caption_style"] != "neon"
    assert [o["asset_id"] for o in out["edit"]["overlays"]] == [snd["id"]]
    assert out["edit"]["framing"]["offset_y"] == 0.1
    assert store.find("job", clip_id=clip["id"], kind="track")[0]["mode"] == "face"
    # applying again appends overlays rather than replacing them
    out = server.apply_plan(clip["id"], {"plan_id": plan["plan_id"], "item_ids": [by["Caption pop"]["id"]]})
    assert [o["kind"] for o in out["edit"]["overlays"]] == ["sound", "text"]
    with pytest.raises(HTTPException):
        server.apply_plan(_clip()["id"], {"plan_id": plan["plan_id"], "item_ids": picked})  # someone else's plan


def test_get_track_returns_zoom_keyframes_unchanged():
    clip = _clip()
    server.patch_clip(clip["id"], {"edit": {"track_manual": [[1.0, 0.2, 0.3, 2.5], [2.0, 0.4, 0.5]]}})
    assert server.get_track(clip["id"])["manual"] == [[1.0, 0.2, 0.3, 2.5], [2.0, 0.4, 0.5]]
