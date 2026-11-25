"""Evaluate a candidate against the active model on the same inputs, promote it only through the gate, roll back in one
click, and record every hash of a release in its manifest.

The release pointer is the Models package's (models.propose / activate / rollback): a promoted specialist becomes the
task's active selection, so ai.route sends that task to Ollama with its exact digest, and the Models page shows it.
"""
import hashlib
import importlib.util
import json
import time
from pathlib import Path

from .. import ai, config, context, log, models, providers, store
from . import dataset, runner

MIN_TEST = 10  # test-split examples needed before the test split decides a promotion
HIGHER = ("precision_at_k", "iou", "complete")  # bad_rate: lower is better
EVAL_SCRIPT = config.ROOT / "scripts" / "eval.py"  # the golden-set harness (a source checkout; not inside the .exe)


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def manifest(hashes: dict, **extra) -> dict:
    return {"hashes": hashes, "pins": list(runner.PINS), "llama_cpp": {"tag": runner.LLAMA_CPP["tag"], "bin_sha256":
            runner.LLAMA_CPP["bin_sha256"]}, "quant": runner.QUANT, "created": time.time(), **extra}


def _harness():
    if not EVAL_SCRIPT.is_file():
        return None
    spec = importlib.util.spec_from_file_location("reelwright_eval_harness", EVAL_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- scoring ----------

def asker_for(provider: str, model: str | None = None):
    """(system, user) -> MomentList dict through the app's own provider path."""
    if provider == "anthropic":
        return lambda system, user: ai._ask(system, user, ai.MomentList, provider="anthropic").model_dump()
    cfg = {**config.provider(provider), **({"model": model} if model else {})}
    return lambda system, user: providers.ask(provider, cfg, system, user, ai.MomentList).model_dump()


def _iou(a, b) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return inter / union if union > 0 else 0.0


def _test_score(picks: list, good: list) -> dict:
    """precision@k (IoU ≥ 0.5, one pick per good clip) and mean best IoU over the top k, k = number of kept clips."""
    k, used, hits = len(good), set(), 0
    for p in picks[:k]:
        best = max(((_iou(p, g), i) for i, g in enumerate(good) if i not in used), default=(0.0, None))
        if best[1] is not None and best[0] >= 0.5:
            used.add(best[1])
            hits += 1
    return {"precision_at_k": hits / k if k else None,
            "iou": sum(max((_iou(p, g) for p in picks[:k]), default=0.0) for g in good) / k if k else None}


def _mean(rows: list[dict], m: str):
    vals = [r[m] for r in rows if r.get(m) is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def scorecard(asker, build: dict) -> dict:
    """The evals harness cases (as pipeline._find runs them: finder → refine → setup guard) and the frozen test split's
    find examples, through one asker. "inputs" hashes every prompt sent, so two cards compare like with like."""
    h, inputs = _harness(), hashlib.sha256()
    evals, test = {}, {}
    for case in (h.load_cases() if h else []):
        segs, sig, dur = case["segments"], case.get("signals") or {}, float(case["duration"])
        clo, ctarget, chi = case["lengths"]
        count = len(case["gold"]["good"]) + 1
        sig = {**sig, "video_type": {"type": case.get("video_type")}}
        found = []
        for wsegs, wsig, wcount, win in dataset.windows(segs, dur, sig, count):
            system, user = dataset.find_prompt(wsegs, dur, wsig, wcount, clo, chi, target=ctarget, window=win)
            inputs.update((system + user).encode())
            try:
                found += asker(system, user).get("clips") or []
            except Exception as e:  # a model that can't answer scores zero on that case, it doesn't stop the card
                log.event("training", f"eval case {case['name']}: {e}")
        found = ai.refine(ai._dedupe(found), segs, dur, clo, chi, target=ctarget)
        found = ai._dedupe(context.guard(found, segs, chi, sig))
        evals[case["name"]] = h.score_case(found[:count], case["gold"])
    d = Path(build["dir"])
    sides = {}
    if (d / "sidecar.jsonl").is_file():
        sides = {x["id"]: x for x in map(json.loads, (d / "sidecar.jsonl").read_text("utf-8").splitlines())}
    for line in (d / "test.jsonl").read_text("utf-8").splitlines() if (d / "test.jsonl").is_file() else []:
        e = json.loads(line)
        if not e["messages"][0]["content"].startswith(ai.FIND_SYSTEM[:40]) or (sides.get(e["id"]) or {}).get("reviewer") == "teacher":
            continue  # the card scores clip finding against the creator's own decisions (not judge/meta, not Claude's labels)
        system, user = e["messages"][0]["content"], e["messages"][1]["content"]
        inputs.update((system + user).encode())
        try:
            picks = asker(system, user).get("clips") or []
        except Exception as ex:
            log.event("training", f"test example {e['id']}: {ex}")
            picks = []
        test[e["id"]] = _test_score(picks, json.loads(e["messages"][2]["content"])["clips"])
    rows = list(evals.values())
    return {"evals": evals, "evals_summary": {m: _mean(rows, m) for m in (*HIGHER, "bad_rate")},
            "test": test, "test_summary": {m: _mean(list(test.values()), m) for m in ("precision_at_k", "iou")},
            "inputs": inputs.hexdigest(), "dataset": build["hash"], "at": time.time()}


def gate(cand: dict, active: dict | None, margin: float, tolerance: float) -> tuple[bool, str]:
    """Promote only when the candidate beats the active model by `margin` on the frozen test split (the eval cases when
    there is no test split) and no golden eval regresses beyond `tolerance`."""
    if not active:
        return False, "The active model has no scorecard on the same inputs: evaluate it first."
    if active.get("inputs") and cand.get("inputs") and active["inputs"] != cand["inputs"]:
        return False, "The two scorecards were made on different inputs."
    ce, ae = cand.get("evals_summary") or {}, active.get("evals_summary") or {}
    for m in HIGHER:
        if ce.get(m) is not None and ae.get(m) is not None and ce[m] < ae[m] - tolerance:
            return False, f"Golden evals regress: {m} {ce[m]:.3f} vs {ae[m]:.3f} (tolerance {tolerance:.3f})."
    if ce.get("bad_rate") is not None and ae.get("bad_rate") is not None and ce["bad_rate"] > ae["bad_rate"] + tolerance:
        return False, f"Golden evals regress: bad_rate {ce['bad_rate']:.3f} vs {ae['bad_rate']:.3f}."
    enough = len(cand.get("test") or {}) >= MIN_TEST  # a handful of test examples is noise: fall back to the evals
    key = "test_summary" if enough and (cand.get("test_summary") or {}).get("precision_at_k") is not None else "evals_summary"
    c, a = (cand.get(key) or {}).get("precision_at_k"), (active.get(key) or {}).get("precision_at_k")
    if c is None or a is None:
        return False, "No comparable precision@k on the test split or the evals."
    if c < a + margin:
        return False, f"precision@k {c:.3f} doesn't beat the active {a:.3f} by the {margin:.3f} margin ({key.split('_')[0]})."
    return True, f"precision@k {c:.3f} vs {a:.3f} ({key.split('_')[0]}), no eval regression."


# ---------- releases ----------

def next_id() -> str:
    nums = [int(r["id"][2:]) for r in store.find("train_release") if r["id"][2:].isdigit()]
    return f"rc{max(nums, default=0) + 1:03d}"


def record_release(rid: str, hashes: dict, tag: str | None = None, card: dict | None = None, **extra) -> dict:
    return store.put("train_release", {"id": rid, "hashes": hashes, "tag": tag, "card": card, "status": "candidate",
                                       "reason": "", **extra})


def variant(rel: dict) -> dict:
    """The specialist as a Models catalog entry (pinned by its Ollama digest), so the release pointer can select it."""
    info = rel.get("model") or {}
    return models.catalog.put({
        "id": f"ollama:{rel['digest']}", "source": "ollama", "provider": "ollama", "name": rel["tag"], "pull": rel["tag"],
        "digest": rel["digest"], "family": "reelwright-specialist", "format": "gguf", "quant": runner.QUANT,
        "params_b": (info.get("params") or 0) / 1e9 or None, "size_bytes": info.get("gguf_bytes"), "context": info.get("context"),
        "layers": info.get("layers"), "kv_heads": info.get("kv_heads"), "head_dim": info.get("head_dim"), "installed": True,
        "capabilities": ["completion"], "task": ["text-generation"], "inputs": ["text"], "outputs": ["text"],
        "license": info.get("license"), "aliases": [rel["tag"]], "src": {"params_b": "measured", "context": "measured"}})


def promote(rid: str, task: str, active_card: dict | None, margin: float, tolerance: float) -> dict:
    """The gate first (beats the active model by margin, no golden-eval regression), then the Models package's own
    release gates; a failure at either keeps the active release and leaves this one a candidate with the reason."""
    rel = store.get("train_release", rid)
    if not rel or not rel.get("tag") or not rel.get("digest"):
        raise RuntimeError("That candidate has no Ollama model to promote.")
    if task != "find":  # ponytail: the scorecard measures clip finding only; judge/meta need their own card first
        raise ValueError("A specialist can be promoted for finding clips (the task its scorecard measures).")
    ok, why = gate(rel.get("card") or {}, active_card, margin, tolerance)
    if ok:
        sel = models.propose(task, variant(rel)["id"], mode={"kind": "existing"})
        res = models.activate(task, sel["id"])
        if not res["ok"]:
            ok, why = False, f"The Models release gates failed: {', '.join(res['failed_gates'])}."
    if not ok:
        store.update("train_release", rid, {"status": "candidate", "reason": why})
        return {"promoted": False, "reason": why}
    for r in store.find("train_release", status="active", task=task):
        store.update("train_release", r["id"], {"status": "retired"})
    store.update("train_release", rid, {"status": "active", "reason": why, "task": task, "selection": sel["id"]})
    log.event("training", f"promoted {rid} ({rel['tag']}) for {task}: {why}")
    return {"promoted": True, "reason": why}


def protected() -> tuple[set, set, set]:
    """(release ids, training job ids, dataset hashes) that must stay: the active release of any task and every release
    the Models pointer can roll back to (its history), with the job whose files they came from and its dataset."""
    hist = {s for t in models.TASKS for s in (store.get(models.selections.REL, t) or {}).get("history") or []}
    rels = [r for r in store.find("train_release") if r.get("status") == "active" or r.get("selection") in hist]
    return ({r["id"] for r in rels}, {(r.get("manifest") or {}).get("train_job") for r in rels} - {None},
            {(r.get("hashes") or {}).get("dataset") for r in rels} - {None})


def delete_release(rid: str) -> dict:
    """Forget a candidate and its manifest file. Its Ollama model stays (remove it with `ollama rm` if unwanted)."""
    if not rid.isalnum():
        raise ValueError("Bad release id.")
    rel = store.get("train_release", rid)
    if not rel:
        raise LookupError("No such candidate.")
    if rid in protected()[0]:
        raise RuntimeError("This is the active release or the one Roll back returns to: it stays.")
    job = (rel.get("manifest") or {}).get("train_job")
    if job:
        (runner.job_dir(job) / f"release-{rid}.json").unlink(missing_ok=True)
    store.delete("train_release", rid)
    log.event("training", f"deleted candidate {rid}")
    return {"ok": True}


def rollback(task: str = "find") -> dict:
    """One click back to the release that was active before (the Models pointer's history)."""
    models.rollback(task)
    now = models.selection(task)
    for r in store.find("train_release", task=task):
        if r["status"] == "active":
            store.update("train_release", r["id"], {"status": "rolled_back"})
        elif r.get("selection") == now["id"]:
            store.update("train_release", r["id"], {"status": "active"})
    log.event("training", f"rolled back {task} to {now.get('model')}")
    return {"task": task, "active": now.get("model")}
