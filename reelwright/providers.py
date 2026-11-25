"""Non-Anthropic AI providers for ai._ask: Ollama (local) and any OpenAI-compatible endpoint (OpenAI, OpenRouter,
Groq, Gemini's OpenAI endpoint, LM Studio...). Each call asks for JSON matching the Pydantic schema, validates it,
and retries once with the validation error before giving up. Anthropic stays in ai._ask (caching, effort, fallbacks)."""
from __future__ import annotations

import functools
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from . import config, log, usage

NAMES = ("anthropic", "ollama", "openai")
LABELS = {"anthropic": "Claude", "ollama": "Ollama", "openai": "OpenAI-compatible"}
TIMEOUT = httpx.Timeout(600, connect=10)  # a local model reading a long transcript can take minutes
TRANSPORT = None  # tests swap in an httpx.MockTransport


def check_url(url) -> str:
    """http(s) with a host, or ValueError. The URL is the user's choice: data goes wherever it points."""
    p = urlsplit(url if isinstance(url, str) else "")
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"Base URL must start with http:// or https:// (got {url!r}).")
    if p.username or p.password:
        raise ValueError("Put the key in the API key field, not in the base URL.")
    return url.rstrip("/")


def _post(label: str, url: str, body: dict, key: str = "") -> dict:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    scrub = (lambda s: s.replace(key, "…") if key else s)  # some APIs echo the bad key back in the error
    try:
        with httpx.Client(transport=TRANSPORT, timeout=TIMEOUT) as c:
            r = c.post(url, json=body, headers=headers)
    except httpx.ConnectError:
        raise RuntimeError(f"Couldn't reach {label} at {urlsplit(url).netloc} — is it running?") from None
    except httpx.TimeoutException:
        raise RuntimeError(f"{label} timed out — try a smaller model or a shorter video.") from None
    if r.status_code >= 400:
        raise RuntimeError(scrub(f"{label} error {r.status_code}: {r.text[:300]}"))
    return r.json()


@functools.lru_cache(maxsize=32)  # ponytail: per-process cache; a model re-pulled with new capabilities needs a restart
def _vision_cached(base: str, model: str) -> bool:
    return any(m.get("name") == model and "vision" in (m.get("capabilities") or []) for m in ollama_models(base))


def _vision(base: str, model: str) -> bool:
    """Ollama lists each installed model's capabilities in /api/tags; only vision models get images."""
    try:
        return _vision_cached(base, model)
    except Exception:  # not cached: Ollama being down once mustn't strip frames for the rest of the session
        return False


_vision.cache_clear = _vision_cached.cache_clear


def ollama_models(base: str) -> list[dict]:
    with httpx.Client(transport=TRANSPORT, timeout=10) as c:
        r = c.get(f"{check_url(base)}/api/tags")
    r.raise_for_status()
    return r.json().get("models") or []


def _parts(user) -> list[dict]:
    """ai._ask's user (str | content blocks | (context, task)) as one flat list of Anthropic-style blocks."""
    if isinstance(user, tuple):
        return [*_parts(user[0]), *_parts(user[1])]
    return [{"type": "text", "text": user}] if isinstance(user, str) else list(user)


def _ollama(cfg: dict, system: str, parts: list, schema, history: list) -> tuple[str, dict]:
    base = check_url(cfg.get("base_url") or "http://127.0.0.1:11434")
    if any(p["type"] == "image" for p in parts) and not _vision(base, cfg["model"]):
        parts = [p for p in parts if p["type"] == "text"]
    msgs = []
    for p in parts:  # a text block opens a message; the images after it ride on it (keeps frames with their candidate)
        if p["type"] == "text":
            if msgs and "images" not in msgs[-1]:
                msgs[-1]["content"] += "\n\n" + p["text"]
            else:
                msgs.append({"role": "user", "content": p["text"]})
        elif msgs:
            msgs[-1].setdefault("images", []).append(p["source"]["data"])
    body = {"model": cfg["model"], "messages": [{"role": "system", "content": system}, *msgs, *history],
            "format": schema.model_json_schema(), "stream": False,
            "options": {"num_ctx": int(cfg.get("num_ctx") or 16384)}}  # Ollama's small default context truncates transcripts
    r = _post("Ollama", f"{base}/api/chat", body)
    if r.get("prompt_eval_count", 0) >= body["options"]["num_ctx"] - 64:  # Ollama silently drops the start of the prompt
        log.event("ai", f"Ollama prompt filled the {body['options']['num_ctx']}-token context; raise Context in Settings → AI")
    return (r.get("message") or {}).get("content") or "", {"input_tokens": r.get("prompt_eval_count", 0),
                                                           "output_tokens": r.get("eval_count", 0)}


def _openai(cfg: dict, system: str, parts: list, schema, history: list) -> tuple[str, dict]:
    base = check_url(cfg.get("base_url") or "https://api.openai.com/v1")
    content = [{"type": "text", "text": p["text"]} if p["type"] == "text" else
               {"type": "image_url", "image_url": {"url": f"data:{p['source'].get('media_type', 'image/jpeg')};base64,{p['source']['data']}"}}
               for p in parts if p["type"] == "text" or cfg.get("vision", True)]
    # strict=False: Pydantic schemas with optional fields don't meet strict mode's all-required rule; we validate below
    body = {"model": cfg["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}, *history],
            "response_format": {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(),
                                                                       "strict": False}}}
    r = _post(LABELS["openai"], f"{base}/chat/completions", body, cfg.get("api_key") or "")
    msg = ((r.get("choices") or [{}])[0]).get("message") or {}
    if msg.get("refusal"):
        raise RuntimeError(f"The model declined this request: {msg['refusal'][:200]}")
    u = r.get("usage") or {}
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    return msg.get("content") or "", {"input_tokens": int(u.get("prompt_tokens") or 0) - cached, "cache_read_input_tokens": cached,
                                      "output_tokens": int(u.get("completion_tokens") or 0)}


def ask(name: str, cfg: dict, system: str, user, schema: type[BaseModel]) -> BaseModel:
    """One structured call to a non-Anthropic provider. Bad JSON gets one retry with the validation error."""
    if config.DEMO:  # belt and braces (ai._ask refuses first): demo mode never reaches any AI provider
        raise RuntimeError("Demo mode: AI features are off (nothing is sent to any AI provider).")
    if not cfg.get("model"):
        raise RuntimeError(f"Pick a model for {LABELS.get(name, name)} in Settings → AI.")
    call = {"ollama": _ollama, "openai": _openai}[name]
    parts, history = _parts(user), []
    for attempt in range(2):
        text, used = call(cfg, system, parts, schema, history)
        usage.record(schema.__name__, f"{name}:{cfg['model']}", used)  # unknown price → cost None, shown as unpriced
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            err = str(e)[:1500]
        history = [{"role": "assistant", "content": text},
                   {"role": "user", "content": f"That reply was invalid: {err}\nReply again with only JSON matching the schema."}]
    raise RuntimeError(f"{LABELS.get(name, name)} ({cfg['model']}) returned invalid JSON for the {schema.__name__} schema twice: {err[:300]}")
