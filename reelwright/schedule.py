"""Best posting times, per platform and clip type, from the best evidence available:

1. "yours"    — your own posts' real view counts by weekday/hour (needs ≥ 8 posts on that platform; the clip's category is
                used when it alone has ≥ 8),
2. "research" — the platform playbook researched live from the web for your niche (posting_slots, your local time),
3. "default"  — a plain starting point (late morning and early evening every day) until 1 or 2 exist.
A time the user picks always wins; these are only suggestions and Autopilot's "best times" mode.
Times are this PC's local time (the creator's), returned as UTC ISO strings.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import store

MIN_OWN = 8
DEFAULT_HOURS = (11, 18)


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()


def _own_posts(platform: str, category: str | None) -> list[tuple[int, int, float]]:
    """(weekday, hour, log-views) of this platform's posts that have results."""
    out = []
    for u in store.find("upload", platform=platform, status="done"):
        when = u.get("publish_at") or u.get("uploaded_at")
        views = (store.get("feedback", u.get("clip_id") or "") or {}).get("views")
        if not when or not isinstance(views, (int, float)) or (category and u.get("category_id") != category):
            continue
        t = _local(when)
        out.append((t.weekday(), t.hour, math.log1p(views)))
    return out


def _research_slots(platform: str) -> list[tuple[int, int]]:
    prof = next((p for p in store.find("profile") if p.get("active")), None) or {}
    pb = (prof.get("playbooks") or {}).get(platform) or {}
    return [(int(s["weekday"]) % 7, int(s["hour"]) % 24) for s in pb.get("posting_slots") or []
            if isinstance(s, dict) and isinstance(s.get("weekday"), int) and isinstance(s.get("hour"), int)]


def slot_scores(platform: str, category: str | None = None) -> tuple[dict, str]:
    """{(weekday, hour): score 0..1} for every hour of the week, and which source it came from."""
    scores: dict = defaultdict(float)
    posts = _own_posts(platform, category) if category else []
    if len(posts) < MIN_OWN:
        posts = _own_posts(platform, None)
    research = _research_slots(platform)
    if len(posts) >= MIN_OWN:
        source = "yours"
        base = sum(v for _, _, v in posts) / len(posts)
        by: dict = defaultdict(list)
        for d, h, v in posts:
            by[(d, h)].append(v - base)
        for (d, h), vs in by.items():  # spread each hour's result to its neighbours: sparse data, smooth curve
            lift = sum(vs) / len(vs) * min(len(vs), 3) / 3
            for dh in (-2, -1, 0, 1, 2):
                scores[(d, (h + dh) % 24)] += lift * (1 - abs(dh) * 0.3)
        for d, h in research:  # research still nudges hours you haven't tried
            scores[(d, h)] += 0.15
    elif research:
        source = "research"
        for d, h in research:
            for dh in (-1, 0, 1):
                scores[(d, (h + dh) % 24)] += 1.0 if dh == 0 else 0.4
    else:
        source = "default"
        for d in range(7):
            for h in DEFAULT_HOURS:
                scores[(d, h)] = 1.0
    lo = min(scores.values(), default=0.0)
    lo = min(lo, 0.0) if source != "yours" else lo
    hi = max(scores.values(), default=1.0)
    span = (hi - lo) or 1.0
    return {(d, h): round((scores.get((d, h), lo if source == "yours" else 0.0) - lo) / span, 3)
            for d in range(7) for h in range(24)}, source


def scheduled_times(platform: str) -> list[str]:
    return [u.get("publish_at") or u.get("upload_at") for u in store.find("upload", platform=platform)
            if u.get("status") not in ("done", "canceled", "failed") and (u.get("publish_at") or u.get("upload_at"))]


def suggest(platform: str, n: int = 1, after: datetime | None = None, category: str | None = None, min_gap_h: float = 3,
            taken: list[str] | None = None, horizon_days: int = 14) -> list[str]:
    """The n best upcoming posting times (UTC ISO), at least min_gap_h apart from each other and from posts already
    scheduled on this platform; sooner is slightly preferred so a queue doesn't drift into next week."""
    scores, _ = slot_scores(platform, category)
    # step in UTC and convert each candidate on its own: a fixed local offset would be an hour off after a DST change
    start = (after or datetime.now(timezone.utc) + timedelta(minutes=20)).astimezone(timezone.utc)
    start = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    busy = [_local(t) for t in (taken if taken is not None else scheduled_times(platform))]
    cands = []
    for i in range(horizon_days * 24):
        t = (start + timedelta(hours=i)).astimezone()
        cands.append((scores[(t.weekday(), t.hour)] * (1 - 0.015 * i / 24), t))
    chosen: list[datetime] = []
    for _, t in sorted(cands, key=lambda c: (-c[0], c[1])):
        if all(abs((t - b).total_seconds()) >= min_gap_h * 3600 for b in busy + chosen):
            chosen.append(t)
            if len(chosen) == n:
                break
    return [t.astimezone(timezone.utc).isoformat() for t in sorted(chosen)]


def heat(platforms=("youtube", "tiktok", "instagram")) -> dict:
    """For the Schedule tab: per platform, the score of each weekday/hour and where it came from."""
    out = {}
    for p in platforms:
        s, src = slot_scores(p)
        out[p] = {"source": src, "scores": [[d, h, s[(d, h)]] for d in range(7) for h in range(24)]}
    return out
