"""Paths and user settings (a JSON file next to the data)."""
import contextvars
import copy
import json
import os
import sys
import threading
from pathlib import Path

from . import vault

ROOT = Path(__file__).resolve().parent.parent
FROZEN = getattr(sys, "frozen", False)  # running as the packaged Reelwright.exe
_default_data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Reelwright" if FROZEN else ROOT / "data"
DATA = Path(os.environ.get("REELWRIGHT_DATA", _default_data)).resolve()
DEFAULT_PROJECTS = DATA / "projects"  # where project folders live unless the creator picks another folder: use projects_dir()
STATIC = Path(__file__).resolve().parent / "static"
SETTINGS_FILE = DATA / "settings.json"
YT_SECRET = DATA / "youtube_client_secret.json"
YT_TOKEN = DATA / "youtube_token.json"

DEFAULT_PROJECTS.mkdir(parents=True, exist_ok=True)  # never a chosen folder: a missing one is flagged, not recreated

DEFAULTS = {
    "anthropic_api_key": "",
    "model": "claude-opus-5-5",
    "visual_judge": True,       # the long-video judge pass also sees a few small frames per candidate (more tokens)
    "classify_model": "claude-haiku-5-5",  # the cheap video-type call before analysis ("" = use "model")
    "whisper_model": "large-v3-turbo",
    "whisper_device": "auto",  # auto | cuda | cpu
    "clip_min": 20,
    "clip_target": 35,          # length clips aim for (0 = halfway between min and max); clip_max is a hard cap
    "clip_max": 60,
    "clip_count": 8,
    "default_aspect": "9:16",
    "default_privacy": "private",
    "caption_uppercase": True,
    "caption_color": "#FFD400",
    "results_at": "",           # last time published-post view counts were pulled into the learning memory
    "autopilot_paused": False,  # stops Autopilot everywhere; its queued posts are held, not deleted
    # OBS over the network: an optional receiver other PCs' Reelwright Relay pushes clips to (token-protected)
    "lan_receive": False,
    "lan_port": 8766,
    "lan_token": "",
    "obs_autopilot": {"enabled": False},  # Autopilot settings applied to clips arriving from OBS
    # storage + space saver (storage.py). storage_dir / storage_move / retention_since / retention_at are set by the app only
    "storage_dir": "",          # the folder holding project folders ("" = DATA/projects); see projects_dir()
    "storage_move": {},         # an unfinished move {"from", "to", "phase": copy|cleanup}, resumed at the next start
    "retention_mode": "",       # "" = not chosen yet (nothing is cleaned) | off | delete | archive
    "retention_days": 14,       # 0 = never
    "retention_since": "",      # when the space saver was switched on: no item's clock starts before it
    "retention_scope": {},      # merged over storage.SCOPE_DEFAULTS
    "retention_at": "",         # last clean-up run
    "archive_uploaded_dir": "", "archive_unfinished_dir": "",
    "archive_other_dir": "",    # sources and long forms ("" = the uploaded-clips folder)
    # phone remote (remote.py): a separate opt-in Wi-Fi listener for the phone app; one signing secret per paired phone
    "remote_enabled": False,
    "remote_port": 8810,        # outside the main app's 8765-8799 fallback range and the LAN receiver's 8766
    "remote_devices": [],       # [{id, name, secret, created, paired_at}] (never sent to any browser)
    "ui_scale": {},             # per-component interface scale, e.g. {"app": 1.1, "text": 1.2} (kept across restarts/updates)
    "close_to_taskbar": True,   # the window's ✕ minimizes (jobs keep running); "Quit Reelwright" exits
    # other AI providers (providers.py): which one each task uses, their settings, and the two-model consensus mode
    "ai_routes": {},            # task -> provider, e.g. {"find": "ollama"}; a missing task uses "anthropic"
    "ai_providers": {},         # provider -> {model, base_url, api_key, ...}, merged over PROVIDER_DEFAULTS
    "ai_consensus": "off",      # off | local_then_claude | "<scout>:<reviewer>", e.g. "ollama:openai"
    # judgment & learning (context.py, metrics.py, experiments.py): setup guard, context cards, post results, experiments
    "context_guard": True,      # a pick that opens mid-story ("he…", "and then…") starts at its setup when that fits
    "context_cards": "auto",    # auto: when the setup doesn't fit, one small AI call writes a 2 s "Earlier: …" card | off
    "experiments": {},          # Autopilot A/B tests {"enabled", "hook": [...], "length": [...], "slot": [...]}: allowed options
    "tiktok_metrics": False,    # also ask TikTok for video.list when connecting (view counts; needs the Display API on the app)
    # signals (signals.py, twitch.py, chatlog.py): Whisper hints, "clip that" phrases, live Twitch chat, audio events.
    # The Twitch app's Client ID/secret live in connections.json with the other platform apps (twitch.set_app), not here
    "whisper_language": "",     # "" = detect per video; an ISO code (en, de, ja…) pins every transcription to it
    "brand_vocab": "",          # names and slang Whisper should spell right (commas or new lines), sent as hotwords
    "voice_triggers": "",       # "" = signals.VOICE_TRIGGERS ("clip that", "clip it"…); else comma-separated phrases
    "twitch_chat_channel": "",  # a Twitch login whose live chat is recorded while Reelwright runs ("" = off)
    "chat_emotes": {},          # extra chat reactions {"*" or channel login: {kind: [emote or word, ...]}}
    "audio_events": True,       # tag laughter/screams/gunshots… around spikes (a 27 MB model downloads on first use)
    # render & QA (media.py, qa.py, pipeline's render queue): renders beside analysis, and the profanity bleep list
    "parallel_render": "auto",  # auto (when NVENC encodes or the CPU has 8+ cores) | on | off: renders wait for analysis
    "bleep_words": "fuck*, motherfuck*, shit*, bullshit, bitch*, cunt*, asshole*, dickhead*, bastard*, cock, pussy, whore*, "
                   "slut*",     # clips with "Bleep" on beep/mute these words (* = any ending) and star them in captions
    # live & integrations (obs.py marks, telemetry.py, hud.py, mcp_server.py): all off; local official interfaces only, and
    # telemetry runs only while an OBS connection is recording (its start time is the clock that places every event)
    "live_mark_hotkey": "",     # a global hotkey that marks the running recording, e.g. "ctrl+alt+m" ("" = off)
    "lol_enabled": False,       # League of Legends Live Client Data on this PC (port 2999)
    "gsi_enabled": False,       # CS2 / Dota 2 Game State Integration: the game posts to a listener on 127.0.0.1
    "gsi_port": 3010,
    "gsi_token": "",            # the token the games send back (it is written into the generated .cfg)
    "vts_enabled": False,       # VTube Studio's plugin API on this PC: hotkeys, item drops, tracking loss
    "vts_port": 8001,
    "vts_token": "",            # VTube Studio's plugin token, given once the creator clicks Allow in VTS
    # publishing & security (vault.py, routes_publish.py): secrets sealed with DPAPI (see SEALED), disclosure, templates
    "ai_disclosure": "auto",    # auto: label posts as AI content when the clip used AI-generated media | always
    "description_template": "Full stream: {source_url}",  # added to each post's text; an account's own template wins.
                                # {title} {hook} {description} {source_url} {vod_time} {channel} {hashtags}; a line whose
                                # variables are all empty is left out ("" = no template)
    # model marketplace (models/, routes_models.py): named hardware profiles hold back VRAM; selections live in the store
    "model_hw_profiles": [{"name": "Idle", "reserve_gib": 0}, {"name": "OBS live", "reserve_gib": 3}],
    "model_hw_profile": "Idle",  # the profile fit estimates use by default
    # Model Lab (training/, routes_training.py): the external training env and the specialist's promote gate
    "training_env_dir": "",     # the venv + llama.cpp tools folder ("" = DATA/training-env)
    "training_python": [],      # an existing env's python, or a WSL command as argv (overrides the venv above)
    "training_wsl": False,      # training_python is a WSL command: paths are passed as /mnt/<drive>/...
    "training_target": "",      # a remote Ollama / Reelwright host the specialist is exported to and fit-checked on ("" = this PC)
    "training_margin": 0.02,    # promote only when precision@k beats the active model by this much
    "training_tolerance": 0.02, # ...and no golden eval metric gets worse than this
}
SEALED = ("anthropic_api_key", "lan_token", "gsi_token", "vts_token", "remote_devices")  # + ai_providers.*.api_key
PROVIDER_DEFAULTS = {
    "ollama": {"base_url": "http://127.0.0.1:11434", "model": "", "num_ctx": 16384},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "", "api_key": "", "vision": True},
}

_lock = threading.RLock()  # re-entrant: save() holds it across its load() so concurrent saves never drop each other


def _raw() -> dict:
    """settings.json as stored (secrets still sealed)."""
    with _lock:
        try:
            saved = json.loads(SETTINGS_FILE.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {}
    return saved if isinstance(saved, dict) else {}


def _opened(v, default):
    try:
        return vault.unseal(v)
    except vault.Locked:  # sealed on another PC / Windows user: reads as not set (vault_locked says which)
        return default.copy() if isinstance(default, (list, dict)) else default


def _providers(d: dict) -> dict:
    p = d.get("ai_providers")
    return {n: v for n, v in p.items() if isinstance(v, dict)} if isinstance(p, dict) else {}


def _write(d: dict) -> None:
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2), "utf-8")
    os.replace(tmp, SETTINGS_FILE)
    if memo := _request.get():
        memo.clear()


# One HTTP request reads the settings once (server's middleware opens a scope): /api/settings alone asked ~10 times, the
# Pipeline board once per card. Keyed on the file's mtime and size, and dropped on every _write, so a save is seen at once.
_request: contextvars.ContextVar[dict | None] = contextvars.ContextVar("settings_memo", default=None)


def load() -> dict:
    """Settings with every secret opened (vault.py); one that can't be opened on this PC reads as its default."""
    memo = _request.get()
    return _load() if memo is None else copy.deepcopy(_memo(memo))


def _memo(memo: dict) -> dict:
    """The request's settings (shared: read, never change them)."""
    try:
        st = SETTINGS_FILE.stat()
        key = (str(SETTINGS_FILE), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(SETTINGS_FILE), None)
    if memo.get("key") != key:
        memo.update(key=key, s=_load())
    return memo["s"]


def _load() -> dict:
    saved = _raw()
    s = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    for k in SEALED:
        s[k] = _opened(s[k], DEFAULTS[k])
    s["ai_providers"] = {n: {**p, **({"api_key": _opened(p["api_key"], "")} if "api_key" in p else {})} for n, p in _providers(s).items()}
    try:  # a target outside min..max (e.g. an old max below the new default) means automatic, so saving never gets stuck
        if not float(s["clip_min"]) <= float(s["clip_target"]) <= float(s["clip_max"]):
            s["clip_target"] = 0
    except (TypeError, ValueError):
        s["clip_target"] = 0
    return s


def save(patch: dict) -> dict:
    with _lock:  # read-modify-write as one step; written whole then swapped in, so a crash never leaves half a file
        raw, cur = _raw(), load()
        cur.update({k: v for k, v in patch.items() if k in DEFAULTS})
        old_p = _providers(raw)
        vault.backup_once(SETTINGS_FILE, [raw.get(k) for k in SEALED] + [p.get("api_key") for p in old_p.values()])
        out = {**cur, **{k: vault.keep(cur[k], raw.get(k)) for k in SEALED}}  # sealed; never blanks an unreadable one
        out["ai_providers"] = {n: {**p, **({"api_key": vault.keep(p["api_key"], old_p.get(n, {}).get("api_key"))} if "api_key" in p else {})}
                               for n, p in _providers(cur).items()}
        _write(out)
    return cur


def locked() -> list[str]:
    """Secret settings sealed on another PC or Windows user (they read as empty here)."""
    raw = _raw()
    return [k for k in SEALED if vault.is_locked(raw.get(k))] + \
        [f"ai_providers.{n}.api_key" for n, p in _providers(raw).items() if vault.is_locked(p.get("api_key"))]


def forget_locked() -> None:
    with _lock:
        raw = _raw()
        for k in SEALED:
            if vault.is_locked(raw.get(k)):
                raw.pop(k)
        for p in _providers(raw).values():
            if vault.is_locked(p.get("api_key")):
                p.pop("api_key")
        _write(raw)


def api_key() -> str:
    return load()["anthropic_api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")


def public() -> dict:
    """Settings safe to send to the browser (key masked)."""
    s = load()
    for k in SEALED:  # keys, tokens and the phones' signing secrets (Phone remote lists devices through /api/remote,
        s.pop(k)      # the OBS page shows the relay token through /api/obs/lan)
    s["vault_locked"] = locked()  # secrets sealed on another PC / Windows user: enter them again
    key = api_key()
    s["anthropic_key_set"] = bool(key)
    s["anthropic_key_hint"] = f"…{key[-4:]}" if key else ""
    s["ai_providers"] = {}
    for name in PROVIDER_DEFAULTS:
        p = provider(name)
        k = p.pop("api_key", None)
        s["ai_providers"][name] = {**p, **({"api_key_set": bool(k), "api_key_hint": f"…{k[-4:]}" if k else ""} if k is not None else {})}
    from . import ai  # ai imports this module
    s["ai_ready"] = {t: ai.ready(t) for t in ai.TASKS}  # per task, as routed (the UI gates its AI buttons on it)
    s["storage_missing"] = not projects_dir().is_dir()  # an unplugged drive: the UI shows a banner, jobs wait
    return s


def projects_dir() -> Path:
    """The folder holding every project folder: the one chosen in Settings › Storage, else DATA/projects. Everything that
    touches project files resolves it through here (pipeline.pdir, the /files route, the LAN receiver, the doctor)."""
    memo = _request.get()
    d = (_load() if memo is None else _memo(memo))["storage_dir"]  # every pdir() asks: no copy of all the settings
    return Path(d).resolve() if d else DEFAULT_PROJECTS


def provider(name: str) -> dict:
    """One provider's settings, defaults filled in."""
    return {**PROVIDER_DEFAULTS.get(name, {}), **(load()["ai_providers"].get(name) or {})}
