"""Online media search with the creator's own free API keys (Settings: config "media_keys", sealed by vault.py and
never sent to a browser): GIPHY (GIFs, stickers), Freesound (sounds), Pixabay and Pexels (photos, videos for B-roll).

Endpoints, auth and terms as documented by each provider (checked 2026-11-24):
- GIPHY  https://developers.giphy.com/docs/api/  GET api.giphy.com/v1/{gifs|stickers}/search?api_key. Show "Powered by
  GIPHY"; don't cache responses or store its media: an import needs the creator's written OK from GIPHY (giphy_storage_ok).
- Freesound  https://freesound.org/docs/api/  GET freesound.org/apiv2/search/ with "Authorization: Token". Originals need
  OAuth2, so an import takes the HQ MP3 preview (a token is enough). Free API use is non-commercial; credit by licence.
- Pixabay  https://pixabay.com/api/docs/  GET pixabay.com/api/ and /api/videos/?key. Cache requests for 24 h; download
  rather than hotlink. No audio API is documented, so none here.
- Pexels  https://www.pexels.com/api/documentation/  GET api.pexels.com/v1/search and /v1/videos/search with
  "Authorization: <key>". Credit "Photo by X on Pexels".
- Tenor is not offered: Google stopped new API keys on 2026-01-13 and switched the API off on 2026-06-30.

search() -> {items: [{id, provider, kind, title, preview_url, duration, width, height, has_alpha, licence, author,
source_url, attribution_text}], attribution, hidden_nc}; download(item) -> a local temp file; import_item() puts it in
the asset library (assets.add: checksum dedupe, licence metadata)."""
import contextlib
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import assets, config, log, store

TRANSPORT = None  # tests: an httpx.MockTransport (no network)
PER_PAGE = 24
PROVIDERS = {
    "giphy": {"name": "GIPHY", "kinds": ("gif", "sticker"), "attribution": "Powered by GIPHY",
              "hosts": ("api.giphy.com", "media.giphy.com", "media0.giphy.com", "media1.giphy.com", "media2.giphy.com",
                        "media3.giphy.com", "media4.giphy.com", "i.giphy.com"),
              "signup_url": "https://developers.giphy.com/dashboard/",
              "terms_url": "https://support.giphy.com/hc/en-us/articles/360028134111-GIPHY-API-Terms-of-Service-"},
    "freesound": {"name": "Freesound", "kinds": ("sound",), "attribution": "Sounds from Freesound",
                  "hosts": ("freesound.org", "cdn.freesound.org"),
                  "signup_url": "https://freesound.org/apiv2/apply/", "terms_url": "https://freesound.org/help/tos_api/"},
    "pixabay": {"name": "Pixabay", "kinds": ("image", "video"), "attribution": "Images and videos from Pixabay",
                "hosts": ("pixabay.com", "cdn.pixabay.com"),
                "signup_url": "https://pixabay.com/api/docs/", "terms_url": "https://pixabay.com/service/terms/"},
    "pexels": {"name": "Pexels", "kinds": ("image", "video"), "attribution": "Photos and videos provided by Pexels",
               "hosts": ("api.pexels.com", "images.pexels.com", "videos.pexels.com", "player.vimeo.com"),
               "signup_url": "https://www.pexels.com/api/new/", "terms_url": "https://www.pexels.com/api/"},
}
CACHE_S = {"giphy": 0, "freesound": 300, "pixabay": 24 * 3600, "pexels": 300}  # GIPHY forbids caching; Pixabay asks for 24 h
DEFAULT_LIMITS = {"giphy": (100, 3600), "freesound": (60, 60), "pixabay": (100, 60), "pexels": (200, 3600)}  # (calls, seconds)
LIMITS = dict(DEFAULT_LIMITS)
FILE_KIND = {"gif": "gif", "sticker": "gif", "image": "image", "video": "video", "sound": "sound"}  # -> assets.CAPS key

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict]] = {}
_hits: dict[str, deque] = {}
_items: OrderedDict = OrderedDict()  # (provider, id) -> the last search's item, with its download_url (server side only)
ITEMS_MAX = 2000  # oldest searched items are forgotten past this (an import needs a recent search anyway)
MAX_REDIRECTS = 5


class MissingKey(Exception):
    """No key (or a rejected one) for that provider: the UI asks the creator to connect it."""


class RateLimited(Exception):
    pass


def reset() -> None:
    with _lock:
        _cache.clear()
        _hits.clear()
        _items.clear()
        LIMITS.clear()
        LIMITS.update(DEFAULT_LIMITS)


def _client() -> httpx.Client:
    # redirects are followed by hand in _send, each hop checked against the provider's hosts
    return httpx.Client(transport=TRANSPORT, timeout=20) if TRANSPORT else httpx.Client(timeout=20)


def _send(c: httpx.Client, provider: str, req: httpx.Request) -> httpx.Response:
    """A streamed response, following up to MAX_REDIRECTS redirects that stay on the provider's https hosts (httpx's
    next_request drops the Authorization header when a hop changes origin)."""
    for _ in range(MAX_REDIRECTS + 1):
        if not _host_ok(provider, str(req.url)):
            raise ValueError("The download was redirected off the provider's site.")
        r = c.send(req, stream=True)
        if not r.is_redirect:
            return r
        r.close()
        req = r.next_request
    raise ValueError("Too many redirects.")


def _key(provider: str) -> str:
    k = str((config.load().get("media_keys") or {}).get(provider) or "").strip()
    if not k:
        raise MissingKey(f"Connect {PROVIDERS[provider]['name']} first: add your free API key in Studio › Media.")
    return k


def _throttle(provider: str) -> None:
    n, window = LIMITS[provider]
    now = time.monotonic()
    with _lock:
        q = _hits.setdefault(provider, deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= n:
            raise RateLimited(f"{PROVIDERS[provider]['name']} allows {n} searches per {window // 60 or 1} min: try again shortly.")
        q.append(now)


def _get(provider: str, url: str, params: dict, headers: dict | None = None) -> dict:
    _throttle(provider)
    with _client() as c:
        r = _send(c, provider, c.build_request("GET", url, params=params, headers=headers or {}))
        try:
            r.read()
        finally:
            r.close()
    if r.status_code in (401, 403):
        raise MissingKey(f"{PROVIDERS[provider]['name']} rejected the API key: connect it again in Studio › Media.")
    if r.status_code == 429:
        raise RateLimited(f"{PROVIDERS[provider]['name']} says too many requests: try again later.")
    r.raise_for_status()
    return r.json()


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _item(provider, kind, id, title, preview, *, duration=None, width=0, height=0, alpha=False, licence, author="",
          source_url="", attribution="", download="") -> dict:
    return {"id": str(id), "provider": provider, "kind": kind, "title": str(title or "")[:200],
            "preview_url": preview if _host_ok(provider, preview) else "",
            "duration": duration, "width": _int(width), "height": _int(height), "has_alpha": alpha, "licence": licence,
            "author": str(author or "")[:120], "source_url": source_url or "", "attribution_text": attribution, "download_url": download or ""}


def freesound_licence(lic: str) -> str:
    """Freesound's licence (a URL or words, both seen) -> assets.LICENCES code."""
    s = str(lic or "").lower()
    if "zero" in s or "creative commons 0" in s or "cc0" in s:
        return "cc0"
    if "sampling" in s:
        return "sampling+"
    if "by-nc" in s or "noncommercial" in s or "non-commercial" in s:
        return "cc-by-nc"
    return "cc-by"  # "Attribution", .../licenses/by/x.y/ (and anything unknown is treated as needing credit)


def _giphy(q, kind, page, safe):
    d = _get("giphy", f"https://api.giphy.com/v1/{'stickers' if kind == 'sticker' else 'gifs'}/search",
             {"api_key": _key("giphy"), "q": q[:50], "limit": PER_PAGE, "offset": min((page - 1) * PER_PAGE, 4999),
              "rating": "g" if safe else "pg-13", "lang": "en"})
    out = []
    for g in d.get("data") or []:
        im = g.get("images") or {}
        orig, small = im.get("original") or {}, im.get("fixed_width") or {}
        author = g.get("username") or (g.get("user") or {}).get("username") or ""
        out.append(_item("giphy", kind, g["id"], g.get("title"), small.get("webp") or small.get("url"),
                         width=orig.get("width"), height=orig.get("height"), alpha=kind == "sticker", licence="giphy",
                         author=author, source_url=g.get("url"),
                         attribution=(f"{g.get('title') or 'GIF'} by @{author}, " if author else "") + "Powered by GIPHY",
                         download=(orig.get("webp") if kind == "sticker" else orig.get("url")) or orig.get("url")))
    return out


def _freesound(q, kind, page, safe):
    d = _get("freesound", "https://freesound.org/apiv2/search/",
             {"query": q[:200], "page": page, "page_size": PER_PAGE, "fields": "id,name,username,license,duration,previews,url"},
             {"Authorization": f"Token {_key('freesound')}"})
    out = []
    for s in d.get("results") or []:
        lic = freesound_licence(s.get("license"))
        prev = (s.get("previews") or {}).get("preview-hq-mp3") or ""
        out.append(_item("freesound", "sound", s["id"], s.get("name"), prev, duration=round(float(s.get("duration") or 0), 3),
                         licence=lic, author=s.get("username"), source_url=s.get("url"),
                         attribution=f"“{s.get('name')}” by {s.get('username')} (freesound.org, {assets.LICENCES[lic][0]})",
                         download=prev))
    return out


def _pixabay(q, kind, page, safe):
    video = kind == "video"
    d = _get("pixabay", f"https://pixabay.com/api/{'videos/' if video else ''}",
             {"key": _key("pixabay"), "q": q[:100], "page": page, "per_page": PER_PAGE, "safesearch": "true" if safe else "false"})
    out = []
    for h in d.get("hits") or []:
        if video:
            v = next((x for x in ((h.get("videos") or {}).get(s) or {} for s in ("medium", "small", "large")) if x.get("url")), {})
            out.append(_item("pixabay", "video", h["id"], h.get("tags"), v.get("thumbnail"), duration=h.get("duration"),
                             width=v.get("width"), height=v.get("height"), licence="pixabay", author=h.get("user"),
                             source_url=h.get("pageURL"), attribution=f"Video by {h.get('user')} from Pixabay", download=v.get("url")))
        else:
            out.append(_item("pixabay", "image", h["id"], h.get("tags"), h.get("previewURL"), width=h.get("imageWidth"),
                             height=h.get("imageHeight"), licence="pixabay", author=h.get("user"), source_url=h.get("pageURL"),
                             attribution=f"Image by {h.get('user')} from Pixabay", download=h.get("largeImageURL")))
    return out


def _pexels(q, kind, page, safe):
    video = kind == "video"
    d = _get("pexels", f"https://api.pexels.com/v1/{'videos/' if video else ''}search",
             {"query": q[:200], "page": page, "per_page": PER_PAGE}, {"Authorization": _key("pexels")})
    out = []
    for p in d.get("videos" if video else "photos") or []:
        if video:
            mp4 = [f for f in p.get("video_files") or [] if f.get("file_type") == "video/mp4" and f.get("height")]
            fit = [f for f in mp4 if f["height"] <= 1080] or mp4  # the largest up to 1080p: not 4K, never the HLS playlist
            f = max(fit, key=lambda x: x["height"]) if fit else {}
            name = (p.get("user") or {}).get("name") or ""
            out.append(_item("pexels", "video", p["id"], f"Video by {name}", p.get("image"), duration=p.get("duration"),
                             width=f.get("width"), height=f.get("height"), licence="pexels", author=name,
                             source_url=p.get("url"), attribution=f"Video by {name} on Pexels", download=f.get("link")))
        else:
            src, name = p.get("src") or {}, p.get("photographer") or ""
            out.append(_item("pexels", "image", p["id"], p.get("alt") or f"Photo by {name}", src.get("medium"),
                             width=p.get("width"), height=p.get("height"), licence="pexels", author=name,
                             source_url=p.get("url"), attribution=f"Photo by {name} on Pexels",
                             download=src.get("large2x") or src.get("original")))
    return out


SEARCH = {"giphy": _giphy, "freesound": _freesound, "pixabay": _pixabay, "pexels": _pexels}


def search(provider: str, q: str, kind: str | None = None, page: int = 1, safe: bool = True) -> dict:
    """One page of results (ValueError: unknown provider/kind; MissingKey; RateLimited; httpx errors on a failed call).
    On a monetized channel, non-commercial (CC BY-NC, Sampling+) sounds are left out and counted in hidden_nc."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown media provider {str(provider)[:30]!r}.")
    p = PROVIDERS[provider]
    kind = kind or p["kinds"][0]
    if kind not in p["kinds"]:
        raise ValueError(f"{p['name']} has {', '.join(p['kinds'])}, not {str(kind)[:20]}.")
    q, page = str(q or "").strip(), max(1, min(int(page or 1), 100))
    if not q:
        raise ValueError("Type something to search for.")
    _key(provider)  # before the cache: a disconnected provider never answers from it
    ck = (provider, kind, q.lower(), page, bool(safe))
    with _lock:
        hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < CACHE_S[provider]:
        items = hit[1]
    else:
        items = SEARCH[provider](q, kind, page, safe)
        if CACHE_S[provider]:
            _cache_put(ck, items)
    _remember(provider, items)
    hidden = 0
    if config.load()["media_monetized"]:
        keep = [it for it in items if not assets.LICENCES.get(it["licence"], ("", False, False))[2]]
        hidden, items = len(items) - len(keep), keep
    return {"items": [{k: v for k, v in it.items() if k != "download_url"} for it in items], "attribution": p["attribution"],
            "hidden_nc": hidden, "page": page}


def _cache_put(ck: tuple, items: list) -> None:
    """Cache a search, dropping every expired entry first (keys start with the provider)."""
    now = time.monotonic()
    with _lock:
        for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_S[k[0]]]:
            del _cache[k]
        _cache[ck] = (now, items)


def _remember(provider: str, items: list) -> None:
    with _lock:
        for it in items:
            _items[(provider, it["id"])] = it
            _items.move_to_end((provider, it["id"]))
        while len(_items) > ITEMS_MAX:
            _items.popitem(last=False)


def _host_ok(provider: str, url: str) -> bool:
    u = urlparse(url or "")
    return u.scheme == "https" and (u.hostname or "") in PROVIDERS[provider]["hosts"]


def download(item: dict) -> Path:
    """The item's file into a temp file under assets/ (the caller deletes it). Only https from the provider's own hosts;
    GIPHY only with giphy_storage_ok (its API terms forbid storing its media); capped at the kind's size limit."""
    provider = item.get("provider")
    if provider not in PROVIDERS:
        raise ValueError("Unknown media provider.")
    if not _host_ok(provider, item.get("download_url")):
        raise ValueError(f"Not a {PROVIDERS[provider]['name']} download link.")
    if provider == "giphy" and not config.load()["giphy_storage_ok"]:
        raise PermissionError("GIPHY's API terms don't allow saving its GIFs. Tick “I have GIPHY's written permission” "
                              "in Studio › Media only if GIPHY approved it.")
    cap = assets.CAPS[FILE_KIND[item["kind"]]]
    ext = Path(urlparse(item["download_url"]).path).suffix.lower() or ".bin"
    tmp = assets.ASSETS / f".dl-{store.new_id()}{ext}"
    try:
        with _client() as c, contextlib.closing(_send(c, provider, c.build_request("GET", item["download_url"]))) as r:
            r.raise_for_status()
            got = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    got += len(chunk)
                    if got > cap:
                        raise ValueError(f"That file is over the {cap >> 20} MB limit for a {FILE_KIND[item['kind']]}.")
                    f.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def import_item(provider: str, item_id: str, loudnorm: bool = False) -> dict:
    """A searched item into the library with its licence metadata (KeyError: not in a recent search). The same file
    again returns the asset already there (duplicate: True)."""
    item = _items.get((provider, str(item_id)))
    if not item:
        raise KeyError(item_id)
    tmp = download(item)
    try:
        a = assets.add(tmp, f"{item['provider']}-{item['id']}{tmp.suffix}", item["title"][:80] or item["provider"], [item["provider"]],
                       source={**{k: item[k] for k in ("provider", "source_url", "licence", "author")},
                               "attribution_text": item["attribution_text"],
                               "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                       loudnorm=loudnorm)
    finally:
        tmp.unlink(missing_ok=True)
    log.event("media", f"imported {provider} {item['id']} as {a['id']}{' (already there)' if a.get('duplicate') else ''}")
    return a
