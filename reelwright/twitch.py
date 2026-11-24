"""Twitch Helix with an app access token (no sign-in): the clips viewers made of a VOD, as labels for clip finding.

The creator registers an app at dev.twitch.tv and pastes its Client ID and Client secret in Settings → Signals. They are
kept with the other platform app credentials in connections.json (platforms._save_platform("twitch")), the one place
the secrets work (vault) has to cover. GET /helix/clips?broadcaster_id&started_at=<VOD start>&ended_at=<now> pages
through the channel's clips; those whose video_id is this VOD become {t: vod_offset, d, views, title, id}
(vod_offset is null once the VOD is deleted, so those are skipped).
"""
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from . import platforms

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.twitch.tv/helix"
VOD_RE = re.compile(r"twitch\.tv/(?:[\w-]+/)?videos?/v?(\d+)", re.I)
MAX_PAGES = 30  # ponytail: 3,000 clips per VOD; raise it if a huge channel's VODs carry more
_token: dict = {}
_lock = threading.Lock()


def app() -> dict:
    return platforms._load().get("twitch") or {}


def has_app() -> bool:
    a = app()
    return bool(a.get("client_id") and a.get("client_secret"))


def set_app(client_id: str, client_secret: str) -> None:
    """Save the app's credentials; both empty removes them."""
    cid, sec = (client_id or "").strip(), (client_secret or "").strip()
    platforms._save_platform("twitch", {"client_id": cid, "client_secret": sec} if cid and sec else None)
    with _lock:
        _token.clear()


def vod_id(url: str) -> str | None:
    m = VOD_RE.search(url or "")
    return m.group(1) if m else None


def app_token(force: bool = False) -> str:
    """Client-credentials token, cached until shortly before it expires."""
    a = app()
    with _lock:
        if not force and _token.get("id") == a.get("client_id") and _token.get("exp", 0) > time.time() + 60:
            return _token["value"]
        r = httpx.post(TOKEN_URL, data={"client_id": a.get("client_id"), "client_secret": a.get("client_secret"),
                                        "grant_type": "client_credentials"}, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"Twitch refused the app's Client ID / Client secret ({r.status_code}).")
        body = r.json()
        _token.update(id=a.get("client_id"), value=body["access_token"], exp=time.time() + float(body.get("expires_in") or 3600))
        return _token["value"]


def _get(path: str, params: dict) -> dict:
    for retry in (False, True):  # an expired or revoked token: fetch a fresh one once
        r = httpx.get(f"{API}/{path}", params=params, timeout=20,
                      headers={"Client-Id": app().get("client_id", ""), "Authorization": f"Bearer {app_token(force=retry)}"})
        if r.status_code == 401 and not retry:
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Twitch {path} failed ({r.status_code}): {r.text[:200]}")
        return r.json()
    return {}


def clips_for_vod(vod: str) -> list[dict]:
    """Viewer clips of one VOD on its own clock, earliest first."""
    v = (_get("videos", {"id": vod}).get("data") or [None])[0]
    if not v:
        return []
    now, out, cursor = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), [], None
    for _ in range(MAX_PAGES):
        body = _get("clips", {"broadcaster_id": v["user_id"], "started_at": v["created_at"], "ended_at": now, "first": 100,
                              **({"after": cursor} if cursor else {})})
        for c in body.get("data") or []:
            if str(c.get("video_id")) == vod and c.get("vod_offset") is not None:
                out.append({"t": float(c["vod_offset"]), "d": float(c.get("duration") or 0), "views": int(c.get("view_count") or 0),
                            "title": str(c.get("title") or "")[:100], "id": str(c.get("id"))})
        cursor = (body.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    return sorted(out, key=lambda c: c["t"])
