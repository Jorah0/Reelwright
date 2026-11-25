"""Online media search (GIPHY, Freesound, Pixabay, Pexels: fake HTTP transports, no network), the richer asset library
(checksums, alpha, sounds, trim, defaults, kits) and licence handling (attribution in the description, NC warnings)."""
import io
import json
import os
import subprocess
import tempfile
import zipfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import assets, config, media, media_search, pipeline, platforms, qa, server, store  # noqa: E402

KEYS = {"giphy": "GIPHYKEY-123", "freesound": "FSKEY-456", "pixabay": "PXBKEY-789", "pexels": "PEXKEY-000"}


def need_ffmpeg():
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")


def ff(*args):
    subprocess.run([media.FFMPEG, "-y", "-v", "error", *args], check=True)


@pytest.fixture
def api():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


@pytest.fixture(autouse=True)
def restore_settings():
    """Every setting these tests touch is put back (keys, monetized, GIPHY storage), and the search cache emptied."""
    before = {k: config.load()[k] for k in ("media_keys", "media_monetized", "giphy_storage_ok")}
    media_search.reset()
    yield
    config.save(before)
    media_search.reset()
    media_search.TRANSPORT = None


# ---------- fake providers ----------

GIPHY = {"data": [{"id": "abc", "title": "Cat Dance Sticker", "url": "https://giphy.com/stickers/abc", "username": "catco",
                   "user": {"username": "catco", "display_name": "Cat Co"}, "rating": "g",
                   "images": {"original": {"url": "https://media1.giphy.com/media/abc/giphy.gif", "webp": "https://media1.giphy.com/media/abc/giphy.webp",
                                           "width": "480", "height": "270", "frames": "12"},
                              "fixed_width": {"url": "https://media1.giphy.com/media/abc/200w.gif", "webp": "https://media1.giphy.com/media/abc/200w.webp",
                                              "width": "200", "height": "113"}}}],
         "pagination": {"total_count": 1, "count": 1, "offset": 0}, "meta": {"status": 200}}
FREESOUND = {"count": 2, "results": [
    {"id": 101, "name": "Airhorn", "username": "djbob", "license": "http://creativecommons.org/licenses/by/4.0/", "duration": 1.2,
     "url": "https://freesound.org/people/djbob/sounds/101/", "previews": {"preview-hq-mp3": "https://cdn.freesound.org/previews/0/101_hq.mp3"}},
    {"id": 102, "name": "Crowd", "username": "amy", "license": "Attribution NonCommercial", "duration": 4.0,
     "url": "https://freesound.org/people/amy/sounds/102/", "previews": {"preview-hq-mp3": "https://cdn.freesound.org/previews/0/102_hq.mp3"}}]}
PIXABAY_IMG = {"total": 1, "totalHits": 1, "hits": [{"id": 7, "pageURL": "https://pixabay.com/photos/sunset-7/", "user": "sunny", "tags": "sunset, sea",
                                                     "previewURL": "https://cdn.pixabay.com/photo/7_150.jpg", "webformatURL": "https://pixabay.com/get/7_640.jpg",
                                                     "largeImageURL": "https://pixabay.com/get/7_1280.jpg", "imageWidth": 4000, "imageHeight": 3000}]}
PIXABAY_VID = {"total": 1, "totalHits": 1, "hits": [{"id": 8, "pageURL": "https://pixabay.com/videos/waves-8/", "user": "wave", "tags": "waves",
                                                     "duration": 12, "videos": {"medium": {"url": "https://cdn.pixabay.com/video/8_medium.mp4", "width": 1920, "height": 1080,
                                                                                           "thumbnail": "https://cdn.pixabay.com/video/8_medium.jpg"},
                                                                                "large": {"url": "", "width": 0, "height": 0, "size": 0}}}]}
PEXELS_IMG = {"page": 1, "photos": [{"id": 9, "width": 5000, "height": 3000, "url": "https://www.pexels.com/photo/9/", "photographer": "Ann Lee",
                                     "src": {"original": "https://images.pexels.com/photos/9/o.jpeg", "large2x": "https://images.pexels.com/photos/9/l2.jpeg",
                                             "medium": "https://images.pexels.com/photos/9/m.jpeg"}}]}
PEXELS_VID = {"page": 1, "videos": [{"id": 10, "width": 3840, "height": 2160, "url": "https://www.pexels.com/video/10/", "image": "https://images.pexels.com/videos/10/p.jpg",
                                     "duration": 7, "user": {"name": "Joe Film"},
                                     "video_files": [{"quality": "hls", "file_type": "application/x-mpegURL", "width": None, "height": None, "link": "https://videos.pexels.com/10.m3u8"},
                                                     {"quality": "sd", "file_type": "video/mp4", "width": 640, "height": 360, "link": "https://videos.pexels.com/10_sd.mp4"},
                                                     {"quality": "hd", "file_type": "video/mp4", "width": 1920, "height": 1080, "link": "https://videos.pexels.com/10_hd.mp4"},
                                                     {"quality": "uhd", "file_type": "video/mp4", "width": 3840, "height": 2160, "link": "https://videos.pexels.com/10_4k.mp4"}]}]}


def fake(seen: list, files: dict | None = None):
    """A MockTransport answering each provider's API like its docs, recording every request."""
    def handler(req: httpx.Request):
        seen.append(req)
        u = req.url
        if u.host == "api.giphy.com":
            return httpx.Response(200, json=GIPHY)
        if u.host == "freesound.org" and u.path == "/apiv2/search/":
            return httpx.Response(200, json=FREESOUND)
        if u.host == "pixabay.com" and u.path == "/api/":
            return httpx.Response(200, json=PIXABAY_IMG)
        if u.host == "pixabay.com" and u.path == "/api/videos/":
            return httpx.Response(200, json=PIXABAY_VID)
        if u.host == "api.pexels.com" and u.path == "/v1/search":
            return httpx.Response(200, json=PEXELS_IMG)
        if u.host == "api.pexels.com" and u.path == "/v1/videos/search":
            return httpx.Response(200, json=PEXELS_VID)
        if files and str(u) in files:
            return httpx.Response(200, content=files[str(u)])
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


def connect_all():
    config.save({"media_keys": dict(KEYS)})


# ---------- search: normalization per provider ----------

ITEM_KEYS = {"id", "provider", "kind", "title", "preview_url", "duration", "width", "height", "has_alpha", "licence", "author",
             "source_url", "attribution_text"}


def test_search_normalizes_every_provider():
    connect_all()
    seen = []
    media_search.TRANSPORT = fake(seen)
    st = media_search.search("giphy", "cat", "sticker")
    (g,) = st["items"]
    assert ITEM_KEYS <= set(g) and "download_url" not in g
    assert (g["provider"], g["kind"], g["id"], g["has_alpha"], g["licence"], g["author"]) == ("giphy", "sticker", "abc", True, "giphy", "catco")
    assert g["preview_url"].endswith("200w.webp") and (g["width"], g["height"]) == (480, 270) and "GIPHY" in g["attribution_text"]
    assert st["attribution"] == "Powered by GIPHY"
    req = seen[-1]
    assert req.url.path == "/v1/stickers/search" and req.url.params["api_key"] == KEYS["giphy"] and req.url.params["rating"] == "g"
    assert media_search.search("giphy", "cat", "gif")["items"][0]["has_alpha"] is False and seen[-1].url.path == "/v1/gifs/search"

    fs = media_search.search("freesound", "airhorn")["items"]
    assert [(i["id"], i["kind"], i["licence"], i["duration"]) for i in fs] == [("101", "sound", "cc-by", 1.2), ("102", "sound", "cc-by-nc", 4.0)]
    assert "djbob" in fs[0]["attribution_text"] and "CC BY" in fs[0]["attribution_text"] and fs[0]["preview_url"].endswith("101_hq.mp3")
    assert seen[-1].headers["authorization"] == f"Token {KEYS['freesound']}" and "previews" in seen[-1].url.params["fields"]

    (pi,) = media_search.search("pixabay", "sunset", "image")["items"]
    assert (pi["kind"], pi["author"], pi["licence"], pi["width"], pi["source_url"]) == ("image", "sunny", "pixabay", 4000, "https://pixabay.com/photos/sunset-7/")
    assert seen[-1].url.params["safesearch"] == "true" and seen[-1].url.params["key"] == KEYS["pixabay"]
    (pv,) = media_search.search("pixabay", "waves", "video")["items"]
    assert (pv["kind"], pv["duration"], pv["width"]) == ("video", 12, 1920) and seen[-1].url.path == "/api/videos/"

    (xi,) = media_search.search("pexels", "city", "image")["items"]
    assert (xi["author"], xi["licence"], xi["attribution_text"]) == ("Ann Lee", "pexels", "Photo by Ann Lee on Pexels")
    assert seen[-1].headers["authorization"] == KEYS["pexels"]
    (xv,) = media_search.search("pexels", "city", "video")["items"]
    assert (xv["kind"], xv["width"], xv["height"]) == ("video", 1920, 1080) and seen[-1].url.path == "/v1/videos/search"
    assert media_search._items[("pexels", "10")]["download_url"].endswith("10_hd.mp4")  # the hd mp4: not the 4K, not HLS


def test_tenor_is_not_offered_and_unknown_providers_or_kinds_fail():
    assert "tenor" not in media_search.PROVIDERS
    with pytest.raises(ValueError):
        media_search.search("tenor", "x")
    connect_all()
    with pytest.raises(ValueError):
        media_search.search("freesound", "x", "video")


def test_searches_are_cached_briefly_except_giphy_and_rate_limited():
    connect_all()
    seen = []
    media_search.TRANSPORT = fake(seen)
    media_search.search("pixabay", "sunset", "image")
    media_search.search("pixabay", "sunset", "image")
    assert len(seen) == 1  # Pixabay asks for 24 h caching
    media_search.search("giphy", "cat", "gif")
    media_search.search("giphy", "cat", "gif")
    assert len(seen) == 3  # GIPHY forbids caching its responses
    media_search.LIMITS["pexels"] = (2, 3600)
    media_search.search("pexels", "a", "image")
    media_search.search("pexels", "b", "image")
    with pytest.raises(media_search.RateLimited):
        media_search.search("pexels", "c", "image")


def test_monetized_hides_non_commercial_sounds():
    connect_all()
    media_search.TRANSPORT = fake([])
    config.save({"media_monetized": True})
    out = media_search.search("freesound", "crowd")
    assert [i["id"] for i in out["items"]] == ["101"] and out["hidden_nc"] == 1


# ---------- keys ----------

def test_missing_key_asks_to_connect(api):
    config.save({"media_keys": {}})
    with pytest.raises(media_search.MissingKey):
        media_search.search("pexels", "x", "image")
    r = api.get("/api/media/search", params={"provider": "pexels", "q": "x", "kind": "image"})
    assert r.status_code == 409 and "Connect Pexels" in r.json()["detail"]
    provs = {p["id"]: p for p in api.get("/api/media/providers").json()["providers"]}
    assert set(provs) == {"giphy", "freesound", "pixabay", "pexels"} and not any(p["connected"] for p in provs.values())
    assert all(p["signup_url"].startswith("https://") and p["terms_url"].startswith("https://") for p in provs.values())


def test_keys_are_sealed_and_never_sent_to_the_browser(api):
    for p, k in KEYS.items():
        assert api.put("/api/media/keys", json={"provider": p, "key": k}).status_code == 200
    assert config.load()["media_keys"] == KEYS  # read back opened
    raw = config.SETTINGS_FILE.read_text("utf-8")
    from reelwright import vault
    if vault.on():
        assert not any(k in raw for k in KEYS.values())  # sealed with DPAPI at rest
    out = json.dumps([api.get("/api/settings").json(), api.get("/api/media/providers").json()])
    assert not any(k in out for k in KEYS.values())
    assert all(p["connected"] for p in api.get("/api/media/providers").json()["providers"])
    assert "media_keys" in config.SEALED
    assert api.put("/api/media/keys", json={"provider": "pexels", "key": ""}).status_code == 200  # disconnect one
    assert config.load()["media_keys"]["pexels"] == "" and config.load()["media_keys"]["giphy"] == KEYS["giphy"]
    assert api.put("/api/media/keys", json={"provider": "tenor", "key": "x"}).status_code == 400


# ---------- import: download, metadata, dedupe ----------

def _png_bytes(rgba=(255, 0, 0, 255), size=(40, 30)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGBA", size, rgba).save(b, "PNG")
    return b.getvalue()


def test_import_saves_metadata_and_dedupes_by_checksum(api):
    need_ffmpeg()
    connect_all()
    img = _png_bytes()
    media_search.TRANSPORT = fake([], {"https://images.pexels.com/photos/9/l2.jpeg": img})
    media_search.search("pexels", "city", "image")
    r = api.post("/api/media/import", json={"provider": "pexels", "id": "9"})
    assert r.status_code == 200, r.text
    a = r.json()
    src = a["source"]
    assert (a["kind"], src["provider"], src["licence"], src["author"], src["source_url"]) == ("image", "pexels", "pexels", "Ann Lee", "https://www.pexels.com/photo/9/")
    assert src["attribution_text"] == "Photo by Ann Lee on Pexels" and src["downloaded_at"] and len(a["checksum"]) == 64
    assert a["thumb"] and (assets.ASSETS / a["thumb"]).exists()
    again = api.post("/api/media/import", json={"provider": "pexels", "id": "9"}).json()
    assert again["id"] == a["id"] and again["duplicate"] and len(store.find("asset", checksum=a["checksum"])) == 1
    assert api.post("/api/media/import", json={"provider": "pexels", "id": "never-searched"}).status_code == 404


def test_download_refuses_hosts_outside_the_provider():
    connect_all()
    item = {"provider": "pexels", "id": "1", "kind": "image", "download_url": "https://evil.example.com/x.jpg"}
    with pytest.raises(ValueError):
        media_search.download(item)
    with pytest.raises(ValueError):
        media_search.download({**item, "download_url": "http://images.pexels.com/x.jpg"})  # https only


def test_giphy_downloads_need_storage_permission():
    connect_all()
    media_search.TRANSPORT = fake([])
    media_search.search("giphy", "cat", "sticker")
    config.save({"giphy_storage_ok": False})
    with pytest.raises(PermissionError):
        media_search.download(media_search._items[("giphy", "abc")])


# ---------- uploads: validation, alpha, sounds, thumbnails ----------

def test_bulk_upload_validation(api, tmp_path):
    need_ffmpeg()
    assert api.post("/api/assets", files={"file": ("virus.exe", b"MZ", "application/octet-stream")}).status_code == 400
    assert api.post("/api/assets", files={"file": ("fake.png", b"not an image", "image/png")}).status_code == 400
    old = dict(assets.CAPS)
    try:
        assets.CAPS["image"] = 100
        r = api.post("/api/assets", files={"file": ("big.png", _png_bytes(size=(200, 200)), "image/png")})
        assert r.status_code == 400 and "too big" in r.json()["detail"]
    finally:
        assets.CAPS.update(old)
    ok = api.post("/api/assets", files={"file": ("sub/dir/clear.png", _png_bytes((0, 255, 0, 0)), "image/png")})  # a folder upload's path
    assert ok.status_code == 200 and ok.json()["has_alpha"] and ok.json()["name"] == "clear"


def test_sound_upload_is_converted_with_waveform_and_optional_loudnorm(api, tmp_path):
    need_ffmpeg()
    mp3 = tmp_path / "hit.mp3"
    ff("-f", "lavfi", "-i", "sine=f=440:d=1.5", "-af", "volume=0.05", str(mp3))
    r = api.post("/api/assets", files={"file": ("hit.mp3", mp3.read_bytes(), "audio/mpeg")}, data={"loudnorm": "true"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["kind"] == "sound" and a["file"].endswith(".wav") and abs(a["duration"] - 1.5) < 0.15
    assert a["waveform"] and (assets.ASSETS / a["waveform"]).exists()
    loud = media._run([media.FFMPEG, "-i", str(assets.path(a)), "-af", "volumedetect", "-f", "null", "-"]).stderr
    assert float(loud.split("mean_volume:")[1].split("dB")[0]) > -30  # normalised up from the quiet original


def _sticker_webm(path):
    """A 64x64 VP9 WebM with alpha: an opaque red square in the middle of a transparent frame."""
    ff("-f", "lavfi", "-i", "color=c=red:s=64x64:d=1:r=10,format=rgba,geq=r=255:g=0:b=0:a='255*between(X,16,47)*between(Y,16,47)'",
       "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(path))


def test_sticker_alpha_survives_import_and_a_real_overlay_render(tmp_path):
    need_ffmpeg()
    if "libvpx-vp9" not in media._run([media.FFMPEG, "-hide_banner", "-encoders"]).stdout:
        pytest.skip("no libvpx-vp9")
    import cv2
    _sticker_webm(tmp_path / "s.webm")
    a = assets.add(tmp_path / "s.webm", "sticker.webm")
    assert a["has_alpha"] and a["kind"] == "gif" and a["file"].endswith(".apng")  # alpha kept in a format the overlay reads
    src = tmp_path / "bg.mp4"
    ff("-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2:r=25", "-pix_fmt", "yuv420p", str(src))
    out = tmp_path / "out.mp4"
    o = assets.placed({"kind": "gif", "asset_id": a["id"], "t": 0.0, "d": 2.0, "x": 0.5, "y": 0.5, "w": 0.5, "anim": "none", "layer": 0}, a)
    media.render(src, out, 0.0, 2.0, {"aspect": "9:16", "speed": 1.0}, {"width": 320, "height": 180, "fps": 25, "has_audio": False},
                 None, overlays=[o])
    cap = cv2.VideoCapture(str(out))
    cap.set(cv2.CAP_PROP_POS_MSEC, 1000)
    _, f = cap.read()
    b, g, r = (int(v) for v in f[960, 540])
    assert r > 150 and b < 100  # the sticker's opaque middle
    b, g, r = (int(v) for v in f[960, 300])  # inside the sticker's box, in its transparent border: the blue shows through
    assert b > 150 and r < 100


# ---------- trim, defaults, favourites ----------

def _project():
    p = pipeline.new_project("media")
    store.update("project", p["id"], {"meta": {"width": 1920, "height": 1080, "duration": 60, "fps": 30, "has_audio": True},
                                      "status": "ready", "source": "source.mp4", "source_info": {"url": "https://www.twitch.tv/videos/1"}})
    return p


def _asset(kind="sound", duration=4.0, **extra):
    return store.put("asset", {"kind": kind, "name": "A", "file": "x.wav", "duration": duration, "width": 0, "height": 0,
                               "builtin": False, "tags": [], **extra})


def test_trim_defaults_and_favourite_are_validated_and_used(api):
    a = _asset()
    r = api.patch(f"/api/assets/{a['id']}", json={"trim": {"in": 1.0, "out": 2.5}, "fav": True, "tags": ["hype", " x "],
                                                    "defaults": {"volume": 0.5, "x": 0.2, "y": 0.8, "w": 0.3, "d": 1.0}})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["trim"] == {"in": 1.0, "out": 2.5} and b["fav"] and b["tags"] == ["hype", "x"]
    for bad in ({"trim": {"in": 3, "out": 2}}, {"trim": {"in": 0, "out": 9}}, {"defaults": {"volume": 9}}, {"defaults": {"spin": 1}},
                {"name": "x" * 200}):
        assert api.patch(f"/api/assets/{a['id']}", json=bad).status_code == 400, bad
    clip = pipeline.new_clip(_project()["id"], {"start": 10.0, "end": 20.0})
    o = server.clean_edit({"overlays": [{"kind": "sound", "asset_id": a["id"], "t": 1}]}, clip)["overlays"][0]
    assert (o["d"], o["volume"], o["x"], o["y"], o["w"]) == (1.0, 0.5, 0.2, 0.8, 0.3)  # the asset's defaults
    api.patch(f"/api/assets/{a['id']}", json={"defaults": {}})
    o = server.clean_edit({"overlays": [{"kind": "sound", "asset_id": a["id"], "t": 1}]}, clip)["overlays"][0]
    assert o["d"] == 1.5  # no default length: the trimmed length
    p = assets.placed({**o, "d": 3.0}, store.get("asset", a["id"]))
    assert p["trim_in"] == 1.0 and p["d"] == 1.5  # never plays past the out point


def test_trim_in_is_applied_in_the_render(tmp_path):
    need_ffmpeg()
    import cv2
    clipf = tmp_path / "rg.mp4"  # red for 2 s, then green
    ff("-f", "lavfi", "-i", "color=c=black:s=64x64:d=4:r=25,format=rgb24,geq=r='255*lt(T,2)':g='255*gte(T,2)':b=0",
       "-pix_fmt", "yuv420p", str(clipf))
    a = assets.add(clipf, "rg.mp4")
    a = assets.patch(a["id"], {"trim": {"in": 2.5, "out": 3.8}})
    src = tmp_path / "bg.mp4"
    ff("-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2:r=25", "-pix_fmt", "yuv420p", str(src))
    o = assets.placed({"kind": "video", "asset_id": a["id"], "t": 0.0, "d": 2.0, "x": 0.5, "y": 0.5, "w": 1, "cutaway": True,
                       "anim": "none", "layer": 0, "volume": 1}, a)
    out = tmp_path / "out.mp4"
    media.render(src, out, 0.0, 2.0, {"aspect": "9:16", "speed": 1.0}, {"width": 320, "height": 180, "fps": 25, "has_audio": False},
                 None, overlays=[o])
    cap = cv2.VideoCapture(str(out))
    cap.set(cv2.CAP_PROP_POS_MSEC, 500)
    _, f = cap.read()
    b, g, r = (int(v) for v in f[960, 540])
    assert g > 150 and r < 100  # starts from the in point (green), not the file's start (red)


# ---------- kits ----------

def test_kit_export_import_round_trip(api, tmp_path):
    need_ffmpeg()
    up = [api.post("/api/assets", files={"file": (f"k{i}.png", _png_bytes((i * 60, 0, 0, 255)), "image/png")}, data={"tags": "kit"}).json()
          for i in range(1, 3)]
    assets.patch(up[0]["id"], {"defaults": {"w": 0.25}, "fav": True})
    kit = api.post("/api/media/kits", json={"name": "My hype kit", "asset_ids": [a["id"] for a in up]}).json()
    assert kit["name"] == "My hype kit" and kit["asset_ids"] == [a["id"] for a in up]
    z = api.get(f"/api/media/kits/{kit['id']}/export")
    assert z.status_code == 200 and "attachment" in z.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert "manifest.json" in names and len(names) == 3
    for a in up:  # import on a "fresh" library: the files come back with their settings
        assets.remove(a["id"])
    r = api.post("/api/media/kits/import", files={"file": ("kit.zip", z.content, "application/zip")})
    assert r.status_code == 200, r.text
    k2 = r.json()
    got = [store.get("asset", i) for i in k2["asset_ids"]]
    assert k2["name"] == "My hype kit" and len(got) == 2 and got[0]["defaults"] == {"w": 0.25} and got[0]["fav"]
    assert {a["checksum"] for a in got} == {a["checksum"] for a in up}
    assert k2["name"] in assets.kit_names().get(got[0]["id"], [])


def _zip(entries: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, data in entries.items():
            z.writestr(n, data)
    return b.getvalue()


def test_kit_import_rejects_traversal_bad_types_and_oversize(api):
    man = lambda f: json.dumps({"version": 1, "name": "Bad", "assets": [{"file": f, "name": "x"}]})  # noqa: E731
    png = _png_bytes()
    for zbytes in (_zip({"manifest.json": man("../evil.png"), "../evil.png": png}),
                   _zip({"manifest.json": man("files/a.png"), "files/a.png": png, "C:/win.png": png}),
                   _zip({"manifest.json": man("files/run.exe"), "files/run.exe": b"MZ"}),
                   _zip({"manifest.json": man("files/missing.png")}),
                   _zip({"files/a.png": png}),
                   b"not a zip"):
        r = api.post("/api/media/kits/import", files={"file": ("kit.zip", zbytes, "application/zip")})
        assert r.status_code == 400, r.text
    old = dict(assets.CAPS)
    try:
        assets.CAPS["image"] = 50
        r = api.post("/api/media/kits/import", files={"file": ("kit.zip", _zip({"manifest.json": man("files/a.png"), "files/a.png": png}), "x")})
        assert r.status_code == 400 and "too big" in r.json()["detail"]
    finally:
        assets.CAPS.update(old)
    assert not store.find("kit", name="Bad")


# ---------- licences: attribution in the description, QA ----------

def _sourced(licence, attribution="Airhorn by djbob (CC BY 4.0)"):
    return _asset(source={"provider": "freesound", "licence": licence, "author": "djbob", "attribution_text": attribution,
                          "source_url": "https://freesound.org/s/101/", "downloaded_at": "2026-11-24T10:00:00"})


def test_cc_by_attribution_reaches_the_description():
    from reelwright import accounts
    by, cc0 = _sourced("cc-by"), _sourced("cc0", "Free thing")
    clip = pipeline.new_clip(_project()["id"], {"start": 10.0, "end": 20.0})
    server.patch_clip(clip["id"], {"edit": {"overlays": [{"kind": "sound", "asset_id": by["id"], "t": 1},
                                                         {"kind": "sound", "asset_id": cc0["id"], "t": 2}]}})
    clip = store.get("clip", clip["id"])
    tv = platforms.template_vars(clip, store.get("project", clip["project_id"]), [])
    assert tv["attributions"] == "Credits: Airhorn by djbob (CC BY 4.0)"  # CC0 needs none
    assert platforms.render_template("Full stream: {source_url}", {**tv, "description": "D"}).endswith("Credits: Airhorn by djbob (CC BY 4.0)")
    assert platforms.render_template("{description}\n{attributions}\nbye", {**tv, "description": "D"}).count("Credits:") == 1
    yt = accounts.add("youtube", name="Y", token={"token": "t"}, approved=True)
    up = pipeline.request_upload(clip["id"], {"account_id": yt["id"], "description": "My desc"})
    assert "Credits: Airhorn by djbob (CC BY 4.0)" in up["description"]
    store.delete("upload", up["id"])
    accounts.remove(yt["id"]) if hasattr(accounts, "remove") else None


def test_licence_qa_warns_on_nc_when_monetized_and_missing_attribution():
    nc, by = _sourced("cc-by-nc"), _sourced("cc-by", "")
    clip = pipeline.new_clip(_project()["id"], {"start": 10.0, "end": 20.0})
    clip["edit"]["overlays"] = [{"kind": "sound", "asset_id": nc["id"], "t": 1, "d": 1}, {"kind": "sound", "asset_id": by["id"], "t": 2, "d": 1}]
    config.save({"media_monetized": False})
    codes = {i["code"]: i for i in assets.licence_issues(clip)}
    assert "licence_nc" not in codes and codes["licences"]["level"] == "info" and "CC BY-NC" in codes["licences"]["msg"]
    assert codes["attribution_missing"]["level"] == "warn"
    config.save({"media_monetized": True})
    codes = {i["code"]: i for i in assets.licence_issues(clip)}
    assert codes["licence_nc"]["level"] == "warn"
    assert assets.licence_issues({"edit": {"overlays": []}}) == []
    assert "licence_issues" in open(qa.__file__, encoding="utf-8").read()  # lint includes them


# ---------- AI plan sees kits; privacy ----------

def test_ai_plan_context_lists_kits_and_favourites():
    a = _asset(name="Vine boom", fav=True)
    store.put("kit", {"name": "VTuber reactions", "asset_ids": [a["id"]]})
    clip = pipeline.new_clip(_project()["id"], {"start": 10.0, "end": 20.0})
    ctx = server._plan_context(clip, store.get("project", clip["project_id"]), assets.listing())
    line = next(x for x in ctx.splitlines() if x.startswith(a["id"]))
    assert "VTuber reactions" in line and "★" in line


def test_privacy_policy_names_every_media_provider_and_its_terms():
    from pathlib import Path
    policy = (Path(media.__file__).parent / "static" / "legal" / "privacy.html").read_text("utf-8").lower()
    for p in media_search.PROVIDERS.values():
        for h in p["hosts"]:
            assert h in policy, h
        assert p["terms_url"].lower() in policy
