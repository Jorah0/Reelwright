"""Lab page data for "What works for you" and "Evals": taste models per platform and account, experiment results,
retention curves, missing-permission notes, and the saved eval baselines (scripts/eval.py)."""
import json

from fastapi import APIRouter

from . import accounts, config, experiments, learn, pipeline, platforms, store

router = APIRouter()


def _model(m: dict | None) -> dict | None:
    return {k: m.get(k) for k in ("n", "useful", "cv_acc", "baseline", "shrunk", "weights")} if m else None


@router.get("/api/learn/insights")
def insights():
    fb = pipeline.feedback(limit=10_000)
    glob = learn.train(fb)
    ups = store.find("upload", status="done")
    accs = accounts.all_accounts()
    s = config.load()
    retention = [{"upload_id": u["id"], "title": u.get("title", ""), "platform": u.get("platform"), "url": u.get("url"),
                  "account_id": u.get("account_id"), **u["retention"]}
                 for u in ups if isinstance(u.get("retention"), dict)][:12]
    return {
        "global": _model(glob),
        "platforms": {p: _model(learn.model_for(fb, glob, platform=p)) for p in platforms.PLATFORMS if learn.scoped(fb, platform=p)},
        "accounts": [{"id": a["id"], "name": a.get("name"), "platform": a["platform"], "lessons": n,
                      "model": _model(learn.model_for(fb, glob, account_id=a["id"]))}
                     for a in accs if (n := len(learn.scoped(fb, account_id=a["id"])))],
        "experiments": {"settings": s.get("experiments") or {}, "dims": experiments.DIMS,
                        "accounts": [{"id": a["id"], "name": a.get("name"), "platform": a["platform"], "stats": st}
                                     for a in accs if (st := experiments.stats(ups, a["id"]))]},
        "retention": retention,
        "notes": (store.get("pref", "metrics_notes") or {}).get("notes") or {},
        "account_names": {a["id"]: a.get("name") or a["platform"] for a in accs},
        "settings": {k: s.get(k) for k in ("context_guard", "context_cards", "tiktok_metrics")},
        "with_lift": sum(f.get("lift") is not None for f in fb),
    }


@router.get("/api/learn/evals")
def evals():
    """The saved baselines (offline and live), or None where scripts/eval.py --save hasn't been run."""
    out = {}
    for name in ("baseline", "baseline_live"):
        try:
            data = json.loads((config.ROOT / "evals" / f"{name}.json").read_text("utf-8"))
            out[name] = {k: data.get(k) for k in ("mode", "at", "guard", "cases")} | {
                "finders": {f: {"summary": r.get("summary"), "cases": r.get("cases")} for f, r in (data.get("finders") or {}).items()}}
        except (OSError, ValueError, AttributeError):
            out[name] = None
    return out
