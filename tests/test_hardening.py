"""Process and resource hardening: ffmpeg dies with the app (Windows job object + a Popen registry), failed renders leave
no temp files (and stale ones are swept at start-up), asset imports are capped and fail cleanly, model downloads are
pinned and size-capped, media-search redirects are checked hop by hop, ffmpeg inputs are local-file only."""
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import zipfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import assets, config, longform, media, media_search, pipeline, server, store, track  # noqa: E402


def need_ffmpeg():
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")


def ff(*args):
    subprocess.run([media.FFMPEG, "-y", "-v", "error", *args], check=True)


@pytest.fixture
def api():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


# ---------- H1: children die with the app ----------

def test_popen_registry_kills_live_children():
    p = media.popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert p.poll() is None
    media.kill_children()
    assert p.wait(10) is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_job_object_kills_grandchildren_when_the_app_exits(tmp_path):
    pidf = tmp_path / "pid"
    code = textwrap.dedent(f"""
        import subprocess, sys
        from reelwright import desktop
        assert desktop.kill_children_on_exit()
        c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pidf)!r}, "w").write(str(c.pid))
    """)
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60, cwd=str(config.ROOT))
    pid = int(pidf.read_text())
    import ctypes
    k = ctypes.windll.kernel32
    deadline = time.time() + 10
    alive = True
    while time.time() < deadline and alive:
        h = k.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        alive = bool(h) and k.WaitForSingleObject(h, 0) != 0
        if h:
            k.CloseHandle(h)
        time.sleep(0.2)
    assert not alive, "the grandchild outlived the app process"


# ---------- M1: no temp files left behind ----------

def test_failed_encode_leaves_no_rendering_file(tmp_path, monkeypatch):
    need_ffmpeg()
    out = tmp_path / "clip.mp4"
    tmp = tmp_path / "clip.rendering.mp4"
    tmp.write_bytes(b"partial")  # what a crashed attempt would leave
    monkeypatch.setattr(media, "encoders", lambda: [("bad", ["-c:v", "no_such_encoder_xyz"])])
    with pytest.raises(RuntimeError):
        media._encode([media.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64"], out, tmp_path, 1.0, None)
    assert not tmp.exists() and not out.exists()


def test_encode_interrupted_by_progress_kills_ffmpeg_and_removes_tmp(tmp_path):
    need_ffmpeg()
    out = tmp_path / "clip.mp4"

    def boom(_):
        raise KeyboardInterrupt  # e.g. the app shutting down mid-render

    with pytest.raises(KeyboardInterrupt):
        media._encode([media.FFMPEG, "-y", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc=d=30:s=64x64"],
                      out, tmp_path, 30.0, boom)
    assert not (tmp_path / "clip.rendering.mp4").exists() and not out.exists()
    assert not [p for p in media._live if p.poll() is None]


def test_join_failure_removes_tmp(tmp_path, monkeypatch):
    out = tmp_path / "lf.mp4"
    tmp = tmp_path / "lf.rendering.mp4"

    def fail(*a, **k):
        tmp.write_bytes(b"half")
        return subprocess.CompletedProcess(a, 1, "", "boom")

    monkeypatch.setattr(longform.subprocess, "run", fail)
    with pytest.raises(RuntimeError):
        longform._join([tmp_path / "part_0000.mp4"], out, {"loudnorm": False}, None, True, 1.0)
    assert not tmp.exists()


def test_startup_sweeps_stale_temp_files_only(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    exp = root / "p1" / "exports"
    exp.mkdir(parents=True)
    adir = tmp_path / "assets"
    adir.mkdir()
    monkeypatch.setattr(config, "projects_dir", lambda: root)
    monkeypatch.setattr(assets, "ASSETS", adir)
    old = time.time() - 7200
    stale = [exp / "c1.rendering.mp4", root / "p1" / "proxy.rendering.mp4", exp / ".work_c1", exp / ".lf_l1",
             adir / ".dl-x.mp4", adir / ".kit-y.png", adir / ".upload-z.wav"]
    for p in stale:
        if p.suffix:
            p.write_bytes(b"x")
        else:
            p.mkdir()
            (p / "inner.txt").write_text("x")
        os.utime(p, (old, old))
    fresh = exp / "c2.rendering.mp4"
    fresh.write_bytes(b"x")
    keep = exp / "c1.mp4"
    keep.write_bytes(b"x")
    os.utime(keep, (old, old))
    pipeline.sweep_temp()
    assert [p for p in stale if p.exists()] == []
    assert fresh.exists() and keep.exists()


# ---------- M4: asset duration cap, kit members that fail to read ----------

def test_long_sounds_are_refused_before_converting(tmp_path, monkeypatch):
    need_ffmpeg()
    src = tmp_path / "long.mp3"
    ff("-f", "lavfi", "-i", "sine=d=3", str(src))
    monkeypatch.setattr(assets, "MAX_MEDIA_S", 2)
    with pytest.raises(ValueError, match="long"):
        assets.add(src, "long.mp3")


def _zip_stored(entries: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_STORED) as z:
        for n, data in entries.items():
            z.writestr(n, data)
    return b.getvalue()


def test_kit_with_a_corrupt_member_is_a_400_and_takes_nothing_in(api):
    need_ffmpeg()
    from PIL import Image
    pngs = []
    for c in ((1, 2, 3, 255), (4, 5, 6, 255)):
        b = io.BytesIO()
        Image.new("RGBA", (33, 21), c).save(b, "PNG")
        pngs.append(b.getvalue())
    man = json.dumps({"version": 1, "name": "Corrupt kit", "assets": [{"file": "files/a.png"}, {"file": "files/b.png"}]})
    z = _zip_stored({"manifest.json": man, "files/a.png": pngs[0], "files/b.png": pngs[1]})
    i = z.index(pngs[1]) + len(pngs[1]) // 2
    z = z[:i] + bytes([z[i] ^ 0xFF]) + z[i + 1:]  # b.png fails its CRC
    before = len(store.find("asset"))
    r = api.post("/api/media/kits/import", files={"file": ("kit.zip", z, "application/zip")})
    assert r.status_code == 400, r.text
    assert len(store.find("asset")) == before and not store.find("kit", name="Corrupt kit")


# ---------- M5: pinned, size-capped model downloads ----------

def test_model_urls_are_pinned_to_a_commit_with_a_sha256():
    for url, sha in ((media.YUNET_URL, media.YUNET_SHA256), (track.ANIME_URL, track.ANIME_SHA256)):
        assert "/main/" not in url and "/master/" not in url and len(sha) == 64


def test_fetch_model_refuses_an_oversized_download(monkeypatch):
    import urllib.request

    class Big(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    monkeypatch.setattr(media, "MODEL_MAX", 1000)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Big(b"x" * 5000))
    with pytest.raises(RuntimeError, match="too big"):
        media.fetch_model("hardening-test-big.bin", "https://example.com/x")
    assert not (config.DATA / "models" / "hardening-test-big.bin").exists()


# ---------- L1/L2: media search redirects and preview hosts ----------

def _redirector(to: str, body: bytes = b"\x89PNG"):
    def handler(req: httpx.Request):
        if req.url.host == "images.pexels.com" and req.url.path == "/start.jpeg":
            return httpx.Response(302, headers={"Location": to})
        if str(req.url) == to:
            return httpx.Response(200, content=body)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture
def no_transport():
    yield
    media_search.TRANSPORT = None


def test_download_refuses_a_redirect_off_the_provider(no_transport):
    item = {"provider": "pexels", "id": "1", "kind": "image", "download_url": "https://images.pexels.com/start.jpeg"}
    media_search.TRANSPORT = _redirector("https://evil.example.com/x.jpeg")
    with pytest.raises(ValueError, match="redirected"):
        media_search.download(item)
    media_search.TRANSPORT = _redirector("http://images.pexels.com/x.jpeg")  # https only, on every hop
    with pytest.raises(ValueError):
        media_search.download(item)
    media_search.TRANSPORT = _redirector("https://videos.pexels.com/x.jpeg")  # another of its own hosts: fine
    tmp = media_search.download(item)
    try:
        assert tmp.read_bytes() == b"\x89PNG"
    finally:
        tmp.unlink(missing_ok=True)


def test_preview_urls_off_the_provider_are_dropped():
    keep = media_search._item("pexels", "image", 1, "t", "https://images.pexels.com/p.jpg", licence="pexels")
    drop = media_search._item("pexels", "image", 1, "t", "https://evil.example.com/p.jpg", licence="pexels")
    assert keep["preview_url"] == "https://images.pexels.com/p.jpg" and drop["preview_url"] == ""


# ---------- L1 (resources): bounded search memory ----------

def test_search_memory_is_bounded(monkeypatch):
    media_search.reset()
    monkeypatch.setattr(media_search, "ITEMS_MAX", 5)
    for i in range(12):
        media_search._remember("pexels", [{"id": str(i)}])
    assert len(media_search._items) == 5 and ("pexels", "11") in media_search._items and ("pexels", "0") not in media_search._items
    media_search._cache[("pexels", "image", "old", 1, True)] = (time.monotonic() - 10 * 24 * 3600, [])
    media_search._cache_put(("pexels", "image", "new", 1, True), [])
    assert list(media_search._cache) == [("pexels", "image", "new", 1, True)]
    media_search.reset()


# ---------- L12: ffmpeg reads local files only ----------

def test_inputs_get_a_local_only_protocol_whitelist():
    args = media.local_inputs([media.FFMPEG, "-ss", "1", "-i", "a.mp4", "-f", "lavfi", "-i", "anullsrc", "-i", "b.wav", "out.mp4"])
    assert args == [media.FFMPEG, "-ss", "1", "-protocol_whitelist", "file,pipe", "-i", "a.mp4", "-f", "lavfi", "-i", "anullsrc",
                    "-protocol_whitelist", "file,pipe", "-i", "b.wav", "out.mp4"]
    assert media.local_inputs(args) == args  # idempotent


def test_a_playlist_disguised_as_a_video_is_refused(tmp_path):
    need_ffmpeg()
    seg = tmp_path / "seg.ts"
    ff("-f", "lavfi", "-i", "testsrc=d=1:s=64x64", str(seg))
    fake = tmp_path / "clip.mp4"
    fake.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{seg.name}\n#EXT-X-ENDLIST\n")
    with pytest.raises(ValueError):
        assets.add(fake, "clip.mp4")


def test_stale_upload_leftovers_in_the_storage_folder_are_swept(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(config, "projects_dir", lambda: root)
    monkeypatch.setattr(assets, "ASSETS", tmp_path / "assets")
    old, fresh = root / ".incoming-abc.mp4", root / ".incoming-def.mp4"  # server._receive's names (a hard kill mid-upload)
    for p in (old, fresh):
        p.write_bytes(b"x")
    os.utime(old, (time.time() - 7200,) * 2)
    pipeline.sweep_temp()
    assert not old.exists() and fresh.exists()


def test_every_ffmpeg_reading_user_media_is_local_only(tmp_path, monkeypatch):
    from reelwright import covers, hud, multicam, qa
    seen = []

    def run(args, **k):
        seen.append(list(args))
        if "-f" in args and args[args.index("-f") + 1] == "concat":
            (tmp_path / "lf.rendering.mp4").write_bytes(b"x")
        return subprocess.CompletedProcess(args, 0, b"" if not k.get("text") else "", b"" if not k.get("text") else "")
    monkeypatch.setattr(subprocess, "run", run)
    multicam._motion(tmp_path / "a.mp4", 1.0, 0.0, 2.0, 0.5)
    with pytest.raises(RuntimeError):
        hud.grab(tmp_path / "a.mp4", 1.0)
    monkeypatch.setattr(covers, "pick", lambda *a, **k: 1.0)
    covers.make(tmp_path / "a.mp4", tmp_path / "c.jpg")
    qa.measure(tmp_path / "a.mp4", True)
    longform._join([tmp_path / "part_0000.mp4"], tmp_path / "lf.mp4", {"loudnorm": False, "music_volume": 0.3, "fade_in": 0,
                   "fade_out": 0, "music_duck": True}, tmp_path / "bed.mp3", True, 1.0)
    assert len(seen) == 5
    for args in seen:
        assert args.count("-i") == args.count("-protocol_whitelist") and all(
            args[i - 2:i] == media.LOCAL_ONLY for i, a in enumerate(args) if a == "-i"), args
