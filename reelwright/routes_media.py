"""/api/media/*: online media search (media_search.py), importing a result into the library, the creator's keys (sealed,
never returned), and kits (assets.py: named collections, exported/imported as a zip). The monetized / GIPHY-permission
switches are plain settings (PUT /api/settings: media_monetized, giphy_storage_ok)."""
import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from . import assets, config, media_search, store

router = APIRouter()


@router.get("/api/media/providers")
def providers():
    keys = config.load()["media_keys"] or {}
    return {"providers": [{"id": pid, "name": p["name"], "kinds": list(p["kinds"]), "connected": bool(keys.get(pid)),
                           "attribution": p["attribution"], "signup_url": p["signup_url"], "terms_url": p["terms_url"]}
                          for pid, p in media_search.PROVIDERS.items()],
            "monetized": bool(config.load()["media_monetized"]), "giphy_storage_ok": bool(config.load()["giphy_storage_ok"])}


@router.put("/api/media/keys")
def put_key(body: dict):
    pid, key = str(body.get("provider") or ""), str(body.get("key") or "").strip()
    if pid not in media_search.PROVIDERS:
        raise HTTPException(400, "Unknown media provider.")
    if len(key) > 200:
        raise HTTPException(400, "That doesn't look like an API key.")
    config.save({"media_keys": {**(config.load()["media_keys"] or {}), pid: key}})
    media_search.reset()  # nothing cached under the old key
    return {"ok": True, "connected": bool(key)}


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except media_search.MissingKey as e:
        raise HTTPException(409, str(e)) from None
    except media_search.RateLimited as e:
        raise HTTPException(429, str(e)) from None
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except KeyError:
        raise HTTPException(404, "Search again: that result is no longer in the list.") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except httpx.HTTPError as e:
        raise HTTPException(502, f"The media site didn't answer ({type(e).__name__}).") from None


@router.get("/api/media/search")
def search(provider: str, q: str, kind: str = "", page: int = 1, safe: bool = True):
    return _call(media_search.search, provider, q, kind or None, page, safe)


@router.post("/api/media/import")
def import_item(body: dict):
    return _call(media_search.import_item, str(body.get("provider") or ""), str(body.get("id") or ""), bool(body.get("loudnorm")))


# ---------- kits ----------

@router.get("/api/media/kits")
def kits():
    return store.find("kit")


@router.post("/api/media/kits")
def new_kit(body: dict):
    return _call(assets.save_kit, body.get("name"), body.get("asset_ids") or [])


@router.put("/api/media/kits/{kid}")
def put_kit(kid: str, body: dict):
    return _call(assets.save_kit, body.get("name"), body.get("asset_ids") or [], kid)


@router.delete("/api/media/kits/{kid}")
def delete_kit(kid: str):
    store.delete("kit", kid)  # the assets stay in the library
    return {"ok": True}


@router.get("/api/media/kits/{kid}/export")
def export_kit(kid: str):
    data = _call(assets.export_kit, kid)
    name = "".join(c if c.isalnum() or c in " -_" else "_" for c in store.get("kit", kid)["name"]).strip() or "kit"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@router.post("/api/media/kits/import")
def import_kit(file: UploadFile = File(...)):
    tmp = assets.ASSETS / f".kitzip-{store.new_id()}.zip"
    try:
        size = 0
        with open(tmp, "wb") as f:
            while chunk := file.file.read(1 << 20):
                size += len(chunk)
                if size > assets.KIT_ZIP_MAX:
                    raise HTTPException(413, "That kit is too big.")
                f.write(chunk)
        return _call(assets.import_kit, tmp)
    finally:
        tmp.unlink(missing_ok=True)
