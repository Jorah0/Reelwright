"""Presets: a named bundle of the parts of a clip's look and settings the creator chose to keep (layout, captions,
headline, card, effects, audio, publish defaults, app settings…), applied to a clip in one go.

A preset doc: {id, name, color, icon, created, order, fav, scope, len, src_project, parts: {part: values}}. Edit parts
hold edit keys and are checked by server.clean_edit, like any PATCH; times inside (effects, overlays, sections) are clip
time, so they keep their offset from the clip's start. Applying merges only the preset's parts: angle ids, library assets
and music files that belong elsewhere are dropped or copied over (pipeline.default_layout does the same for angles).
"Use for new clips" (pref preset_default) is applied in pipeline.new_clip after the single-purpose defaults (layout,
headline, streamer card), which keep working on their own. Also here: the editors' remembered tool-panel layout."""
import json
import math
import re
import shutil

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from . import config, log, media, store

router = APIRouter()

_TS = ("font", "size", "weight", "color", "outline", "outline_color", "shadow", "spacing")
PARTS = {  # part -> the edit keys it carries
    "layout": media.LAYOUT_KEYS,
    "sections": ("layout_segments",),
    "frame": ("aspect", "fit", "crop_x", "crop_y", "zoom"),
    "captions": ("captions", "caption_style", "caption_pos", "caption_anim", "caption_emphasis", *(f"caption_{f}" for f in _TS)),
    "headline": ("headline", *(f"headline_{f}" for f in _TS)),
    "card": ("streamer_card",),
    "title": ("title_text", "sticker_text", "sticker_pos", *(f"{w}_{f}" for w in ("title", "sticker") for f in _TS)),
    "look": ("brightness", "contrast", "saturation", "gamma", "sharpen", "speed", "fade_in", "fade_out"),
    "effects": ("effects",),
    "overlays": ("overlays",),
    "audio": ("volume", "loudnorm", "denoise", "music", "music_volume", "music_duck"),
    "bleep": ("bleep",),
    "cuts": ("remove_silence", "remove_fillers"),  # the options, never this clip's own cut spans
    "tracking": ("follow", "framing"),
}
OTHER = ("publish", "app")  # not edit keys: publish defaults (stored on the clip) and app settings
ALL_PARTS = (*PARTS, *OTHER)
PUBLISH_BOOLS = ("ai_content", "made_for_kids", "template")
APP_KEYS = ("clip_min", "clip_target", "clip_max", "clip_count", "ai_routes")
MAX_BYTES = 512_000
_ID = re.compile(r"[\w-]{1,40}")


def _srv():
    from . import server  # server includes this router: imported when used, not at load (no import cycle)
    return server


def _404(doc):
    if not doc:
        raise HTTPException(404, "That preset no longer exists.")
    return doc


# ---------- validation ----------

def _name(v) -> str:
    if not isinstance(v, str) or not 1 <= len(v.strip()) <= 60:
        raise HTTPException(400, "A preset needs a name of 1 to 60 characters.")
    return v.strip()


def _color(v) -> str:
    if v in (None, ""):
        return ""
    if not isinstance(v, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
        raise HTTPException(400, "Preset colour must be #RRGGBB.")
    return v.upper()


def _icon(v) -> str:
    if v in (None, ""):
        return ""
    if not isinstance(v, str) or len(v) > 4 or any(ch in v for ch in "<>&\"'\\/"):
        raise HTTPException(400, "Preset icon must be an emoji or up to 4 characters.")
    return v


def _publish(v) -> dict:
    if not isinstance(v, dict):
        raise HTTPException(400, "publish must be an object.")
    out = {}
    if "privacy" in v:
        if v["privacy"] not in ("public", "unlisted", "private"):
            raise HTTPException(400, "Publish privacy must be public, unlisted or private.")
        out["privacy"] = v["privacy"]
    if "accounts" in v:
        acc = v["accounts"]
        if not isinstance(acc, list) or len(acc) > 20 or not all(isinstance(a, str) and _ID.fullmatch(a) for a in acc):
            raise HTTPException(400, "Publish accounts must be a list of up to 20 account ids.")
        out["accounts"] = list(dict.fromkeys(acc))
    if "category_id" in v:
        if not isinstance(v["category_id"], str) or not re.fullmatch(r"\d{1,3}", v["category_id"]):
            raise HTTPException(400, "Publish category must be a YouTube category number.")
        out["category_id"] = v["category_id"]
    out.update({k: bool(v[k]) for k in PUBLISH_BOOLS if k in v})
    return out


def _app(v) -> dict:
    if not isinstance(v, dict):
        raise HTTPException(400, "app must be an object.")
    out: dict = {}
    for k in ("clip_min", "clip_target", "clip_max"):
        if k in v:
            x = v[k]
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
                raise HTTPException(400, "Clip lengths must be numbers.")
            out[k] = round(float(x), 1)
    lo, tg, hi = (out.get(k, config.load()[k]) for k in ("clip_min", "clip_target", "clip_max"))
    if out and (not 3 <= float(lo) < float(hi) <= 900 or (tg and not float(lo) <= float(tg) <= float(hi))):
        raise HTTPException(400, "Clip lengths need min < max (3–900 s), with the target between them.")
    if "clip_count" in v:
        x = v["clip_count"]
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise HTTPException(400, "Clip count must be a number.")
        out["clip_count"] = int(min(max(x, 1), 50))
    if "ai_routes" in v:
        from . import providers
        r = v["ai_routes"]
        if not isinstance(r, dict) or set(r) - {"find"} or ("find" in r and r["find"] not in providers.NAMES):
            raise HTTPException(400, f"The preset's AI routing names the provider for finding clips ({', '.join(providers.NAMES)}).")
        out["ai_routes"] = dict(r)
    return out


def _timed_ok(items: list, length: float, key: str = "t") -> list:
    """Items (effects, overlays, sections) that still start inside a clip of `length` s; the validator clamps their ends."""
    return [x for x in items if isinstance(x, dict) and isinstance(x.get(key), (int, float)) and x[key] < length - (0.04 if key == "s" else 0)]


def clean_parts(parts, length: float, project_id: str = "") -> dict:
    """Every known part checked by the validators clean_edit uses (unknown parts and keys are dropped, anything invalid
    is a 400). `length` bounds clip-time items; project_id is the project whose camera angles a region may show."""
    if not isinstance(parts, dict):
        raise HTTPException(400, "parts must be an object.")
    out, edit = {}, {}
    for part, values in parts.items():
        if part not in ALL_PARTS:
            continue
        if part == "publish":
            out[part] = _publish(values)
        elif part == "app":
            out[part] = _app(values)
        else:
            if not isinstance(values, dict):
                raise HTTPException(400, f"Preset part {part} must be an object.")
            edit.update({k: values[k] for k in PARTS[part] if k in values})
    if edit:
        clip = {"start": 0.0, "end": max(float(length), 0.5), "project_id": project_id}
        if not project_id:  # no project: no camera angle can be checked, so none is kept
            edit = _remap_sources(edit, set())
        if edit.get("framing", 0) is None:  # "no framing" (the default) is the absence of the key, not a value
            edit.pop("framing")
        if isinstance(edit.get("headline"), dict):  # the look only: each clip brings its own words
            edit["headline"] = {k: v for k, v in edit["headline"].items() if k not in ("text", "kicker")}
        cleaned = _srv().clean_edit(edit, clip)
        for part, keys in PARTS.items():
            out[part] = {k: cleaned[k] for k in keys if k in cleaned}
    return {p: v for p, v in out.items() if v}


def _remap_sources(edit: dict, have: set) -> dict:
    """Region camera-angle sources (the clip's and its sections') this project has no angle for are dropped."""
    strip = lambda rs: [{k: v for k, v in r.items() if not (k == "source" and v not in have)} if isinstance(r, dict) else r  # noqa: E731
                        for r in rs]
    if isinstance(edit.get("regions"), list):
        edit["regions"] = strip(edit["regions"])
    if isinstance(edit.get("layout_segments"), list):
        edit["layout_segments"] = [{**s, "regions": strip(s["regions"])} if isinstance(s, dict) and isinstance(s.get("regions"), list) else s
                                   for s in edit["layout_segments"]]
    return edit


# ---------- applying ----------

def _angles(proj: dict | None) -> set:
    return _srv().angle_ids(proj)


def _remap(edit: dict, preset: dict, proj: dict | None, length: float) -> dict:
    """The preset's edit for a clip of `length` s in `proj`: items past the clip's end, angles this project lacks and
    library assets since deleted are dropped; a music file from another project is copied into this one."""
    from . import pipeline
    edit = _remap_sources(json.loads(json.dumps(edit)), _angles(proj))
    if edit.get("layout_segments"):
        edit["layout_segments"] = _timed_ok(edit["layout_segments"], length, "s") or None
    if edit.get("effects"):
        edit["effects"] = _timed_ok(edit["effects"], length) or None
    if edit.get("overlays"):
        edit["overlays"] = [o for o in _timed_ok(edit["overlays"], length)
                            if o.get("kind") in ("text", "emoji") or (store.get("asset", str(o.get("asset_id") or "")) or {}).get("kind") == o.get("kind")] or None
    sc = edit.get("streamer_card")
    if sc and sc.get("avatar_asset_id") and not store.get("asset", str(sc["avatar_asset_id"])):
        sc["avatar_asset_id"] = None
    if edit.get("music"):
        dest = pipeline.pdir(proj["id"]) / edit["music"] if proj else None
        src = pipeline.pdir(preset["src_project"]) / edit["music"] if preset.get("src_project") else None
        if dest and not dest.exists() and src and src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        if not (dest and dest.exists()):
            edit.pop("music")
    return edit


def _edit_of(preset: dict) -> dict:
    return {k: v for part, vals in preset["parts"].items() if part in PARTS for k, v in vals.items()}


def edit_for(preset: dict, clip: dict) -> dict:
    """The PATCH edit body that applies the preset to this clip, checked by clean_edit for it."""
    proj = store.get("project", clip["project_id"])
    edit = _remap(_edit_of(preset), preset, proj, clip["end"] - clip["start"])
    hl = edit.get("headline")
    if hl and hl.get("on") and not (clip["edit"].get("headline") or {}).get("text"):
        text = media.headline_text(clip.get("hook") or clip.get("title"))
        if text:
            edit["headline"] = {**hl, "text": text}
    return _srv().clean_edit(edit, clip)


def new_clip_edit(proj: dict | None, length: float, preset_id: str | None = None) -> dict:
    """The default preset's edit (or preset_id's) for a new clip of `length` s, unchecked by clean_edit (its values were
    checked when saved); {} when there is none. pipeline.new_clip fills in the headline text and the card."""
    pid = preset_id or (store.get("pref", "preset_default") or {}).get("preset_id")
    preset = store.get("preset", pid) if pid else None
    if not preset:
        return {}
    try:
        return _remap(_edit_of(preset), preset, proj, length)
    except Exception as e:  # never stop a clip being made
        log.event("presets", f"preset {pid} not applied: {e}")
        return {}


# ---------- routes ----------

def _sorted(ps: list) -> list:
    return sorted(ps, key=lambda p: (not p.get("fav"), p.get("order", 1e9), -p.get("created", 0)))


@router.get("/api/presets")
def list_presets(project: str = ""):
    """Every preset (favourites first, then the order the creator set), the default for new clips and the part names."""
    ps = [p for p in store.find("preset") if p.get("scope", "global") in ("global", project or "global")]
    default = (store.get("pref", "preset_default") or {}).get("preset_id")
    return {"presets": _sorted(ps), "default": default if default and store.get("preset", default) else None, "parts": list(ALL_PARTS)}


@router.post("/api/presets")
def create_preset(body: dict):
    """{name, clip_id, parts: [names], color?, icon?, scope?, publish?: {...}}: the chosen parts of this clip (and of the
    app settings, for "app") saved as a preset. Only those parts are stored."""
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object.")
    clip = store.get("clip", str(body.get("clip_id") or ""))
    if not clip:
        raise HTTPException(404, "Clip not found.")
    want = [p for p in body.get("parts") or [] if p in ALL_PARTS] if isinstance(body.get("parts"), list) else []
    if not want:
        raise HTTPException(400, "Pick at least one part to keep in the preset.")
    e = {**media.DEFAULT_EDIT, **clip["edit"]}
    raw: dict = {}
    for part in want:
        if part == "publish":
            raw[part] = body.get("publish") or {}
        elif part == "app":
            s = config.load()
            raw[part] = {**{k: s[k] for k in APP_KEYS[:4]}, **({"ai_routes": {"find": s["ai_routes"]["find"]}} if s["ai_routes"].get("find") else {})}
        else:
            raw[part] = {k: e.get(k) for k in PARTS[part] if k in e}
    length = clip["end"] - clip["start"]
    parts = clean_parts(raw, length, clip["project_id"])
    if not parts:
        raise HTTPException(400, "Those parts are empty on this clip: nothing to keep.")
    scope = body.get("scope") or "global"
    if scope != "global" and not store.get("project", str(scope)):
        raise HTTPException(400, "scope must be \"global\" or a project id.")
    return store.put("preset", {"name": _name(body.get("name")), "color": _color(body.get("color")), "icon": _icon(body.get("icon")),
                                "fav": False, "scope": scope, "len": round(length, 3), "src_project": clip["project_id"], "parts": parts})


@router.patch("/api/presets/{pid}")
def patch_preset(pid: str, body: dict):
    """Rename, recolour, favourite (or order) one preset."""
    _404(store.get("preset", pid))
    patch: dict = {}
    if "name" in body:
        patch["name"] = _name(body["name"])
    if "color" in body:
        patch["color"] = _color(body["color"])
    if "icon" in body:
        patch["icon"] = _icon(body["icon"])
    if "fav" in body:
        patch["fav"] = bool(body["fav"])
    return store.update("preset", pid, patch)


@router.delete("/api/presets/{pid}")
def delete_preset(pid: str):
    store.delete("preset", pid)
    if (store.get("pref", "preset_default") or {}).get("preset_id") == pid:
        store.delete("pref", "preset_default")
    return {"ok": True}


@router.post("/api/presets/{pid}/duplicate")
def duplicate_preset(pid: str):
    p = _404(store.get("preset", pid))
    copy = {k: json.loads(json.dumps(v)) for k, v in p.items() if k not in ("id", "created", "order")}
    return store.put("preset", {**copy, "name": f"{p['name'][:55]} copy", "fav": False})


@router.put("/api/presets/order")
def reorder_presets(body: dict):
    """{ids: [...]}: the creator's order (favourites still come first)."""
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or len(ids) > 500:
        raise HTTPException(400, "ids must be a list of preset ids.")
    for i, pid in enumerate(ids):
        if isinstance(pid, str) and store.get("preset", pid):
            store.update("preset", pid, {"order": i})
    return list_presets()


@router.put("/api/presets/default")
def set_default(body: dict):
    """{preset_id}: new clips (manual, AI and Autopilot) start with this preset's parts; null stops that."""
    pid = body.get("preset_id") if isinstance(body, dict) else None
    if not pid:
        store.delete("pref", "preset_default")
        return {"default": None}
    _404(store.get("preset", str(pid)))
    store.put("pref", {"id": "preset_default", "preset_id": pid})
    return {"default": pid}


@router.get("/api/presets/{pid}/edit")
def preset_edit(pid: str, clip_id: str):
    """{edit, publish?, app?}: the PATCH body that applies the preset to this clip (the UI sends it through its own patch,
    so undo and the posted-clip lock work as for any edit)."""
    preset = _404(store.get("preset", pid))
    clip = store.get("clip", clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found.")
    if clip.get("parts"):
        raise HTTPException(400, "Montages take their parts' edits: apply the preset to those clips.")
    return {"edit": edit_for(preset, clip), **{k: preset["parts"][k] for k in OTHER if k in preset["parts"]}}


@router.post("/api/presets/{pid}/apply")
def apply_preset(pid: str, body: dict):
    """{clip_ids: [...]} or {project_id}, optional {parts: [names]}: the preset on each editable clip (posted, expired
    clips and montages are skipped and listed); publish defaults go on the clip, app settings are saved once."""
    preset = _404(store.get("preset", pid))
    srv = _srv()
    only = body.get("parts") if isinstance(body.get("parts"), list) else None
    use = {k: v for k, v in preset["parts"].items() if only is None or k in only}
    if "clip_ids" in body:
        ids = body["clip_ids"] if isinstance(body["clip_ids"], list) else []
    elif body.get("project_id"):
        ids = [c["id"] for c in store.find("clip", project_id=str(body["project_id"]))]
    else:
        raise HTTPException(400, "Name the clips (clip_ids) or the project (project_id) to apply the preset to.")
    if "app" in use:
        srv.put_settings(dict(use["app"]))
    updated, skipped = 0, []
    one = {**preset, "parts": {k: v for k, v in use.items() if k in PARTS}}
    for cid in ids[:2000]:
        c = store.get("clip", str(cid))
        if not c or c.get("parts") or c.get("expired") or srv.posted(c["id"]):
            skipped.append(cid)
            continue
        try:  # one clip that can't take it (posted meanwhile, a value its length refuses) is skipped, not fatal
            if one["parts"]:
                srv.patch_clip(c["id"], {"edit": edit_for(one, c)})
        except HTTPException as e:
            log.event("presets", f"preset {pid} skipped clip {c['id']}: {e.detail}")
            skipped.append(cid)
            continue
        if "publish" in use:
            store.update("clip", c["id"], {"publish_defaults": use["publish"]})
        updated += 1
    return {"updated": updated, "skipped": skipped}


@router.get("/api/presets/{pid}/export")
def export_preset(pid: str):
    """The preset as a JSON file to share: its name, look and parts, without file paths, local ids or project angles."""
    p = _404(store.get("preset", pid))
    parts = json.loads(json.dumps(p["parts"]))
    parts.get("audio", {}).pop("music", None)  # a file in one project's folder
    parts.get("publish", {}).pop("accounts", None)  # this PC's linked accounts
    if (parts.get("overlays") or {}).get("overlays"):  # library media is this PC's: text and emoji travel
        parts["overlays"]["overlays"] = [o for o in parts["overlays"]["overlays"] if o.get("kind") in ("text", "emoji")]
    for k in ("layout", "sections"):
        for r in (parts.get(k) or {}).get("regions") or []:
            r.pop("source", None)
        for s in (parts.get(k) or {}).get("layout_segments") or []:
            for r in s.get("regions") or []:
                r.pop("source", None)
    if (parts.get("card") or {}).get("streamer_card"):
        parts["card"]["streamer_card"].pop("avatar_asset_id", None)
    out = {"reelwright_preset": 1, "name": p["name"], "color": p.get("color", ""), "icon": p.get("icon", ""), "len": p.get("len"),
           "parts": {k: v for k, v in parts.items() if v}}
    name = re.sub(r"[^\w.-]+", "_", p["name"])[:40] or "preset"
    return Response(json.dumps(out, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}.rwpreset.json"'})


@router.post("/api/presets/import")
def import_preset(body: dict):
    """A preset file (from export): checked like any preset; music files and region camera angles never come in."""
    if not isinstance(body, dict) or body.get("reelwright_preset") != 1:
        raise HTTPException(400, "That isn't a Reelwright preset file.")
    try:
        size = len(json.dumps(body))
    except (TypeError, ValueError):
        raise HTTPException(400, "That preset file can't be read.")
    if size > MAX_BYTES:
        raise HTTPException(413, "That preset file is too big.")
    parts = body.get("parts")
    if not isinstance(parts, dict):
        raise HTTPException(400, "parts must be an object.")
    parts = json.loads(json.dumps(parts))
    if isinstance(parts.get("audio"), dict):
        parts["audio"].pop("music", None)
    if isinstance(parts.get("app"), dict):  # a shared file never picks which AI provider (and so whose bill) runs searches
        parts["app"].pop("ai_routes", None)
    if isinstance(parts.get("overlays"), dict) and isinstance(parts["overlays"].get("overlays"), list):  # another PC's
        parts["overlays"]["overlays"] = [o for o in parts["overlays"]["overlays"]                  # library: only ours
                                         if not isinstance(o, dict) or o.get("kind") in ("text", "emoji") or o.get("kind") not in
                                         ("image", "gif", "video", "sound") or store.get("asset", str(o.get("asset_id") or ""))]
    n = body.get("len")
    length = float(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) and 0.5 <= n <= 7200 else 600.0
    clean = clean_parts(parts, length)
    if not clean:
        raise HTTPException(400, "That preset has nothing Reelwright can use.")
    return store.put("preset", {"name": _name(body.get("name")), "color": _color(body.get("color")), "icon": _icon(body.get("icon")),
                                "fav": False, "scope": "global", "len": round(length, 3), "src_project": "", "parts": clean})


# ---------- the editors' tool panel layout (also kept in localStorage; this copy survives a reinstall) ----------

UI_EDITORS = ("studio", "editor", "longform")
_TOOL = re.compile(r"[a-z][a-z0-9_]{0,23}")


@router.get("/api/ui-layout")
def get_ui_layout():
    return (store.get("pref", "ui_layout") or {}).get("layout") or {}


@router.put("/api/ui-layout")
def put_ui_layout(body: dict):
    """{studio|editor|longform: {min, col: [tool ids], hide: [...], order: [...]}}; unknown editors and ids are dropped."""
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object.")
    with store._lock:  # read-modify-write: the editors save their own keys at about the same time
        return _put_ui_layout(body)


def _put_ui_layout(body: dict) -> dict:
    out = {**get_ui_layout()}
    for ed, v in body.items():
        if ed not in UI_EDITORS or not isinstance(v, dict):
            continue
        one: dict = {"min": bool(v.get("min"))}
        for k in ("col", "hide", "order"):
            ids = v.get(k) or []
            if not isinstance(ids, list) or len(ids) > 40:
                raise HTTPException(400, f"{k} must be a list of up to 40 tool ids.")
            one[k] = [i for i in ids if isinstance(i, str) and _TOOL.fullmatch(i)]
        out[ed] = one
    store.put("pref", {"id": "ui_layout", "layout": out})
    return out
