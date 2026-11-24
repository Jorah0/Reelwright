"""Golden-set evals for clip finding:  python scripts/eval.py [--save] [--no-guard] [--case NAME] [--live [--yes]]

Each case in evals/cases/*.json is a small transcript with its signals, the good moments a creator would post (with where
their setup starts and their payoff lands) and known bad ones (sponsor reads, brb breaks...). A run sends every case through
the same steps as pipeline._find — the finder, ai.refine, then the setup guard (context.guard) — and scores the picks:

  precision_at_k  share of the top k picks (k = number of good moments) that hit a good moment (IoU >= 0.5; one pick each)
  iou             best IoU each good moment gets from the top k, averaged
  complete        share of those hits that contain the whole setup and the payoff (0.5 s slack)
  bad_rate        share of all picks that mostly sit inside a known-bad span

Offline (default; free, no network, safe for CI): two finders, "stub" (each case's recorded AI picks, replayed through
ai.find_moments) and "heuristic" (the no-key fallback). Any AI call is refused. --live runs the finder configured in
Settings → AI on every case; it prints a cost estimate and asks first (--yes skips the question). Paid runs happen
only when you start them. --save writes evals/baseline.json (evals/baseline_live.json for --live); otherwise the run
is compared with the saved baseline.
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "cases"
HIT_IOU, BAD_SHARE, SLACK = 0.5, 0.5, 0.5
METRICS = ("precision_at_k", "iou", "complete", "bad_rate")


def iou(a: dict, b: dict) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return inter / union if union > 0 else 0.0


def score_case(picks: list[dict], gold: dict) -> dict:
    good, bad = gold["good"], gold.get("bad") or []
    k = len(good)
    top, used, hits, complete = picks[:k], set(), 0, 0
    for p in top:
        best, gi = max(((iou(p, g), i) for i, g in enumerate(good) if i not in used), default=(0.0, None))
        if gi is not None and best >= HIT_IOU:
            used.add(gi)
            hits += 1
            g = good[gi]
            complete += p["start"] <= g.get("setup", g["start"]) + SLACK and p["end"] >= g.get("payoff", g["end"]) - SLACK
    inside = lambda p, b: max(0.0, min(p["end"], b["end"]) - max(p["start"], b["start"])) / max(p["end"] - p["start"], 1e-6)  # noqa: E731
    return {"precision_at_k": hits / k if k else 0.0,
            "iou": sum(max((iou(p, g) for p in top), default=0.0) for g in good) / k if k else 0.0,
            "complete": complete / hits if hits else None,
            "bad_rate": sum(any(inside(p, b) >= BAD_SHARE for b in bad) for p in picks) / len(picks) if picks else 0.0}


def summarize(rows: dict) -> dict:
    out = {}
    for m in METRICS:
        vals = [r[m] for r in rows.values() if r[m] is not None]
        out[m] = round(sum(vals) / len(vals), 3) if vals else None
    return out


def load_cases(names=None) -> list[dict]:
    cases = [{"name": f.stem, **json.loads(f.read_text("utf-8"))} for f in sorted(CASES.glob("*.json"))]
    return [c for c in cases if not names or c["name"] in names]


def _refuse(*a, **k):
    raise RuntimeError("offline eval: AI calls are refused (use --live)")


def pick(case: dict, finder: str, guard: bool = True) -> list[dict]:
    """The case's picks, best first, the way pipeline._find makes them (no creator lessons: an empty learning memory)."""
    from reelwright import ai, context

    segs, sig, dur = case["segments"], case.get("signals") or {}, float(case["duration"])
    lo, target, hi = case["lengths"]
    count = len(case["gold"]["good"]) + 1
    if finder == "heuristic":
        found = ai.heuristic_moments(segs, sig.get("loud", []), dur, count, lo, hi)
    else:
        if finder == "stub":
            ai._find_pass = lambda *a, **k: [dict(x) for x in case["stub"]]
        raw = ai.find_moments(segs, dur, {**sig, "video_type": {"type": case.get("video_type")}}, count, lo, hi, target=target)
        found = ai.refine(raw, segs, dur, lo, hi, target=target)
    if guard:
        found = ai._dedupe(context.guard(found, segs, hi, sig))
    return [{"start": c["start"], "end": c["end"], "title": c.get("title", "")} for c in found[:count]]


def evaluate(cases: list[dict], finders: list[str], guard: bool = True) -> dict:
    from reelwright import ai

    saved = ai._find_pass, ai._ask
    out = {}
    try:
        for finder in finders:
            if finder != "live":
                ai._ask = _refuse  # offline: nothing paid can run, whatever the code path
            rows, picks = {}, {}
            for c in cases:
                picks[c["name"]] = pick(c, finder, guard)
                rows[c["name"]] = score_case(picks[c["name"]], c["gold"])
            out[finder] = {"summary": summarize(rows), "cases": rows, "picks": picks}
            ai._find_pass, ai._ask = saved
    finally:
        ai._find_pass, ai._ask = saved
    return out


def estimate(cases: list[dict]) -> tuple[float | None, str]:
    """Rough cost of a live run with the configured finder: ~4 characters per token, ~150 output tokens per pick."""
    from reelwright import ai, config, usage

    s = config.load()
    provider, model = ai.route("find"), s["model"]
    tokens_in = sum((len(ai.FIND_SYSTEM) + len(ai.tagged_lines(c["segments"], c.get("signals") or {})) + 1500) / 4 for c in cases)
    tokens_out = sum(150 * (len(c["gold"]["good"]) + 1) + 300 for c in cases)
    pair = ai.consensus_pair()
    calls = 2 if pair else 1
    if provider != "anthropic" and not pair:
        return None, f"{len(cases)} cases via {provider} ({ai.model_name(provider)}): no price known (local/other provider)"
    price = usage.PRICES.get(model)
    if not price:
        return None, f"{len(cases)} cases on {model}: no price known for this model"
    usd = calls * (tokens_in * price[0] + tokens_out * price[1]) / 1e6
    return usd, (f"{len(cases)} cases, ~{tokens_in * calls:,.0f} input + ~{tokens_out * calls:,.0f} output tokens on {model}"
                 f"{' (consensus: two models)' if pair else ''}: about ${usd:.3f} (rough, ±50%)")


def _table(res: dict, base: dict | None) -> str:
    lines = []
    for finder, r in res.items():
        b = ((base or {}).get("finders") or {}).get(finder, {}).get("summary") or {}
        cells = []
        for m in METRICS:
            v, was = r["summary"][m], b.get(m)
            delta = f" ({v - was:+.3f})" if isinstance(v, float) and isinstance(was, float) and abs(v - was) > 1e-9 else ""
            cells.append(f"{m} {'-' if v is None else f'{v:.3f}'}{delta}")
        lines.append(f"{finder:10} " + "  ".join(cells))
        for name, row in r["cases"].items():
            lines.append(f"    {name:20} " + "  ".join(f"{m} {'-' if row[m] is None else f'{row[m]:.2f}'}" for m in METRICS))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Golden-set evals for clip finding (offline unless --live).")
    ap.add_argument("--live", action="store_true", help="run the AI finder configured in Settings (paid; asks first)")
    ap.add_argument("--yes", action="store_true", help="with --live: don't ask before spending")
    ap.add_argument("--save", action="store_true", help="save this run as the baseline")
    ap.add_argument("--no-guard", action="store_true", help="skip the setup guard (to measure what it adds)")
    ap.add_argument("--case", action="append", help="only this case (repeatable)")
    a = ap.parse_args(argv)
    if a.save and a.case:
        ap.error("--save writes the baseline for every case: run it without --case")
    if not a.live:  # never the real library or settings: a throwaway data folder, as scripts/verify.py does
        os.environ["REELWRIGHT_DATA"] = tempfile.mkdtemp(prefix="rw-eval-")
    sys.path.insert(0, str(ROOT))
    cases = load_cases(a.case)
    if not cases:
        print(f"No eval cases found in {CASES}")
        return 1
    if a.live:
        usd, line = estimate(cases)
        print(f"Live eval: {line}")
        if not a.yes:
            if not sys.stdin.isatty() or input("Spend this? [y/N] ").strip().lower() != "y":
                print("Not run.")
                return 1
    res = evaluate(cases, ["live"] if a.live else ["stub", "heuristic"], guard=not a.no_guard)
    path = ROOT / "evals" / ("baseline_live.json" if a.live else "baseline.json")
    try:
        base = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        base = None
    print(_table(res, None if a.save else base))
    if a.save:
        path.write_text(json.dumps({"mode": "live" if a.live else "offline", "guard": not a.no_guard,
                                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "cases": [c["name"] for c in cases],
                                    "finders": res}, indent=1), "utf-8")
        print(f"Saved {path.relative_to(ROOT)}")
    elif base:
        print(f"(deltas vs {path.relative_to(ROOT)}, saved {base.get('at', '?')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
