"""Secrets at rest, sealed with Windows DPAPI for the current Windows user (CryptProtectData through ctypes, no dependency):
a copied settings or accounts file opens on no other PC and under no other Windows account.

Each secret is sealed on its own, inside the file it always lived in, so the rest stays readable JSON:
  settings.json     anthropic_api_key, lan_token, gsi_token, vts_token, remote_devices, media_keys, ai_providers.*.api_key
                    (config.SEALED)
  accounts.json     each account's token / access_token / refresh_token (accounts.SEALED)
  connections.json  the TikTok and Twitch apps' client_secret (platforms.SEALED)
  the store (obs)   each direct OBS connection's websocket password (obs.reseal)
A sealed value is the string "dpapi:<base64>" (of the value's JSON). One that won't open here (sealed on another PC or
Windows user, or damaged) reads as empty, so that sign-in shows "reconnect"; it is never overwritten by an empty value,
only by a new one (or by forget_locked()).

Migration: every writer seals the whole file, after first copying a file that still holds plain-text secrets to
<name>.pre-vault.bak (never overwritten, so it keeps the original). migrate() runs at each start and is idempotent: it
rewrites files that still hold plain text and drops the pre-accounts copies of sign-ins (youtube_token.json, the old
TikTok fields in connections.json) once accounts.json holds them. The .bak copies hold secrets in plain text, so they go
after KEEP_STARTS (2) clean starts in a row (everything sealed, everything opened): the start that sealed them and the next
one, which proves the sealed files open again in a new process; until then they are the way back if the vault misbehaves.
Rollback: rollback() (Publish page → Security, or `python -m reelwright.vault rollback`) writes every secret back as plain
text and keeps sealing off (marker file DATA/vault-off) until enable().
Without DPAPI (not Windows) nothing is sealed: values stay plain text and status() says "not encrypted".
"""
import base64
import ctypes
import functools
import json
import shutil
import time

PREFIX = "dpapi:"
BAK = ".pre-vault.bak"
KEEP_STARTS = 2  # the sealing start, then one start that reopened everything from disk
STATE = None  # DATA/vault.json: clean starts counted towards deleting the backups (set below, once config is importable)
OFF = None    # DATA/vault-off: present = sealing turned off by rollback()
_ENTROPY = b"Reelwright vault 1"  # app-specific: another program of this user can't open our blobs by accident
LABELS = {"anthropic_api_key": "Claude API key (Settings)", "lan_token": "OBS relay token (OBS page: make a new token)",
          "remote_devices": "Paired phones (Settings → Phone remote: pair again)", "gsi_token": "CS2 / Dota 2 game link (install its config again)",
          "vts_token": "VTube Studio link (allow Reelwright in VTube Studio again)",
          "media_keys": "Media search keys: GIPHY, Freesound, Pixabay, Pexels (Studio › Media: connect again)"}


class Locked(ValueError):
    """Sealed on another PC or Windows user (or damaged): sign in or enter it again."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


try:
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for _f in (_crypt32.CryptProtectData, _crypt32.CryptUnprotectData):
        _f.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_ulong, ctypes.POINTER(_Blob)]
        _f.restype = ctypes.c_int
    _kernel32.LocalFree.argtypes, _kernel32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    AVAILABLE = True
except (AttributeError, OSError):  # not Windows: the plain-text fallback
    AVAILABLE = False


def _crypt(data: bytes, protect: bool) -> bytes:
    def blob(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf  # keep buf alive with the blob
    (src, _a), (ent, _b), out = blob(data), blob(_ENTROPY), _Blob()
    fn = _crypt32.CryptProtectData if protect else _crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):  # 0x1 = UI_FORBIDDEN
        raise OSError(ctypes.get_last_error(), "DPAPI refused")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _paths():
    global STATE, OFF
    from . import config  # config imports this module
    STATE = STATE or config.DATA / "vault.json"
    OFF = OFF or config.DATA / "vault-off"
    return STATE, OFF


def on() -> bool:
    """New and rewritten secrets are sealed (DPAPI is here and rollback() hasn't turned it off)."""
    return AVAILABLE and not _paths()[1].exists()


def is_sealed(v) -> bool:
    return isinstance(v, str) and v.startswith(PREFIX)


@functools.lru_cache(maxsize=256)
def _plain(sealed: str) -> str:
    """The JSON text inside. Successes only are cached (settings are read many times a second; text, so callers can't
    mutate the cache): a failure raises, so a passing DPAPI hiccup is retried on the next read, never remembered."""
    return _crypt(base64.b64decode(sealed[len(PREFIX):], validate=True), False).decode("utf-8")


def _text(sealed: str) -> str | None:
    try:
        return _plain(sealed)
    except (OSError, ValueError):  # another user/PC, damaged, not base64, or DPAPI not ready
        return None


def seal(v):
    """v sealed for this Windows user; empty values ("not set"), already sealed ones, and everything when off, unchanged."""
    if not v or is_sealed(v) or not on():
        return v
    return PREFIX + base64.b64encode(_crypt(json.dumps(v).encode("utf-8"), True)).decode("ascii")


def unseal(v):
    """The value inside a sealed one (plain values pass through); raises Locked when it can't be opened here."""
    if not is_sealed(v):
        return v
    txt = _text(v) if AVAILABLE else None
    try:
        return json.loads(txt)
    except (TypeError, ValueError):
        raise Locked("This was saved on another PC or Windows user (or is damaged): sign in or enter it again.") from None


def is_locked(v) -> bool:
    try:
        unseal(v)
        return False
    except Locked:
        return True


def keep(new, old):
    """What a writer stores for one secret, given the new value (plain, or as read raw) and what is on disk now."""
    try:
        new = unseal(new)
    except Locked:
        return new                      # can't open it here: store it exactly as it was
    if not new and is_locked(old):
        return old                      # an empty value never replaces one this PC just can't read
    if on() and is_sealed(old) and not is_locked(old) and unseal(old) == new:
        return old                      # unchanged: same blob (no churn, cache stays warm)
    return seal(new)


def backup_once(path, values) -> None:
    """Before the first sealing write over plain-text secrets: copy the file to <name>.pre-vault.bak (kept if present)."""
    bak = path.with_name(path.name + BAK)
    if on() and not bak.exists() and path.exists() and any(v and not is_sealed(v) for v in values):
        shutil.copy2(path, bak)
        _save_state({"clean_starts": 0})


def _state() -> dict:
    try:
        return json.loads(_paths()[0].read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    _paths()[0].write_text(json.dumps(st), "utf-8")


def _files() -> list:
    from . import accounts, config, platforms
    return [config.SETTINGS_FILE, accounts.FILE, platforms.CONN_FILE, accounts.LEGACY_YT]


def backups() -> list:
    return [p.with_name(p.name + BAK) for p in _files() if p.with_name(p.name + BAK).exists()]


def _locked() -> list[dict]:
    from . import accounts, config, platforms
    out = [{"what": LABELS.get(k, "OpenAI-compatible API key (Settings)" if k.startswith("ai_providers") else k), "key": k} for k in config.locked()]
    try:
        names = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
        out += [{"what": f"{names.get(a['platform'], a['platform'])} account {a.get('name') or a['id']} (Publish: Reconnect)", "account_id": a["id"]}
                for a in accounts.all_accounts() if a.get("locked")]
    except RuntimeError:  # a damaged accounts.json is reported by accounts itself
        pass
    out += [{"what": f"{n.title()} app secret (enter it again)", "key": f"connections.{n}"} for n in platforms.locked()]
    return out


def status() -> dict:
    enc = on()
    return {"encrypted": enc, "available": AVAILABLE, "off": AVAILABLE and not enc,
            "scheme": "Windows DPAPI, this Windows user only" if enc else
            "none: secrets are stored as plain text" + (" (turned off)" if AVAILABLE else " (DPAPI needs Windows)"),
            "locked": _locked(), "backups": [p.name for p in backups()], "clean_starts": _state().get("clean_starts", 0)}


def _reseal_all() -> bool:
    """Rewrite every secret file (sealed, or plain when off); False when one of them couldn't be rewritten."""
    from . import accounts, config, log, obs, platforms
    ok = True
    for name, fn in (("settings", lambda: config.save({})), ("accounts", accounts.reseal), ("connections", platforms.reseal),
                     ("obs", obs.reseal)):
        try:
            fn()
        except Exception as e:  # one damaged file must not stop the others, nor the app
            ok = False
            log.error("vault", f"{name}: {type(e).__name__}")  # the type only: never risk a value in the log
    return ok


def migrate() -> dict:
    """At every start: seal what is still plain text (backup first), drop migrated pre-accounts copies, count clean starts;
    after KEEP_STARTS clean starts in a row the plain-text backups are deleted. Never raises."""
    from . import accounts, log
    try:
        if not on():
            log.event("vault", "secrets are NOT encrypted: " + status()["scheme"])
            return status()
        sealed = _reseal_all()
        try:
            if accounts._read_accounts() is not None and accounts.LEGACY_YT.exists() and accounts.legacy("youtube"):
                backup_once(accounts.LEGACY_YT, ["x"])   # its sign-in lives (sealed) in the migrated account now
                accounts.LEGACY_YT.unlink()
        except (OSError, RuntimeError) as e:
            log.error("vault", f"old YouTube token file: {e}")
        st = status()
        clean = sealed and not st["locked"]  # every file rewritten sealed and every secret opened: backups may count down
        n = _state().get("clean_starts", 0) + 1 if clean else 0
        if n >= KEEP_STARTS:
            for p in backups():
                p.unlink(missing_ok=True)
            log.event("vault", f"{KEEP_STARTS} clean starts: plain-text backups deleted")
        _save_state({"clean_starts": n, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return status()
    except Exception as e:
        log.error("vault", f"migration: {type(e).__name__}")
        return {"encrypted": False, "error": str(e)}


def rollback(confirm: str = "rollback") -> dict:
    """Every secret back to plain text (for an older Reelwright, or before moving to another PC), sealing off until enable().
    The routes pass the caller's confirm word, so a request without it does nothing."""
    if confirm != "rollback":
        raise PermissionError('Send {"confirm": "rollback"} to store the secrets as plain text.')
    _paths()[1].write_text("Sealing turned off by rollback(); delete this file or use enable() to seal again.\n", "utf-8")
    _reseal_all()
    return status()


def enable() -> dict:
    _paths()[1].unlink(missing_ok=True)
    return migrate()


def forget_locked(confirm: str = "forget") -> dict:
    """Drop the secrets that can't be opened here (accounts are removed or reconnected on the Publish page instead).
    Each is checked again right now (failures are never cached), so a passing DPAPI hiccup can't make it drop a good one."""
    if confirm != "forget":
        raise PermissionError('Send {"confirm": "forget"} to drop the unreadable secrets.')
    from . import config, platforms
    config.forget_locked()
    platforms.forget_locked()
    return status()


if __name__ == "__main__":  # python -m reelwright.vault status|rollback|enable  (close Reelwright first)
    import sys
    cmd = (sys.argv[1:] or ["status"])[0]
    print(json.dumps({"status": status, "rollback": rollback, "enable": enable}[cmd](), indent=2))
