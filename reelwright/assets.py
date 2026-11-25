"""Media library: the creator's images, GIFs, stickers, video snippets and sounds for overlays, plus built-in sound effects
that are synthesised locally with ffmpeg (no downloads, nothing copyrighted). Files live in DATA/assets, docs in the store.

An asset doc also carries: checksum (sha256 of the file as received: the same file is never stored twice), source (where
an online import came from: provider, source_url, licence, author, attribution_text, downloaded_at), has_alpha, thumb /
waveform (pictures under assets/thumbs), fav, trim {in, out} (non-destructive: the render seeks to `in`), and defaults
{volume, x, y, w, d} for new overlays of it. Kits ("kit" docs: name + asset_ids) are named collections, exported and
imported as a zip of the files plus manifest.json."""
import functools
import hashlib
import io
import json
import os
import zipfile
import zlib
from pathlib import Path

from . import log, media, store
from .config import DATA

ASSETS = DATA / "assets"
FONTS = ASSETS / "fonts"  # uploaded fonts (also handed to libass as its fontsdir)
THUMBS = ASSETS / "thumbs"  # thumbnails and sound waveforms (served like the files)
FONTS.mkdir(parents=True, exist_ok=True)
THUMBS.mkdir(parents=True, exist_ok=True)
KINDS = {".ttf": "font", ".otf": "font", ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "gif",
         ".apng": "gif", ".mp4": "video", ".mov": "video", ".webm": "video", ".mp3": "sound", ".wav": "sound", ".m4a": "sound",
         ".ogg": "sound", ".flac": "sound"}
MAX_BYTES = 200 * 1024 * 1024  # one upload request
MAX_MEDIA_S = 30 * 60  # longest sound/video taken in: a long low-bitrate file would expand ~100x as 48 kHz WAV
CAPS = {"image": 30 << 20, "gif": 50 << 20, "video": 200 << 20, "sound": 50 << 20, "font": 20 << 20}  # per kind
# licence code -> (label, attribution required, non-commercial or otherwise restricted). Online imports carry one.
LICENCES = {"cc0": ("CC0", False, False), "cc-by": ("CC BY", True, False), "cc-by-nc": ("CC BY-NC", True, True),
            "sampling+": ("Sampling+", True, True), "giphy": ("GIPHY", True, False), "pixabay": ("Pixabay Content Licence", False, False),
            "pexels": ("Pexels Licence", False, False)}
DEFAULT_KEYS = {"volume": (0, 2), "x": (0, 1), "y": (0, 1), "w": (0.02, 1), "d": (0.1, 600)}

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


INDIRECT = {"hls", "applehttp", "concat", "dash", "ffmetadata", "tee"}  # formats that are lists of other files


def info(p: Path) -> dict:
    """Duration and picture size via ffprobe (images have no duration; sounds no size)."""
    d = json.loads(media._run([media.FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)]).stdout)
    if set(str(d.get("format", {}).get("format_name", "")).split(",")) & INDIRECT:
        raise ValueError("That file is a playlist, not media.")  # it would make ffmpeg read other files at render time
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    return {"duration": round(media._num(d.get("format", {}).get("duration")), 3), "width": int(v["width"]) if v else 0,
            "height": int(v["height"]) if v else 0, "has_audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _streams(p: Path) -> list[dict]:
    return json.loads(media._run([media.FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", str(p)]).stdout).get("streams", [])


def _vpx_alpha(p: Path) -> str | None:
    """The libvpx decoder that keeps a WebM's alpha (ffmpeg's native VP8/VP9 decoders drop it), or None: no alpha."""
    v = next((s for s in _streams(p) if s.get("codec_type") == "video"), {})
    if str({k.lower(): x for k, x in (v.get("tags") or {}).items()}.get("alpha_mode")) != "1":
        return None
    return {"vp9": "libvpx-vp9", "vp8": "libvpx"}.get(v.get("codec_name"))


def _pil_alpha(p: Path) -> bool:
    """Any transparent pixel in the (first frame of the) picture."""
    from PIL import Image
    try:
        with Image.open(p) as im:
            if im.mode not in ("RGBA", "LA", "PA", "RGBa") and "transparency" not in im.info:
                return False
            return im.convert("RGBA").getchannel("A").getextrema()[0] < 255
    except Exception:  # Pillow can't read it (a format only ffmpeg knows): no alpha claimed
        return False


def _animated_webp(p: Path) -> bool:
    from PIL import Image
    try:
        with Image.open(p) as im:
            return bool(getattr(im, "is_animated", False))
    except Exception:
        return False


def _thumbs(aid: str, p: Path, kind: str) -> dict:
    """A small picture of it (PNG, alpha kept) or a sound's waveform, under assets/thumbs; {} when ffmpeg can't."""
    try:
        if kind == "sound":
            out = THUMBS / f"{aid}.wave.png"
            media._run([media.FFMPEG, "-y", "-v", "error", "-i", str(p), "-filter_complex", "showwavespic=s=320x64:colors=#8f9bff",
                        "-frames:v", "1", str(out)])
            return {"waveform": f"thumbs/{out.name}"}
        out = THUMBS / f"{aid}.png"
        media._run([media.FFMPEG, "-y", "-v", "error", "-i", str(p), "-frames:v", "1", "-vf", "scale=160:-2", str(out)])
        return {"thumb": f"thumbs/{out.name}"}
    except RuntimeError:
        log.error("assets", f"no thumbnail for {aid}")
        return {}


def add(src: Path, filename: str, name: str = "", tags=(), source: dict | None = None, loudnorm: bool = False) -> dict:
    """Take a file into the library (ValueError on an unsupported, oversized or unreadable one). The same file again
    (same checksum) returns the asset already there, marked duplicate. Sounds become 48 kHz stereo WAV (loudness-normalised
    on request); a WebM / animated WebP sticker becomes APNG (kind gif) so the overlay keeps its transparency.
    `src` is moved in, or left for the caller to delete (a duplicate, a converted file)."""
    ext = Path(filename).suffix.lower()
    kind = KINDS.get(ext)
    if not kind:
        raise ValueError(f"Unsupported file type {ext or '(none)'}.")
    size = src.stat().st_size
    if size > CAPS[kind]:
        raise ValueError(f"That {kind} is too big ({size / 2**20:.1f} MB; the limit is {CAPS[kind] >> 20} MB).")
    digest = _sha256(src)
    dup = next((a for a in store.find("asset", checksum=digest) if path(a).exists()), None)
    if dup:
        return {**dup, "duplicate": True}
    name = (name or Path(filename.replace("\\", "/")).stem).strip()[:80]
    tags = [t.strip()[:30] for t in tags if t.strip()][:20]
    if kind == "font":
        return store.update("asset", _add_font(src, ext, name, tags)["id"], {"checksum": digest})
    aid, alpha, convert = store.new_id(), False, None
    try:
        if ext == ".webp" and _animated_webp(src):  # ffmpeg can't be relied on to decode animated WebP: Pillow -> APNG
            from PIL import Image
            convert = ASSETS / f"{aid}.apng"
            with Image.open(src) as im:
                im.save(convert, format="PNG", save_all=True, loop=0)
            kind = "gif"
        meta = info(convert or src)
        if kind == "sound" and not meta["has_audio"] or kind != "sound" and not meta["width"]:
            raise ValueError(f"That file has no {'audio' if kind == 'sound' else 'picture'}.")
        if kind in ("sound", "video") and meta["duration"] > MAX_MEDIA_S:
            raise ValueError(f"That {kind} is too long ({meta['duration'] / 60:.0f} min; the limit is {MAX_MEDIA_S // 60} min).")
        if ext == ".webm" and (dec := _vpx_alpha(src)):  # alpha WebM -> APNG (decoded with libvpx, which keeps alpha)
            convert = ASSETS / f"{aid}.apng"
            media._run([media.FFMPEG, "-y", "-v", "error", "-c:v", dec, "-i", str(src), "-t", str(MAX_MEDIA_S), "-an", "-plays", "0",
                        "-f", "apng", str(convert)])
            kind, alpha = "gif", True
        elif kind == "sound":
            convert = ASSETS / f"{aid}.wav"
            media._run([media.FFMPEG, "-y", "-v", "error", "-i", str(src), "-t", str(MAX_MEDIA_S), "-vn",
                        *(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"] if loudnorm else []), "-ar", "48000", "-ac", "2", str(convert)])
    except (RuntimeError, ValueError, OSError) as e:
        for f in ASSETS.glob(f"{aid}.*"):
            f.unlink(missing_ok=True)
        raise ValueError(str(e) if isinstance(e, ValueError) else "Couldn't read that file.") from None
    final = convert or ASSETS / f"{aid}{ext}"
    if not convert:
        os.replace(src, final)
    meta = info(final)
    if kind in ("image", "gif") and not alpha:
        alpha = _pil_alpha(final)
    elif kind == "video":
        v = next((x for x in _streams(final) if x.get("codec_type") == "video"), {})
        alpha = any(k in str(v.get("pix_fmt")) for k in ("yuva", "rgba", "argb", "bgra", "abgr", "gbrap"))
    return store.put("asset", {"id": aid, "kind": kind, "name": name, "file": final.name, "duration": meta["duration"], "width": meta["width"],
                               "height": meta["height"], "builtin": False, "tags": tags, "checksum": digest, "has_alpha": bool(alpha),
                               "source": _clean_source(source), "fav": False, "trim": None, "defaults": {}, **_thumbs(aid, final, kind)})


def _clean_source(src: dict | None) -> dict | None:
    """Where an import came from, as plain capped strings (it also arrives from kit manifests, i.e. from anyone)."""
    if not isinstance(src, dict):
        return None
    out = {k: str(src.get(k) or "")[:500] for k in ("provider", "source_url", "licence", "author", "attribution_text", "downloaded_at")}
    if out["licence"] not in LICENCES:
        out["licence"] = ""
    return out if out["provider"] or out["licence"] else None


def patch(aid: str, body: dict) -> dict:
    """Name, tags, favourite, trim {in, out} (seconds inside the file) and per-asset defaults (ValueError when invalid)."""
    a = store.get("asset", aid)
    if not a:
        raise KeyError(aid)
    up: dict = {}
    if "name" in body:
        n = str(body["name"] or "").strip()
        if not 1 <= len(n) <= 80:
            raise ValueError("A name is 1 to 80 characters.")
        up["name"] = n
    if "tags" in body:
        if not isinstance(body["tags"], list) or len(body["tags"]) > 20:
            raise ValueError("tags must be a list of up to 20.")
        up["tags"] = [str(t).strip()[:30] for t in body["tags"] if str(t).strip()]
    if "fav" in body:
        up["fav"] = bool(body["fav"])
    if "trim" in body:
        t = body["trim"]
        if t is not None:
            try:
                i, o = float(t["in"]), float(t["out"])
            except (TypeError, KeyError, ValueError):
                raise ValueError("trim must be {in, out} in seconds.") from None
            if a["kind"] not in ("sound", "video", "gif") or not 0 <= i < o <= (a.get("duration") or 0) + 0.01 or o - i < 0.05:
                raise ValueError("Trim must keep a part of the sound or clip: 0 ≤ in < out ≤ its length.")
            t = {"in": round(i, 3), "out": round(min(o, a["duration"]), 3)}
        up["trim"] = t
    if "defaults" in body:
        d = body["defaults"] or {}
        if not isinstance(d, dict) or set(d) - set(DEFAULT_KEYS):
            raise ValueError(f"defaults may set {', '.join(DEFAULT_KEYS)}.")
        out = {}
        for k, v in d.items():
            lo, hi = DEFAULT_KEYS[k]
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"default {k} must be a number.") from None
            if not lo <= v <= hi:
                raise ValueError(f"default {k} must be between {lo} and {hi}.")
            out[k] = round(v, 4)
        up["defaults"] = out
    return store.update("asset", aid, up)


def placed(o: dict, a: dict) -> dict:
    """An overlay resolved for media.render: the file, plus the trim (the render seeks to `in`; never plays past `out`)."""
    out = {**o, "path": str(path(a))}
    tr = a.get("trim")
    if tr and o["kind"] in ("sound", "video", "gif"):
        out["trim_in"] = float(tr["in"])
        out["d"] = min(float(o["d"]), float(tr["out"]) - float(tr["in"]))
    return out


def trimmed_length(a: dict) -> float:
    tr = a.get("trim")
    return round(tr["out"] - tr["in"], 3) if tr else (a.get("duration") or 0)


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
    for _, overlays in store.fields("clip", "edit.overlays"):  # the overlays alone, not each clip's words / track
        for aid in {o.get("asset_id") for o in overlays or []}:
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
    for k in ("thumb", "waveform"):
        if doc.get(k):
            (ASSETS / doc[k]).unlink(missing_ok=True)
    for k in store.find("kit"):
        if aid in k.get("asset_ids", []):
            store.update("kit", k["id"], {"asset_ids": [x for x in k["asset_ids"] if x != aid]})
    store.delete("asset", aid)


# ---------- kits: named collections, exported/imported as a zip ----------

KIT_MAX = 500           # assets per kit
KIT_ZIP_MAX = 2 << 30   # total unpacked bytes of an imported kit
MANIFEST_KEYS = ("kind", "name", "tags", "source", "trim", "defaults", "fav")


def kit_names() -> dict[str, list[str]]:
    """asset id -> the names of the kits it is in."""
    out: dict[str, list[str]] = {}
    for k in store.find("kit"):
        for aid in k.get("asset_ids", []):
            out.setdefault(aid, []).append(k["name"])
    return out


def save_kit(name: str, asset_ids: list, kid: str | None = None) -> dict:
    name = str(name or "").strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("A kit name is 1 to 60 characters.")
    if not isinstance(asset_ids, list) or len(asset_ids) > KIT_MAX:
        raise ValueError(f"A kit holds up to {KIT_MAX} assets.")
    ids = list(dict.fromkeys(str(a) for a in asset_ids))
    if any(not store.get("asset", a) for a in ids):
        raise ValueError("A kit can only hold assets from the library.")
    if kid:
        if not store.get("kit", kid):
            raise KeyError(kid)
        return store.update("kit", kid, {"name": name, "asset_ids": ids})
    return store.put("kit", {"name": name, "asset_ids": ids})


def export_kit(kid: str) -> bytes:
    """The kit as a zip: files/<n>.<ext> + manifest.json {version, name, assets: [{file, kind, name, tags, source, trim, defaults, fav}]}."""
    k = store.get("kit", kid)
    if not k:
        raise KeyError(kid)
    buf, entries = io.BytesIO(), []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, aid in enumerate(k["asset_ids"]):
            a = store.get("asset", aid)
            if not a or not path(a).exists():
                continue
            arc = f"files/{i + 1:03d}{path(a).suffix.lower()}"
            z.write(path(a), arc)
            entries.append({"file": arc, **{f: a.get(f) for f in MANIFEST_KEYS}})
        z.writestr("manifest.json", json.dumps({"version": 1, "name": k["name"], "assets": entries}, indent=2))
    return buf.getvalue()


def _safe_member(n: str) -> bool:
    p = n.replace("\\", "/")
    return bool(p) and not p.startswith("/") and ":" not in p and ".." not in p.split("/") and len(p) <= 200


def import_kit(zpath: Path) -> dict:
    """A kit zip into the library (ValueError on anything off: not a zip, no manifest, a path that climbs out or is
    absolute, an unsupported type, a file over its kind's size cap, too many files). Files already in the library (same
    checksum) are reused. Nothing is extracted under its own name: each member is copied to a temporary file."""
    try:
        z = zipfile.ZipFile(zpath)
    except (zipfile.BadZipFile, OSError):
        raise ValueError("That isn't a kit zip.") from None
    with z:
        infos = z.infolist()
        if len(infos) > KIT_MAX + 1 or any(not _safe_member(i.filename) for i in infos):
            raise ValueError("The kit zip holds an unsafe path (absolute, a drive, or ..) or too many files.")
        if sum(i.file_size for i in infos) > KIT_ZIP_MAX:
            raise ValueError("The kit is too big.")
        members = {i.filename: i for i in infos}
        man = members.get("manifest.json")
        if not man or man.file_size > 1 << 20:
            raise ValueError("The kit zip has no manifest.json.")
        try:
            m = json.loads(z.read(man))
            entries, kit_name = m["assets"], str(m.get("name") or "Imported kit")
            if not isinstance(entries, list) or len(entries) > KIT_MAX or not all(isinstance(e, dict) for e in entries):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise ValueError("The kit's manifest.json isn't valid.") from None
        for e in entries:  # check everything before taking anything in
            f = str(e.get("file") or "")
            kind = KINDS.get(Path(f).suffix.lower())
            if f not in members or not kind:
                raise ValueError(f"The kit lists {f[:60]!r}, which is missing or not a supported type.")
            if members[f].file_size > CAPS[kind]:
                raise ValueError(f"{f[:60]} is too big for a {kind} ({CAPS[kind] >> 20} MB).")
        ids, new = [], []
        for e in entries:
            f = e["file"]
            kind, ext = KINDS[Path(f).suffix.lower()], Path(f).suffix.lower()
            tmp = ASSETS / f".kit-{store.new_id()}{ext}"
            try:
                with z.open(members[f]) as src, open(tmp, "wb") as dst:
                    left = CAPS[kind]
                    while chunk := src.read(1 << 20):
                        left -= len(chunk)
                        if left < 0:
                            raise ValueError(f"{f[:60]} is too big for a {kind}.")
                        dst.write(chunk)
                tags = e.get("tags") if isinstance(e.get("tags"), list) else []
                a = add(tmp, f, str(e.get("name") or "")[:80], [str(t) for t in tags], source=e.get("source"))
            except (ValueError, zipfile.BadZipFile, zlib.error, OSError, EOFError) as err:
                for aid in new:  # all or nothing: no half-imported kit
                    remove(aid)
                raise ValueError(str(err) if type(err) is ValueError else f"{f[:60]} in the kit couldn't be read.") from None
            finally:
                tmp.unlink(missing_ok=True)
            if not a.get("duplicate"):
                new.append(a["id"])
            if not a.get("duplicate"):
                try:  # the creator's settings ride along; a bad one is dropped, never the asset
                    a = patch(a["id"], {k: e[k] for k in ("trim", "defaults", "fav") if e.get(k) is not None})
                except ValueError:
                    log.event("assets", f"kit {kit_name[:60]!r}: dropped bad settings of {f[:60]}")
            ids.append(a["id"])
    return save_kit(kit_name[:60].strip() or "Imported kit", ids)


# ---------- licences of the media a clip uses ----------

def used(item: dict) -> list[dict]:
    """The library assets a clip's (or long form's) overlays use, once each, in order."""
    ids = dict.fromkeys(o.get("asset_id") for o in ((item.get("edit") or {}).get("overlays") or []) if o.get("asset_id"))
    return [a for a in (store.get("asset", i) for i in ids) if a]


def attributions(item: dict) -> str:
    """"Credits: …" for every used asset whose licence asks for attribution ("" = none needed)."""
    texts = [a["source"]["attribution_text"] for a in used(item) if (a.get("source") or {}).get("attribution_text")
             and LICENCES.get(a["source"].get("licence"), ("", False, False))[1]]
    return "Credits: " + " · ".join(dict.fromkeys(texts)) if texts else ""


def licence_issues(item: dict) -> list[dict]:
    """Pre-publish QA (qa.lint): a licence summary (level info), CC BY-NC media on a monetized channel and a missing
    attribution (warn). Media with no recorded source (own uploads, built-ins) is the creator's and isn't listed."""
    from . import config
    src = [a for a in used(item) if (a.get("source") or {}).get("licence") in LICENCES]
    if not src:
        return []
    count: dict[str, int] = {}
    for a in src:
        label = LICENCES[a["source"]["licence"]][0]
        count[label] = count.get(label, 0) + 1
    out = [{"code": "licences", "level": "info", "platform": None, "fix": "Credits for CC BY media are added to the description.",
            "msg": "Licences: " + ", ".join(f"{k} ×{n}" for k, n in count.items())}]
    monetized = config.load()["media_monetized"]
    for a in src:
        label, attr, nc = LICENCES[a["source"]["licence"]]
        if nc and monetized:
            out.append({"code": "licence_nc", "level": "warn", "platform": None, "msg": f"“{a['name']}” is {label}: not for a monetized channel",
                        "fix": "Replace it with CC0 / CC BY media, or untick “My channel is monetized” (Studio › Media) if this post earns nothing."})
        if attr and not (a["source"].get("attribution_text") or "").strip():
            out.append({"code": "attribution_missing", "level": "warn", "platform": None,
                        "msg": f"“{a['name']}” ({label}) needs a credit and has none recorded",
                        "fix": f"Credit {a['source'].get('author') or 'its author'} in the description ({a['source'].get('source_url') or 'its source'})."})
    return out


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
