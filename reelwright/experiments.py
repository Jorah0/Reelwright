"""Autopilot experiments, per account: Thompson sampling over a few posting choices (does a question hook or a statement
do better here? shorter or longer clips? which time of day?). Only options the creator ticked in the Lab are ever
chosen (setting "experiments"); the posting slot only when Autopilot picks the times ("best times"). Each post records
what was actually posted as upload["arm"]; a post wins when its lift (metrics.lifts) is ≥ 0, i.e. at least that
account's normal. Exploring a choice that looks worse is capped at a quarter of recent posts.
"""
import random
from datetime import datetime

DIMS = {"hook": ("question", "statement"), "length": ("short", "long"), "slot": ("morning", "afternoon", "evening", "night")}
CLIP_DIMS = ("hook", "length")  # chosen by which clip is posted; "slot" by when
SLOT_HOURS = {"morning": range(6, 12), "afternoon": range(12, 17), "evening": range(17, 22), "night": (*range(22, 24), *range(0, 6))}
EXPLORE_CAP, RECENT = 0.25, 20


def allowed(cfg, slot: bool = True) -> dict[str, list[str]]:
    """The options the creator allows per dimension; a dimension with fewer than 2 is no experiment. Off: {}."""
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return {}
    out = {}
    for dim, vals in DIMS.items():
        mine = cfg.get(dim) if isinstance(cfg.get(dim), list) else []
        pick = [v for v in vals if v in mine]
        if len(pick) >= 2 and (slot or dim != "slot"):
            out[dim] = pick
    return out


def _lift(u: dict):
    v = (((u.get("metrics") or {}).get("lift")) or {}).get("value")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def stats(uploads: list[dict], account_id: str) -> dict[str, dict[str, list[int]]]:
    """{dim: {option: [wins, losses]}} from this account's posts that have a result."""
    out: dict = {}
    for u in uploads:
        if u.get("account_id") != account_id or not isinstance(u.get("arm"), dict) or (lf := _lift(u)) is None:
            continue
        for dim, v in u["arm"].items():
            if dim in DIMS and v in DIMS[dim]:
                out.setdefault(dim, {}).setdefault(v, [0, 0])[0 if lf >= 0 else 1] += 1
    return out


def recent_explore(uploads: list[dict], account_id: str) -> list[bool]:
    arms = [u["arm"] for u in uploads if u.get("account_id") == account_id and isinstance(u.get("arm"), dict) and u["arm"]]
    return [bool(a.get("explore")) for a in arms[:RECENT]]  # uploads come newest first


def choose(opts: dict[str, list[str]], st: dict, history: list[bool], rng=random) -> dict:
    """One draw per dimension from Beta(1 + wins, 1 + losses); the posterior-best option instead when recent posts
    already explored too much. "explore" marks a draw that took an option with a lower posterior mean."""
    if not opts:
        return {}
    recent = history[:RECENT]
    over = bool(recent) and sum(recent) / len(recent) >= EXPLORE_CAP
    arm, explored = {}, False
    for dim, vals in opts.items():
        wl = {v: (st.get(dim) or {}).get(v, [0, 0]) for v in vals}
        mean = {v: (1 + w) / (2 + w + lo) for v, (w, lo) in wl.items()}
        pick = max(vals, key=lambda v: rng.betavariate(1 + wl[v][0], 1 + wl[v][1]))
        if mean[pick] < max(mean.values()):
            if over:
                pick = max(vals, key=mean.get)
            else:
                explored = True
        arm[dim] = pick
    return {**arm, "explore": explored}


def traits(clip: dict, target: float) -> dict:
    """What a clip is on the clip dimensions."""
    f = clip.get("features") or {}
    return {"hook": "question" if (f.get("question") or 0) > 0.5 else "statement",
            "length": "short" if clip["end"] - clip["start"] <= target else "long"}


def plan(ranked: list[dict], n: int, opts: dict, st: dict, history: list[bool], target: float, rng=random) -> list[tuple[dict, dict]]:
    """n (clip, arm) picks for one account, from its ranking (best first). Without experiments: simply the top n.
    With them: each post draws an arm and takes the best-ranked clip that matches it (else the best one left); the arm
    then records what was actually posted, so results are credited to the truth."""
    left, out = list(ranked), []
    for _ in range(min(n, len(left))):
        want = choose(opts, st, history, rng)
        dims = [d for d in CLIP_DIMS if d in want]
        c = next((c for c in left if all(traits(c, target)[d] == want[d] for d in dims)), left[0])
        left.remove(c)
        if want:
            history.insert(0, want["explore"])
            want = {**want, **{d: traits(c, target)[d] for d in dims}}
        out.append((c, want))
    return out


def slot_of(iso: str) -> str:
    h = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().hour  # the creator's local time
    return next(k for k, hours in SLOT_HOURS.items() if h in hours)


def pick_time(cands: list[str], slot: str | None) -> str:
    """The first suggested time in the wanted slot, else the first suggestion."""
    return next((t for t in cands if slot and slot_of(t) == slot), cands[0])
