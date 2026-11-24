"""Claude usage meter: tokens, prompt-cache hits and estimated cost for every call (shown on the Activity page).

Makes the AI layer measurable: is the transcript cache actually hitting? Which feature costs the most?
Prices are USD per million tokens; a model or token type with no known price is reported as unknown (None),
never guessed. Cache writes (5-minute TTL) bill at 1.25x input.
"""
import time

from . import store

# model: (input, output, cache read)
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, None),
}
KEEP = 5000  # usage rows kept (oldest pruned)


def _tokens(u) -> dict:
    g = (lambda k: int((u.get(k) if isinstance(u, dict) else getattr(u, k, 0)) or 0))
    return {"input": g("input_tokens"), "output": g("output_tokens"), "cache_read": g("cache_read_input_tokens"),
            "cache_write": g("cache_creation_input_tokens")}


def cost(model: str, u) -> float | None:
    p = PRICES.get(model)
    if not p:
        return None
    t = _tokens(u)
    if t["cache_read"] and p[2] is None:
        return None
    return (t["input"] * p[0] + t["output"] * p[1] + t["cache_read"] * (p[2] or 0) + t["cache_write"] * p[0] * 1.25) / 1e6


def record(kind: str, model: str, u) -> None:
    """Best-effort: metering must never break the feature that was metered."""
    try:
        store.put("usage", {"kind": kind, "model": model, **_tokens(u), "cost": cost(model, u), "at": time.time()})
        rows = store.find("usage")
        for old in rows[KEEP:]:
            store.delete("usage", old["id"])
    except Exception:
        pass


def summary(days: int = 30) -> dict:
    since = time.time() - days * 86400
    rows = [r for r in store.find("usage") if r["at"] >= since]
    by: dict = {}
    for r in rows:
        k = by.setdefault(r["kind"], {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cost": 0.0, "unpriced": 0})
        k["calls"] += 1
        for f in ("input", "output", "cache_read"):
            k[f] += r[f]
        if r["cost"] is None:
            k["unpriced"] += 1
        else:
            k["cost"] += r["cost"]
    fresh = sum(r["input"] + r["cache_write"] for r in rows)
    cached = sum(r["cache_read"] for r in rows)
    return {"days": days, "calls": len(rows), "cost_usd": round(sum(r["cost"] or 0 for r in rows), 6),
            "unpriced_calls": sum(r["cost"] is None for r in rows),
            "input_tokens": fresh + cached, "output_tokens": sum(r["output"] for r in rows),
            "cache_hit_ratio": round(cached / (fresh + cached), 3) if fresh + cached else None,
            "by_kind": dict(sorted(by.items(), key=lambda kv: -kv[1]["cost"]))}
