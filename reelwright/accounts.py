"""Linked accounts: any number per platform, each with its own sign-in and an explicit approval.

accounts.json = {"accounts": [account, ...], "migrated_at": iso}. An account:
  id, platform, ext_id (the platform's id for it: channel id / TikTok open_id / Instagram user id), name, handle, avatar,
  approved   — the user okayed uploads to it (new sign-ins start False),
  autopilot  — Autopilot may post here too (default False; Autopilot needs approved AND autopilot),
  legacy     — migrated from the single-connection files (posts made before accounts existed belong to it),
  plus its own tokens: YouTube "token" (Google authorized-user dict), TikTok access/refresh_token + expires_at,
  Instagram access_token + ig_user_id + host. public() strips them before anything reaches the browser.
"""
import json
import secrets
import threading
import time

from . import vault
from .config import DATA, YT_TOKEN

FILE = DATA / "accounts.json"
LEGACY_CONN = DATA / "connections.json"  # before accounts: one TikTok / Instagram sign-in each (TikTok app keys stay here)
LEGACY_YT = YT_TOKEN                     # before accounts: one YouTube token
SECRET = ("token", "access_token", "refresh_token", "expires_at", "open_id")
SEALED = ("token", "access_token", "refresh_token")  # sealed at rest with DPAPI (vault.py)
_lock = threading.RLock()


def new_id(platform: str) -> str:
    return f"{platform[:2]}_{secrets.token_hex(4)}"


def _read_json(path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _read_accounts() -> dict | None:
    """accounts.json, or None only when it doesn't exist yet. A corrupt or empty file is kept as it is (with a backup
    copy, accounts.json.bad-<time>) and refused: re-migrating would drop the linked accounts and re-approve old ones."""
    try:
        raw = FILE.read_text("utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except ValueError as e:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(FILE.stat().st_mtime))
        bak = FILE.with_name(f"{FILE.name}.bad-{stamp}")
        if not bak.exists():  # once per broken version of the file, not on every read
            bak.write_text(raw, "utf-8")
            from . import log
            log.error("accounts", f"{FILE} is unreadable ({e}); copied to {bak.name}, not overwritten")
        raise RuntimeError(f"Your linked accounts file (accounts.json) is damaged, so nothing will be posted or changed. "
                           f"A copy was saved as {bak.name} next to it. Fix or remove accounts.json, then reopen Publish.") from None


def _write(data: dict) -> None:
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(FILE)  # atomic: a crash mid-write never leaves half a file


def _legacy() -> list[dict]:
    """The single connections from before accounts, as accounts. They were already in use, so they stay approved and
    Autopilot keeps posting to them (both can be switched off on the Publish page). Fixed ids: stable even while the
    migration keeps failing and these are rebuilt from the old files on every read."""
    out, base = [], {"approved": True, "autopilot": True, "legacy": True, "created": time.time()}
    tok = _read_json(LEGACY_YT)
    if tok:
        out.append({**base, "id": "youtube_legacy", "platform": "youtube", "ext_id": None, "name": None, "token": tok})
    conn = _read_json(LEGACY_CONN) or {}
    tt = conn.get("tiktok") or {}
    if tt.get("access_token"):
        out.append({**base, "id": "tiktok_legacy", "platform": "tiktok", "ext_id": tt.get("open_id"), "name": tt.get("display_name"),
                    "avatar": tt.get("avatar"), **{k: tt.get(k) for k in ("access_token", "refresh_token", "expires_at", "open_id")}})
    ig = conn.get("instagram") or {}
    if ig.get("access_token"):
        out.append({**base, "id": "instagram_legacy", "platform": "instagram", "ext_id": ig.get("ig_user_id"), "name": ig.get("username"),
                    "handle": ig.get("username"), "avatar": ig.get("avatar"), "followers": ig.get("followers"),
                    **{k: ig.get(k) for k in ("access_token", "ig_user_id", "host")}})
    return out


def _load() -> list[dict]:
    with _lock:
        data = _read_accounts()
        if data is not None:
            return [_open(a) for a in data.get("accounts") or [] if isinstance(a, dict)]
        accs = _legacy()  # first run with accounts: migrate once (the old files are left as they were)
        try:
            _write({"accounts": [_sealed(a, None) for a in accs], "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        except OSError:
            from . import log
            log.error("accounts", "migration failed; reading the old connection files until it succeeds")
        return accs


def _save(accs: list[dict]) -> None:
    with _lock:
        data = _read_accounts() or {}  # raises on a damaged file: never written over
        prev = {a.get("id"): a for a in data.get("accounts") or [] if isinstance(a, dict)}
        vault.backup_once(FILE, [a.get(k) for a in prev.values() for k in SEALED])
        _write({**data, "accounts": [_sealed(a, prev.get(a["id"])) for a in accs]})


def _open(a: dict) -> dict:
    """An account with its tokens opened; one sealed on another PC / Windows user is "locked": no token, so it shows
    Reconnect, and its stored token is kept until a new sign-in replaces it."""
    a = dict(a)
    for k in SEALED:
        try:
            if k in a:
                a[k] = vault.unseal(a[k])
        except vault.Locked:
            a[k], a["locked"] = None, True
    return a


def _sealed(a: dict, prev: dict | None) -> dict:
    out = {k: v for k, v in a.items() if k != "locked"}
    for k in SEALED:
        if out.get(k) is not None or (prev or {}).get(k) is not None:
            out[k] = vault.keep(out.get(k), (prev or {}).get(k))
    return {k: v for k, v in out.items() if k not in SEALED or v is not None}


def reseal() -> None:
    """Rewrite accounts.json so every token is sealed (or, with the vault off, plain); vault.migrate / rollback."""
    with _lock:
        if _read_accounts() is not None:
            _save(_load())


def all_accounts(platform: str | None = None) -> list[dict]:
    return [a for a in _load() if platform in (None, a["platform"])]


def get(aid: str | None) -> dict | None:
    return next((a for a in _load() if a["id"] == aid), None) if aid else None


def resolve(platform: str, aid: str | None = None) -> dict | None:
    """That account (if it is this platform's), or the platform's first account when none is named."""
    acc = get(aid) if aid else next(iter(all_accounts(platform)), None)
    return acc if acc and acc["platform"] == platform else None


def legacy(platform: str) -> dict | None:
    return next((a for a in all_accounts(platform) if a.get("legacy")), None)


def usable(platform: str | None = None, auto: bool = False) -> list[dict]:
    """Accounts uploads may go to: approved, and for Autopilot also allowed for Autopilot."""
    return [a for a in all_accounts(platform) if a.get("approved") and (a.get("autopilot") or not auto)]


def add(platform: str, **fields) -> dict:
    acc = {"id": fields.pop("id", None) or new_id(platform), "platform": platform, "ext_id": None, "name": None, "handle": None,
           "avatar": None, "approved": False, "autopilot": False, "created": time.time(), **fields}
    with _lock:
        _save([*_load(), acc])
    return acc


def update(aid: str, patch: dict) -> dict | None:
    with _lock:
        accs = _load()
        acc = next((a for a in accs if a["id"] == aid), None)
        if acc is None:
            return None
        acc.update(patch)
        _save(accs)
        return acc


def link(platform: str, aid: str, ext_id: str | None, **fields) -> dict:
    """Store a finished sign-in. aid is the account being reconnected, or a fresh id for "+ Add another account".
    The same person again refreshes their existing account (no duplicates, approval kept); a different person signing in
    on Reconnect becomes a new, unapproved account — another account's sign-in is never overwritten."""
    fields = {k: v for k, v in fields.items() if v is not None}
    with _lock:
        accs = all_accounts(platform)
        same = next((a for a in accs if ext_id and a.get("ext_id") == ext_id), None)
        target = get(aid)
        if same:
            return update(same["id"], fields)
        if target and target["platform"] == platform and not target.get("ext_id"):  # e.g. a migrated account: learn its id
            return update(aid, {**fields, "ext_id": ext_id})
        return add(platform, id=None if target else aid, ext_id=ext_id, **fields)


def remove(aid: str) -> dict | None:
    """Forget an account and its tokens (for a migrated one, also in the old single-connection files)."""
    with _lock:
        acc = get(aid)
        if not acc:
            return None
        _save([a for a in _load() if a["id"] != aid])
        if acc.get("legacy"):
            if acc["platform"] == "youtube":
                LEGACY_YT.unlink(missing_ok=True)
            else:
                from . import platforms  # it imports this module; its lock guards connections.json (also app secrets)
                with platforms._lock:
                    conn = _read_json(LEGACY_CONN) or {}
                    if acc["platform"] == "instagram":
                        conn.pop("instagram", None)
                    else:  # TikTok: keep the app's client key/secret (as stored: sealed), drop the sign-in
                        conn["tiktok"] = {k: v for k, v in (conn.get("tiktok") or {}).items() if k in ("client_key", "client_secret")}
                    tmp = LEGACY_CONN.with_name(LEGACY_CONN.name + ".tmp")
                    tmp.write_text(json.dumps(conn, indent=2), "utf-8")
                    tmp.replace(LEGACY_CONN)  # atomic: a crash mid-write never truncates the sealed app secrets
        return acc


def public(acc: dict) -> dict:
    return {k: v for k, v in acc.items() if k not in SECRET}
