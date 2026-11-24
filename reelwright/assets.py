"""Media library: the creator's images, GIFs, video snippets and sounds for overlays, plus built-in sound effects that
are synthesised locally with ffmpeg (no downloads, nothing copyrighted). Files live in DATA/assets, docs in the store."""
import functools
import json
import os
from pathlib import Path

from . import log, media, store
from .config import DATA

ASSETS = DATA / "assets"
FONTS = ASSETS / "fonts"  # uploaded fonts (also handed to libass as its fontsdir)
FONTS.mkdir(parents=True, exist_ok=True)
KINDS = {".ttf": "font", ".otf": "font", ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "gif", ".mp4": "video",
         ".mov": "video", ".webm": "video", ".mp3": "sound", ".wav": "sound", ".m4a": "sound", ".ogg": "sound"}
MAX_BYTES = 200 * 1024 * 1024

# name: (lavfi source, extra audio filters, tags). Pure synthesis: sine sweeps, decays and shaped noise.
BUILTIN_SFX = {
    "Whoosh": ("anoisesrc=d=0.7:c=pink:a=0.8", "highpass=f=400,lowpass=f=5000,afade=t=in:d=0.35,afade=t=out:st=0.35:d=0.35",
               ["transition", "swoosh", "fast"]),
    "Boom": ("aevalsrc='0.9*sin(2*PI*(70*t-25*t*t))*exp(-3*t)':d=1.4", "", ["impact", "hit", "explosion"]),
    "Ding": ("aevalsrc='0.5*sin(2*PI*1320*t)*exp(-4*t)+0.25*sin(2*PI*2640*t)*exp(-6*t)':d=1.2", "",
             ["bell", "correct", "notification"]),
    "Pop": ("aevalsrc='0.8*sin(2*PI*(500+2500*exp(-40*t))*t)*exp(-35*t)':d=0.25", "", ["bubble", "appear", "cute"]),
    "Riser": ("aevalsrc='0.4*sin(2*PI*(150*t+200*t*t))*t/2.5':d=2.5", "", ["build-up", "tension", "transition"]),
    "Bass drop": ("aevalsrc='0.9*sin(2*PI*(110*t-35*t*t))*exp(-1.1*t)':d=1.6", "", ["impact", "drop", "hype"]),
    "Rimshot": ("aevalsrc='0.6*sin(2*PI*330*t)*exp(-30*t)+0.4*(random(0)*2-1)*exp(-45*t)':d=0.4", "", ["joke", "drum", "comedy"]),
    "Glitch": ("aevalsrc='0.35*sgn(sin(2*PI*(90+300*mod(floor(t*30),7))*t))*lt(mod(t,0.11),0.07)':d=0.6", "",
               ["digital", "error", "transition"]),
    "Record scratch": ("aevalsrc='0.5*sin(2*PI*(300*t+40*sin(2*PI*7*t)))*exp(-1.8*t)+0.15*(random(0)*2-1)*exp(-3*t)':d=0.7",
                       "", ["stop", "fail", "comedy"]),
    "Applause": ("anoisesrc=d=2.5:c=white:a=0.5",
                 "highpass=f=500,lowpass=f=7000,tremolo=f=11:d=0.7,afade=t=in:d=0.3,afade=t=out:st=1.6:d=0.9",
                 ["crowd", "cheer", "win"]),
}


def path(doc: dict) -> Path:
    return ASSETS / doc["file"]


def info(p: Path) -> dict:
    """Duration and picture size via ffprobe (images have no duration; sounds no size)."""
    d = json.loads(media._run([media.FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)]).stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    return {"duration": round(media._num(d.get("format", {}).get("duration")), 3), "width": int(v["width"]) if v else 0,
            "height": int(v["height"]) if v else 0, "has_audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}


def add(src: Path, filename: str, name: str = "", tags=()) -> dict:
    """Move an uploaded file into the library (ValueError on an unsupported or unreadable file)."""
    ext = Path(filename).suffix.lower()
    kind = KINDS.get(ext)
    if not kind:
        raise ValueError(f"Unsupported file type {ext or '(none)'}.")
    if kind == "font":
        return _add_font(src, ext, name, tags)
    try:
        meta = info(src)
    except RuntimeError:
        raise ValueError("Couldn't read that file.") from None
    if kind == "sound" and not meta["has_audio"] or kind != "sound" and not meta["width"]:
        raise ValueError(f"That file has no {'audio' if kind == 'sound' else 'picture'}.")
    aid = store.new_id()
    os.replace(src, ASSETS / f"{aid}{ext}")
    return store.put("asset", {"id": aid, "kind": kind, "name": (name or Path(filename).stem).strip()[:80], "file": f"{aid}{ext}",
                               "duration": meta["duration"], "width": meta["width"], "height": meta["height"], "builtin": False,
                               "tags": [t.strip()[:30] for t in tags if t.strip()][:20]})


def ensure_builtins() -> list[dict]:
    """The built-in sound effects, synthesised on first use (stable ids "sfx-<name>")."""
    docs = []
    for name, (source, af, tags) in BUILTIN_SFX.items():
        aid = "sfx-" + name.lower().replace(" ", "-")
        doc = store.get("asset", aid)
        if not doc or not path(doc).exists():
            out = ASSETS / f"{aid}.wav"
            media._run([media.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", source, *(["-af", af] if af else []),
                        "-ar", "48000", "-ac", "2", str(out)])
            doc = store.put("asset", {"id": aid, "kind": "sound", "name": name, "file": out.name, "duration": info(out)["duration"],
                                      "width": 0, "height": 0, "builtin": True, "tags": ["sfx", *tags]})
        docs.append(doc)
    return docs


def listing() -> list[dict]:
    """Every asset, built-ins first, each with how many clips' overlays use it."""
    try:
        ensure_builtins()
    except RuntimeError:
        log.error("assets", "couldn't make the built-in sound effects")
    used: dict[str, int] = {}
    for c in store.find("clip"):
        for aid in {o.get("asset_id") for o in (c.get("edit") or {}).get("overlays") or []}:
            used[aid] = used.get(aid, 0) + 1
    docs = sorted(store.find("asset"), key=lambda a: (not a.get("builtin"), a.get("name", "").lower()))
    return [{**a, "used_by": used.get(a["id"], 0)} for a in docs]


def remove(aid: str) -> None:
    """Delete an uploaded asset (ValueError for built-ins). Clips still using it simply render without that overlay."""
    doc = store.get("asset", aid)
    if not doc:
        return
    if doc.get("builtin"):
        raise ValueError("Built-in sound effects can't be deleted.")
    path(doc).unlink(missing_ok=True)
    store.delete("asset", aid)


# ---------- fonts ----------

def font_name(p: Path) -> tuple[str, str] | None:
    """(family, style) from a font file's name table, or None if it isn't a readable font."""
    from PIL import ImageFont

    try:
        family, style = ImageFont.truetype(str(p), 12).getname()
        return (family, style or "Regular") if family else None
    except (OSError, ValueError):
        return None


def _add_font(src: Path, ext: str, name: str, tags) -> dict:
    fn = font_name(src)
    if not fn:
        raise ValueError("That isn't a readable TrueType/OpenType font.")
    aid = store.new_id()
    os.replace(src, FONTS / f"{aid}{ext}")
    return store.put("asset", {"id": aid, "kind": "font", "name": (name or fn[0]).strip()[:80], "family": fn[0], "style": fn[1],
                               "file": f"fonts/{aid}{ext}", "duration": 0, "width": 0, "height": 0, "builtin": False,
                               "tags": [t.strip()[:30] for t in tags if t.strip()][:20]})


@functools.cache
def system_fonts() -> tuple[dict, ...]:
    """Installed fonts (Windows: machine-wide and per-user folders), scanned once per run."""
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    out = []
    for d in dirs:
        for f in sorted(d.glob("*")) if d.is_dir() else []:
            if f.suffix.lower() in (".ttf", ".otf", ".ttc") and (fn := font_name(f)):
                out.append({"family": fn[0], "style": fn[1], "file": f.name, "source": "system", "url": None})
    return tuple(out)


def fonts() -> list[dict]:
    """System fonts plus the creator's uploaded ones (served from /assets/fonts/)."""
    up = [{"family": a.get("family") or a["name"], "style": a.get("style") or "Regular", "file": Path(a["file"]).name,
           "source": "uploaded", "url": f"/assets/{a['file']}"} for a in store.find("asset", kind="font")]
    return [*system_fonts(), *up]


def resolve_fonts(edit: dict) -> dict:
    """The edit with any font nobody has installed or uploaded reset to the default (logged, never a failed render)."""
    known = {f["family"].lower() for f in fonts()}
    ok = lambda f: not f or f.lower() in known  # noqa: E731
    out = dict(edit)
    for k in ("caption_font", "title_font", "sticker_font"):
        if not ok(out.get(k)):
            log.event("fonts", f"{out[k]!r} isn't installed; using the default")
            out[k] = None
    out["overlays"] = [o if ok(o.get("font")) else {**o, "font": None} for o in out.get("overlays") or []] or out.get("overlays")
    return out


def uploaded_font_files() -> list[Path]:
    return [path(a) for a in store.find("asset", kind="font") if path(a).exists()]
