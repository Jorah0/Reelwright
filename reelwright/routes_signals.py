"""Settings → Signals: transcription hints (language pin, brand vocabulary), "clip that" phrases, live Twitch chat
capture and its reaction emotes, the Twitch app used for viewer clips, and the audio-event model's status."""
import re

from fastapi import APIRouter, HTTPException

from . import chatlog, config, signals, twitch

router = APIRouter(on_startup=[lambda: chatlog.apply()])  # live chat capture starts with the app (apply is idempotent)

LANG_RE = re.compile(r"[a-z]{2,3}")
LOGIN_RE = re.compile(r"[a-z0-9_]{3,25}")


@router.get("/api/signals")
def get_signals():
    s = config.load()
    return {"settings": {k: s[k] for k in ("whisper_language", "brand_vocab", "voice_triggers", "twitch_chat_channel", "audio_events")}
            | {"chat_emotes": signals.emote_text(s["chat_emotes"])},
            "default_triggers": list(signals.VOICE_TRIGGERS), "emotes": {k: list(v) for k, v in signals.EMOTES.items()},
            "twitch": {"has_app": twitch.has_app(), "client_id": twitch.app().get("client_id", "")},  # the id is public; never the secret
            "chatlog": chatlog.status(), "audio": signals.tagger_status()}


def _text(body: dict, key: str, cap: int) -> str:
    v = body.get(key) or ""
    if not isinstance(v, str) or len(v) > cap:
        raise HTTPException(400, f"{key} must be text of at most {cap} characters.")
    return v.strip()


@router.put("/api/signals")
def put_signals(body: dict):
    patch: dict = {}
    if "whisper_language" in body:
        patch["whisper_language"] = v = _text(body, "whisper_language", 3).lower()
        if v and not LANG_RE.fullmatch(v):
            raise HTTPException(400, "Language must be an ISO code such as en, de or ja (empty = detect it).")
    for key, cap in (("brand_vocab", 2000), ("voice_triggers", 500)):
        if key in body:
            patch[key] = _text(body, key, cap)
    if "twitch_chat_channel" in body:
        v = re.sub(r"^(https?://)?(www\.)?twitch\.tv/", "", _text(body, "twitch_chat_channel", 60).lower()).strip("#/")
        if v and not LOGIN_RE.fullmatch(v):
            raise HTTPException(400, "Enter the Twitch channel's login name (letters, digits and _), or leave it empty.")
        patch["twitch_chat_channel"] = v
    if "chat_emotes" in body:
        patch["chat_emotes"] = signals.parse_emote_text(_text(body, "chat_emotes", 5000))
    if "audio_events" in body:
        if not isinstance(body["audio_events"], bool):
            raise HTTPException(400, "audio_events must be true or false.")
        patch["audio_events"] = body["audio_events"]
    config.save(patch)
    if "twitch_chat_channel" in patch:
        chatlog.apply()
    return get_signals()


@router.put("/api/signals/twitch")
def put_twitch_app(body: dict):
    cid, sec = _text(body, "client_id", 100), _text(body, "client_secret", 100)
    if not cid or not sec:
        raise HTTPException(400, "Paste both the Client ID and the Client secret of your Twitch app.")
    twitch.set_app(cid, sec)
    return {"has_app": True}


@router.post("/api/signals/twitch/test")
def test_twitch_app():
    if not twitch.has_app():
        raise HTTPException(400, "Save your Twitch app's Client ID and Client secret first.")
    try:
        twitch.app_token(force=True)
    except Exception as e:  # Twitch said no, or isn't reachable: say why
        raise HTTPException(400, str(e)[:300]) from e
    return {"ok": True}


@router.delete("/api/signals/twitch")
def delete_twitch_app():
    twitch.set_app("", "")
    return {"has_app": False}
