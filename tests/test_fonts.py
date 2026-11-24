"""Fonts (system + uploaded) and per-text styling for captions, title, sticker and text overlays."""
import glob
import os
import shutil
import subprocess
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import assets, media, server  # noqa: E402


def _a_ttf():
    files = sorted(glob.glob(r"C:\Windows\Fonts\*.ttf"))
    if not files:
        pytest.skip("no system TrueType font to copy")
    return files[0]


@pytest.fixture
def api():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


def test_fonts_list_system_and_uploaded(api, tmp_path):
    src = _a_ttf()
    r = api.post("/api/assets", files={"file": ("Brand.ttf", open(src, "rb").read(), "font/ttf")}, data={"name": "Brand"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["kind"] == "font" and a["file"].startswith("fonts/") and (assets.ASSETS / a["file"]).exists()
    fonts = api.get("/api/fonts").json()
    assert any(f["source"] == "system" and f["family"] for f in fonts)
    up = next(f for f in fonts if f["source"] == "uploaded" and f["file"] == a["file"].split("/")[-1])
    assert up["family"] and up["url"] == f"/assets/{a['file']}" and api.get(up["url"]).status_code == 200
    bad = api.post("/api/assets", files={"file": ("x.ttf", b"not a font", "font/ttf")})
    assert bad.status_code == 400


def test_text_style_keys_validate():
    clip = {"start": 0.0, "end": 10.0, "edit": {}}
    ok = server.clean_edit({"caption_font": "Arial", "caption_size": 500, "caption_weight": "bold", "caption_color": "#ff0000",
                            "title_outline": 25, "sticker_spacing": -9, "title_shadow": None}, clip)
    assert ok == {"caption_font": "Arial", "caption_size": 200.0, "caption_weight": "bold", "caption_color": "#FF0000",
                  "title_outline": 20.0, "sticker_spacing": -5.0, "title_shadow": None}
    o = server.clean_edit({"overlays": [{"kind": "text", "text": "HI", "t": 1, "font": "Arial", "size": 90, "color": "#00ff00",
                                         "outline": 4, "outline_color": "#000000", "shadow": 2, "spacing": 1, "weight": "normal"}]},
                          clip)["overlays"][0]
    assert (o["font"], o["size"], o["color"], o["weight"]) == ("Arial", 90.0, "#00FF00", "normal")
    for bad in ({"caption_weight": "heavy"}, {"caption_color": "red"}, {"title_size": "big"}, {"caption_font": "x" * 81},
                {"overlays": [{"kind": "text", "text": "HI", "t": 1, "color": "blue"}]}):
        with pytest.raises(HTTPException):
            server.clean_edit(bad, clip)


def test_ass_applies_styles_and_defaults_are_unchanged():
    words = [{"s": 0.2, "e": 0.6, "w": "hello"}]
    plain = media.build_ass(words, 0, 2, 1.0, (1080, 1920), title="T")
    assert plain == media.build_ass(words, 0, 2, 1.0, (1080, 1920), title="T", text_style={})  # defaults keep today's look
    styled = media.build_ass(words, 0, 2, 1.0, (1080, 1920), title="T", text_style={
        "caption_font": "Georgia", "caption_size": 100, "caption_weight": "normal", "caption_color": "#FF0000",
        "caption_outline": 6, "caption_outline_color": "#0000FF", "caption_shadow": 1, "caption_spacing": 2, "title_font": "Impact"})
    cap = next(line for line in styled.splitlines() if line.startswith("Style: Cap,")).split(",")
    assert cap[1:4] == ["Georgia", "100", "&H000000FF&"] and cap[5] == "&H00FF0000&" and cap[7] == "0"
    assert (cap[13], cap[16], cap[17]) == ("2", "6", "1")
    assert next(line for line in styled.splitlines() if line.startswith("Style: Title,")).split(",")[1] == "Impact"
    over = media.build_ass([], 0, 2, 1.0, (1080, 1920), overlays=[
        {"kind": "text", "text": "YO", "t": 0, "d": 1, "font": "Impact", "size": 80, "color": "#00FF00", "weight": "bold",
         "outline": 3, "outline_color": "#000000", "shadow": 0, "spacing": 4}])
    line = next(line for line in over.splitlines() if ",Over," in line)
    for tag in ("\\fnImpact", "\\fs80", "\\c&H0000FF00&", "\\b1", "\\bord3", "\\3c&H00000000&", "\\shad0", "\\fsp4"):
        assert tag in line, tag


def test_missing_font_falls_back_and_uploaded_font_renders(tmp_path):
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")
    src = tmp_path / "s.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=2:r=25", "-pix_fmt", "yuv420p",
                    str(src)], check=True)
    font_file = tmp_path / "Brand.ttf"
    shutil.copy(_a_ttf(), font_file)
    a = assets.add(font_file, "Brand.ttf", "Brand")
    fam = a["family"]
    style = assets.resolve_fonts({"caption_font": "No Such Font 123", "title_font": fam,
                                  "overlays": [{"kind": "text", "font": "Nope 9", "text": "x"}]})
    assert style["caption_font"] is None and style["title_font"] == fam and style["overlays"][0]["font"] is None
    ass = media.build_ass([], 0, 2, 1.0, (1080, 1920), title="Hello", text_style=style)
    assert f"Style: Title,{fam}," in ass
    out = tmp_path / "o.mp4"
    media.render(src, out, 0, 2, {"aspect": "9:16", "loudnorm": False}, {"width": 320, "height": 180, "fps": 25, "has_audio": False},
                 ass, fonts=[assets.path(a)])
    assert media.probe(out)["width"] == 1080
    g, _ = media.build_filters({"aspect": "9:16"}, 320, 180, 2, True, fontsdir=True)
    assert "subtitles=captions.ass:fontsdir=fonts" in g
