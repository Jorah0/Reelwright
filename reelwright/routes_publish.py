"""WP3 publishing & security routes: the secrets vault (status, rollback, re-enable, forget unreadable ones) and
description templates. Included in server.py above the static mounts. No route returns a secret value."""
from fastapi import APIRouter, HTTPException

from . import accounts, config, platforms, store, vault

router = APIRouter()


@router.on_event("startup")
def _seal_secrets():
    vault.migrate()  # idempotent; every writer also seals, so the order against other startup work doesn't matter


@router.get("/api/vault")
def vault_status():
    return vault.status()


def _confirmed(fn, body: dict | None):
    """Turning encryption off and dropping secrets need {"confirm": "<word>"} in a JSON body (no bare form posts)."""
    try:
        return fn(confirm=str((body or {}).get("confirm") or ""))
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None


@router.post("/api/vault/rollback")
def vault_rollback(body: dict | None = None):
    return _confirmed(vault.rollback, body)


@router.post("/api/vault/enable")
def vault_enable():
    return vault.enable()


@router.post("/api/vault/forget")
def vault_forget(body: dict | None = None):
    return _confirmed(vault.forget_locked, body)


@router.get("/api/publish/template")
def get_template(clip_id: str | None = None):
    """The default template, each account's own (null = uses the default), and this clip's variables (its source link)."""
    out = {"default": config.load()["description_template"],
           "accounts": {a["id"]: a.get("description_template") for a in accounts.all_accounts()}}
    clip = store.get("clip", clip_id) if clip_id else None
    if clip:
        out["vars"] = platforms.template_vars(clip, store.get("project", clip["project_id"]), [h.lstrip("#") for h in clip.get("hashtags") or []])
    return out


@router.put("/api/publish/template")
def put_template(body: dict):
    """{template, account_id?}: an account's own template (null = use the default), else the default one."""
    tpl = body.get("template")
    if tpl is not None and (not isinstance(tpl, str) or len(tpl) > 2000):
        raise HTTPException(400, "A template is text of at most 2,000 characters.")
    if body.get("account_id"):
        if not accounts.update(body["account_id"], {"description_template": tpl}):
            raise HTTPException(404, "That account is no longer linked.")
    else:
        config.save({"description_template": tpl or ""})
    return get_template()
