"""Streamer badges and the streamer title card: badge themes per platform, auto-fill from the source, validation,
collision avoidance (headline, captions, app safe zones), a real 9:16 render with a round avatar, and old renders
unchanged."""
import os
import re
import shutil
import subprocess
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from reelwright import assets, longform, media, pipeline, qa, server, store  # noqa: E402

V = (1080, 1920)
WORDS = [{"s": 1.0, "e": 1.4, "w": "hello"}, {"s": 1.5, "e": 1.9, "w": "world"}]
CLIP = {"start": 0, "end": 10}


KINDS = ("project", "clip", "longform", "source", "asset", "job")


@pytest.fixture(autouse=True)
def clean():
    """Every doc (and project folder / asset file) a test makes is removed afterwards, and the saved card pref restored."""
    before, pref = {k: {d["id"] for d in store.find(k)} for k in KINDS}, store.get("pref", "streamer_card")
    yield
    for k in KINDS:
        for d in store.find(k):
            if d["id"] not in before[k]:
                store.delete(k, d["id"])
                if k == "project":
                    shutil.rmtree(pipeline.pdir(d["id"]), ignore_errors=True)
                if k == "asset" and d.get("file"):
                    (assets.ASSETS / d["file"]).unlink(missing_ok=True)
    if pref:
        store.put("pref", pref)
    else:
        store.delete("pref", "streamer_card")


def _card(**kw):
    e = {**media.DEFAULT_EDIT, "aspect": "9:16",
         "streamer_card": {"on": True, "name": "Night Owl", "handle": "nightowl", "category": "Just Chatting", **kw}}
    return media.card_spec(e)


def _events(ass: str) -> list[str]:
    return [x for x in ass.splitlines() if x.startswith("Dialogue:") and ",Card," in x]


# ---------- badge themes ----------

def test_badge_themes_cover_every_platform_and_use_bundled_fonts():
    plats = {b["platform"] for b in media.BADGE_THEMES.values()}
    assert {"twitch", "kick", "youtube", "generic"} <= plats
    for p in ("twitch", "kick"):
        assert {b["kind"] for b in media.BADGE_THEMES.values() if b["platform"] == p} == {"live", "card", "minimal"}
    for bid, b in media.BADGE_THEMES.items():
        assert b["font"] in media.HEADLINE_FONTS and b["sub_font"] in media.HEADLINE_FONTS, bid
    assert media.BADGE_THEMES["twitch_live"]["accent"] == "#9146FF" and media.BADGE_THEMES["kick_live"]["accent"] == "#53FC18"


@pytest.mark.parametrize("bid", list(media.BADGE_THEMES))
def test_each_badge_draws_its_platform_text(bid):
    b = media.BADGE_THEMES[bid]
    spec = _card(style=bid, platform=b["platform"])
    ass = media.build_ass([], 0, 10, 1, V, captions=False, card=spec)
    ev = _events(ass)
    assert ev and all("\\fn" not in x or any(f"\\fn{f}" in x for f in media.HEADLINE_FONTS) for x in ev)
    body = "\n".join(ev)
    url = media.card_url(b["platform"], "nightowl")
    if b["kind"] == "minimal":
        assert "nightowl" in body and media.PLATFORM_NAMES[b["platform"]].upper() in body
    else:
        assert url in body
    if b["kind"] == "live":
        assert "LIVE" in body and "\\t(" in body  # the dot pulses
    if b["kind"] == "card":
        assert "Night Owl" in body and "Just Chatting" in body
    assert media._ass_color(b["accent"]) in body
    assert "\\p1" in body  # plates, pills and dots are vector drawings (no logo artwork)


def test_platform_headline_themes_exist():
    for tid in ("twitch_purple", "kick_green", "youtube_red"):
        th = media.HEADLINE_THEMES[tid]
        assert th["font"] in media.HEADLINE_FONTS and th["kicker_font"] in media.HEADLINE_FONTS


def test_card_off_or_empty_is_no_card():
    assert media.card_spec({**media.DEFAULT_EDIT}) is None
    assert _card(on=False) is None
    assert _card(name="", handle="") is None


# ---------- auto-fill ----------

def _proj(**kw):
    p = pipeline.new_project("sc")
    store.update("project", p["id"], {"meta": {"width": 1920, "height": 1080, "duration": 60.0, "fps": 30, "has_audio": True},
                                      "status": "ready", "source": "source.mp4", **kw})
    return store.get("project", p["id"])


def test_autofill_reads_twitch_kick_and_youtube_sources(monkeypatch):
    monkeypatch.setattr(pipeline.accounts, "all_accounts", lambda platform=None: [])
    g = pipeline.streamer_guess(_proj(source_url="https://www.twitch.tv/videos/2001", source_info={"channel": "NightOwl"}))
    assert g == {"platform": "twitch", "handle": "nightowl", "name": "NightOwl"}
    g = pipeline.streamer_guess(_proj(source_url="https://kick.com/trainwreck/videos/abc-1", source_info={"channel": "Trainwreck"}))
    assert g == {"platform": "kick", "handle": "trainwreck", "name": "Trainwreck"}
    g = pipeline.streamer_guess(_proj(source_url="https://www.youtube.com/@CoolCreator/streams"))
    assert g["platform"] == "youtube" and g["handle"] == "CoolCreator"
    src = store.put("source", {"platform": "kick", "handle": "xqc", "imported": [], "vods": []})
    try:
        g = pipeline.streamer_guess(_proj(origin={"source_id": src["id"], "platform": "kick", "url": "https://kick.com/video/1"}))
        assert g["platform"] == "kick" and g["handle"] == "xqc"
    finally:
        store.delete("source", src["id"])
    assert pipeline.streamer_guess(_proj()) == {}
    monkeypatch.setattr(pipeline.accounts, "all_accounts", lambda platform=None: [{"platform": "youtube", "handle": "@me", "name": "Me"}])
    assert pipeline.streamer_guess(_proj()) == {"platform": "youtube", "handle": "me", "name": "Me"}


# ---------- validation ----------

def _image_asset(tmp_path):
    f = assets.ASSETS / f"av_{store.new_id()}.png"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(f)], check=True)
    return store.put("asset", {"kind": "image", "file": f.name, "name": "avatar", "tags": []})


def test_card_is_validated_and_cleaned(tmp_path):
    a = _image_asset(tmp_path)
    try:
        out = server.clean_edit({"streamer_card": {
            "on": 1, "style": "kick_card", "name": "  Night Owl  ", "handle": "@nightowl", "platform": "kick", "category": " Chess ",
            "avatar_asset_id": a["id"], "accent": "#abcdef", "show": "first_n_seconds", "seconds": 99, "position": "custom",
            "x": 5, "y": -1, "scale": 9, "anim": "pop", "junk": 1}}, CLIP)
        assert out["streamer_card"] == {"on": True, "style": "kick_card", "name": "Night Owl", "handle": "nightowl", "platform": "kick",
                                        "category": "Chess", "avatar_asset_id": a["id"], "accent": "#ABCDEF", "show": "first_n_seconds",
                                        "seconds": 30.0, "position": "custom", "x": 1.0, "y": 0.0, "scale": 2.0, "anim": "pop"}
        assert server.clean_edit({"streamer_card": None}, CLIP) == {"streamer_card": None}
        assert server.clean_edit({"streamer_card": {"avatar_asset_id": None}}, CLIP) == {"streamer_card": {"avatar_asset_id": None}}
    finally:
        store.delete("asset", a["id"])


@pytest.mark.parametrize("bad", [
    {"name": "n" * 41}, {"handle": "h" * 41}, {"handle": "bad handle!"}, {"category": "c" * 41}, {"style": "sparkly"},
    {"platform": "myspace"}, {"show": "forever"}, {"position": "middle"}, {"anim": "spin"}, {"accent": "purple"},
    {"seconds": "soon"}, {"avatar_asset_id": "nope"}, {"name": 5}, "on",
])
def test_bad_cards_are_rejected(bad):
    with pytest.raises(HTTPException) as ex:
        server.clean_edit({"streamer_card": bad}, CLIP)
    assert ex.value.status_code == 400


def test_avatar_must_be_an_image():
    s = store.put("asset", {"kind": "sound", "file": "x.wav", "name": "s", "tags": []})
    try:
        with pytest.raises(HTTPException):
            server.clean_edit({"streamer_card": {"avatar_asset_id": s["id"]}}, CLIP)
    finally:
        store.delete("asset", s["id"])


# ---------- collisions ----------

def _overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def test_card_keeps_clear_of_the_headline():
    hl = media.headline_spec({**media.DEFAULT_EDIT, "headline": {"on": True, "text": "Biggest clutch of the year", "position": "top"}})
    spec = _card(style="twitch_card", position="top")
    ass = media.build_ass([], 0, 10, 1, V, captions=False, headline=hl, card=spec)
    _, hy, _, hh = media.headline_layout(hl, *V)["rect"]
    lay = media.card_place(spec, *V, hl, None, ass)
    x, y, w, h = lay["rect"]
    assert not _overlaps((y, y + h), (hy, hy + hh))


def test_card_keeps_clear_of_the_captions():
    spec = _card(style="kick_live", position="bottom")
    ass = media.build_ass(WORDS, 0, 10, 1, V, pos="low", card=spec)
    band = media.caption_band(ass, V[1])
    assert band
    x, y, w, h = media.card_place(spec, *V, None, None, ass)["rect"]
    assert not _overlaps((y, y + h), band)
    # the card events in the ASS are drawn at that same placement
    assert any(f" {y} " in ev for ev in _events(ass))


def test_corner_cards_stay_out_of_the_app_ui_on_vertical_video():
    for pos in ("tl", "tr", "bl", "br"):
        x, y, w, h = media.card_layout(_card(style="twitch_card", position=pos), *V)["rect"]
        assert y >= media.CARD_SAFE["top"] * V[1] - 1 and y + h <= (1 - media.CARD_SAFE["bottom"]) * V[1] + 1, pos
        assert x + w <= (1 - media.CARD_SAFE["right"]) * V[0] + 1, pos
    assert media.CARD_SAFE == {k: max(z[k] for z in qa.SAFE_ZONES.values()) for k in ("top", "bottom", "right")}


def test_qa_warns_when_a_custom_card_sits_under_the_app_ui():
    clip = {"edit": {**media.DEFAULT_EDIT, "aspect": "9:16", "captions": False,
                     "streamer_card": {"on": True, "name": "Night Owl", "handle": "nightowl", "position": "custom", "x": 0.5, "y": 0.97}}}
    msgs = [i["msg"] for i in qa.safe_zones(clip, *V)]
    assert any("streamer card" in m for m in msgs)


# ---------- rendering ----------

def test_renders_without_a_card_are_byte_identical():
    for edit in ({"aspect": "9:16"}, {"aspect": "9:16", "layout": "split", "split": 0.4}, {"aspect": "16:9", "fit": "blur"}):
        assert media.build_filters(edit, 1920, 1080, 10, True)[0] == media.build_filters({**edit, "streamer_card": None}, 1920, 1080, 10, True)[0]
    a = media.build_ass(WORDS, 0, 10, 1, V, title="t", sticker="@me", pos="high")
    assert a == media.build_ass(WORDS, 0, 10, 1, V, title="t", sticker="@me", pos="high", card=None)
    assert ",Card," not in a and "Style: Card" not in a
    o = {"input": 1, "a": 0.0, "b": 2.0, "x": 0.5, "y": 0.5, "w": 0.3, "anim": "slide"}
    assert media._overlay_chain(o, 0, 1080, 1920) == (
        "[1:v]scale=324:-2,format=rgba,setpts=PTS-STARTPTS+0.000/TB[o0];[b0][o0]overlay=x='(540.0-w/2)-(540.0-w/2+w)*"
        "max(0,1-(t-0.000)/0.3)':y='960.0-h/2':eof_action=pass:enable='between(t,0.000,2.000)'[b1]")


def test_render_part_passes_the_card(monkeypatch):
    calls = []
    monkeypatch.setattr(media, "render", lambda src, out, start, end, e, meta, ass, *a, **kw: calls.append({"ass": ass, **kw}))
    p = _proj()
    d = store.put("longform", {"project_id": p["id"], "segments": [{"start": 0.0, "end": 30.0}], "edit": {}, "chapters": []})
    e = {**media.DEFAULT_EDIT, "captions": False, "streamer_card": {"on": True, "style": "twitch_live", "handle": "nightowl"}}
    longform._render_part(d, store.get("project", p["id"]), [(0.0, 30.0)], e, [], pipeline.pdir(p["id"]) / "x.mp4", 0.0, None)
    [c] = calls
    assert c["card"] and c["card"]["handle"] == "nightowl" and "twitch.tv/nightowl" in c["ass"]


def test_saved_card_goes_on_new_clips():
    p = _proj(source_url="https://www.twitch.tv/videos/1", source_info={"channel": "NightOwl"})
    c = pipeline.new_clip(p["id"], {"start": 1.0, "end": 9.0, "title": "x"})
    server.patch_clip(c["id"], {"edit": {"streamer_card": {"on": True, "style": "kick_card", "name": "", "handle": "", "position": "br"}}})
    try:
        server.save_streamer_card(c["id"], {"auto": True})
        n = pipeline.new_clip(p["id"], {"start": 2.0, "end": 8.0, "title": "y"})
        sc = n["edit"]["streamer_card"]
        assert sc["on"] and sc["style"] == "kick_card" and sc["position"] == "br"
        assert sc["handle"] == "nightowl" and sc["name"] == "NightOwl"  # empty fields auto-filled from the source
        server.save_streamer_card(c["id"], {"auto": False})
        assert not (pipeline.new_clip(p["id"], {"start": 2.0, "end": 8.0})["edit"].get("streamer_card") or {}).get("on")
    finally:
        store.delete("pref", "streamer_card")


def _pixel(path, t, x, y):
    raw = subprocess.run([media.FFMPEG, "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-vf", f"crop=2:2:{x - x % 2}:{y - y % 2},format=rgb24", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return tuple(raw[:3])


def test_real_9_16_render_with_a_round_avatar_and_card(tmp_path):
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")
    src = tmp_path / "src.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=640x360:r=10:d=2",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    av = tmp_path / "av.png"
    subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=0x00FF00:s=96x96", "-frames:v", "1", str(av)], check=True)
    meta = {"width": 640, "height": 360, "fps": 10, "has_audio": False}
    for layout in ("single", "split", "pip"):
        edit = {"aspect": "9:16", "loudnorm": False, "layout": layout,
                "headline": {"on": True, "text": "Clutch", "push": True},
                "streamer_card": {"on": True, "style": "twitch_card", "name": "Night Owl", "handle": "nightowl", "position": "bl"}}
        e = {**media.DEFAULT_EDIT, **edit}
        hl, card = media.headline_spec(e), media.card_spec(e, str(av))
        ass = media.build_ass([], 0, 2, 1, V, captions=False, split=media.seam(e), headline=hl, card=card)
        out = tmp_path / f"out_{layout}.mp4"
        media.render(src, out, 0.0, 2.0, edit, meta, ass, headline=hl, card=card)
        m = media.probe(out)
        assert (m["width"], m["height"]) == V and m["duration"] > 1.5
    cx, cy, d = media.card_place(card, *V, hl, media.seam(e), ass)["avatar"]
    r, g, b = _pixel(out, 1.0, cx, cy)
    assert g > 180 and r < 90 and b < 90  # the avatar's green at the circle's centre
    r, g, b = _pixel(out, 1.0, cx - d // 2 + 3, cy - d // 2 + 3)  # its square corner is cut away (round mask)
    assert not (g > 180 and r < 90 and b < 90)


# ---------- review fixes ----------

def test_autofill_ignores_clip_slugs_sanitizes_handles_and_keeps_foreign_names(monkeypatch):
    monkeypatch.setattr(pipeline.accounts, "all_accounts", lambda platform=None: [{"platform": "youtube", "handle": "@me", "name": "Me"}])
    g = pipeline.streamer_guess(_proj(source_url="https://clips.twitch.tv/FunnySlug-abc123", source_info={"channel": "NightOwl"}))
    assert g == {"platform": "twitch", "handle": "nightowl", "name": "NightOwl"}  # the slug isn't the handle
    g = pipeline.streamer_guess(_proj(source_url="https://www.youtube.com/@Cool%20Creator!/streams"))
    assert g["handle"] == "Cool20Creator"  # only what clean_edit accepts
    server.clean_edit({"streamer_card": g}, CLIP)
    g = pipeline.streamer_guess(_proj(source_url="https://www.tiktok.com/@someone/video/1", source_info={"channel": "Someone"}))
    assert g == {"name": "Someone"}  # not merged with an unrelated linked YouTube account


def test_qa_places_the_card_clear_of_the_captions_like_the_render():
    e = {**media.DEFAULT_EDIT, "aspect": "9:16", "captions": True, "caption_pos": "low",
         "streamer_card": {"on": True, "name": "Night Owl", "handle": "nightowl", "style": "twitch_card", "position": "bottom"}}
    spec = media.card_spec(e)
    real = media.card_place(spec, *V, None, None, media.build_ass(WORDS, 0, 10, 1, V, pos="low", card=spec))["rect"]
    seen = []
    orig = media.card_place
    media.card_place = lambda *a, **k: seen.append(orig(*a, **k)["rect"]) or orig(*a, **k)
    try:
        qa.safe_zones({"edit": e}, *V)
    finally:
        media.card_place = orig
    assert seen and all(r == real for r in seen)


# ---------- legibility on every pill and over bright frames ----------

@pytest.mark.parametrize("bid", [b for b, t in media.BADGE_THEMES.items() if t["kind"] == "live"])
def test_the_live_dot_has_a_ring_in_the_pill_text_colour_that_reads_on_every_pill(bid):
    ev = _events(media.build_ass([], 0, 10, 1, V, captions=False, card=_card(style=bid)))
    dot = next(x for x in ev if "\\t(" in x)  # the pulsing dot
    ring = "\\3c" + media._ass_color(media.BADGE_THEMES[bid]["on_accent"])  # white on Twitch/YouTube, near-black on Kick
    assert re.search(r"\\bord[1-9]", dot) and ring in dot  # the ring stays while only the fill pulses


@pytest.mark.parametrize("bid", [b for b, t in media.BADGE_THEMES.items() if t["kind"] == "minimal"])
def test_the_minimal_handle_sits_on_a_soft_dark_chip_without_a_heavy_outline(bid):
    ev = _events(media.build_ass([], 0, 10, 1, V, captions=False, card=_card(style=bid)))
    who = next(x for x in ev if "/nightowl" in x)
    assert "\\bord0" in who
    chip = [x for x in ev if "\\p1" in x and media._ass_color(media.BADGE_THEMES[bid]["bg"]) in x]
    assert chip and all("\\1a&H00&" not in x for x in chip)  # see-through, not a solid block
