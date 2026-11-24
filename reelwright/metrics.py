"""Post results per account, each asked with that account's own sign-in:

- YouTube: statistics (Data API, youtube.readonly) and the audience-retention curve (YouTube Analytics API
  https://youtubeanalytics.googleapis.com, scope yt-analytics.readonly: audienceWatchRatio by elapsedVideoTimeRatio);
- TikTok: view/like/comment/share counts (video/query, scope video.list: needs the Display API on the creator's app);
- Instagram: Reel insights (views, reach, saved, shares, average watch time; an insights permission on the token).

A sign-in without the permission raises MissingScope, whose message says exactly what to add. Snapshots are kept at
48 h and 7 d after a post went public (upload["metrics"]), and lifts() turns them into a log2 lift over the median of the
same account's posts at the same age, so a small channel's good day and a big channel's normal day aren't confused.
"""
import math
from datetime import date, datetime, timezone

import httpx
import numpy as np

from . import platforms, youtube

SNAPSHOTS = (("48h", 48), ("7d", 168))
MIN_PEERS = 3
YT_ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"
TT_FIELDS = "id,view_count,like_count,comment_count,share_count"
IG_METRICS = "views,reach,saved,shares,ig_reels_avg_watch_time"
HOLD_OK = 0.65  # share still watching at 3 s below which the opening is losing people
NO_CURVE = "YouTube returned no retention curve for this video yet (it needs more views), so it teaches from its view count only."
NO_CURVE_SHORT = ("YouTube returned no retention curve for this video: it doesn't report one for most Shorts, so this post "
                  "teaches from its view count only.")


class MissingScope(RuntimeError):
    """The account's sign-in lacks a permission; the message says which one and how to add it."""

    def __init__(self, scope: str, message: str):
        super().__init__(message)
        self.scope = scope


def _when(iso) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def posted_at(u: dict) -> datetime | None:
    """When the post went public: a YouTube publishAt when it is later than the upload, else the upload itself."""
    ts = [t for t in (_when(u[k]) for k in ("publish_at", "uploaded_at") if u.get(k)) if t]
    return max(ts) if ts else None


def age_h(u: dict, now: datetime) -> float | None:
    t = posted_at(u)
    return (now - t).total_seconds() / 3600 if t else None


def due(u: dict, now: datetime) -> str | None:
    """The snapshot to take now, if any: the latest one whose age has passed, once. A 48 h snapshot missed until after
    7 d is skipped rather than taken late (it would compare a week of views with two days)."""
    age = age_h(u, now)
    if age is None:
        return None
    for label, hours in reversed(SNAPSHOTS):
        if age >= hours:
            return None if label in (u.get("metrics") or {}) else label
    return None


def due_soon(u: dict, now: datetime) -> bool:
    """A snapshot fell due within the last day (worth an hourly pull; after that the daily pull still takes it, so a post
    that can't be read — no permission, signed out, deleted — never keeps the hourly pulls going)."""
    label, age = due(u, now), age_h(u, now)
    return bool(label) and age is not None and age < dict(SNAPSHOTS)[label] + 24


def lift(views: float, peers: list[float]) -> float | None:
    if len(peers) < MIN_PEERS:
        return None
    return round(math.log2((views + 1) / (float(np.median(peers)) + 1)), 3)


def lifts(uploads: list[dict]) -> dict[str, dict]:
    """{upload id: {"label", "value"}} for each post with a snapshot: its views against the median of its account's
    other posts at the same age, at the most mature age that has enough of them."""
    snap = {}
    for u in uploads:
        for label, _ in SNAPSHOTS:
            v = ((u.get("metrics") or {}).get(label) or {}).get("views")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                snap.setdefault((u.get("account_id"), u.get("platform", "youtube"), label), []).append((u["id"], float(v)))
    out = {}
    for u in uploads:
        for label, _ in reversed(SNAPSHOTS):
            rows = snap.get((u.get("account_id"), u.get("platform", "youtube"), label)) or []
            mine = next((v for uid, v in rows if uid == u["id"]), None)
            if mine is not None and (val := lift(mine, [v for uid, v in rows if uid != u["id"]])) is not None:
                out[u["id"]] = {"label": label, "value": val}
                break
    return out


# ---------------- per-platform counts ----------------

def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def youtube_stats(acc: dict, ids: list[str]) -> dict[str, dict]:
    svc, out = youtube._service(acc["id"]), {}
    for i in range(0, len(ids), 50):
        for it in svc.videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute().get("items", []):
            st = it.get("statistics", {})
            out[it["id"]] = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                             "comments": int(st.get("commentCount", 0))}
    return out


def tiktok_stats(acc: dict, ids: list[str]) -> dict[str, dict]:
    ids = [i for i in ids if i and not str(i).startswith("v_pub")]  # a publish id: TikTok had no public post id yet
    if not ids:
        return {}
    h = {"Authorization": f"Bearer {platforms._tiktok_access(acc['id'])}", "Content-Type": "application/json; charset=UTF-8"}
    out = {}
    for i in range(0, len(ids), 20):
        r = httpx.post(f"{platforms.TT_API}/v2/video/query/?fields={TT_FIELDS}", headers=h,
                       json={"filters": {"video_ids": ids[i:i + 20]}}, timeout=30)
        code = (_json(r).get("error") or {}).get("code")
        if code in ("scope_not_authorized", "scope_permission_missed"):
            raise MissingScope("video.list", f"{acc.get('name') or 'TikTok'}: view counts need the video.list permission. Add the "
                                             "Display API (video.list) to your TikTok app, turn on “Ask TikTok for view counts” in the "
                                             "Lab, then Reconnect this account on the Publish page.")
        for v in platforms._tt(r, "TikTok results")["data"].get("videos") or []:
            out[str(v["id"])] = {"views": int(v.get("view_count") or 0), "likes": int(v.get("like_count") or 0),
                                 "comments": int(v.get("comment_count") or 0), "shares": int(v.get("share_count") or 0)}
    return out


def instagram_stats(acc: dict, ids: list[str]) -> dict[str, dict]:
    api, out = platforms._ig_api(acc), {}
    for mid in ids:
        r = httpx.get(f"{api}/{mid}/insights", params={"metric": IG_METRICS, "access_token": acc.get("access_token")}, timeout=20)
        err = _json(r).get("error") or {}
        if err.get("code") in (10, 200) or "permission" in str(err.get("message", "")).lower():
            raise MissingScope("instagram_manage_insights",
                               f"{acc.get('name') or 'Instagram'}: Reel results need an insights permission on the token: "
                               "instagram_business_manage_insights (Instagram Login) or instagram_manage_insights (Facebook "
                               "Login). Generate a new token with it and paste it on the Publish page.")
        try:
            data = platforms._ig(r, "Instagram results").get("data") or []
        except platforms.AuthError:
            raise
        except RuntimeError:  # this Reel only (deleted, or a metric it doesn't have): skip it, keep the others
            continue
        out[str(mid)] = {m["name"]: (m.get("values") or [{}])[0].get("value") for m in data}
    return out


def fetch(acc: dict, ids: list[str]) -> dict[str, dict]:
    """{video id: counts} for this account's posts ({} for a platform with nothing to ask)."""
    fn = {"youtube": youtube_stats, "tiktok": tiktok_stats, "instagram": instagram_stats}.get(acc["platform"])
    return fn(acc, ids) if fn and ids else {}


# ---------------- retention ----------------

def _analytics(aid: str):
    from googleapiclient.discovery import build

    creds = youtube._creds(aid)
    if not creds:
        raise platforms.AuthError("This YouTube channel is not connected: reconnect it on the Publish page.")
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def youtube_retention(aid: str, video_id: str, start: str, length: float) -> dict:
    """The video's audience-retention curve, summarised by retention()."""
    if YT_ANALYTICS in youtube.missing_scopes(aid):
        raise MissingScope(YT_ANALYTICS, "Learning where viewers stop watching needs one more YouTube permission "
                                         f"({YT_ANALYTICS}): Reconnect this channel on the Publish page and approve it.")
    res = _analytics(aid).reports().query(ids="channel==MINE", startDate=start, endDate=date.today().isoformat(),
                                          metrics="audienceWatchRatio", dimensions="elapsedVideoTimeRatio",
                                          filters=f"video=={video_id}").execute()
    return retention(res.get("rows") or [], length)


def retention(rows: list, length: float) -> dict:
    """Rows of [elapsed ratio, audienceWatchRatio] -> {available, curve [[s, share]], hold_3s, drop_s, trim [start, end]}.
    hold_3s = share (of those there at the start) still watching at 3 s; drop_s = where the steepest exit after the
    first second happens; trim = the re-trim these suggest, in the same form as a creator's own (learn.trim_bias): start
    later when the opening loses people, end earlier when the last quarter empties out.
    ponytail: one post's curve, simple steepest-step rules; a learned hook model needs far more posts than most have."""
    pts = []
    for x in rows:
        try:
            r, w = float(x[0]), float(x[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(r) and math.isfinite(w):
            pts.append((r, w))
    pts.sort()
    if len(pts) < 5 or length <= 0:
        return {"available": False, "reason": NO_CURVE_SHORT if length <= 180 else NO_CURVE}
    t = np.array([r * length for r, _ in pts])
    rel = np.array([w for _, w in pts]) / (pts[0][1] or 1e-6)
    d, mid = np.diff(rel), (t[1:] + t[:-1]) / 2
    hold = float(np.interp(3.0, t, rel))
    late = mid >= 1.0
    drop = float(mid[late][np.argmin(d[late])]) if late.any() else None
    start, end = 0.0, 0.0
    early = mid <= 5.0
    if hold < HOLD_OK and early.any():
        start = min(3.0, float(t[1:][early][np.argmin(d[early])]))
    tail = mid >= 0.75 * length
    if tail.any() and rel[-1] < 0.5 * float(np.interp(0.75 * length, t, rel)):
        end = -min(5.0, length - float(mid[tail][np.argmin(d[tail])]))
    return {"available": True, "curve": [[round(float(a), 2), round(float(b), 3)] for a, b in zip(t, rel)],
            "hold_3s": round(hold, 3), "drop_s": round(drop, 1) if drop is not None else None,
            "trim": [round(start, 2), round(end, 2)]}
