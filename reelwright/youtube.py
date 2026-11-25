"""YouTube Data API v3: OAuth connection (one token per channel, see accounts.py), resumable uploads, scheduled
publishing (publishAt)."""
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from . import accounts, config
from .config import YT_SECRET, YT_TOKEN

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",     # upload videos, set thumbnails
          "https://www.googleapis.com/auth/youtube.readonly",   # channel name, view counts
          "https://www.googleapis.com/auth/youtube.force-ssl",  # move a scheduled video's publish time (videos.update)
          "https://www.googleapis.com/auth/yt-analytics.readonly"]  # where viewers stop watching (metrics.youtube_retention)
_state = {"connecting": False, "error": ""}


def _token(account_id: str | None = None) -> dict | None:
    """That channel's stored sign-in (no id: the first channel; no channels at all: the pre-accounts token file)."""
    acc = accounts.resolve("youtube", account_id)
    if acc or account_id:
        return (acc or {}).get("token")
    try:
        return json.loads(YT_TOKEN.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def missing_scopes(account_id: str | None = None) -> list[str]:
    """Permissions added in newer versions that this sign-in hasn't granted yet (fixed by reconnecting)."""
    granted = set((_token(account_id) or {}).get("scopes") or [])
    return [s for s in SCOPES if granted and s not in granted]


def _creds(account_id: str | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if config.DEMO:  # demo mode: no real sign-in is ever used, so nothing can reach YouTube
        return None
    info = _token(account_id)
    if not info:
        return None
    # the scopes this sign-in actually granted (stored in the token): forcing today's longer list on an older token
    # would make Google refuse the refresh and break all uploads until reconnecting
    creds = Credentials.from_authorized_user_info(info)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        acc = accounts.resolve("youtube", account_id)
        if acc:
            accounts.update(acc["id"], {"token": json.loads(creds.to_json())})
        else:
            YT_TOKEN.write_text(creds.to_json(), "utf-8")
    return creds if creds.valid else None


def _service(account_id: str | None = None):
    from googleapiclient.discovery import build

    creds = _creds(account_id)
    if not creds:
        from .platforms import AuthError

        raise AuthError("This YouTube channel is not connected: reconnect it on the Publish page.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _channel_info(creds) -> dict:
    """The signed-in channel (channels.list mine=true; youtube.readonly allows it)."""
    from googleapiclient.discovery import build

    items = build("youtube", "v3", credentials=creds, cache_discovery=False).channels().list(
        part="snippet,statistics", mine=True).execute().get("items") or []
    if not items:
        raise RuntimeError("That Google account has no YouTube channel: create one on youtube.com, then sign in again.")
    sn, st = items[0]["snippet"], items[0].get("statistics", {})
    return {"ext_id": items[0]["id"], "name": sn.get("title"), "handle": sn.get("customUrl"),
            "avatar": ((sn.get("thumbnails") or {}).get("default") or {}).get("url"), "subscribers": st.get("subscriberCount")}


def status() -> dict:
    out = []
    for a in accounts.all_accounts("youtube"):
        err = ""
        try:
            creds = _creds(a["id"])
        except Exception as e:
            creds, err = None, f"Token refresh failed: {e}"
        if creds and not a.get("ext_id"):  # migrated from the single-channel days: learn its name once
            try:
                a = accounts.link("youtube", a["id"], **_channel_info(creds))
            except Exception as e:
                err = str(e)
        missing = missing_scopes(a["id"]) if creds else []
        out.append({**accounts.public(a), "connected": creds is not None, "error": err, "needs_reconnect": bool(missing),
                    "missing_scopes": missing})
    first = next((a for a in out if a["connected"]), None)
    return {"has_secret": YT_SECRET.exists(), "connecting": _state["connecting"], "error": _state["error"], "accounts": out,
            "connected": bool(first), "channel": {"title": first["name"], "thumb": first.get("avatar")} if first else None,
            "needs_reconnect": any(a["needs_reconnect"] for a in out)}


def connect(account_id: str | None = None, on_connected=None) -> None:
    """Runs Google's installed-app OAuth flow in the background (opens the browser for consent). account_id = the
    channel to reconnect; none = "+ Add another account": the sign-in goes to a new account, never over another one.
    prompt=select_account makes Google ask which Google account (and so which channel) to use."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not YT_SECRET.exists():
        raise RuntimeError("Upload your Google OAuth client secret JSON first.")
    if _state["connecting"]:
        return
    # the pending account rides in OAuth's state, which the flow checks on the way back
    state = f"{account_id or accounts.new_id('youtube')}.{secrets.token_urlsafe(16)}"

    def run():
        _state.update(connecting=True, error="")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(YT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True, prompt="select_account consent", state=state,
                                          timeout_seconds=300)
            accounts.link("youtube", state.split(".", 1)[0], token=json.loads(creds.to_json()), **_channel_info(creds))
            if on_connected:
                on_connected()
        except Exception as e:
            _state["error"] = f"Connection failed: {e}"
        finally:
            _state["connecting"] = False

    threading.Thread(target=run, daemon=True).start()


def effective_publish_at(publish_at: str | None) -> str | None:
    """YouTube rejects publishAt in the past/too soon; return a valid RFC3339 UTC time or None (= publish per privacy now)."""
    if not publish_at:
        return None
    t = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if t <= datetime.now(timezone.utc) + timedelta(minutes=10):
        return None
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def set_publish_at(video_id: str, iso: str, account_id: str | None = None, ai_content: bool | None = None) -> str:
    """Move the publish time of an uploaded, still-private scheduled video. ai_content = its AI-content label, sent
    again from the post (videos.list doesn't return containsSyntheticMedia, so a status update would otherwise drop it)."""
    pub = effective_publish_at(iso)
    if not pub:
        raise RuntimeError("Pick a time at least a few minutes in the future.")
    if "https://www.googleapis.com/auth/youtube.force-ssl" in missing_scopes(account_id):
        raise RuntimeError("Moving an already-uploaded YouTube video needs one more permission: reconnect YouTube on the "
                           "Publish page (Reconnect on that channel) and approve it.")
    svc = _service(account_id)  # send the whole existing status: a partial update resets fields like made-for-kids
    items = svc.videos().list(part="status", id=video_id).execute().get("items") or []
    if not items:
        raise RuntimeError("That video is no longer on YouTube.")
    keep = ("embeddable", "license", "publicStatsViewable", "selfDeclaredMadeForKids", "containsSyntheticMedia")
    status = {k: v for k, v in items[0]["status"].items() if k in keep}
    if ai_content is not None:
        status["containsSyntheticMedia"] = bool(ai_content)
    svc.videos().update(part="status", body={"id": video_id, "status": {**status, "privacyStatus": "private", "publishAt": pub}}).execute()
    return pub


def delete_video(video_id: str, account_id: str | None = None) -> None:
    """Delete an uploaded video from that channel (videos.delete; needs youtube.force-ssl). Already gone counts as done."""
    if "https://www.googleapis.com/auth/youtube.force-ssl" in missing_scopes(account_id):
        raise RuntimeError("Deleting a YouTube video needs one more permission: reconnect that channel on the Publish page and "
                           "approve it.")
    try:
        _service(account_id).videos().delete(id=video_id).execute()
    except Exception as e:
        if getattr(getattr(e, "resp", None), "status", None) != 404:
            raise


def set_thumbnail(video_id: str, image, account_id: str | None = None) -> None:
    """Custom thumbnail (needs a verified channel; raises otherwise)."""
    from googleapiclient.http import MediaFileUpload

    _service(account_id).thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(image), mimetype="image/jpeg")).execute()


BACKOFF = 2.0  # seconds before retrying a failed chunk; doubles each try


def find_upload(title: str, since: float, account_id: str | None = None) -> str | None:
    """A video with this title this channel uploaded since `since` (epoch): an earlier attempt of the same post that
    finished without us hearing back. Reads the channel's uploads playlist (youtube.readonly)."""
    svc = _service(account_id)
    ch = (svc.channels().list(part="contentDetails", mine=True).execute().get("items") or [{}])[0]
    pl = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if not pl:
        return None
    for it in svc.playlistItems().list(part="snippet", playlistId=pl, maxResults=15).execute().get("items") or []:
        sn = it.get("snippet") or {}
        try:
            at = datetime.fromisoformat(str(sn.get("publishedAt")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if sn.get("title") == title[:100] and at >= since - 60:
            return (sn.get("resourceId") or {}).get("videoId")
    return None


def upload(path, title: str, description: str, tags: list[str], category_id: str, privacy: str,
           publish_at: str | None, made_for_kids: bool = False, progress=None, account_id: str | None = None,
           ai_content: bool = False, resume: dict | None = None, on_resume=None) -> dict:
    """Resumable upload. on_resume gets {uri, offset, size} after every chunk; given back as resume (after a crash or a
    failure) the same upload session continues where YouTube says it stopped. A session that is gone, or an earlier
    attempt that never saved one, is first looked for among the channel's uploads: never a second video for one post.
    ai_content sets status.containsSyntheticMedia (YouTube's altered or synthetic content label)."""
    import os

    import httplib2
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    resume, on_resume = resume or {}, on_resume or (lambda patch: None)
    pub = effective_publish_at(publish_at)
    body = {
        "snippet": {"title": title[:100], "description": description[:5000], "tags": tags, "categoryId": category_id or "22"},
        "status": {"privacyStatus": "private" if pub else privacy, "selfDeclaredMadeForKids": made_for_kids,
                   "containsSyntheticMedia": bool(ai_content)},
    }
    if pub:
        body["status"]["publishAt"] = pub  # uploaded now as private, YouTube flips it public at this time
    svc, size = _service(account_id), os.path.getsize(path)

    def done(vid):
        return {"video_id": vid, "url": f"https://youtu.be/{vid}", "publish_at": pub}

    def new_request():
        return svc.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(
            str(path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4"))
    req = new_request()
    if resume.get("uri") and resume.get("size") == size:
        # googleapiclient's own resume path: in error state, next_chunk first asks YouTube how far it got (PUT bytes */size)
        req.resumable_uri, req._in_error_state = resume["uri"], True
    elif resume.get("started") is not None and (vid := find_upload(title, resume["started"], account_id)):
        return done(vid)
    resp, tries = None, 0
    while resp is None:
        try:
            st, resp = req.next_chunk()
        except HttpError as e:
            code = e.resp.status
            if code in (404, 410) and resume.get("uri") and req.resumable_uri == resume["uri"]:  # the saved session expired
                if resume.get("started") is not None and (vid := find_upload(title, resume["started"], account_id)):
                    return done(vid)
                req, resume = new_request(), {}
                continue
            if (code < 500 and code != 429) or tries >= 5:
                raise
        except (OSError, httplib2.HttpLib2Error):  # no answer: the next call asks YouTube where the upload stopped
            if tries >= 5:
                raise
        else:
            tries = 0
            if req.resumable_uri:
                on_resume({"uri": req.resumable_uri, "offset": req.resumable_progress, "size": size})
            if st and progress:
                progress(st.progress())
            continue
        tries += 1
        time.sleep(BACKOFF * 2 ** tries)
    return done(resp["id"])
