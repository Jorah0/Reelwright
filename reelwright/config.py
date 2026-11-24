"""Paths and user settings (a JSON file next to the data)."""
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FROZEN = getattr(sys, "frozen", False)  # running as the packaged Reelwright.exe
_default_data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Reelwright" if FROZEN else ROOT / "data"
DATA = Path(os.environ.get("REELWRIGHT_DATA", _default_data)).resolve()
PROJECTS = DATA / "projects"
STATIC = Path(__file__).resolve().parent / "static"
SETTINGS_FILE = DATA / "settings.json"
YT_SECRET = DATA / "youtube_client_secret.json"
YT_TOKEN = DATA / "youtube_token.json"

PROJECTS.mkdir(parents=True, exist_ok=True)

DEFAULTS = {
    "anthropic_api_key": "",
    "model": "claude-opus-5-5",
    "visual_judge": True,       # the long-video judge pass also sees a few small frames per candidate (more tokens)
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
    "ui_scale": {},             # per-component interface scale, e.g. {"app": 1.1, "text": 1.2} (kept across restarts/updates)
}

_lock = threading.Lock()


def load() -> dict:
    with _lock:
        try:
            saved = json.loads(SETTINGS_FILE.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save(patch: dict) -> dict:
    cur = load()
    cur.update({k: v for k, v in patch.items() if k in DEFAULTS})
    with _lock:
        SETTINGS_FILE.write_text(json.dumps(cur, indent=2), "utf-8")
    return cur


def api_key() -> str:
    return load()["anthropic_api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")


def public() -> dict:
    """Settings safe to send to the browser (key masked)."""
    s = load()
    s.pop("anthropic_api_key")
    key = api_key()
    s["anthropic_key_set"] = bool(key)
    s["anthropic_key_hint"] = f"…{key[-4:]}" if key else ""
    return s
