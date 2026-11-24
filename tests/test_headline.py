"""Themed headline banner ("headliner" + "top line" kicker): themes, text fitting, ASS/filter output, layouts and
captions, validation, AI options, defaults for new clips, and a real render."""
import os
import re
import subprocess
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from reelwright import ai, media, pipeline, server, store  # noqa: E402

V = (1080, 1920)
WORDS = [{"s": 1.0, "e": 1.4, "w": "hello"}, {"s": 1.5, "e": 1.9, "w": "world"}]


def _spec(**h):
    e = {**media.DEFAULT_EDIT, "aspect": "9:16", "headline": {"on": True, "text": "He actually pulled it off", **h}}
    return media.headline_spec(e)


def _ass(spec, **kw):
    return media.build_ass(WORDS, 0.0, 10.0, 1.0, V, headline=spec, **kw)


def _margin_v(ass: str, style: str) -> int:
    line = next(x for x in ass.splitlines() if x.startswith(f"Style: {style},"))
    return int(line.split(",")[21])


# ---------- themes ----------

def test_themes_are_complete_and_use_only_bundled_fonts():
    assert len(media.HEADLINE_THEMES) == 13  # 10 looks + Twitch, Kick and YouTube colour schemes
    for tid, th in media.HEADLINE_THEMES.items():
        assert media.HEADLINE_THEME_KEYS <= th.keys(), tid
        assert th["font"] in media.HEADLINE_FONTS and th["kicker_font"] in media.HEADLINE_FONTS, tid
    assert set(media.HEADLINE_AUTO) <= set(ai.VIDEO_TYPES) and set(media.HEADLINE_AUTO.values()) <= set(media.HEADLINE_THEMES)


@pytest.mark.parametrize("tid", list(media.HEADLINE_THEMES))
def test_each_theme_draws_its_headline(tid):
    spec = _spec(theme=tid, kicker="Part 2")
    ass = _ass(spec)
    th = media.HEADLINE_THEMES[tid]
    text = "HE ACTUALLY PULLED IT OFF" if th["uppercase"] else "He actually pulled it off"
    assert text.split()[0] in ass and "PART 2" in ass.upper()
    head = [x for x in ass.splitlines() if x.startswith("Dialogue:") and "\\q2" in x]
    assert head and all(f"\\fn{th['font']}" in x for x in head if "PULLED" in x.upper())
    bars = [x for x in ass.splitlines() if "\\p1" in x]
    assert bool(bars) == bool(th["bar"]) or tid == "news_ticker"  # the ticker's kicker block is a drawing too
    if th["bar2"]:
        assert any("\\clip(" in x for x in bars)  # gradient bands clipped to the rounded bar
    graph = media.build_filters({"aspect": "9:16"}, 1920, 1080, 10, True, headline=spec)[0]
    assert ("boxblur" in graph) == bool(th["blur"])


def test_auto_follows_the_video_type_and_custom_uses_the_saved_theme():
    e = {**media.DEFAULT_EDIT, "headline": {"on": True, "text": "x", "theme": "auto"}}
    assert media.headline_spec(e, "gaming")["theme"] == "gaming_hud"
    assert media.headline_spec(e, "podcast")["theme"] == "podcast_card"
    assert media.headline_spec(e, None)["theme"] == "bold_bar"
    mine = {**media.HEADLINE_THEMES["neon"], "color": "#123456"}
    spec = media.headline_spec({**e, "headline": {**e["headline"], "theme": "custom"}}, custom=mine)
    assert spec["color"] == "#123456"
    assert media.headline_spec({**e, "headline": {**e["headline"], "theme": "custom"}})["theme"] == "custom"  # none saved: still draws
    # the shared text-style keys override the theme
    assert media.headline_spec({**e, "headline_color": "#00FF00", "headline_size": 99}, "gaming")["color"] == "#00FF00"


def test_off_or_empty_headline_is_no_headline():
    assert media.headline_spec(media.DEFAULT_EDIT) is None
    assert media.headline_spec({**media.DEFAULT_EDIT, "headline": {"on": False, "text": "x"}}) is None
    assert media.headline_spec({**media.DEFAULT_EDIT, "headline": {"on": True, "text": "   "}}) is None


# ---------- fitting ----------

def test_short_text_stays_one_line_at_full_size():
    lines, size = media.fit_headline("Big win", 90, 0.6, 1000)
    assert lines == ["Big win"] and size == 90


def test_long_text_wraps_to_two_lines_then_shrinks():
    text = "The moment he realised the whole lobby was watching him"
    lines, size = media.fit_headline(text, 90, 0.6, 900)
    assert len(lines) == 2 and size < 90
    assert all(len(x) * size * 0.6 <= 900 for x in lines) and " ".join(lines) == text
    lines2, size2 = media.fit_headline("Two lines fit fine here", 90, 0.6, 900)
    assert len(lines2) == 2 and size2 == 90


def test_text_too_long_even_at_the_floor_is_cut_with_an_ellipsis():
    lines, size = media.fit_headline("word " * 60, 90, 0.6, 600)
    assert len(lines) == 2 and lines[-1].endswith("…") and size == 45


# ---------- layouts and captions ----------

def test_top_headline_pushes_high_captions_below_it():
    spec = media.headline_spec({**media.DEFAULT_EDIT, "headline_size": 130, "headline": {
        "on": True, "text": "He actually pulled it off", "kicker": "Part 2", "position": "top"}})  # a big banner reaching "high"
    lay = media.headline_layout(spec, *V)
    plain = media.build_ass(WORDS, 0, 10, 1, V, pos="high")
    with_h = _ass(spec, pos="high")
    assert _margin_v(with_h, "Cap") < _margin_v(plain, "Cap")
    fs = int(next(x for x in with_h.splitlines() if x.startswith("Style: Cap,")).split(",")[2])
    cap_top = V[1] - _margin_v(with_h, "Cap") - 2.4 * fs
    assert cap_top >= lay["rect"][1] + lay["rect"][3]
    assert _margin_v(_ass(spec, pos="low"), "Cap") == _margin_v(media.build_ass(WORDS, 0, 10, 1, V, pos="low"), "Cap")
    small = _spec(position="top")  # a one-line banner clear of "high" captions leaves them where they were
    assert _margin_v(_ass(small, pos="high"), "Cap") == _margin_v(plain, "Cap")


def test_title_card_moves_under_a_top_headline():
    spec = _spec(position="top")
    lay = media.headline_layout(spec, *V)
    ass = _ass(spec, title="Title card")
    assert _margin_v(ass, "Title") >= lay["rect"][1] + lay["rect"][3]


def test_seam_position_sits_on_the_stacked_seam():
    spec = _spec(position="seam", kicker="")
    lay = media.headline_layout(spec, *V, seam_frac=0.4)
    x, y, w, h = lay["bar"]
    assert abs((y + h / 2) - 0.4 * V[1]) <= 1
    e = {**media.DEFAULT_EDIT, "aspect": "9:16", "layout": "split", "split": 0.4}
    ass = media.build_ass(WORDS, 0, 10, 1, V, split=media.seam(e), headline=spec)
    assert any("\\p1" in ln and f" {y} l " in ln for ln in ass.splitlines())
    assert media.headline_layout(spec, *V)["bar"][1] < V[1] * 0.2  # no seam (single layout): falls back to the top


def test_push_letterboxes_the_video_under_the_band():
    spec = _spec(position="top", push=True)
    lay = media.headline_layout(spec, *V)
    by, bh = lay["band"]
    assert by == 0 and bh % 2 == 0 and lay["rect"][1] + lay["rect"][3] <= bh
    g = media.build_filters({"aspect": "9:16"}, 1920, 1080, 10, False, headline=spec)[0]
    assert f"scale=1080:{1920 - bh}" in g and f"pad=1080:1920:0:{bh}" in g
    g2 = media.build_filters({"aspect": "9:16", "layout": "split", "regions": [
        {"id": "c", "role": "cam", "x": 0.7, "y": 0, "w": 0.3, "h": 0.3}]}, 1920, 1080, 10, False, headline=spec)[0]
    assert f"pad=1080:1920:0:{bh}" in g2


def test_first_seconds_limits_the_headline_window():
    spec = _spec(show="first_n_seconds", seconds=3)
    heads = [x for x in _ass(spec).splitlines() if x.startswith("Dialogue:") and ("\\q2" in x or "\\p1" in x)]
    assert heads and all(",0:00:03.00," in x for x in heads)
    g = media.build_filters({"aspect": "9:16"}, 1920, 1080, 10, True, headline=_spec(theme="glass", show="first_n_seconds", seconds=3))[0]
    assert "enable='lte(t,3.000)'" in g


# ---------- old renders unchanged ----------

def test_renders_without_a_headline_are_byte_identical():
    for edit in ({"aspect": "9:16"}, {"aspect": "9:16", "layout": "split", "split": 0.4}, {"aspect": "16:9", "fit": "blur"}):
        assert media.build_filters(edit, 1920, 1080, 10, True) == media.build_filters(edit, 1920, 1080, 10, True, headline=None)
    assert media.build_filters({"aspect": "9:16"}, 1920, 1080, 10, True)[0] == (
        "[0:v]crop=608:1080:656:0,scale=1080:1920,setsar=1,subtitles=captions.ass,format=yuv420p[v];"
        "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    a = media.build_ass(WORDS, 0, 10, 1, V, title="t", sticker="@me", pos="high")
    assert a == media.build_ass(WORDS, 0, 10, 1, V, title="t", sticker="@me", pos="high", headline=None)
    assert "\\q2" not in a and "\\p1" not in a


# ---------- validation ----------

CLIP = {"start": 0, "end": 10}


def test_headline_is_validated_and_cleaned():
    out = server.clean_edit({"headline": {"on": 1, "text": "  Hi  ", "kicker": "PART 2", "theme": "neon", "position": "seam",
                                          "show": "first_n_seconds", "seconds": 99, "push": 0, "junk": 1},
                             "headline_size": 999, "headline_color": "#abcdef"}, CLIP)
    assert out["headline"] == {"on": True, "text": "Hi", "kicker": "PART 2", "theme": "neon", "position": "seam",
                               "show": "first_n_seconds", "seconds": 30.0, "push": False}
    assert out["headline_size"] == 200 and out["headline_color"] == "#ABCDEF"
    assert server.clean_edit({"headline": None}, CLIP) == {"headline": None}
    assert server.clean_edit({"headline": {"theme": "auto"}}, CLIP) == {"headline": {"theme": "auto"}}  # partial update


@pytest.mark.parametrize("bad", [
    {"text": "x" * (media.HEADLINE_MAX + 1)}, {"kicker": "k" * (media.KICKER_MAX + 1)}, {"theme": "sparkly"},
    {"position": "left"}, {"show": "forever"}, {"text": 5}, "on", {"seconds": "soon"},
])
def test_bad_headlines_are_rejected(bad):
    with pytest.raises(HTTPException) as ex:
        server.clean_edit({"headline": bad}, CLIP)
    assert ex.value.status_code == 400


def _clip(hook="He hit the impossible shot", vt=None):
    p = pipeline.new_project("hl")
    store.update("project", p["id"], {"meta": {"width": 1920, "height": 1080, "duration": 60.0, "fps": 30, "has_audio": True},
                                      "status": "ready", "source": "source.mp4", **({"video_type": {"type": vt}} if vt else {})})
    return pipeline.new_clip(p["id"], {"start": 5.0, "end": 15.0, "title": "Clutch", "hook": hook})


def test_partial_patches_merge_into_the_headline():
    c = _clip()
    server.patch_clip(c["id"], {"edit": {"headline": {"on": True, "text": "First"}}})
    out = server.patch_clip(c["id"], {"edit": {"headline": {"theme": "comic"}}})
    assert out["edit"]["headline"] == {"on": True, "text": "First", "theme": "comic"}


# ---------- AI writing ----------

def test_ai_options_are_parsed_trimmed_and_capped(monkeypatch):
    seen = {}

    def fake(system, user, schema, effort="high", **kw):
        seen.update(system=system, user=user, effort=effort)
        return schema(options=["  One  ", "", "Two" * 40, "Three", "Four"], kicker="  STREAMER  ")

    monkeypatch.setattr(ai, "_ask", fake)
    out = ai.headline_options({"title": "T", "hook": "H", "start": 0, "end": 5}, [{"start": 0, "end": 4, "text": "words here"}])
    assert out["options"][0] == "One" and len(out["options"]) == 3
    assert all(len(o) <= media.HEADLINE_MAX for o in out["options"]) and out["kicker"] == "STREAMER"
    assert seen["effort"] == "low" and "words here" in seen["user"] and "honest" in seen["system"].lower()


def test_write_route_falls_back_to_hook_and_title_without_ai(monkeypatch):
    c = _clip()
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Add your Anthropic API key")))
    out = server.headline_write(c["id"])
    assert out["ai"] is False and out["options"][:2] == ["He hit the impossible shot", "Clutch"] and out["kicker"] == ""


# ---------- theme pref and new-clip default ----------

def test_saved_theme_and_new_clip_default(monkeypatch):
    c = _clip(vt="gaming")
    server.patch_clip(c["id"], {"edit": {"headline": {"on": True, "text": "x", "theme": "neon", "position": "bottom"},
                                         "headline_color": "#FF0000"}})
    try:
        server.save_headline_theme(c["id"])
        assert store.get("pref", "headline_theme")["theme"]["color"] == "#FF0000"
        assert server.headline_themes()["custom"]["color"] == "#FF0000"
        server.save_headline_default(c["id"], {"on": True})
        n = _clip(hook="A brand new hook")
        assert n["edit"]["headline"]["on"] and n["edit"]["headline"]["text"] == "A brand new hook"
        assert n["edit"]["headline"]["position"] == "bottom" and n["edit"]["headline_color"] == "#FF0000"
        server.save_headline_default(c["id"], {"on": False})
        assert not (_clip()["edit"].get("headline") or {}).get("on")
    finally:
        store.delete("pref", "headline")
        store.delete("pref", "headline_theme")


# ---------- real render ----------

def test_real_render_with_a_headline_is_9_16(tmp_path):
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")
    src = tmp_path / "src.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=10",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    meta = {"width": 640, "height": 360, "fps": 10, "has_audio": False}
    for i, h in enumerate(({"theme": "glass", "push": True, "kicker": "PART 2"}, {"theme": "gradient", "position": "seam"})):
        edit = {"aspect": "9:16", "loudnorm": False, "layout": "split" if i else "single",
                "headline": {"on": True, "text": "He actually pulled it off", **h}}
        e = {**media.DEFAULT_EDIT, **edit}
        spec = media.headline_spec(e)
        ass = media.build_ass([], 0, 2, 1, (1080, 1920), captions=False, split=media.seam(e), headline=spec)
        out = tmp_path / f"out{i}.mp4"
        media.render(src, out, 0.0, 2.0, edit, meta, ass, headline=spec)
        m = media.probe(out)
        assert (m["width"], m["height"]) == (1080, 1920) and m["duration"] > 1.5
    assert re.search(r"\\p1", ass)


# ---------- contrast on bright footage ----------

@pytest.mark.parametrize("tid", list(media.HEADLINE_THEMES))
def test_theme_text_reads_on_bright_footage(tid):
    """Light text needs a mostly opaque bar behind it, or an outline plus a shadow (a shadow alone doesn't
    separate white from a bright frame)."""
    th = media.HEADLINE_THEMES[tid]
    solid_bar = bool(th["bar"]) and float(th["bar_opacity"]) >= 0.5
    assert solid_bar or (float(th["outline"]) >= 2 and float(th["shadow"]) >= 2), tid


@pytest.mark.parametrize("tid", list(media.HEADLINE_THEMES))
def test_kicker_is_a_sensible_size(tid):
    th = media.HEADLINE_THEMES[tid]
    lay = media.headline_layout({**th, "text": "Hi", "kicker": "Part 2", "position": "top"}, 1080, 1920)
    assert lay["kfs"] >= 0.45 * lay["fs"], tid
    if not th["kicker_bg"]:  # bare text (no pill) reads smaller: it needs more size
        assert lay["kfs"] >= 0.6 * lay["fs"], tid


# ---------- font names can't inject ASS ----------

EVIL = "Arial}{\\pos(0,0)\\c&H0000FF&}\nDialogue: 9,0:00:00.00,9:00:00.00,Over,,0,0,0,,X"  # under 80 chars


def test_font_names_are_sanitized_in_validation_and_at_ass_write_time():
    for name in ("headline_font", "caption_font", "title_font", "sticker_font"):
        v = server._text_style("font", EVIL, name)
        assert not set(v) & set("{}\\,\n\r") and len(v) <= 80
    # an older saved edit (or a custom theme) that already holds a bad name still writes clean ASS
    e = {**media.DEFAULT_EDIT, "aspect": "9:16", "headline_font": EVIL, "caption_font": EVIL,
         "headline": {"on": True, "text": "Hello there", "kicker": "Top"}}
    spec = {**media.headline_spec(e), "kicker_font": EVIL}
    ass = media.build_ass(WORDS, 0.0, 10.0, 1.0, V, headline=spec, text_style=e,
                          overlays=[{"kind": "text", "text": "hi", "t": 0, "d": 1, "font": EVIL}])
    assert not any(x.startswith("Dialogue: 9") for x in ass.splitlines())
    assert "\\pos(0,0)\\c&H0000FF&" not in ass
    for line in ass.splitlines():
        if line.startswith("Style: Cap,"):
            assert len(line.split(",")) == 23  # a comma in the name can't shift the style fields
