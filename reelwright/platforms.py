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


def _duration(path: Path) -> float:
    from . import media
    return float(media.probe(path)["duration"])


TT_AUTH_ERRORS = {"access_token_invalid", "scope_not_authorized", "scope_permission_missed"}
TT_HINTS = {
    "unaudited_client_can_only_post_to_private_accounts": "Until TikTok audits your app, set your TikTok account to private to post.",
    "spam_risk_too_many_posts": "TikTok's daily post limit for this account was reached; try again tomorrow.",
    "spam_risk_user_banned_from_posting": "TikTok has blocked this account from posting through apps.",
    "reached_active_user_cap": "Your TikTok app reached its daily limit of posting users (5 until it is audited).",
    "privacy_level_option_mismatch": "That privacy option isn't available for this TikTok account anymore; pick another.",
    "rate_limit_exceeded": "TikTok is rate-limiting; it will be retried.",
}


def _tt(r: httpx.Response, what: str) -> dict:
    """TikTok responses always carry error.code ("ok" on success); map the documented codes to useful actions."""
    try:
        err = (r.json().get("error") or {})
    except ValueError:
        err = {}
    code = err.get("code") if isinstance(err, dict) else None
    if code in TT_AUTH_ERRORS or r.status_code == 401:
        raise AuthError(f"TikTok sign-in expired or lacks permission — reconnect TikTok on the Publish page. ({code})")
    if code in TT_HINTS:
        raise RuntimeError(f"{what}: {TT_HINTS[code]}")
    return _check(r, what)


def tiktok_creator() -> dict:
    """What TikTok allows this creator right now (shown in the publish dialog, as TikTok's guidelines require)."""
    h = {"Authorization": f"Bearer {_tiktok_access()}", "Content-Type": "application/json; charset=UTF-8"}
    return _tt(httpx.post(f"{TT_API}/v2/post/publish/creator_info/query/", headers=h, timeout=30), "TikTok creator info")["data"]


def tiktok_upload(path: Path, caption: str, opts: dict | None = None, progress=None, cover_t: float | None = None) -> dict:
    """Direct Post. opts: privacy_level (a TikTok value the user picked), allow_comment / allow_duet / allow_stitch
    (off unless turned on, never on when the creator disabled them), brand_organic / brand_content (disclosures)."""
    opts = opts or {}
    token = _tiktok_access()
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    creator = _tt(httpx.post(f"{TT_API}/v2/post/publish/creator_info/query/", headers=h, timeout=30), "TikTok creator info")["data"]
    options = creator.get("privacy_level_options") or ["SELF_ONLY"]
    level = opts.get("privacy_level") or "SELF_ONLY"
    if level not in options:  # e.g. unaudited apps / private accounts: fall back to the most private allowed option
        level = "SELF_ONLY" if "SELF_ONLY" in options else options[-1]
    if opts.get("brand_content") and level == "SELF_ONLY":
        raise RuntimeError("Branded content can't be private on TikTok: choose a visibility other than Only me.")
    limit = creator.get("max_video_post_duration_sec")
    dur = _duration(path)
    if limit and dur > limit:
        raise RuntimeError(f"This clip is {dur:.0f} s; TikTok allows this account up to {limit} s per post.")
    size = path.stat().st_size
    chunks = tiktok_chunks(size)
    init = _tt(httpx.post(f"{TT_API}/v2/post/publish/video/init/", headers=h, timeout=30, json={
        "post_info": {"title": caption[:2200], "privacy_level": level,
                      "disable_comment": bool(creator.get("comment_disabled") or not opts.get("allow_comment")),
                      "disable_duet": bool(creator.get("duet_disabled") or not opts.get("allow_duet")),
                      "disable_stitch": bool(creator.get("stitch_disabled") or not opts.get("allow_stitch")),
                      "brand_content_toggle": bool(opts.get("brand_content")), "brand_organic_toggle": bool(opts.get("brand_organic")),
                      "video_cover_timestamp_ms": int(max(0.0, min(1.0 if cover_t is None else cover_t, dur - 0.1)) * 1000)},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunks[0][1] + 1, "total_chunk_count": len(chunks)},
    }), "TikTok upload init")["data"]
    with open(path, "rb") as f:
        for a, b in chunks:  # sequential, as TikTok requires; 206 = more expected, 201 = complete
            f.seek(a)
            r = httpx.put(init["upload_url"], content=f.read(b - a + 1), timeout=300,
                          headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {a}-{b}/{size}"})
            if r.status_code == 403:
                raise RuntimeError("TikTok's upload link expired (they last one hour); the post will be retried.")
            if r.status_code >= 400:
                raise RuntimeError(f"TikTok chunk upload failed ({r.status_code}): {r.text[:300]}")
            if progress:
                progress((b + 1) / size * 0.9)
    for _ in range(120):  # TikTok processes asynchronously; 5 s polling stays under its 30/min status limit
        st = _tt(httpx.post(f"{TT_API}/v2/post/publish/status/fetch/", headers=h, json={"publish_id": init["publish_id"]}, timeout=30),
                 "TikTok status")["data"]
        if st.get("status") == "PUBLISH_COMPLETE":
            ids = st.get("publicaly_available_post_id") or []  # (sic) TikTok's field name; empty until public + moderated
            user = creator.get("creator_username")
            url = f"https://www.tiktok.com/@{user}/video/{ids[0]}" if ids and user else None
            return {"video_id": str(ids[0]) if ids else init["publish_id"], "url": url, "privacy": level,
                    **({} if ids else {"note": "Posted; TikTok shows the link once it's public and reviewed"})}
        if st.get("status") == "FAILED":
            raise RuntimeError(f"TikTok rejected the post: {st.get('fail_reason')}")
        time.sleep(5)
    return {"video_id": init["publish_id"], "url": None, "privacy": level, "note": "Still processing on TikTok"}


# ======================= Instagram =======================
IG_VERSION = "v27.0"  # latest Graph API version (introduced 4 Nov 2026)
IG_API = f"https://graph.facebook.com/{IG_VERSION}"
# Two Meta flows give two kinds of token: "Instagram API with Facebook Login" (needs a linked Facebook Page; token works on
# graph.facebook.com) and "Instagram API with Instagram Login" (no Page; Instagram App ID; token works on graph.instagram.com).
IG_HOSTS = {"facebook": IG_API, "instagram": f"https://graph.instagram.com/{IG_VERSION}"}


def _ig_api() -> str:
    return IG_HOSTS.get(_load().get("instagram", {}).get("host", "facebook"), IG_API)
IG_MAX_BYTES = 300 * 1000 * 1000


def _ig(r: httpx.Response, what: str) -> dict:
    try:
        err = r.json().get("error")
    except ValueError:
        err = None
    if isinstance(err, dict) and (err.get("code") == 190 or err.get("type") == "OAuthException" and err.get("code") in (102, 463, 467)):
        raise AuthError("Instagram access token expired or was revoked — reconnect Instagram on the Publish page "
                        "(use a long-lived token: they last about 60 days).")
    if isinstance(err, dict) and err.get("code") in (4, 9, 32, 613) or "content_publishing_limit" in str(err):
        raise RuntimeError(f"{what}: Instagram rate limit (100 API posts per 24 h) reached; try later.")
    return _check(r, what)


def ig_caption(caption: str) -> str:
    """Instagram allows 2,200 characters, 30 hashtags and 20 @mentions: keep the first ones, drop the extra."""
    import re

    count = {"#": 0, "@": 0}

    def keep(m):  # tags separated by spaces or new lines both count
        count[m.group(1)] += 1
        return m.group(0) if count[m.group(1)] <= (30 if m.group(1) == "#" else 20) else ""

    out = re.sub(r"(?<!\S)([#@])\S+", keep, caption)
    return re.sub(r"[ \t]{2,}", " ", out).strip()[:2200]


def clean_ig_token(raw: str) -> str:
    """Strip what usually comes along when a token is copied, and name the common wrong pastes, before Meta's
    unhelpful 'Cannot parse access token' (190) can appear."""
    import re

    tok = (raw or "").strip().strip("\"'`").strip()
    tok = re.sub(r"^(access_token=|Bearer\s+)", "", tok, flags=re.I).strip()
    tok = tok.split("&", 1)[0]  # copied from a URL: access_token=...&expires_in=...
    if tok.endswith("#_"):
        raise RuntimeError("That looks like the one-time sign-in code from the redirect URL, not an access token. Use "
                           "Generate token in the Meta app (Instagram → API setup), or exchange the code for a token first.")
    if tok.isdigit():
        raise RuntimeError("That's an App ID (or account ID), not an access token. Paste the long token from Generate token.")
    if re.fullmatch(r"[0-9a-f]{32}", tok):
        raise RuntimeError("That looks like your App Secret, not an access token. Keep the secret private and paste the token.")
    if len(tok) < 40 or re.search(r"\s", tok):
        raise RuntimeError("That token is too short or contains spaces: it was probably cut off when copying. Copy it again.")
    return tok


def _ig_profile(base: str, uid: str, tok: str) -> dict:
    """The account's username (required), plus picture and follower count when this account type / API offers them:
    Meta rejects a whole request over one field it doesn't have ("#100 Tried accessing nonexisting field")."""
    info = _check(httpx.get(f"{base}/{uid}", params={"fields": "username", "access_token": tok}, timeout=20), "Instagram account check")
    for field in ("profile_picture_url", "followers_count"):
        try:
            r = httpx.get(f"{base}/{uid}", params={"fields": field, "access_token": tok}, timeout=20)
            if r.status_code < 400:
                info[field] = r.json().get(field)
        except (httpx.HTTPError, ValueError):
            pass
    return info


class _Specific(RuntimeError):
    """A diagnosis precise enough to show on its own (not merged with the other host's error)."""


FB_REQUIRED = ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement")
_PAGE_FIELDS = "name,instagram_business_account"


def _fb_pages(base: str, tok: str) -> list[dict]:
    """Pages this person manages directly, plus Pages held in a Meta Business portfolio (only visible with
    business_management)."""
    r = httpx.get(f"{base}/me/accounts", params={"fields": _PAGE_FIELDS, "limit": 100, "access_token": tok}, timeout=20)
    pages = list(_check(r, "Facebook Pages lookup").get("data") or [])
    try:
        r = httpx.get(f"{base}/me/businesses", params={"fields": f"owned_pages{{{_PAGE_FIELDS}}},client_pages{{{_PAGE_FIELDS}}}",
                                                        "limit": 50, "access_token": tok}, timeout=20)
        if r.status_code < 400:
            for b in r.json().get("data") or []:
                for kind in ("owned_pages", "client_pages"):
                    pages += (b.get(kind) or {}).get("data") or []
    except (httpx.HTTPError, ValueError):
        pass
    return pages


def _fb_permissions(base: str, tok: str) -> dict:
    try:
        r = httpx.get(f"{base}/me/permissions", params={"access_token": tok}, timeout=20)
        return {p["permission"]: p["status"] for p in r.json().get("data") or []} if r.status_code < 400 else {}
    except (httpx.HTTPError, ValueError, KeyError):
        return {}


def _fb_find_ig(base: str, tok: str) -> str:
    """Facebook Login: the Instagram professional account linked to one of the Pages this person manages."""
    pages = _fb_pages(base, tok)
    for p in pages:
        ig = (p.get("instagram_business_account") or {}).get("id")
        if ig:
            return str(ig)  # ponytail: first linked account; multi-account support will let the user pick
    if pages:
        raise _Specific(f"Your Facebook Page ({', '.join(p.get('name', '?') for p in pages)}) isn't linked to an Instagram "
                        "professional account. Link them in Page settings → Linked accounts → Instagram, then generate the token again.")
    granted = _fb_permissions(base, tok)
    missing = [p for p in FB_REQUIRED if granted.get(p) != "granted"]
    if missing:  # say exactly which ones this token lacks (declined, or never requested)
        raise _Specific(f"This token is missing: {', '.join(missing)}. In Graph API Explorer add every one of "
                        f"{', '.join(FB_REQUIRED)}, click Generate Access Token again, and approve them (if Facebook doesn't ask, "
                        "click 'Edit previous settings' in the pop-up).")
    raise _Specific("This token has the right permissions but no Facebook Page was shared with it. Click Generate Access Token again, "
                    "choose 'Edit previous settings' in the pop-up and tick your Page and its Instagram account. If your Page is "
                    "managed in a Meta Business portfolio (Business Suite), also add the business_management permission.")


def instagram_set(access_token: str, ig_user_id: str = "") -> dict:
    """Accept a token from either Meta flow: try the host that matches it, then the other one. The Instagram account ID
    is optional: Instagram Login reads it from /me; Facebook Login finds it through the linked Page (also when the
    Facebook user or Page ID was typed by mistake)."""
    tok, uid = clean_ig_token(access_token), (ig_user_id or "").strip()
    order = ("instagram", "facebook") if tok.startswith("IG") else ("facebook", "instagram")
    errors: list[str] = []
    for host in order:
        base = IG_HOSTS[host]
        try:
            if host == "instagram":
                if uid:
                    found = uid
                else:
                    me = _check(httpx.get(f"{base}/me", params={"fields": "user_id,username", "access_token": tok}, timeout=20),
                                "Instagram account check")
                    found = str(me.get("user_id") or me.get("id") or "")
                info = _ig_profile(base, found, tok)
            else:
                found, info = uid, None
                if uid:
                    try:
                        info = _ig_profile(base, uid, tok)
                    except RuntimeError:  # e.g. "(#12) username deprecated": that was a Facebook user/Page ID, not Instagram's
                        found = ""
                if not found:
                    found = _fb_find_ig(base, tok)
                    info = _ig_profile(base, found, tok)
        except _Specific:
            raise
        except (RuntimeError, httpx.HTTPError) as e:
            errors.append(str(e))
            continue
        _save_platform("instagram", {"access_token": tok, "ig_user_id": found, "host": host, "username": info.get("username"),
                                     "avatar": info.get("profile_picture_url"), "followers": info.get("followers_count")})
        return info
    raise RuntimeError("Instagram didn't accept that token. Check it has instagram_business_basic + instagram_business_content_publish "
                       "(Instagram Login) or instagram_basic + instagram_content_publish + pages_show_list + pages_read_engagement "
                       f"(Facebook Login). ({'; '.join(errors)[:300]})")


def instagram_upload(path: Path, caption: str, progress=None, cover_t: float | None = None) -> dict:
    c = _load().get("instagram", {})
    if not c.get("access_token"):
        raise AuthError("Instagram is not connected — connect it on the Publish page.")
    tok, ig = c["access_token"], c["ig_user_id"]
    api = _ig_api()
    size, dur = path.stat().st_size, _duration(path)
    if dur < 3:  # Reels spec: 3 s – 15 min, ≤ 300 MB (checked here so the post fails clearly, not deep in Meta's processing)
        raise RuntimeError(f"Instagram Reels must be at least 3 seconds (this clip is {dur:.1f} s).")
    if dur > 15 * 60:
        raise RuntimeError("Instagram Reels can be at most 15 minutes.")
    if size > IG_MAX_BYTES:
        raise RuntimeError(f"Instagram Reels can be at most 300 MB (this file is {size / 1e6:.0f} MB).")
    params = {"media_type": "REELS", "upload_type": "resumable", "caption": ig_caption(caption), "share_to_feed": "true",
              "access_token": tok}
    if cover_t is not None:
        params["thumb_offset"] = int(max(0.0, cover_t) * 1000)  # the auto-picked cover frame, in ms
    cont = _ig(httpx.post(f"{api}/{ig}/media", timeout=30, params=params), "Instagram container")
    # the reference shows {"id", "uri"}; the publishing guide shows only {"id"}: use the uri if given, else build it
    upload_url = cont.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{IG_VERSION}/{cont['id']}"
    with open(path, "rb") as f:  # streamed from disk, not read into memory
        r = httpx.post(upload_url, content=f, timeout=600,
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
        st = _ig(httpx.get(f"{api}/{cont['id']}", params={"fields": "status_code,status", "access_token": tok}, timeout=30),
                 "Instagram status")
        if st.get("status_code") in ("FINISHED", "PUBLISHED"):
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video: {st.get('status')}")
        time.sleep(5)
    else:
        raise RuntimeError("Instagram is still processing the video after 10 minutes — try publishing again later.")
    pub = _ig(httpx.post(f"{api}/{ig}/media_publish", params={"creation_id": cont["id"], "access_token": tok}, timeout=60),
              "Instagram publish")
    try:  # the post is live; a missing permalink must not turn it into a failure
        link = httpx.get(f"{api}/{pub['id']}", params={"fields": "permalink", "access_token": tok}, timeout=20).json().get("permalink")
    except (httpx.HTTPError, ValueError):
        link = None
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
        opts = u.get("tiktok") or {"privacy_level": TT_PRIVACY.get(u.get("privacy"), "SELF_ONLY")}  # Autopilot: its privacy setting
        return tiktok_upload(path, caption, opts, progress, u.get("cover_t"))
    if platform == "instagram":
        return instagram_upload(path, caption, progress, u.get("cover_t"))
    raise RuntimeError(f"Unknown platform {platform}")

