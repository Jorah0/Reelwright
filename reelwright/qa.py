"""Pre-publish QA lint: what would make a platform refuse a render, or a viewer swipe it away, checked on the finished file.

lint() runs one ffmpeg pass over the export (black and frozen video, loudness, true peak, clipping, a silent opening),
then the platform limits (length, size) and the apps' UI safe zones for the clip's text. Each issue is
{code, level, msg, fix, platform}: level "error" = the platform would refuse it, "warn" = worth a look before posting;
platform None = it matters everywhere. pipeline._export stores the list as clip["export"]["qa"].
"""
import re
import subprocess
from pathlib import Path

from . import media, store

TARGET_LUFS = -14.0
NAMES = {"youtube": "YouTube Shorts", "tiktok": "TikTok", "instagram": "Instagram Reels"}
# Share of a 9:16 frame each app's own UI covers: the top bar, the caption/name block at the bottom, the button column on
# the right (drawn by Studio's safe-zone overlay; text is checked against top and bottom). Approximate: calibrate against
# current screenshots now and then, the apps move them. Mirrored in app.js SAFE_ZONES.
SAFE_ZONES = {"tiktok": {"top": 0.07, "bottom": 0.20, "right": 0.12},
              "youtube": {"top": 0.06, "bottom": 0.18, "right": 0.12},
              "instagram": {"top": 0.10, "bottom": 0.20, "right": 0.12}}


def _issue(code: str, level: str, msg: str, fix: str = "", platform: str | None = None) -> dict:
    return {"code": code, "level": level, "msg": msg, "fix": fix, "platform": platform}


def _last(pattern: str, text: str) -> float | None:
    found = re.findall(pattern, text)
    try:
        return float(found[-1]) if found else None
    except ValueError:  # "-inf"
        return None


def measure(path: Path, has_audio: bool) -> str:
    """ffmpeg's log of one decode with the detectors on (blackdetect, freezedetect, silencedetect, astats, ebur128)."""
    af = ["-af", "silencedetect=noise=-35dB:d=1.5,astats=metadata=0,ebur128=peak=true:framelog=verbose"] if has_audio else ["-an"]
    r = subprocess.run([media.FFMPEG, "-hide_banner", "-nostats", *media.LOCAL_ONLY, "-i", str(path), "-vf",
                        "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-60dB:d=2", *af, "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=media.NO_WINDOW, timeout=300)
    return r.stderr


def signal_issues(log: str, duration: float, has_audio: bool) -> list[dict]:
    out = []
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", log)]
    if blacks:
        a, b = max(blacks, key=lambda x: x[1] - x[0])
        out.append(_issue("black", "warn", f"Black picture for {b - a:.1f} s at {a:.1f} s" + (f" (and {len(blacks) - 1} more)" if len(blacks) > 1 else ""),
                          "Trim it, or check the source and the layout's regions."))
    starts = [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"freeze_end:\s*([\d.]+)", log)]
    if starts:
        a = starts[0]
        b = ends[0] if ends else duration
        out.append(_issue("frozen", "warn", f"The picture freezes for {b - a:.1f} s at {a:.1f} s",
                          "A stalled capture or a still frame: trim it or cut away."))
    if not has_audio:
        return out
    if re.search(r"silence_start:\s*(-?0(\.0\d*)?|-[\d.e-]+)\s", log + " "):  # silent from the very start, for 1.5 s+
        out.append(_issue("no_hook", "warn", "Nothing is said or heard in the first 2 seconds",
                          "Start on the hook: trim the start to the first line that grabs."))
    peak = _last(r"Peak level dB:\s*(-?[\d.]+|-inf)", log)
    lufs = _last(r"I:\s+(-?[\d.]+) LUFS", log)
    true_peak = _last(r"Peak:\s+(-?[\d.]+|-inf) dBFS", log)
    if peak is not None and peak >= -0.1:
        out.append(_issue("clipping", "warn", f"The audio clips (peaks at {peak:.1f} dBFS): it will sound distorted",
                          "Lower the voice volume, or turn on Normalize loudness."))
    elif true_peak is not None and true_peak > -1.0:
        out.append(_issue("true_peak", "warn", f"True peak {true_peak:.1f} dBTP (platforms want -1 or lower)",
                          "Turn on Normalize loudness."))
    if lufs is not None and lufs > -60 and abs(lufs - TARGET_LUFS) > 2:
        out.append(_issue("loudness", "warn", f"Loudness {lufs:.1f} LUFS; the apps play at about {TARGET_LUFS:.0f}",
                          "Turn on Normalize loudness (-14 LUFS)."))
    return out


def limits(duration: float, size: float, vertical: bool, account: dict | None = None) -> list[dict]:
    """Length and file-size rules. account may carry TikTok's creator_info "max_video_post_duration_sec"."""
    out = []
    if vertical and duration > 180:
        out.append(_issue("too_long", "warn", f"{duration:.0f} s is over 3 minutes: YouTube posts it as a regular video, not a Short",
                          "Trim it to 3 minutes or less to post a Short.", "youtube"))
    tt = (account or {}).get("max_video_post_duration_sec")
    if tt and duration > float(tt):
        out.append(_issue("too_long", "error", f"{duration:.0f} s is over this TikTok account's {float(tt):.0f} s limit",
                          "Trim the clip.", "tiktok"))
    if duration < 3:
        out.append(_issue("too_short", "error", f"Instagram Reels must be at least 3 s (this is {duration:.1f} s)",
                          "Make the clip longer.", "instagram"))
    if duration > 15 * 60:
        out.append(_issue("too_long", "error", "Instagram Reels can be at most 15 minutes", "Trim the clip.", "instagram"))
    if size > 300e6:
        out.append(_issue("too_big", "error", f"Instagram Reels can be at most 300 MB (this file is {size / 1e6:.0f} MB)",
                          "Trim it, or render it shorter.", "instagram"))
    return out


def _span(style: list[str], lines: int, H: int) -> tuple[float, float]:
    """(top, bottom) px of `lines` lines in an ASS style (alignment 7-9 = top, 4-6 = middle, 1-3 = bottom)."""
    tall, align, mv = lines * 1.2 * float(style[2]), int(style[18]), int(style[21])
    if align >= 7:
        return mv, mv + tall
    if align >= 4:
        return H / 2 - tall / 2, H / 2 + tall / 2
    return H - mv - tall, H - mv


def safe_zones(clip: dict, W: int, H: int) -> list[dict]:
    """Captions, title card, sticker and headline that sit under an app's own UI (vertical videos only). Positions come from
    the same build_ass the render uses (banner avoidance included)."""
    if H <= W:
        return []
    e = {**media.DEFAULT_EDIT, **(clip.get("edit") or {})}
    proj = store.get("project", clip.get("project_id") or "") or {}
    hl = media.headline_spec(e, (proj.get("video_type") or {}).get("type"),
                             (store.get("pref", "headline_theme") or {}).get("theme")) if e.get("headline") else None
    card = media.card_spec(e)
    # one probe word when captioned, so the card is placed clear of the caption band as in the render (media.card_place)
    ass = media.build_ass([{"s": 0.0, "e": 0.5, "w": "x"}] if e["captions"] else [], 0, 1, 1, (W, H), e["title_text"], True,
                          "#FFD400", bool(e["captions"]), e["caption_style"], e["caption_pos"],
                          e["sticker_text"], e["sticker_pos"], None, media.seam(e), text_style=e, headline=hl, card=card)
    st = {f[0][7:]: f for f in (line.split(",") for line in ass.splitlines() if line.startswith("Style: "))}
    boxes = ([("Captions", *_span(st["Cap"], 2, H))] if e["captions"] else []) + \
        ([("The title card", *_span(st["Title"], 1, H))] if str(e["title_text"]).strip() else []) + \
        ([("The sticker", *_span(st["Sticker"], 1, H))] if str(e["sticker_text"]).strip() else [])
    if hl:
        _, y, _, h = media.headline_layout(hl, W, H, media.seam(e))["rect"]
        boxes.append(("The headline", y, y + h))
    out = []
    for plat, z in SAFE_ZONES.items():
        for what, y0, y1 in boxes:
            if y0 < z["top"] * H or y1 > (1 - z["bottom"]) * H:
                where = "top bar" if y0 < z["top"] * H else "caption and buttons at the bottom"
                out.append(_issue("safe_zone", "warn", f"{what} sits under {NAMES[plat]}'s {where}",
                                  "Move it (position) and check with Studio's safe-zone overlay.", plat))
        if card:  # the card isn't full width: the button column on the right counts too
            x, y, w, h = media.card_place(card, W, H, hl, media.seam(e), ass)["rect"]
            if y < z["top"] * H or y + h > (1 - z["bottom"]) * H or (x + w > (1 - z["right"]) * W and y + h > 0.4 * H):
                out.append(_issue("safe_zone", "warn", f"The streamer card sits under {NAMES[plat]}'s UI",
                                  "Move it (a corner spot stays clear) and check with Studio's safe-zone overlay.", plat))
    return out


def lint(clip: dict, export_path: Path, platform: str | None = None, account: dict | None = None) -> list[dict]:
    """Every issue of a finished render; platform = only the issues that matter there (plus the general ones)."""
    path = Path(export_path)
    m = media.probe(path)
    issues = signal_issues(measure(path, m["has_audio"]), m["duration"], m["has_audio"])
    issues += limits(m["duration"], path.stat().st_size, m["height"] > m["width"], account)
    issues += safe_zones(clip, m["width"], m["height"])
    from . import assets
    issues += assets.licence_issues(clip)  # licence summary, NC on a monetized channel, a missing credit
    return [i for i in issues if not platform or i["platform"] in (None, platform)]
