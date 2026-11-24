"""Versioned model selection per task, release gates, the appropriateness score, and the release pointer.

A selection record (store kind "model_selection") pins one exact variant (digest or revision) for a task, with the
workload and hardware it was judged on, its gate results and the creator's choice (plus any override reason). mode is a
tagged union: {"kind": "existing"} (use the variant as is) or {"kind": "scratch", "base": <digest>, "recipe": {...}}
(the training package builds a specialist from it).

The release pointer (kind "model_release", id = task) keeps desired (latest proposed), candidate (latest tried) and
active apart. activate() is atomic with a revision guard; a candidate that fails a gate leaves active in place;
rollback() returns to the release before it. Migration captures the routing in Settings → AI as each task's first,
"existing" selection (origin "settings"); while that one is active, routing() returns None and ai.route behaves exactly
as before."""
from __future__ import annotations

import contextlib
import contextvars
import importlib.util
import math
import threading
import time
from datetime import datetime, timezone

from .. import config, providers, store, usage
from . import catalog, fit

TASKS = {"find": "Finding clips", "judge": "Ranking clips (long videos)", "classify": "Video type", "meta": "Titles & captions",
         "plan": "Edit plans", "specialist": "Specialist (training package)"}
SEL, REL = "model_selection", "model_release"
QUALITY_MIN, SUPPORT_MIN = 0.70, 0.90
# the handbook's default weighting; quality and support are higher-is-better, latency (s per eval case) lower-is-better
METRICS = [{"name": "quality", "worst": 0.70, "best": 0.90, "weight": 0.5},
           {"name": "support", "worst": 0.90, "best": 1.00, "weight": 0.3},
           {"name": "latency", "worst": 2.8, "best": 1.2, "weight": 0.2}]
_lock = threading.Lock()
_trial = contextvars.ContextVar("model_trial", default=None)


class Conflict(Exception):
    """The release changed since the caller read it (revision guard)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _profile() -> dict:
    return catalog.current_profile()


# ---------- routing (ai.route's hook) ----------

def routing(task: str) -> dict | None:
    """{provider, model, digest} for a task whose active release was chosen here; None = follow Settings → AI."""
    t = _trial.get()
    if t and t["task"] == task:
        return t
    rel = store.get(REL, task)
    sel = rel and store.get(SEL, rel["active"])
    if not sel or sel.get("origin") == "settings" or not sel.get("provider"):
        return None
    return {"provider": sel["provider"], "model": sel["model"], "digest": sel.get("digest")}


@contextlib.contextmanager
def trial(task: str, provider: str, model: str):
    """Routes one task to a candidate inside this thread only (benchmarks), never the creator's other jobs."""
    tok = _trial.set({"task": task, "provider": provider, "model": model, "digest": None})
    try:
        yield
    finally:
        _trial.reset(tok)


# ---------- migration ----------

def _settings_route(task: str) -> tuple[str, str]:
    s = config.load()
    p = (s.get("ai_routes") or {}).get(task) or "anthropic"
    p = p if p in providers.NAMES else "anthropic"
    model = (s.get("classify_model") or s["model"]) if (p, task) == ("anthropic", "classify") else \
        s["model"] if p == "anthropic" else config.provider(p).get("model", "")
    return p, model


def migrate(task: str | None = None) -> None:
    """Each task without a release gets one: the current Settings routing as an "existing" selection."""
    with _lock:
        for t in [task] if task else TASKS:
            if store.get(REL, t):
                continue
            p, model = _settings_route(t)
            digest = next((v["digest"] for v in catalog.entries() if p == "ollama" and v["source"] == "ollama"
                           and v.get("name") == model and v.get("installed")), None)
            sel = store.put(SEL, {"task": t, "version": 1, "origin": "settings", "mode": {"kind": "existing"}, "variant_id": None,
                                  "provider": p, "model": model, "digest": digest, "revision": model if p != "ollama" else None,
                                  "workload": None, "hardware": None, "gates": {}, "metrics": {}, "score": None,
                                  "choice": {"by": "migration", "at": _now(), "override_reason": ""}})
            store.put(REL, {"id": t, "task": t, "rev": 1, "desired": sel["id"], "candidate": None, "candidate_status": "",
                            "active": sel["id"], "history": [sel["id"]], "last_good": sel["id"]})


def release(task: str) -> dict:
    if task not in TASKS:
        raise ValueError(f"Unknown task (known: {', '.join(TASKS)}).")
    migrate(task)
    return store.get(REL, task)


def selection(task: str) -> dict:
    """The active selection record for a task (the training package reads "specialist" here)."""
    return store.get(SEL, release(task)["active"])


def history(task: str) -> list[dict]:
    return sorted(store.find(SEL, task=task), key=lambda s: -s["version"])


# ---------- gates ----------

def gates(v: dict, task: str, est: dict, metrics: dict | None = None) -> dict:
    """pass | fail | unmeasured | inapplicable per gate."""
    m, local = metrics or {}, v.get("provider") == "ollama"
    remote = v.get("provider") in ("anthropic", "openai")
    res = [c["status"] for c in est.get("checks", []) if c["name"] in ("ram", "disk")]
    ctx = (est.get("workload") or {}).get("context")
    images = task == "classify" or (task == "judge" and config.load().get("visual_judge"))
    return {
        "fit": "inapplicable" if not local else {"fits": "pass", "incompatible": "fail"}.get(est["status"], "unmeasured"),
        "resources": "inapplicable" if not local else "fail" if "fail" in res else "pass" if res and set(res) == {"pass"} else "unmeasured",
        "runtime": "pass" if remote or (local and v.get("installed")) else "fail",
        "context": "inapplicable" if remote else "unmeasured" if not (v.get("context") and ctx) else "pass" if v["context"] >= ctx else "fail",
        "vision": "inapplicable" if not images or remote else "pass" if "image" in (v.get("inputs") or []) else "fail",
        "schema": "unmeasured" if m.get("support") is None else "pass" if m["support"] >= SUPPORT_MIN else "fail",
        "quality": "unmeasured" if m.get("quality") is None else "pass" if m["quality"] >= QUALITY_MIN else "fail",
    }


# ---------- appropriateness score ----------

def check_metrics(metrics: list[dict]) -> None:
    if not metrics or not all(isinstance(m, dict) for m in metrics):
        raise ValueError("No metrics to score on.")
    ws = [m.get("weight") for m in metrics]
    if any(not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0 for w in ws) or abs(sum(ws) - 1) > 1e-6:
        raise ValueError("Weights must be non-negative and sum to 1.")
    for m in metrics:
        a, b = m.get("worst"), m.get("best")
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (a, b)) or a == b:
            raise ValueError(f"{m.get('name')}: worst and best must be two different numbers.")


def score(values: dict, metrics: list[dict] = METRICS) -> int | None:
    """100 x sum(weight x normalised metric), each normalised 0..1 from worst to best; None when any is unmeasured."""
    check_metrics(metrics)
    if any(values.get(m["name"]) is None for m in metrics):
        return None
    return round(100 * sum(m["weight"] * min(1.0, max(0.0, (values[m["name"]] - m["worst"]) / (m["best"] - m["worst"])))
                           for m in metrics))


def rank(cands: dict[str, dict], metrics: list[dict] = METRICS) -> list[dict]:
    """{id: metric values} -> rows best first; an unmeasured candidate gets no score and no rank (listed last)."""
    rows = [{"id": k, "score": score(v, metrics)} for k, v in cands.items()]
    scored = sorted((r for r in rows if r["score"] is not None), key=lambda r: -r["score"])
    for i, r in enumerate(scored, 1):
        r["rank"] = i
    return scored + [{**r, "rank": None} for r in rows if r["score"] is None]


# ---------- proposing, activating, rolling back ----------

def _mode(mode: dict | None, v: dict) -> dict:
    kind = (mode or {}).get("kind", "existing")
    if kind == "existing":
        return {"kind": "existing"}
    if kind == "scratch":
        return {"kind": "scratch", "base": v.get("digest") or v.get("revision"), "recipe": dict((mode or {}).get("recipe") or {})}
    raise ValueError("mode.kind is existing or scratch.")


def propose(task: str, variant_id: str, mode: dict | None = None, workload: dict | None = None) -> dict:
    """A new selection record for a task (it becomes the release's desired selection; activate() tries it)."""
    release(task)
    v = catalog.get(variant_id)
    if not v:
        raise ValueError("Unknown model variant.")
    if not (v.get("digest") or v.get("revision")):
        raise ValueError("This entry isn't pinned to a digest or revision yet: install it first, then pick the installed variant.")
    if not v.get("provider"):
        raise ValueError("Reelwright can't route a task to this entry (it isn't an Ollama model or an API model).")
    prof = _profile()
    est = fit.estimate(v, workload, prof)
    doc = {
        "task": task, "origin": "user", "mode": _mode(mode, v), "variant_id": v["id"],
        "provider": v["provider"], "model": v.get("pull") or v["name"], "digest": v.get("digest"), "revision": v.get("revision"),
        "workload": est["workload"],
        "hardware": {"profile": prof.get("name"), "reserve_gib": prof.get("reserve_gib"), "budget_gib": prof.get("budget_gib"),
                     "gpus": [{k: g.get(k) for k in ("index", "name", "total_gib", "free_gib")} for g in prof.get("gpus", [])],
                     "telemetry": prof.get("telemetry"), "at": prof.get("at")},
        "fit": {k: est.get(k) for k in ("status", "range_gib", "pct_total", "left_gib", "short_gib", "source", "measured_at", "checks")},
        "gates": gates(v, task, est), "metrics": {}, "score": None,
        "choice": {"by": "user", "at": _now(), "override_reason": ""}}
    with _lock:  # one version number per proposal
        sel = store.put(SEL, {**doc, "version": len(store.find(SEL, task=task)) + 1})
    store.update(REL, task, fn=lambda r: r.update(desired=sel["id"], rev=r["rev"] + 1))
    return sel


def activate(task: str, candidate: str, expected_rev: int | None = None, override_reason: str = "") -> dict:
    """Makes a selection the task's active release, atomically. A failing gate leaves the active release in place
    (candidate_status "failed") unless the creator gives an override reason."""
    release(task)
    sel = store.get(SEL, candidate)
    if not sel or sel["task"] != task:
        raise ValueError("That selection doesn't belong to this task.")
    v = catalog.get(sel["variant_id"]) if sel.get("variant_id") else None
    if v:  # re-checked now, outside the store lock: the model may be gone or OBS may have started since it was proposed
        fresh = gates(v, task, fit.estimate(v, sel["workload"], _profile()), sel.get("metrics"))
        sel["gates"] = {**sel.get("gates", {}), **{k: fresh[k] for k in ("fit", "resources", "runtime", "context", "vision")}}
        store.update(SEL, candidate, {"gates": sel["gates"]})
    failing = [k for k, s in (sel.get("gates") or {}).items() if s == "fail"]
    reason = (override_reason or "").strip()[:500]
    ok = not failing or (bool(reason) and "runtime" not in failing)  # a model that can't run isn't overridable

    def fn(r):
        if expected_rev is not None and r["rev"] != expected_rev:
            raise Conflict(f"The {task} release changed (revision {r['rev']}, expected {expected_rev}): reload and try again.")
        r.update(rev=r["rev"] + 1, candidate=candidate, candidate_status="active" if ok else "failed")
        if ok:
            r.update(active=candidate, last_good=candidate, history=[h for h in r["history"] if h != candidate] + [candidate])
    rel = store.update(REL, task, fn=fn)
    if ok and reason:
        store.update(SEL, candidate, fn=lambda s: s["choice"].update(override_reason=reason, at=_now()))
    return {"ok": ok, "failed_gates": failing, "release": rel}


def rollback(task: str, expected_rev: int | None = None) -> dict:
    """Back to the release that was active before the current one."""
    release(task)

    def fn(r):
        if expected_rev is not None and r["rev"] != expected_rev:
            raise Conflict(f"The {task} release changed: reload and try again.")
        if len(r["history"]) < 2:
            raise ValueError("There's no earlier release to roll back to.")
        h = r["history"][:-1]
        r.update(rev=r["rev"] + 1, history=h, active=h[-1], last_good=h[-1], candidate=None, candidate_status="")
    return store.update(REL, task, fn=fn)


# ---------- benchmarks (the evals harness, through ai routing) ----------

def _eval_module():
    path = config.ROOT / "scripts" / "eval.py"
    if not path.is_file():
        return None  # the packaged .exe ships without the eval harness
    spec = importlib.util.spec_from_file_location("reelwright_eval_harness", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cost_estimate(sel: dict) -> dict:
    """What a benchmark would cost: free for local models; for paid APIs, the eval cases' tokens at the model's price."""
    if sel["provider"] == "ollama":
        return {"paid": False, "usd": 0.0, "line": "Local model: free."}
    from .. import ai

    ev = _eval_module()
    cases = ev.load_cases() if ev else []
    tin = sum((len(ai.FIND_SYSTEM) + len(ai.tagged_lines(c["segments"], c.get("signals") or {})) + 1500) / 4 for c in cases) \
        if sel["task"] == "find" else 400
    tout = sum(150 * (len(c["gold"]["good"]) + 1) + 300 for c in cases) if sel["task"] == "find" else 50
    price = usage.PRICES.get(sel["model"])
    usd = (tin * price[0] + tout * price[1]) / 1e6 if price else None
    return {"paid": True, "usd": usd, "line": f"~{tin:,.0f} input + ~{tout:,.0f} output tokens on {sel['model']}: "
                                             + (f"about ${usd:.3f} (rough, ±50%)" if usd is not None else "no price known for this model")}


def measure(sel_id: str, confirm: bool = False) -> dict:
    """Benchmarks a selection through ai routing and records quality / support / latency, gates and score.
    find: the golden eval cases (scripts/eval.py). Other tasks: one structured smoke call (quality stays unmeasured).
    A paid provider runs only with confirm=True; without it this returns the cost estimate."""
    from .. import ai

    sel = store.get(SEL, sel_id)
    if not sel:
        raise ValueError("Unknown selection.")
    cost = cost_estimate(sel)
    if cost["paid"] and not confirm:
        return {"needs_confirm": True, "cost": cost}
    with trial("find" if sel["task"] == "find" else "other", sel["provider"], sel["model"]):  # the smoke call's schema routes as "other"
        ev = _eval_module() if sel["task"] == "find" else None
        if ev:
            cases, rows, ok, t0 = ev.load_cases(), {}, 0, time.perf_counter()
            for c in cases:
                try:
                    rows[c["name"]] = ev.score_case(ev.pick(c, "live"), c["gold"])
                    ok += 1
                except Exception as e:  # an invalid reply counts against support, it doesn't stop the run
                    rows[c["name"]] = {"error": str(e)[:200]}
            good = {k: r for k, r in rows.items() if "error" not in r}
            m = {"quality": ev.summarize(good)["precision_at_k"] if good else None, "support": ok / len(cases) if cases else None,
                 "latency": round((time.perf_counter() - t0) / max(1, len(cases)), 2), "cases": rows}
        else:
            t0 = time.perf_counter()
            try:
                ai._ask("Reply with JSON only.", "Write a 2-4 word search query about cats doing funny things.", ai.Query, effort="low",
                        provider=sel["provider"])
                support = 1.0
            except Exception:
                support = 0.0
            m = {"quality": None, "support": support, "latency": round(time.perf_counter() - t0, 2)}
    m["at"], m["cost"] = _now(), cost
    v = catalog.get(sel["variant_id"]) if sel.get("variant_id") else None
    g = gates(v, sel["task"], fit.estimate(v, sel["workload"], _profile()), m) if v else sel["gates"]  # probes run outside the store lock
    return store.update(SEL, sel_id, {"metrics": m, "score": score(m), "gates": g})


def choose(task: str, sel_ids: list[str], metrics: list[dict] | None = None) -> dict:
    """"Help me choose": each candidate's gates, measured values, score and rank."""
    metrics = metrics or METRICS
    check_metrics(metrics)
    sels = [s for s in (store.get(SEL, i) for i in sel_ids) if s and s["task"] == task]
    ranked = {r["id"]: r for r in rank({s["id"]: s.get("metrics") or {} for s in sels}, metrics)}
    return {"metrics": metrics, "rows": sorted(({**s, "score": ranked[s["id"]]["score"], "rank": ranked[s["id"]]["rank"]} for s in sels),
                                               key=lambda s: (s["rank"] is None, s["rank"] or 0))}
