"""Where the video-heavy project folders live, and the space saver.

Storage folder: the creator may keep every project folder (source videos, renders, analysis) in a folder of their
choice, e.g. a big second drive; the database, settings, logs and asset library stay in config.DATA. Everything resolves
the folder through config.projects_dir(). A move copies every project, verifies every copy, switches the setting, then
deletes each original only after re-checking its copy; it is recorded in settings ("storage_move") so a move the app was
closed in finishes at the next start. A chosen folder that isn't there (an unplugged drive) is flagged and waited for,
never silently replaced: jobs that need project files hold (pipeline._wait_for_storage) and imports are refused.

Space saver ("retention_mode" delete | archive): past-due downloaded sources, clip renders and long-form exports are
deleted, or moved to archive folders under readable names with a sidecar JSON. Each item's clock:
  source            imported (or re-downloaded / re-imported)
  unfinished clip   created, or last rendered
  posted clip/long  last uploaded (a long form never posted: created or last rendered)
and no clock starts before the creator switched the space saver on ("retention_since"), so nothing already on disk goes
early. Never touched: anything pinned with Keep (the item or its whole project), anything with a pending post or a
queued/running job on its project. Analysis files, posters, clip docs, post history and learning feedback always stay.
Model Lab files (scope "training", off unless ticked; in the app data folder) go by the same days, in delete mode only:
see _prune_training.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, log, media, pipeline, store

DAY = 86400.0
RESERVE = 2 * 1024 ** 3   # a move or an archive copy never fills a drive to its last 2 GB
STARTUP_DELAY = 300       # the first clean-up after a start waits 5 minutes
MODES = ("off", "delete", "archive")
SCOPE_DEFAULTS = {"sources": True, "unfinished": True, "uploaded": True, "longform": True, "local_sources": False, "training": False}
PENDING = {"waiting_render", "queued", "scheduled", "held", "uploading", "needs_auth"}
ARCHIVE_KEYS = ("archive_uploaded_dir", "archive_unfinished_dir", "archive_other_dir")
APP_KEYS = ("storage_dir", "storage_move", "retention_since", "retention_at")  # set by the app, never through PUT /api/settings
ID_RE = re.compile(r"[0-9a-f]{12}")
MAX_IMPORT = 500

_move = {"active": False, "phase": "", "progress": 0.0, "message": "", "error": "", "from": "", "to": ""}
_move_lock = threading.Lock()
_boot = time.time()
_ran = False


class Skip(Exception):
    """An item that must not (or can't) be cleaned up now: nothing about it was changed."""


# ---------------- the folder ----------------

def root() -> Path:
    return config.projects_dir()


def missing() -> bool:
    return not root().is_dir()


def hold_reason() -> str:
    """Why project files can't be touched right now ("" = they can)."""
    if missing():
        return (f"The storage folder {root()} isn't available (is the drive connected?). Reconnect it, or pick another "
                "folder in Settings › Storage.")
    if config.load()["storage_move"]:
        return "Your projects are moving to a new storage folder; this continues when the move finishes."
    return ""


def check_writable() -> None:
    why = hold_reason()
    if why:
        raise RuntimeError(why)


def _inside(p: Path, q: Path) -> bool:
    return p == q or q in p.parents


def _existing(p: Path) -> Path:
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _free(p: Path) -> int:
    return shutil.disk_usage(_existing(p)).free


def _writable(p: Path) -> bool:
    """Can Reelwright make folders (and files in them) there? A folder test, not a file test: on Windows a user may create
    folders but not files in C:\\ itself, and a new storage folder only needs the former."""
    try:
        with tempfile.TemporaryDirectory(dir=_existing(p)) as d:
            (Path(d) / "probe").write_bytes(b"")
        return True
    except OSError:
        return False


def _system_dirs() -> list[Path]:
    names = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
    dirs = [Path(os.environ[n]) for n in names if os.environ.get(n)]
    if config.FROZEN:
        dirs.append(Path(sys.executable).parent)  # the app's own folder: replaced by every update
    return [d.resolve() for d in dirs]


def gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def validate(path, purpose: str = "storage") -> Path:
    """The folder as a resolved path, or ValueError saying what's wrong. Nothing is created. purpose "archive" skips the
    move-only checks (current folder, space for everything)."""
    raw = str(path or "").strip().strip('"')
    if not raw:
        raise ValueError("Choose a folder.")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("Enter the folder's full path, e.g. D:\\Reelwright.")
    p = p.resolve()
    if p.exists() and not p.is_dir():
        raise ValueError(f"{p} is a file, not a folder.")
    if any(_inside(p, d) for d in _system_dirs()):
        raise ValueError(f"{p} is inside a system folder: pick a folder of your own (e.g. on another drive).")
    if _inside(config.DATA, p) or (_inside(p, config.DATA) and p != config.DEFAULT_PROJECTS.resolve()):
        raise ValueError("That folder holds Reelwright's own data: pick another one.")
    cur = root().resolve()
    if purpose == "storage" and p == cur:
        raise ValueError("That's already the storage folder.")
    if _inside(p, cur) or _inside(cur, p):
        raise ValueError(f"Pick a folder that isn't inside the storage folder {cur} (or around it).")
    if not _writable(p):
        raise ValueError(f"Reelwright can't write to {_existing(p)}.")
    if purpose == "storage" and not missing():
        need = usage()["total"] + RESERVE
        if _free(p) < need:
            raise ValueError(f"Not enough free space there: {gb(_free(p))} free, {gb(need)} needed.")
    return p


def _setting(p: Path) -> str:
    return "" if p == config.DEFAULT_PROJECTS.resolve() else str(p)


def usage() -> dict:
    """Bytes Reelwright keeps in the storage folder: sources (videos + camera angles), renders (exports/), other; plus the
    live chat logs (chatlogs, in the app's data folder: not part of total, which is what a folder move copies)."""
    from . import chatlog
    out = {"sources": 0, "renders": 0, "other": 0}
    if not missing():
        for proj in store.find("project"):
            d = pipeline.pdir(proj["id"])
            for dp, _, files in os.walk(d):
                top = Path(dp).relative_to(d).parts[:1]
                for f in files:
                    try:
                        n = os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        continue
                    video = Path(f).suffix.lower() in media.VIDEO_EXT
                    kind = ("renders" if top == ("exports",) else
                            "sources" if (not top and f == proj.get("source")) or (top == ("angles",) and video) else "other")
                    out[kind] += n
    try:
        from .training import runner
        training = runner.usage()  # app data folder, like the chat logs: not part of total
    except OSError:  # a file removed mid-walk: shown next time
        training = {"jobs": 0, "exports": 0, "datasets": 0}
    return {**out, "total": sum(out.values()), "chatlogs": _chat_size(chatlog.DIR), "training": training}


def _chat_size(d: Path) -> int:
    try:
        return _size(d) if d.is_dir() else 0
    except OSError:  # a log pruned or rotated mid-walk: shown next time
        return 0


def status(with_usage: bool = False) -> dict:
    gone = missing()
    st = {"path": str(root()), "default": str(config.DEFAULT_PROJECTS), "custom": bool(config.load()["storage_dir"]),
          "missing": gone, "free": None if gone else _free(root()), "data": str(config.DATA), "move": dict(_move)}
    if with_usage:
        st["usage"] = usage()
    return st


# ---------------- moving the folder ----------------

def _copy_file(a: Path, b: Path) -> None:
    shutil.copy2(a, b)


def _copy_verified(a: Path, b: Path) -> None:
    """Copy a to b through b.part: b only ever appears complete and the same size as a."""
    tmp = b.with_name(b.name + ".part")
    try:
        _copy_file(a, tmp)
        if tmp.stat().st_size != a.stat().st_size:
            raise OSError(f"The copy of {a.name} came out the wrong size.")
        os.replace(tmp, b)
    finally:
        tmp.unlink(missing_ok=True)


def _sync(src: Path, dst: Path, on=lambda n: None) -> None:
    """Copy every file of src that dst lacks (or has at another size): a resumed move redoes only what's missing."""
    for dp, _, files in os.walk(src):
        out = dst / Path(dp).relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        for f in files:
            a, b = Path(dp) / f, out / f
            n = a.stat().st_size
            if not (b.is_file() and b.stat().st_size == n):
                _copy_verified(a, b)
            on(n)


def _same(src: Path, dst: Path) -> bool:
    for dp, _, files in os.walk(src):
        for f in files:
            a = Path(dp) / f
            try:
                if (dst / a.relative_to(src)).stat().st_size != a.stat().st_size:
                    return False
            except OSError:
                return False
    return True


def _size(p: Path) -> int:
    return sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(p) for f in fs)


def start_move(dest) -> threading.Thread | None:
    """Move every project folder to dest in the background (status()["move"] has the progress). Refused while jobs that
    touch project files are queued or running. If the current folder isn't connected nothing can be copied: the setting
    just switches (switching back later finds the old projects again) and None is returned."""
    with _move_lock:
        if _move["active"] or config.load()["storage_move"]:
            raise RuntimeError("A move is already running.")
        p = validate(dest)
        if missing():
            p.mkdir(parents=True, exist_ok=True)
            old = root()
            config.save({"storage_dir": _setting(p)})
            log.event("storage", f"switched to {p}; the old folder {old} isn't connected, so nothing was moved")
            return None
        config.save({"storage_move": {"from": str(root().resolve()), "to": str(p), "phase": "copy"}})  # jobs hold from here
        busy = [j for j in store.find("job") if j["status"] in ("queued", "running") and j["kind"] not in pipeline.NO_MEDIA]
        if busy or any(u.get("status") == "uploading" for u in store.find("upload")):
            config.save({"storage_move": {}})
            raise RuntimeError("Wait until renders, imports and uploads finish (see Activity), then move the folder.")
        return _spawn()


def resume() -> threading.Thread | None:
    """At startup: finish a move the app was closed (or crashed) in the middle of."""
    with _move_lock:
        if config.load()["storage_move"] and not _move["active"]:
            return _spawn()
    return None


def _spawn() -> threading.Thread:
    _move.update(active=True, error="", progress=0.0, message="Starting the move")
    t = threading.Thread(target=_run_move, daemon=True)
    t.start()
    return t


def _run_move() -> None:
    m = dict(config.load()["storage_move"])
    src, dst = Path(m["from"]), Path(m["to"])
    _move.update(phase=m["phase"], **{"from": str(src), "to": str(dst)})
    try:
        if m["phase"] == "copy" and not src.is_dir():  # resumed with the old drive unplugged: never switch to an empty folder
            raise OSError(f"The old storage folder {src} isn't available, so nothing was moved. Reconnect it and try again.")
        ids = [p["id"] for p in store.find("project") if ID_RE.fullmatch(p["id"]) and (src / p["id"]).is_dir()]
        if m["phase"] == "copy":
            total, done = sum(_size(src / i) for i in ids) or 1, 0

            def on(n):
                nonlocal done
                done += n
                _move.update(progress=round(0.95 * done / total, 4))
            dst.mkdir(parents=True, exist_ok=True)
            for k, i in enumerate(ids):
                _move["message"] = f"Copying project {k + 1} of {len(ids)}"
                _sync(src / i, dst / i, on)
            bad = [i for i in ids if (src / i).is_dir() and not _same(src / i, dst / i)]
            if bad:
                raise OSError(f"{len(bad)} copied project(s) don't match their originals; nothing was deleted.")
            m["phase"] = "cleanup"  # every copy verified: switch, then remove the originals
            config.save({"storage_dir": _setting(dst.resolve()), "storage_move": m})
            log.event("storage", f"switched the storage folder to {dst} ({len(ids)} projects copied and verified)")
        _move.update(phase="cleanup", message="Removing the old copies")
        left = []
        for i in ids:
            if not (src / i).is_dir():
                continue
            try:
                _sync(src / i, dst / i)  # anything written to the old folder since the copy goes along first
                if not _same(src / i, dst / i):
                    raise OSError("copy differs")
                shutil.rmtree(src / i)
            except OSError:
                left.append(i)
        config.save({"storage_move": {}})
        msg = f"Moved {len(ids)} project(s) to {dst}."
        if left:
            msg += f" {len(left)} old folder(s) in {src} couldn't be removed (in use?): delete them by hand."
        _move.update(active=False, progress=1.0, message=msg)
        log.event("storage", msg)
    except Exception as e:
        # before the switch: still on the old folder, nothing deleted; after it: everything is verified in the new one and
        # only old copies may be left. Either way jobs must stop waiting for the move.
        config.save({"storage_move": {}})
        _move.update(active=False, error=str(e) or type(e).__name__, message="")
        log.error("storage", f"moving the storage folder to {dst} failed")


# ---------------- the space saver ----------------

def _ts(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def policy() -> dict:
    s = config.load()
    try:
        days = max(0.0, float(s["retention_days"]))
    except (TypeError, ValueError):
        days = 0.0
    since = _ts(s["retention_since"])  # 0 = lost or never stamped: off until saved again (check_settings restamps it)
    mode = s["retention_mode"] if s["retention_mode"] in ("delete", "archive") and days > 0 and since else ""
    return {"mode": mode, "days": days, "since": since, "s": s,
            "scope": {**SCOPE_DEFAULTS, **{k: bool(v) for k, v in (s["retention_scope"] or {}).items() if k in SCOPE_DEFAULTS}}}


def source_url(p: dict) -> str | None:
    """The link a project's source came from (URL import or channel scan): it can be downloaded again."""
    return p.get("source_url") or (p.get("origin") or {}).get("url")


def _items(pol: dict, project_ids=None) -> list[dict]:
    """Everything the policy covers, each with its due time; pinned, out-of-scope and already removed items left out."""
    scope, out = pol["scope"], []
    if project_ids:  # the Editor's poll: its own project, not the whole library
        projects = {p["id"]: p for p in filter(None, (store.get("project", pid) for pid in project_ids))}
    else:
        projects = {p["id"]: p for p in store.find("project")}
    done_at: dict[str, list] = {}  # clip / long-form id -> when each of its done posts went out (fields: no whole uploads)
    for _, lid, cid, status, at in store.fields("upload", "longform_id", "clip_id", "status", "uploaded_at"):
        if status == "done":
            done_at.setdefault(lid or cid, []).append(_ts(at))

    def add(kind, doc, proj, bucket, clock):
        out.append({"kind": kind, "id": doc["id"], "project_id": proj["id"], "bucket": bucket,
                    "title": doc.get("title") or doc.get("name") or "Untitled",
                    "due": max(clock, pol["since"]) + pol["days"] * DAY})

    for p in projects.values():
        if p.get("keep") or not p.get("source") or p.get("source_removed") or p.get("status") not in ("ready", "failed"):
            continue
        if scope["sources" if source_url(p) else "local_sources"]:
            add("source", p, p, "other", max(_ts(p.get("created")), _ts(p.get("source_at"))))
    names = ("project_id", "keep", "expired", "export", "created", "title", "name")  # what a due date needs, not the edit
    for kind in ("clip", "longform"):
        rows = ([r for pid in projects for r in store.fields(kind, *names, project_id=pid)] if project_ids
                else store.fields(kind, *names))
        for did, pid, keep, expired, export, created, title, name in rows:
            p, export = projects.get(pid), export or {}
            if not p or p.get("keep") or keep or expired or not export.get("file"):
                continue
            d, done = {"id": did, "title": title, "name": name}, done_at.get(did, [])
            made = max(_ts(created), _ts(export.get("at")))
            if kind == "longform":
                if scope["longform"]:
                    add(kind, d, p, "other", max(done or [made]))
            elif done and scope["uploaded"]:
                add(kind, d, p, "uploaded", max(done) or made)
            elif not done and scope["unfinished"]:
                add(kind, d, p, "unfinished", made)
    return out


def _blocked(it: dict) -> str:
    """Why an item must not be touched now ("" = it may)."""
    pid = it["project_id"]
    if pipeline._project_jobs(pid):
        return "A job is queued or running for this project."
    pending = [u for u in store.find("upload", project_id=pid) if u.get("status") in PENDING]
    if it["kind"] == "source":
        return "A post from this project is still waiting to go out." if pending else ""
    key = "longform_id" if it["kind"] == "longform" else "clip_id"
    for u in pending:
        if u.get(key) == it["id"] or (it["kind"] == "clip" and it["id"] in ((store.get("clip", u.get("clip_id") or "") or {}).get("parts") or [])):
            return "Its post is still waiting to go out."
    return ""


def _archive_dir(it: dict, s: dict) -> Path:
    key = {"uploaded": "archive_uploaded_dir", "unfinished": "archive_unfinished_dir"}.get(it["bucket"])
    raw = s[key] if key else (s["archive_other_dir"] or s["archive_uploaded_dir"])
    label = {"uploaded": "uploaded clips", "unfinished": "unfinished clips"}.get(it["bucket"], "sources and long forms")
    if not raw:
        raise Skip(f"Choose the archive folder for {label} in Settings › Storage (nothing is deleted instead).")
    p = Path(raw)
    if not p.is_dir():
        raise Skip(f"The archive folder {p} isn't available (is the drive connected?).")
    return p


def _files(it: dict, scope: dict) -> tuple[list[tuple[Path, str]], list[Path]]:
    """(files to delete or archive with their name tag, files only deleted) for an item; only paths inside its project.
    A source takes its downloaded camera angles along; angles the creator recorded or added are originals (local scope)."""
    from . import multicam
    d = pipeline.pdir(it["project_id"])
    if it["kind"] == "source":
        p = store.get("project", it["id"])
        keep = [(d / p["source"], "source")]
        for a in multicam.angles_of(p):
            if not (a.get("source_url") or scope["local_sources"]):
                continue
            f = multicam.angle_path(p["id"], a)
            if f and f.is_file():
                keep.append((f, f"angle {a.get('name') or ''}".strip()))
        prox = d / Path(p.get("proxy") or "proxy.mp4").name  # the preview copy (pipeline._proxy) only goes with it
        return [(f, t) for f, t in keep if f.is_file()], [prox] if prox.is_file() else []
    exp = (store.get(it["kind"], it["id"]) or {}).get("export") or {}

    def inside(rel):  # a render path can never reach outside its project folder
        f = (d / rel).resolve() if rel else None
        return f if f and f.is_file() and d.resolve() in f.parents else None
    render, cover = inside(exp.get("file")), inside(exp.get("cover"))
    return ([(render, "")] if render else []), ([cover] if cover else [])


def _clean(s) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(s or "")).strip().rstrip(". ")
    return re.sub(r"\s+", " ", s)[:90].strip() or "Untitled"


def _unique(folder: Path, base: str, exts: tuple) -> str:
    name, n = base, 1
    while any((folder / f"{name}{e}").exists() or (folder / f"{name}{e}.part").exists() for e in exts):
        n += 1
        name = f"{base} ({n})"
    return name


def _sidecar(it: dict, files: list[str], now: float) -> dict:
    doc = store.get("project" if it["kind"] == "source" else it["kind"], it["id"]) or {}
    proj = store.get("project", it["project_id"]) or {}
    ups = [u for u in store.find("upload", project_id=it["project_id"]) if u.get("status") == "done" and
           (u.get("clip_id") == it["id"] or u.get("longform_id") == it["id"])]
    return {"title": it["title"], "kind": it["kind"], "project": proj.get("name"), "source_url": source_url(proj),
            "caption": next((u.get("caption") or u.get("description") for u in ups if u.get("caption") or u.get("description")), doc.get("hook", "")),
            "hook": doc.get("hook", ""), "hashtags": doc.get("hashtags", []), "start": doc.get("start"), "end": doc.get("end"),
            "posts": [{"platform": u.get("platform"), "url": u.get("url"), "title": u.get("title"), "uploaded_at": u.get("uploaded_at")} for u in ups],
            "files": files, "archived_at": _iso(now), "reelwright_id": it["id"]}


def _archive(it: dict, files: list[tuple[Path, str]], s: dict, now: float) -> list[Path]:
    """Copy (verified) into the archive folder under readable names, plus a sidecar JSON; returns what was written."""
    folder = _archive_dir(it, s)
    need = sum(f.stat().st_size for f, _ in files)
    if _free(folder) < need + RESERVE:
        raise Skip(f"Not enough free space in the archive folder {folder}.")
    ups = [u for u in store.find("upload", project_id=it["project_id"]) if u.get("status") == "done" and
           (u.get("clip_id") == it["id"] or u.get("longform_id") == it["id"])]
    doc = store.get("project" if it["kind"] == "source" else it["kind"], it["id"]) or {}
    clock = max([_ts(u.get("uploaded_at")) for u in ups] or [_ts((doc.get("export") or {}).get("at")) or _ts(doc.get("created"))])
    date = datetime.fromtimestamp(clock or now).strftime("%Y-%m-%d")
    tags = sorted({u.get("platform") for u in ups if u.get("platform")})
    tags = (["long form"] if it["kind"] == "longform" else []) + tags
    written: list[Path] = []
    try:
        for f, tag in files:
            t = ", ".join(([tag] if tag else []) + tags)
            base = _unique(folder, _clean(f"{date} {_clean(it['title'])}") + (f" [{_clean(t)}]" if t else ""), (f.suffix, ".json"))
            dst = folder / f"{base}{f.suffix}"
            _copy_verified(f, dst)
            written.append(dst)
        side = written[0].with_suffix(".json")
        side.write_text(json.dumps(_sidecar(it, [w.name for w in written], now), indent=2, ensure_ascii=False), "utf-8")
        written.append(side)
    except Exception:
        for w in written:
            w.unlink(missing_ok=True)
        raise
    return written


def _expire(it: dict, pol: dict, now: float) -> dict:
    """Delete or archive one item's files, then mark its doc. Skip (nothing changed) when it must not or can't."""
    files, extra = _files(it, pol["scope"])
    if not files:
        raise Skip("Nothing on disk to remove.")
    written = _archive(it, files, pol["s"], now) if pol["mode"] == "archive" else []
    kind = "project" if it["kind"] == "source" else it["kind"]
    gone, size, problem = [], 0, ""
    try:
        with store._lock:  # renders / uploads claim work under this lock: nothing can start between the check and the delete
            doc = store.get(kind, it["id"])
            why = _blocked(it) or ("" if doc and not doc.get("keep") and not doc.get("expired") and not doc.get("source_removed")
                                   else "Changed meanwhile.")
            if why:
                raise Skip(why)
            for f in [f for f, _ in files] + extra:  # the main file first: if it can't go, nothing has
                try:
                    n = f.stat().st_size
                    f.unlink()
                except OSError as e:  # in use (e.g. open in a player)
                    if not gone:
                        raise Skip(f"In use, retried next time: {e}") from None
                    problem = f"{f.name} couldn't be removed ({e})"
                    continue
                gone.append(f)
                size += n
            mark = {"at": _iso(now), "mode": pol["mode"], "archive": str(written[0]) if written else None, "bytes": size,
                    **({"error": problem} if problem else {})}
            store.update(kind, it["id"], {"source_removed": mark, "proxy": None} if kind == "project" else {"expired": mark, "export": None})
    except Exception:
        if not gone:  # nothing was deleted: the archive copies would only be duplicates
            for w in written:
                w.unlink(missing_ok=True)
        raise
    log.event("retention", f"{'archived' if written else 'deleted'} {it['kind']} {it['id']} of project {it['project_id']} "
                           f"({it['title']!r}, {size / 1e6:.1f} MB){' to ' + str(written[0]) if written else ''}{'; ' + problem if problem else ''}")
    return {**_brief(it), "bytes": size, "archive": mark["archive"]}


def _brief(it: dict) -> dict:
    return {k: it[k] for k in ("kind", "id", "project_id", "title", "bucket")} | {"due": _iso(it["due"])}


def preview(now: float | None = None) -> dict:
    """What a clean-up would do right now (the "Clean up now" confirmation), and what it skips and why."""
    pol, now = policy(), now or time.time()
    out = {"mode": pol["mode"], "items": [], "blocked": []}
    if not pol["mode"]:
        return out
    for it in [i for i in _items(pol) if i["due"] <= now]:
        files, extra = _files(it, pol["scope"])
        why = _blocked(it)
        if not why and pol["mode"] == "archive" and files:
            try:
                _archive_dir(it, pol["s"])
            except Skip as e:
                why = str(e)
        size = sum(f.stat().st_size for f in [f for f, _ in files] + extra)
        (out["blocked"] if why else out["items"]).append({**_brief(it), "bytes": size, "reason": why})
    return out


def cleanup(job: dict | None = None, now: float | None = None) -> dict:
    """Clean up everything due. Never raises for one item: it is skipped (with the reason) and retried next run."""
    pol, now = policy(), now or time.time()
    done, skipped = [], []
    if pol["mode"]:
        try:
            _prune_chatlogs(pol, now)  # in the app's data folder: not held by a missing storage drive
        except OSError:
            log.error("retention", "chat logs")
        if pol["scope"]["training"]:
            try:
                _prune_training(pol, now)  # also in the app's data folder
            except OSError:
                log.error("retention", "training files")
    if not pol["mode"] or hold_reason():
        return {"done": done, "skipped": skipped}
    due = [i for i in _items(pol) if i["due"] <= now]
    for n, it in enumerate(due):
        try:
            why = _blocked(it)
            if why:
                raise Skip(why)
            done.append(_expire(it, pol, now))
        except Skip as e:
            skipped.append({**_brief(it), "reason": str(e)})
        except Exception as e:
            log.error("retention", f"{it['kind']} {it['id']}")
            skipped.append({**_brief(it), "reason": str(e)[:300] or type(e).__name__})
        if job:
            pipeline._progress(job["id"], (n + 1) / len(due), f"Cleaned up {len(done)} of {len(due)}")
    config.save({"retention_at": pipeline.now_iso()})
    if job:
        freed = sum(d["bytes"] for d in done)
        store.update("job", job["id"], {"message": f"{'Archived' if pol['mode'] == 'archive' else 'Freed'} {gb(freed)} "
                                                   f"({len(done)} item(s)){f'; {len(skipped)} skipped' if skipped else ''}"})
    return {"done": done, "skipped": skipped}


def _prune_chatlogs(pol: dict, now: float) -> None:
    """Delete live chat logs (chatlog.py, one <UTC date>.jsonl per channel and day) past the space saver's days, counted
    from the end of their day and never before the space saver was switched on. Deleted in archive mode too."""
    from . import chatlog
    gone = 0
    for f in chatlog.DIR.glob("*/*.jsonl") if chatlog.DIR.is_dir() else []:
        try:
            end = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + DAY
        except ValueError:
            continue
        if max(end, pol["since"]) + pol["days"] * DAY <= now:
            try:
                f.unlink()
                gone += 1
            except OSError:  # open (today's capture) or locked: next run
                pass
    if gone:
        log.event("retention", f"deleted {gone} chat log(s) older than {pol['days']:.0f} days")


def _prune_training(pol: dict, now: float) -> None:
    """Model Lab files past the space saver's days, in delete mode only (archive mode moves nothing out of Model Lab,
    so it deletes nothing there either): finished jobs' folders (checkpoints, adapters, GGUF exports) and their records,
    then dataset folders no remaining job uses (their records stay for lineage; the newest build always stays). Never a
    queued/running/paused job, nor one any release record points at (a candidate not yet released included: its files
    are what Release loads), nor the active release or the one Roll back returns to (runner.delete_job refuses those)."""
    from .training import dataset, runner
    if pol["mode"] != "delete":
        return
    due = lambda t: max(t, pol["since"]) + pol["days"] * DAY <= now  # noqa: E731
    rels = store.find("train_release")
    keep = {(r.get("manifest") or {}).get("train_job") for r in rels}
    gone = 0
    for j in store.find("train_job"):
        if j["status"] in runner.LIVE or j["id"] in keep or not due(max(_ts(j.get("created")), _ts(j.get("finished")))):
            continue
        try:
            gone += runner.delete_job(j["id"])["bytes"]
        except (RuntimeError, ValueError, LookupError):  # live, protected or in use: kept (retried next run)
            continue
    used = {(j.get("result") or {}).get("dataset") for j in store.find("train_job")} | \
        {(r.get("hashes") or {}).get("dataset") for r in rels}
    for d in store.find("train_dataset")[1:]:  # newest first: the latest build stays
        folder = dataset.root() / d["id"][:16]
        stamp = folder / "manifest.json"
        if d["id"] in used or not d["id"].isalnum() or not folder.is_dir():
            continue
        if due(max(_ts(d.get("created")), stamp.stat().st_mtime if stamp.is_file() else 0)):
            gone += runner.size(folder)
            shutil.rmtree(folder)
    if gone:
        log.event("retention", f"deleted {gb(gone)} of old training files")


def tick() -> None:
    """From the scheduler loop: one clean-up a few minutes after startup, then one a day."""
    global _ran
    pol = policy()
    if not pol["mode"] or time.time() - _boot < STARTUP_DELAY or hold_reason() or pipeline._busy("cleanup"):
        return
    if _ran and _ts(pol["s"]["retention_at"]) > time.time() - DAY:
        return
    _ran = True
    pipeline.submit("cleanup")


def due_map(project_ids=None) -> dict:
    """(kind, id) -> {"at", "action"}: the dates the Library, Editor, Studio and Pipeline show."""
    pol = policy()
    if not pol["mode"]:
        return {}
    return {(it["kind"], it["id"]): {"at": _iso(it["due"]), "action": pol["mode"]} for it in _items(pol, project_ids)}


def project_view(p: dict, dues: dict) -> dict:
    """What the Library / Editor show about a project's source."""
    f = pipeline.pdir(p["id"]) / p["source"] if p.get("source") else None
    state = ("removed" if p.get("source_removed") else "ok" if f and f.is_file() else
             "missing" if p.get("status") == "ready" else "none")
    return {"source_state": state, "can_redownload": bool(source_url(p)), "source_due": dues.get(("source", p["id"]))}


def check_settings(patch: dict) -> None:
    """Validate the space-saver keys of PUT /api/settings in place (ValueError = 400). Switching the space saver on
    stamps retention_since: no item's clock starts before that, so nothing already on disk goes early."""
    for k in APP_KEYS:
        patch.pop(k, None)
    s = config.load()
    if "retention_mode" in patch and patch["retention_mode"] not in MODES:
        raise ValueError("The space saver is off, delete or archive.")
    if "retention_days" in patch:
        try:
            patch["retention_days"] = int(patch["retention_days"])
            assert 0 <= patch["retention_days"] <= 3650
        except (TypeError, ValueError, AssertionError):
            raise ValueError("Days must be a whole number from 0 (never) to 3650.") from None
    if "retention_scope" in patch:
        sc = patch["retention_scope"]
        if not isinstance(sc, dict) or not set(sc) <= set(SCOPE_DEFAULTS):
            raise ValueError(f"The space saver covers {', '.join(SCOPE_DEFAULTS)}.")
        patch["retention_scope"] = {**(s["retention_scope"] or {}), **{k: bool(v) for k, v in sc.items()}}
    for k in ARCHIVE_KEYS:
        if patch.get(k):
            p = validate(patch[k], "archive")
            p.mkdir(parents=True, exist_ok=True)
            patch[k] = str(p)
        elif k in patch:
            patch[k] = ""
    on = lambda d: d.get("retention_mode") in ("delete", "archive") and int(d.get("retention_days") or 0) > 0  # noqa: E731
    if on({**s, **patch}) and (not on(s) or not _ts(s["retention_since"])):
        patch["retention_since"] = pipeline.now_iso()


def start() -> None:
    """At app start: resume an unfinished move; give old URL imports their link (so a removed source can be re-downloaded)."""
    resume()
    for p in store.find("project"):
        if not source_url(p):
            url = next((j.get("url") for j in store.find("job", kind="import_url", project_id=p["id"]) if j.get("url")), None)
            if url:
                store.update("project", p["id"], {"source_url": url})


# ---------------- re-import a removed source ----------------

def reimport(p: dict, src: Path, filename: str) -> dict:
    """Put a removed source back from a file the creator picks (src: it, already received into the storage folder; the
    caller removes it if this refuses). It must be the same video (same length), so every clip's times still line up."""
    if not p.get("source_removed"):
        raise ValueError("This project still has its source video.")
    check_writable()
    dst = pipeline.pdir(p["id"]) / f"source{Path(src).suffix.lower()}"
    try:
        meta = media.probe(src)
    except Exception:
        raise ValueError("That file isn't a readable video.") from None
    want = float((p.get("meta") or {}).get("duration") or 0)
    if want and abs(float(meta["duration"]) - want) > 1.5:
        raise ValueError(f"That video is {meta['duration']:.0f} s long and the original was {want:.0f} s: choose the "
                         "same video, so your clips still line up.")
    os.replace(src, dst)
    log.event("storage", f"re-imported the source of project {p['id']} from {filename}")
    return store.update("project", p["id"], {"source": dst.name, "source_removed": None, "source_at": pipeline.now_iso(),
                                             "meta": {**(p.get("meta") or {}), **meta}})


# ---------------- importing old shorts ----------------

SHORT_ORIGIN = "imported_short"


def file_hash(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _duplicate(h: str) -> dict | None:
    return next((p for p in store.find("project") if p.get("source_hash") == h), None)


def import_short_paths(paths: list, video_type: dict | None = None) -> dict:
    """Shorts already on this PC (files and/or whole folders): one project each, copied in and analysed one at a time on
    the render queue. Same content as a project already here (by hash) = skipped as a duplicate.
    ponytail: hashes in the request, fine for a folder of shorts; hash in the job if people import huge folders."""
    check_writable()
    files, skipped, mine = [], [], root().resolve()
    for raw in paths if isinstance(paths, list) else []:
        p = Path(str(raw))
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in media.VIDEO_EXT)
        elif p.is_file() and p.suffix.lower() in media.VIDEO_EXT:
            files.append(p)
        else:
            skipped.append({"path": str(raw), "reason": "Not a video file or folder."})
    skipped += [{"path": str(f), "reason": "Already in Reelwright's storage folder."} for f in files if _inside(f.resolve(), mine)]
    files = [f for f in files if not _inside(f.resolve(), mine)]
    added, dups, seen = [], [], {}
    for f in files[:MAX_IMPORT]:
        h = file_hash(f)
        dup = seen.get(h) or _duplicate(h)
        if dup:
            dups.append({"name": f.name, "path": str(f), "id": dup["id"], "of": dup.get("name")})
            continue
        proj = pipeline.new_project(f.stem, None, origin={"kind": SHORT_ORIGIN, "file": f.name}, video_type=video_type)
        proj = store.update("project", proj["id"], {"source_hash": h, "stage": "Queued to copy in"})
        pipeline.submit("ingest_file", project_id=proj["id"], path=str(f), stable=True)
        seen[h] = proj
        added.append({"id": proj["id"], "name": proj["name"]})
    skipped += [{"path": str(f), "reason": f"More than {MAX_IMPORT} files at once."} for f in files[MAX_IMPORT:]]
    log.event("import", f"shorts: {len(added)} added, {len(dups)} duplicates, {len(skipped)} skipped")
    return {"added": added, "duplicates": dups, "skipped": skipped}


def import_short_upload(src: Path, filename: str, digest: str, video_type: dict | None = None) -> dict:
    """One short uploaded from the browser (src: already received into the storage folder, hashed on the way in; the
    caller removes it if it isn't moved): a duplicate never becomes a project."""
    dup = _duplicate(digest)
    if dup:
        return {"duplicate": {"id": dup["id"], "name": dup.get("name"), "file": filename}}
    p = pipeline.new_project(Path(filename).stem, None, origin={"kind": SHORT_ORIGIN, "file": Path(filename).name},
                             video_type=video_type)
    return {"project": adopt_upload(p, src, source_hash=digest)}


def adopt_upload(p: dict, src: Path, **fields) -> dict:
    """Move a received upload into its new project (a rename: same folder tree) and queue the analysis. If the move
    fails the half-made project goes too, never left "importing" forever."""
    ext = Path(src).suffix.lower()
    try:
        os.replace(src, pipeline.pdir(p["id"]) / f"source{ext}")
    except OSError:
        pipeline.delete_project(p["id"])
        raise
    store.update("project", p["id"], {"source": f"source{ext}", "status": "queued", "stage": "Queued for analysis", **fields})
    pipeline.submit("analyze", project_id=p["id"])
    return store.get("project", p["id"])
