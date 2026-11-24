"""The Models page: catalog search and sync, hardware fit, compare, install (Ollama pull), benchmarks, and the per-task
selection / release pointer (reelwright/models). Long runs (pull, benchmark, measure) go to threads; /api/models/jobs polls."""
import math
import threading

from fastapi import APIRouter, HTTPException, Request

from . import config, log, store
from .models import catalog, fit, hardware, selections

router = APIRouter(on_startup=[lambda: _safe(selections.migrate)])  # Settings → AI becomes each task's first release
JOBS: dict[str, dict] = {}  # "bench:<variant>" / "measure:<selection>" -> {state, error, result}


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:
        log.event("models", f"{getattr(fn, '__name__', fn)} failed: {e}")
        return None


def _call(fn, *a, **k):
    """Domain errors as HTTP: bad input 400, a stale release revision 409, an unreachable service 502."""
    try:
        return fn(*a, **k)
    except selections.Conflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except (OSError, RuntimeError) as e:
        raise HTTPException(502, str(e)[:300])
    except Exception as e:  # httpx errors and the like: say what failed without a stack trace
        raise HTTPException(502, f"{type(e).__name__}: {str(e)[:300]}")


def _workload(d) -> dict:
    w = {}
    try:
        if d.get("op"):
            w["op"] = str(d["op"])
        for k, lo, hi in (("context", 256, 1 << 21), ("concurrency", 1, 64)):
            if d.get(k) not in (None, ""):
                w[k] = min(max(int(float(d[k])), lo), hi)
    except (TypeError, ValueError):
        raise HTTPException(400, "context and concurrency must be numbers.")
    if d.get("kv_dtype"):
        w["kv_dtype"] = str(d["kv_dtype"])
    if d.get("placement"):
        w["placement"] = str(d["placement"])
    if w.get("op", "run") not in fit.OPS or w.get("kv_dtype", "f16") not in fit.KV_BYTES or w.get("placement", "gpu") not in ("gpu", "split", "cpu"):
        raise HTTPException(400, f"op is {'/'.join(fit.OPS)}, kv_dtype {'/'.join(fit.KV_BYTES)}, placement gpu/split/cpu.")
    return w


def _profile(name=None) -> dict:
    return catalog.current_profile(name or None)


_jobs_lock = threading.Lock()


def _bg(key: str, fn, *a) -> dict:
    with _jobs_lock:  # one run per key, even when two clicks arrive together
        if JOBS.get(key, {}).get("state") == "running":
            return JOBS[key]
        JOBS[key] = st = {"key": key, "state": "running", "error": "", "result": None}

    def run():
        try:
            st.update(state="done", result=fn(*a))
        except Exception as e:
            st.update(state="failed", error=str(e)[:400])
            log.event("models", f"{key} failed: {e}")
    threading.Thread(target=run, daemon=True, name=key).start()
    return st


# ---------- catalog ----------

@router.get("/api/models/catalog")
def get_catalog(request: Request):
    q = dict(request.query_params)
    if not catalog.entries():  # first visit: what's on this PC plus the curated picks (no Hub call until asked)
        _safe(catalog.sync_ollama)
        _safe(catalog.load_manifest)
        _safe(catalog.sync_remote)
    facets = {k: q[k] for k in ("task", "input", "output", "quant", "min_ctx", "min_params", "max_params", "source") if q.get(k)}
    if q.get("installed") in ("1", "true"):
        facets["installed"] = True
    if q.get("fit") in ("1", "true"):
        facets["fit"] = True
    prof = _profile(q.get("profile"))
    vs = _call(catalog.search, q.get("q", ""), facets, prof, _workload(q), q.get("sort") or "relevance")
    for v in vs:
        v.pop("_score", None)
    return {"groups": catalog.groups(vs), "count": len(vs), "profile": _summary(prof),
            "hf": max((c.get("synced_at", "") for c in store.find("hf_cache")), default="")}


def _summary(p: dict) -> dict:
    return {k: p.get(k) for k in ("name", "reserve_gib", "budget_gib", "gpus", "telemetry", "driver", "ram_total_gib", "ram_free_gib",
                                  "disk_free_gib", "loaded", "at", "stale")}


@router.post("/api/models/sync")
def sync(body: dict):
    src = body.get("source")
    if src == "ollama":
        n = len(_call(catalog.sync_ollama))
        _safe(catalog.sync_remote)
        return {"count": n}
    if src == "manifest":
        return {"count": len(_call(catalog.load_manifest))}
    if src == "hf":
        r = _call(catalog.sync_hf, str(body.get("search") or "")[:100], str(body.get("filter") or "")[:100],
                  str(body.get("pipeline_tag") or "")[:60], str(body.get("sort") or "downloads")[:30], body.get("limit") or 20,
                  str(body.get("next") or ""), bool(body.get("refresh")))
        return {"count": len(r["entries"]), "next": r["next"], "synced_at": r["synced_at"]}
    raise HTTPException(400, "source is ollama, hf or manifest.")


@router.get("/api/models/variant/{vid:path}")
def variant(vid: str, request: Request):
    v = catalog.get(vid)
    if not v:
        raise HTTPException(404, "Unknown model variant.")
    q = dict(request.query_params)
    return {"variant": v, "fit": fit.estimate(v, _workload(q), _profile(q.get("profile")))}


@router.post("/api/models/card")
def card(body: dict):
    return {"variant": _call(catalog.hf_card, str(body.get("id") or ""))}


@router.post("/api/models/compare")
def compare(body: dict):
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not 1 <= len(ids) <= 4:
        raise HTTPException(400, "Compare 2 to 4 variants.")
    prof, w = _profile(body.get("profile")), _workload(body.get("workload") or {})
    vs = [v for v in (catalog.get(str(i)) for i in ids) if v]
    return {"rows": [{"variant": v, "fit": fit.estimate(v, w, prof)} for v in vs], "profile": _summary(prof)}


@router.post("/api/models/import")
def import_gguf(body: dict):
    return {"variant": _call(catalog.import_gguf, str(body.get("path") or ""))}


# ---------- hardware ----------

@router.get("/api/models/hardware")
def get_hardware(profile: str = ""):
    return {"profile": _summary(_profile(profile)), "profiles": hardware.profiles(), "active": config.load()["model_hw_profile"]}


@router.put("/api/models/hardware")
def put_hardware(body: dict):
    patch = {}
    if "profiles" in body:
        ps = body["profiles"]
        try:
            ps = [{"name": str(p["name"]).strip()[:40], "reserve_gib": min(max(float(p.get("reserve_gib") or 0), 0), 256)} for p in ps]
            if not all(math.isfinite(p["reserve_gib"]) for p in ps):
                raise ValueError
        except (TypeError, KeyError, ValueError):
            raise HTTPException(400, "Each profile needs a name and a reserve in GiB.")
        if not ps or any(not p["name"] for p in ps) or len({p["name"] for p in ps}) != len(ps) or len(ps) > 12:
            raise HTTPException(400, "Profiles need 1-12 distinct names.")
        patch["model_hw_profiles"] = ps
    names = [p["name"] for p in patch.get("model_hw_profiles") or hardware.profiles()]
    if "active" in body:
        if body["active"] not in names:
            raise HTTPException(400, "Pick one of the profiles.")
        patch["model_hw_profile"] = body["active"]
    elif config.load()["model_hw_profile"] not in names:
        patch["model_hw_profile"] = names[0]
    config.save(patch)
    return get_hardware()


# ---------- install & benchmark ----------

@router.post("/api/models/pull")
def pull(body: dict):
    return _call(catalog.pull, str(body.get("name") or ""), body.get("size_bytes") if isinstance(body.get("size_bytes"), int) else None)


@router.post("/api/models/pull/cancel")
def pull_cancel(body: dict):
    return {"canceled": catalog.cancel(str(body.get("name") or ""))}


@router.get("/api/models/jobs")
def jobs():
    return {"pulls": list(catalog.PULLS.values()), "jobs": list(JOBS.values())}


@router.post("/api/models/benchmark")
def benchmark(body: dict):
    vid = str(body.get("id") or "")
    v = catalog.get(vid)
    if not v or v.get("provider") != "ollama" or not v.get("installed"):
        raise HTTPException(400, "Only an installed Ollama model can be benchmarked on this PC.")
    return _bg(f"bench:{vid}", catalog.benchmark, vid, _workload(body).get("context", 8192))


# ---------- selection & release ----------

@router.get("/api/models/tasks")
def tasks():
    out = []
    for t, label in selections.TASKS.items():
        rel = _call(selections.release, t)
        hist = selections.history(t)
        keep = {rel["active"], rel["desired"], rel.get("candidate"), *rel["history"][-2:]}  # never drop what the card points at
        hist = [s for i, s in enumerate(hist) if i < 12 or s["id"] in keep]
        out.append({"task": t, "label": label, "release": rel, "selections": hist})
    return {"tasks": out, "metrics": selections.METRICS}


@router.post("/api/models/propose")
def propose(body: dict):
    mode = body.get("mode") if isinstance(body.get("mode"), dict) else None
    return _call(selections.propose, str(body.get("task") or ""), str(body.get("id") or ""), mode, _workload(body.get("workload") or {}))


@router.post("/api/models/choose")
def choose(body: dict):
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(400, "ids is a list of selection ids.")
    return _call(selections.choose, str(body.get("task") or ""), [str(i) for i in ids], body.get("metrics") or None)


@router.post("/api/models/measure")
def measure(body: dict):
    sid = str(body.get("selection") or "")
    sel = store.get(selections.SEL, sid)
    if not sel:
        raise HTTPException(404, "Unknown selection.")
    cost = _call(selections.cost_estimate, sel)
    if cost["paid"] and body.get("confirm") is not True:  # paid providers run only on an explicit click after the estimate
        return {"needs_confirm": True, "cost": cost}
    return _bg(f"measure:{sid}", selections.measure, sid, True)


@router.post("/api/models/activate")
def activate(body: dict):
    rev = body.get("rev")
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise HTTPException(400, "rev (the release revision you read) is required.")
    return _call(selections.activate, str(body.get("task") or ""), str(body.get("candidate") or ""),
                 rev if isinstance(rev, int) and not isinstance(rev, bool) else None, str(body.get("override_reason") or ""))


@router.post("/api/models/rollback")
def rollback(body: dict):
    rev = body.get("rev")
    if not isinstance(rev, int) or isinstance(rev, bool):
        raise HTTPException(400, "rev (the release revision you read) is required.")
    return _call(selections.rollback, str(body.get("task") or ""), rev if isinstance(rev, int) and not isinstance(rev, bool) else None)
