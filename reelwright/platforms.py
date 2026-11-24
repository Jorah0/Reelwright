"""TikTok (Content Posting API) and Instagram (Reels via Graph API) publishing, plus a common registry with YouTube.

Neither TikTok nor Instagram offers native scheduled publishing through their APIs, so scheduled posts to them are
held locally and uploaded at the scheduled time by the pipeline scheduler (YouTube uses its native publishAt).
"""
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

import httpx

from . import youtube
from .config import DATA

CONN_FILE = DATA / "connections.json"


class AuthError(RuntimeError):
    """The platform needs the user to sign in again."""
_lock = threading.Lock()
_pending_oauth: dict[str, str] = {}  # state -> PKCE verifier


def _load() -> dict:
    with _lock:
        try:
            return json.loads(CONN_FILE.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def _save_platform(name: str, data: dict | None) -> None:
    with _lock:
        try:
            cur = json.loads(CONN_FILE.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            cur = {}
        if data is None:
            cur.pop(name, None)
        else:
            cur[name] = {**cur.get(name, {}), **data}
        CONN_FILE.write_text(json.dumps(cur, indent=2), "utf-8")


def _check(r: httpx.Response, what: str) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {}
    err = body.get("error")
    if r.status_code >= 400 or (isinstance(err, dict) and err.get("code") not in (None, "ok")):
        msg = (err or {}).get("message") if isinstance(err, dict) else err
        raise RuntimeError(f"{what} failed ({r.status_code}): {msg or r.text[:300]}")
    return body


# ======================= TikTok =======================
TT_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TT_API = "https://open.tiktokapis.com"
TT_SCOPES = "user.info.basic,video.publish"
TT_PRIVACY = {"public": "PUBLIC_TO_EVERYONE", "unlisted": "FOLLOWER_OF_CREATOR", "private": "SELF_ONLY"}


def tiktok_redirect_uri(port: int) -> str:
    return f"http://localhost:{port}/oauth/tiktok/callback"


def tiktok_set_app(client_key: str, client_secret: str) -> None:
    _save_platform("tiktok", {"client_key": client_key.strip(), "client_secret": client_secret.strip()})


def tiktok_auth_url(port: int) -> str:
    c = _load().get("tiktok", {})
    if not c.get("client_key"):
        raise RuntimeError("Save your TikTok app's Client key and Client secret first.")
    state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(48)
    _pending_oauth[state] = verifier
    challenge = hashlib.sha256(verifier.encode()).hexdigest()  # TikTok desktop PKCE uses a hex SHA-256 challenge
    q = httpx.QueryParams({"client_key": c["client_key"], "scope": TT_SCOPES, "response_type": "code",
                           "redirect_uri": tiktok_redirect_uri(port), "state": state,
                           "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{TT_AUTH}?{q}"


def _tiktok_token_request(data: dict) -> dict:
    c = _load().get("tiktok", {})
    r = httpx.post(f"{TT_API}/v2/oauth/token/", data={"client_key": c.get("client_key"), "client_secret": c.get("client_secret"), **data},
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    body = _check(r, "TikTok token exchange")
    if "access_token" not in body:
        raise RuntimeError(f"TikTok token exchange failed: {body.get('error_description') or body}")
    tok = {"access_token": body["access_token"], "open_id": body.get("open_id"),
           "expires_at": time.time() + int(body.get("expires_in", 86400)) - 120}
    if body.get("refresh_token"):  # keep the stored refresh token when the response omits one
        tok["refresh_token"] = body["refresh_token"]
    return tok


def tiktok_callback(code: str, state: str, port: int) -> None:
    verifier = _pending_oauth.pop(state, None)
    if not verifier:
        raise RuntimeError("Unknown or expired sign-in attempt — start the TikTok connection again.")
    tok = _tiktok_token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": tiktok_redirect_uri(port), "code_verifier": verifier})
    _save_platform("tiktok", tok)
    try:
        info = httpx.get(f"{TT_API}/v2/user/info/?fields=open_id,display_name,avatar_url", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=20).json()
        u = info.get("data", {}).get("user", {})
        _save_platform("tiktok", {"display_name": u.get("display_name"), "avatar": u.get("avatar_url")})
    except (httpx.HTTPError, ValueError):
        pass


def _tiktok_access() -> str:
    c = _load().get("tiktok", {})
    if not c.get("access_token"):
        raise AuthError("TikTok is not connected — connect it on the Publish page.")
    if time.time() > c.get("expires_at", 0):
        if not c.get("refresh_token"):
            raise AuthError("TikTok sign-in expired — reconnect TikTok on the Publish page.")
        try:
            tok = _tiktok_token_request({"grant_type": "refresh_token", "refresh_token": c["refresh_token"]})
        except (RuntimeError, httpx.HTTPError) as e:
            raise AuthError(f"TikTok sign-in expired — reconnect TikTok on the Publish page. ({e})") from None
        _save_platform("tiktok", tok)
        return tok["access_token"]
    return c["access_token"]


def tiktok_chunks(size: int, chunk: int = 10 * 1024 * 1024) -> list[tuple[int, int]]:
    """TikTok rules: files ≤64 MB go in one chunk; otherwise fixed-size chunks with the remainder merged into the last."""
    if size <= 64 * 1024 * 1024:
        return [(0, size - 1)]
    count = size // chunk
    return [(i * chunk, (size - 1) if i == count - 1 else (i + 1) * chunk - 1) for i in range(count)]


def tiktok_upload(path: Path, caption: str, privacy: str, progress=None, cover_t: float | None = None) -> dict:
    token = _tiktok_access()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    creator = _check(httpx.post(f"{TT_API}/v2/post/publish/creator_info/query/", headers=h, timeout=30), "TikTok creator info")["data"]
    options = creator.get("privacy_level_options") or ["SELF_ONLY"]
    level = TT_PRIVACY.get(privacy, "SELF_ONLY")
    if level not in options:  # unaudited apps can only post SELF_ONLY
        level = "SELF_ONLY" if "SELF_ONLY" in options else options[0]
    size = path.stat().st_size
    chunks = tiktok_chunks(size)
    init = _check(httpx.post(f"{TT_API}/v2/post/publish/video/init/", headers=h, timeout=30, json={
        "post_info": {"title": caption[:2200], "privacy_level": level, "disable_duet": False, "disable_comment": False,
                      "disable_stitch": False, "video_cover_timestamp_ms": int((cover_t or 1.0) * 1000)},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunks[0][1] + 1, "total_chunk_count": len(chunks)},
    }), "TikTok upload init")["data"]
    with open(path, "rb") as f:
        for a, b in chunks:
            f.seek(a)
            r = httpx.put(init["upload_url"], content=f.read(b - a + 1), timeout=300,
                          headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {a}-{b}/{size}"})
            if r.status_code >= 400:
                raise RuntimeError(f"TikTok chunk upload failed ({r.status_code}): {r.text[:300]}")
            if progress:
                progress((b + 1) / size * 0.9)
    for _ in range(120):  # TikTok processes the video asynchronously
        st = _check(httpx.post(f"{TT_API}/v2/post/publish/status/fetch/", headers=h, json={"publish_id": init["publish_id"]}, timeout=30),
                    "TikTok status")["data"]
        if st.get("status") == "PUBLISH_COMPLETE":
            ids = st.get("publicaly_available_post_id") or []
            url = f"https://www.tiktok.com/@/video/{ids[0]}" if ids else None
            return {"video_id": str(ids[0]) if ids else init["publish_id"], "url": url, "privacy": level}
        if st.get("status") == "FAILED":
            raise RuntimeError(f"TikTok rejected the post: {st.get('fail_reason')}")
        time.sleep(5)
    return {"video_id": init["publish_id"], "url": None, "privacy": level, "note": "Still processing on TikTok"}


# ======================= Instagram =======================
IG_API = "https://graph.facebook.com/v27.0"


def instagram_set(access_token: str, ig_user_id: str) -> dict:
    r = httpx.get(f"{IG_API}/{ig_user_id.strip()}", params={"fields": "username,profile_picture_url,followers_count",
                                                            "access_token": access_token.strip()}, timeout=20)
    info = _check(r, "Instagram account check")
    _save_platform("instagram", {"access_token": access_token.strip(), "ig_user_id": ig_user_id.strip(),
                                 "username": info.get("username"), "avatar": info.get("profile_picture_url"),
                                 "followers": info.get("followers_count")})
    return info


def instagram_upload(path: Path, caption: str, progress=None) -> dict:
    c = _load().get("instagram", {})
    if not c.get("access_token"):
        raise AuthError("Instagram is not connected — connect it on the Publish page.")
    tok, ig = c["access_token"], c["ig_user_id"]
    cont = _check(httpx.post(f"{IG_API}/{ig}/media", timeout=30, params={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200], "share_to_feed": "true", "access_token": tok}),
        "Instagram container")
    size = path.stat().st_size
    with open(path, "rb") as f:
        r = httpx.post(cont["uri"], content=f.read(), timeout=600,
                       headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)})
    try:
        ok = r.status_code < 400 and r.json().get("success")
    except ValueError:
        ok = False
    if not ok:
        raise RuntimeError(f"Instagram video upload failed: {r.text[:300]}")
    if progress:
        progress(0.7)
    for _ in range(120):
        st = _check(httpx.get(f"{IG_API}/{cont['id']}", params={"fields": "status_code,status", "access_token": tok}, timeout=30), "Instagram status")
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video: {st.get('status')}")
        time.sleep(5)
    else:
        raise RuntimeError("Instagram is still processing the video after 10 minutes — try publishing again later.")
    pub = _check(httpx.post(f"{IG_API}/{ig}/media_publish", params={"creation_id": cont["id"], "access_token": tok}, timeout=60), "Instagram publish")
    link = httpx.get(f"{IG_API}/{pub['id']}", params={"fields": "permalink", "access_token": tok}, timeout=20).json().get("permalink")
    return {"video_id": pub["id"], "url": link}


# ======================= registry =======================
PLATFORMS = ("youtube", "tiktok", "instagram")


def status() -> dict:
    conn = _load()
    tt, ig = conn.get("tiktok", {}), conn.get("instagram", {})
    return {
        "youtube": youtube.status(),
        "tiktok": {"connected": bool(tt.get("access_token")), "has_app": bool(tt.get("client_key")),
                   "name": tt.get("display_name"), "avatar": tt.get("avatar")},
        "instagram": {"connected": bool(ig.get("access_token")), "name": ig.get("username"), "avatar": ig.get("avatar"),
                      "followers": ig.get("followers")},
    }


def connected(platform: str) -> bool:
    if platform == "youtube":
        return youtube.status()["connected"]
    return bool(_load().get(platform, {}).get("access_token"))


def disconnect(platform: str) -> None:
    if platform == "youtube":
        youtube.disconnect()
    elif platform == "tiktok":
        _save_platform("tiktok", {"access_token": None, "refresh_token": None, "display_name": None, "avatar": None})
    else:
        _save_platform(platform, None)


def publish(platform: str, path: Path, u: dict, progress=None, cover: Path | None = None) -> dict:
    caption = u.get("caption") or u["title"]
    if platform == "youtube":
        res = youtube.upload(path, u["title"], u["description"], u["tags"], u["category_id"], u["privacy"],
                             u["publish_at"], u["made_for_kids"], progress)
        if cover:
            try:
                youtube.set_thumbnail(res["video_id"], cover)
                res["thumbnail"] = True
            except Exception as e:  # unverified channels can't set custom thumbnails: the post itself still succeeded
                res["note"] = f"Posted; custom thumbnail not set ({str(e)[:140]})"
        return res
    if platform == "tiktok":
        return tiktok_upload(path, caption, u["privacy"], progress, u.get("cover_t"))
    if platform == "instagram":
        return instagram_upload(path, caption, progress)
    raise RuntimeError(f"Unknown platform {platform}")

