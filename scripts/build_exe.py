"""Build dist/Reelwright/Reelwright.exe (a self-contained Windows app folder) with PyInstaller.

    python scripts/build_exe.py            # GPU build (bundles NVIDIA cuBLAS/cuDNN for fast transcription)
    python scripts/build_exe.py --cpu-only # smaller build; transcription runs on the CPU
"""
import shutil
import site
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEP = ";" if sys.platform == "win32" else ":"


def main():
    cpu_only = "--cpu-only" in sys.argv
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_icon.py")], check=True)
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "Reelwright",
        "--icon", str(ROOT / "assets" / "reelwright.ico"),
        "--add-data", f"{ROOT / 'reelwright' / 'static'}{SEP}reelwright/static",
        "--collect-all", "faster_whisper", "--collect-all", "ctranslate2", "--collect-all", "webview",
        "--collect-data", "googleapiclient", "--collect-submodules", "uvicorn", "--collect-submodules", "yt_dlp",
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
    for tool in ("ffmpeg", "ffprobe"):  # make the app self-contained
        src = shutil.which(tool)
        if src:
            shutil.copy2(src, out / Path(src).name)
        else:
            print(f"warning: {tool} not found on PATH; install it or place {tool}.exe next to Reelwright.exe")
    print(f"\nBuilt {out / 'Reelwright.exe'}")


if __name__ == "__main__":
    main()
