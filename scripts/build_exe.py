"""Build dist/Reelwright/Reelwright.exe (a self-contained Windows app folder) with PyInstaller.

    python scripts/build_exe.py            # GPU build (bundles NVIDIA cuBLAS/cuDNN for fast transcription)
    python scripts/build_exe.py --cpu-only # smaller build; transcription runs on the CPU
    python scripts/build_exe.py --lgpl     # for distribution: bundles a pinned LGPL ffmpeg instead of the one on PATH

ffmpeg: by default the build copies the ffmpeg on PATH (for a personal build, e.g. gyan.dev's GPL "full" build with
libx264 and NVENC). A GPL ffmpeg must not ship with a closed app, so a build for others uses --lgpl: the FFmpeg-Builds
(formerly BtbN) win64 LGPL build below, downloaded once into build/ffmpeg-lgpl/ (gitignored) and checked against its
sha256. It has NVENC/QSV/AMF/Media Foundation and OpenH264 but no libx264 (media.ENCODERS falls back to OpenH264) and no
boxblur/eq filters (media._blur/_eq substitute gblur/lutyuv). To move the pin, take a month-end autobuild (those are kept)
from https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases and copy its URL and the sha256 GitHub shows for the asset.
"""
import hashlib
import shutil
import site
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEP = ";" if sys.platform == "win32" else ":"
LGPL_URL = ("https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/"
            "ffmpeg-n8.1.3-27-g8dca1586f4-win64-lgpl-8.1.zip")  # FFmpeg 8.1.3, release branch, built 2026-10-31
LGPL_SHA256 = "d4c2a42673917b4a97104a3e9f082729a9730796b98664e37c0b7eba6c6c613d"
LGPL_CACHE = ROOT / "build" / "ffmpeg-lgpl"
TOOLS = ("ffmpeg.exe", "ffprobe.exe", "LICENSE.txt")


def fetch_lgpl(cache: Path = LGPL_CACHE, url: str = LGPL_URL, sha: str = LGPL_SHA256) -> list[Path]:
    """The pinned LGPL ffmpeg.exe, ffprobe.exe and its LICENSE.txt, downloaded once (cached by checksum), verified, and
    pulled out of the zip by name only (no paths from the archive are used)."""
    key = sha[:16]
    folder, zpath = cache / key, cache / f"{key}.zip"
    found = [folder / t for t in TOOLS]
    if all(p.is_file() for p in found):
        return found
    cache.mkdir(parents=True, exist_ok=True)
    if not zpath.is_file() or hashlib.sha256(zpath.read_bytes()).hexdigest() != sha:
        part = zpath.with_suffix(".part")
        print(f"downloading {url}")
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f)
        got = hashlib.sha256(part.read_bytes()).hexdigest()
        if got != sha:
            part.unlink()
            raise RuntimeError(f"LGPL ffmpeg checksum mismatch: expected {sha}, got {got} ({url})")
        part.replace(zpath)
    folder.mkdir(exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            parts = name.split("/")
            if (parts[-1] in TOOLS[:2] and parts[-2:-1] == ["bin"]) or (parts[-1] == TOOLS[2] and len(parts) == 2):
                part = folder / (parts[-1] + ".part")  # whole files only: a crash mid-write leaves no truncated exe
                part.write_bytes(z.read(name))
                part.replace(folder / parts[-1])
    missing = [p.name for p in found if not p.is_file()]
    if missing:
        raise RuntimeError(f"The LGPL ffmpeg zip has no {', '.join(missing)}")
    return found


def main():
    cpu_only = "--cpu-only" in sys.argv
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_icon.py")], check=True)
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "Reelwright",
        "--icon", str(ROOT / "assets" / "reelwright.ico"),
        "--add-data", f"{ROOT / 'reelwright' / 'static'}{SEP}reelwright/static",
        "--add-data", f"{ROOT / 'reelwright' / 'training' / 'worker.py'}{SEP}reelwright/training",  # runs in the training env
        "--collect-all", "faster_whisper", "--collect-all", "ctranslate2", "--collect-all", "webview",
        "--collect-data", "googleapiclient", "--collect-submodules", "uvicorn", "--collect-submodules", "yt_dlp",
        "--collect-submodules", "websockets",  # OBS link: websockets loads its sync client lazily
        "--collect-submodules", "reelwright",  # every app module, incl. models/ and training/ sub-packages
        "--collect-submodules", "mcp",  # Claude Desktop link (Reelwright.exe --mcp): FastMCP loads parts lazily
        "--collect-submodules", "segno",  # phone remote: the pairing QR (imported only while pairing)
        "--collect-all", "sherpa_onnx",  # audio events: its .pyd + its own onnxruntime.dll in sherpa_onnx/lib (~29 MB)
        "--hidden-import", "clr", "--hidden-import", "google_auth_oauthlib.flow",
        # ML stacks pulled in transitively that Reelwright never imports (keeps the build lean)
        *[a for m in ("torch", "torchvision", "torchaudio", "tensorflow", "keras", "jax", "transformers", "diffusers",
                      "matplotlib", "pandas", "scipy", "sklearn", "IPython", "notebook", "tkinter", "PyQt5", "PyQt6", "PySide6")
          for a in ("--exclude-module", m)],
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
        str(ROOT / "Reelwright.pyw"),
    ]
    if not cpu_only:
        for sp in [*site.getsitepackages(), site.getusersitepackages()]:
            for bin_dir in Path(sp).glob("nvidia/*/bin"):
                args[-1:-1] = ["--add-binary", f"{bin_dir}{SEP}nvidia/{bin_dir.parent.name}/bin"]
    subprocess.run(args, check=True, cwd=ROOT)
    out = ROOT / "dist" / "Reelwright"
    if "--lgpl" in sys.argv:  # a build to hand out: the verified LGPL ffmpeg, its licence and where it came from
        ff, fp, lic = fetch_lgpl()
        shutil.copy2(ff, out / "ffmpeg.exe")
        shutil.copy2(fp, out / "ffprobe.exe")
        shutil.copy2(lic, out / "FFMPEG-LICENSE.txt")
        (out / "FFMPEG-NOTICE.txt").write_text(
            "This program runs FFmpeg (ffmpeg.exe, ffprobe.exe), licensed under the GNU LGPL v2.1 or later (FFMPEG-LICENSE.txt).\n"
            f"Binaries: {LGPL_URL}\nsha256: {LGPL_SHA256}\n"
            "Source code: https://git.ffmpeg.org/ffmpeg.git (release/8.1, commit 8dca1586f4) and the build scripts at\n"
            "https://github.com/FFmpeg-Builds/FFmpeg-Builds. They are separate programs: replace them with any compatible build.\n",
            "utf-8")
    else:
        for tool in ("ffmpeg", "ffprobe"):  # make the app self-contained
            src = shutil.which(tool)
            if src:
                shutil.copy2(src, out / Path(src).name)
            else:
                print(f"warning: {tool} not found on PATH; install it or place {tool}.exe next to Reelwright.exe")
    print(f"\nBuilt {out / 'Reelwright.exe'}")


if __name__ == "__main__":
    main()
