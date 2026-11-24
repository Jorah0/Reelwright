"""YouTube Data API v3: OAuth connection, resumable uploads, scheduled publishing (publishAt)."""
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import YT_SECRET, YT_TOKEN

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
_state = {"connecting": False, "error": "", "channel": None}


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not YT_TOKEN.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(YT_TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        YT_TOKEN.write_text(creds.to_json(), "utf-8")
    return creds if creds.valid else None


def _service():
    from googleapiclient.discovery import build

    creds = _creds()
    if not creds:
        from .platforms import AuthError

        raise AuthError("YouTube is not connected — connect your channel on the Publish page.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def status() -> dict:
    connected = False
    try:
        connected = _creds() is not None
    except Exception as e:
        _state["error"] = f"Token refresh failed: {e}"
    if connected and not _state["channel"]:
        try:
            items = _service().channels().list(part="snippet,statistics", mine=True).execute().get("items", [])
            if items:
                sn, st = items[0]["snippet"], items[0].get("statistics", {})
                _state["channel"] = {"id": items[0]["id"], "title": sn["title"], "thumb": sn["thumbnails"]["default"]["url"],
                                     "subscribers": st.get("subscriberCount"), "videos": st.get("videoCount")}
        except Exception as e:
            _state["error"] = str(e)
    return {"has_secret": YT_SECRET.exists(), "connected": connected, "connecting": _state["connecting"],
            "error": _state["error"], "channel": _state["channel"] if connected else None}


def connect(on_connected=None) -> None:
    """Runs Google's installed-app OAuth flow in the background (opens the browser for consent)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not YT_SECRET.exists():
        raise RuntimeError("Upload your Google OAuth client secret JSON first.")
    if _state["connecting"]:
        return

    def run():
        _state.update(connecting=True, error="", channel=None)
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(YT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True, prompt="consent", timeout_seconds=300)
            YT_TOKEN.write_text(creds.to_json(), "utf-8")
            if on_connected:
                on_connected()
        except Exception as e:
            _state["error"] = f"Connection failed: {e}"
        finally:
            _state["connecting"] = False

    threading.Thread(target=run, daemon=True).start()


def disconnect() -> None:
    YT_TOKEN.unlink(missing_ok=True)
    _state.update(channel=None, error="")


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


def set_thumbnail(video_id: str, image) -> None:
    """Custom thumbnail (needs a verified channel; raises otherwise)."""
    from googleapiclient.http import MediaFileUpload

    _service().thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(image), mimetype="image/jpeg")).execute()


def upload(path, title: str, description: str, tags: list[str], category_id: str, privacy: str,
           publish_at: str | None, made_for_kids: bool = False, progress=None) -> dict:
    from googleapiclient.http import MediaFileUpload

    pub = effective_publish_at(publish_at)
    body = {
        "snippet": {"title": title[:100], "description": description[:5000], "tags": tags, "categoryId": category_id or "22"},
        "status": {"privacyStatus": "private" if pub else privacy, "selfDeclaredMadeForKids": made_for_kids},
    }
    if pub:
        body["status"]["publishAt"] = pub  # uploaded now as private, YouTube flips it public at this time
    req = _service().videos().insert(part="snippet,status", body=body,
                                     media_body=MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4"))
    from googleapiclient.errors import HttpError

    resp, retries = None, 0
    while resp is None:
        try:
            st, resp = req.next_chunk()
        except HttpError as e:
            if e.resp.status < 500 or retries >= 5:
                raise
            retries += 1
            time.sleep(2 ** retries)  # resumable upload continues from the last good chunk
            continue
        if st and progress:
            progress(st.progress())
    return {"video_id": resp["id"], "url": f"https://youtu.be/{resp['id']}", "publish_at": pub}
