"""Multi-provider AI (Ollama, OpenAI-compatible) and the "local first, Claude agrees" consensus mode. No network:
every HTTP call goes through an httpx.MockTransport."""
import json
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import httpx  # noqa: E402
import pytest  # noqa: E402

from reelwright import ai, config, providers, store, usage  # noqa: E402

SEGS = [{"start": float(i * 5), "end": float(i * 5 + 5), "text": f"line {i} words here"} for i in range(40)]


@pytest.fixture
def net(monkeypatch):
    """Records every request; reply(fn) sets the handler (request -> dict | httpx.Response)."""
    sent, box = [], {}

    def handler(req):
        body = json.loads(req.content or b"{}")
        sent.append({"url": str(req.url), "body": body, "headers": dict(req.headers)})
        out = box["fn"](req, body)
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)

    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(handler))
    providers._vision.cache_clear()
    return type("Net", (), {"sent": sent, "reply": staticmethod(lambda fn: box.update(fn=fn))})


@pytest.fixture(autouse=True)
def clean_settings():
    keep = config.load()
    yield
    config.save({k: keep[k] for k in ("ai_routes", "ai_providers", "ai_consensus", "anthropic_api_key")})


def ollama_chat(content, tags=None):
    def fn(req, body):
        if req.url.path == "/api/tags":
            return {"models": tags or []}
        return {"model": body["model"], "message": {"role": "assistant", "content": content},
                "done": True, "prompt_eval_count": 120, "eval_count": 30}
    return fn


def test_ollama_request_uses_schema_format_no_stream_and_images_for_vision_models(net):
    net.reply(ollama_chat(json.dumps({"query": "cat fails"}), tags=[{"name": "llava:7b", "capabilities": ["completion", "vision"]}]))
    cfg = {"base_url": "http://127.0.0.1:11434", "model": "llava:7b", "num_ctx": 8192}
    user = [{"type": "text", "text": "look"}, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}]
    out = providers.ask("ollama", cfg, "SYS", user, ai.Query)
    assert out.query == "cat fails"
    chat = [s for s in net.sent if s["url"].endswith("/api/chat")][0]
    b = chat["body"]
    assert chat["url"] == "http://127.0.0.1:11434/api/chat"
    assert b["stream"] is False and b["format"] == ai.Query.model_json_schema()
    assert b["model"] == "llava:7b" and b["options"]["num_ctx"] == 8192
    assert b["messages"][0] == {"role": "system", "content": "SYS"}
    assert b["messages"][1]["images"] == ["QUJD"] and "look" in b["messages"][1]["content"]


def test_ollama_drops_images_for_text_only_models(net):
    net.reply(ollama_chat(json.dumps({"query": "q"}), tags=[{"name": "llama3.1:8b", "capabilities": ["completion"]}]))
    user = [{"type": "text", "text": "t"}, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}]
    providers.ask("ollama", {"base_url": "http://127.0.0.1:11434", "model": "llama3.1:8b"}, "S", user, ai.Query)
    chat = [s for s in net.sent if s["url"].endswith("/api/chat")][0]["body"]
    assert all("images" not in m for m in chat["messages"])


def test_openai_compatible_request_shape(net):
    net.reply(lambda req, body: {"choices": [{"message": {"role": "assistant", "content": json.dumps({"query": "x"})}}],
                                 "usage": {"prompt_tokens": 100, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 40}}})
    cfg = {"base_url": "https://openrouter.ai/api/v1", "model": "meta-llama/llama-3.3-70b", "api_key": "sk-secret"}
    user = ("CONTEXT", [{"type": "text", "text": "TASK"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}])
    assert providers.ask("openai", cfg, "SYS", user, ai.Query).query == "x"
    s = net.sent[0]
    assert s["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert s["headers"]["authorization"] == "Bearer sk-secret"
    rf = s["body"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "Query"
    assert rf["json_schema"]["schema"] == ai.Query.model_json_schema() and "strict" in rf["json_schema"]
    content = s["body"]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "CONTEXT"} and content[1] == {"type": "text", "text": "TASK"}
    assert content[2] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}


def test_bad_json_is_retried_once_with_the_validation_error_then_fails_clearly(net):
    replies = iter(['{"nope": 1}', json.dumps({"query": "fixed"})])
    net.reply(lambda req, body: {"message": {"content": next(replies)}, "done": True})
    cfg = {"base_url": "http://127.0.0.1:11434", "model": "m"}
    assert providers.ask("ollama", cfg, "S", "U", ai.Query).query == "fixed"
    retry = net.sent[-1]["body"]["messages"]
    assert retry[-2] == {"role": "assistant", "content": '{"nope": 1}'} and "query" in retry[-1]["content"]

    net.reply(lambda req, body: {"message": {"content": "not json"}, "done": True})
    with pytest.raises(RuntimeError, match="(?i)invalid|schema"):
        providers.ask("ollama", cfg, "S", "U", ai.Query)


def test_unreachable_ollama_says_so(net):
    def down(req, body):
        raise httpx.ConnectError("refused")
    net.reply(down)
    with pytest.raises(RuntimeError, match="Ollama"):
        providers.ask("ollama", {"base_url": "http://127.0.0.1:11434", "model": "m"}, "S", "U", ai.Query)


def test_error_messages_never_echo_the_key(net):
    net.reply(lambda req, body: httpx.Response(401, json={"error": {"message": "Incorrect API key provided: sk-secret"}}))
    with pytest.raises(RuntimeError) as e:
        providers.ask("openai", {"base_url": "https://api.openai.com/v1", "model": "m", "api_key": "sk-secret"}, "S", "U", ai.Query)
    assert "sk-secret" not in str(e.value) and "401" in str(e.value)


def test_routing_per_task_and_usage_metering(net, monkeypatch):
    for d in store.find("usage"):
        store.delete("usage", d["id"])
    config.save({"ai_routes": {"meta": "ollama"}, "ai_providers": {"ollama": {"model": "qwen3:8b"}}})
    net.reply(ollama_chat(json.dumps({"title": "T", "description": "D", "tags": ["a"], "category_id": "22",
                                      "social_caption": "c"})))
    monkeypatch.setattr(ai.config, "api_key", lambda: "")  # no Claude key needed for an Ollama-routed task
    d = ai.youtube_meta({"start": 0, "end": 10, "title": "t", "hook": "h"}, SEGS, vertical=False)
    assert d["title"] == "T" and net.sent[0]["body"]["model"] == "qwen3:8b"
    row = store.find("usage")[0]
    assert row["model"] == "ollama:qwen3:8b" and row["cost"] is None and row["input"] == 120 and row["output"] == 30
    assert usage.summary()["by_model"]["ollama:qwen3:8b"] == {"calls": 1, "input": 120, "output": 30, "cache_read": 0,
                                                              "cost": 0.0, "unpriced": 1}
    assert ai.route("meta") == "ollama" and ai.route("find") == "anthropic"
    assert ai.ready("meta") and not ai.ready("find")


def test_anthropic_path_is_unchanged_by_default(monkeypatch):
    import anthropic
    sent = {}

    class Fake:
        def __init__(self, **kw):
            parse = staticmethod(lambda **kw: sent.update(kw) or type(
                "R", (), {"stop_reason": "end_turn", "parsed_output": ai.Query(query="q")})())
            self.beta = type("B", (), {"messages": type("M", (), {"parse": parse})()})()

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    monkeypatch.setattr(ai.config, "api_key", lambda: "k")
    monkeypatch.setattr(providers, "ask", lambda *a, **k: pytest.fail("default routes must stay on Anthropic"))
    assert ai._ask("sys", ("C", "T"), ai.Query, effort="low").query == "q"
    assert sent["output_config"] == {"effort": "low"} and sent["messages"][0]["content"][0]["cache_control"]


def test_keys_are_masked_in_public_settings():
    config.save({"ai_providers": {"openai": {"api_key": "sk-abcdef1234", "model": "gpt-x"}}})
    pub = config.public()
    flat = json.dumps(pub)
    assert "sk-abcdef1234" not in flat
    assert pub["ai_providers"]["openai"]["api_key_set"] and pub["ai_providers"]["openai"]["api_key_hint"] == "…1234"
    assert pub["ai_providers"]["openai"]["model"] == "gpt-x"


def test_settings_route_validates_urls_and_keeps_keys(monkeypatch):
    from fastapi import HTTPException

    from reelwright import server
    config.save({"ai_providers": {"openai": {"api_key": "sk-keep", "model": "a"}}})
    server.put_settings({"ai_providers": {"openai": {"model": "b", "api_key": None}}})  # untouched key field: keep it
    p = config.provider("openai")
    assert p["api_key"] == "sk-keep" and p["model"] == "b"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x.com", "127.0.0.1:11434", ""):
        with pytest.raises(HTTPException):
            server.put_settings({"ai_providers": {"ollama": {"base_url": bad}}})
    with pytest.raises(HTTPException):
        server.put_settings({"ai_routes": {"find": "skynet"}})
    with pytest.raises(HTTPException):
        server.put_settings({"ai_consensus": "ollama:ollama"})
    server.put_settings({"ai_consensus": "local_then_claude", "ai_routes": {"judge": "openai"}})
    assert config.load()["ai_consensus"] == "local_then_claude" and ai.route("judge") == "openai"
    with pytest.raises(ValueError):
        providers.check_url("gopher://x")


def test_privacy_policy_discloses_cloud_ai_providers_and_custom_urls():
    from pathlib import Path
    policy = (Path(providers.__file__).parent / "static" / "legal" / "privacy.html").read_text("utf-8").lower()
    for host in ("api.openai.com", "openrouter.ai", "generativelanguage.googleapis.com", "ollama"):
        assert host in policy
    assert "base url" in policy


# ---------- consensus ----------

def _cands():
    return [{"start": 10.0, "end": 40.0, "title": "A", "hook": "h", "reason": "scout likes A", "score": 70, "hashtags": []},
            {"start": 60.0, "end": 90.0, "title": "B", "hook": "h", "reason": "scout likes B", "score": 65, "hashtags": []},
            {"start": 120.0, "end": 150.0, "title": "C", "hook": "h", "reason": "scout likes C", "score": 60, "hashtags": []}]


def _consensus_on(monkeypatch):
    config.save({"ai_consensus": "local_then_claude", "ai_providers": {"ollama": {"model": "llama3.1:8b"}}})
    monkeypatch.setattr(ai.config, "api_key", lambda: "k")


def test_consensus_scout_finds_many_then_reviewer_agrees_adjusts_rejects(monkeypatch):
    _consensus_on(monkeypatch)
    calls, events = [], []
    monkeypatch.setattr(ai.log, "event", lambda kind, msg: events.append((kind, msg)))

    def fake(system, user, schema, effort="high", model=None, provider=None):
        calls.append((schema.__name__, provider))
        if schema is ai.MomentList:
            return ai.MomentList(clips=[ai.Moment(**{k: v for k, v in c.items()}) for c in _cands()])
        assert schema is ai.Review and provider == "anthropic"
        assert "#0" in str(user) and "#2" in str(user) and "scout likes B" in str(user)
        return ai.Review(verdicts=[
            ai.Verdict(index=0, verdict="agree", score=88, reason="great hook"),
            ai.Verdict(index=1, verdict="adjust", start=55.0, end=85.0, score=75, reason="needs the setup"),
            ai.Verdict(index=2, verdict="reject", score=20, reason="no payoff")])

    monkeypatch.setattr(ai, "_ask", fake)
    out = ai.find_moments(SEGS, 200.0, {"video_type": {"type": "gaming"}}, 2, 15, 40)
    assert calls[0] == ("MomentList", "ollama") and calls[-1] == ("Review", "anthropic")
    assert [c["title"] for c in out] == ["A", "B"] and out[0]["score"] == 88
    assert out[1]["start"] == 55.0 and out[1]["end"] == 85.0
    a, b = out[0]["consensus"], out[1]["consensus"]
    assert a["verdict"] == "agree" and b["verdict"] == "adjust"
    assert a["scout"]["provider"] == "ollama" and a["scout"]["reason"] == "scout likes A" and a["scout"]["model"] == "llama3.1:8b"
    assert a["reviewer"]["provider"] == "anthropic" and a["reviewer"]["reason"] == "great hook"
    assert not any(c["title"] == "C" for c in out)
    assert any(k == "consensus" and "no payoff" in m for k, m in events)  # rejects live in the log only


def test_consensus_asks_the_scout_for_more_candidates_with_playbook(monkeypatch):
    _consensus_on(monkeypatch)
    seen = {}

    def fake(system, user, schema, effort="high", model=None, provider=None):
        if schema is ai.MomentList:
            seen["ctx"], seen["task"] = user
            return ai.MomentList(clips=[ai.Moment(**c) for c in _cands()])
        seen["review_system"] = system
        return ai.Review(verdicts=[ai.Verdict(index=0, verdict="agree", score=80, reason="ok")])

    monkeypatch.setattr(ai, "_ask", fake)
    ai.find_moments(SEGS, 200.0, {"video_type": {"type": "podcast"}}, 4, 15, 40)
    assert "Find the 10 best" in seen["task"]  # 2.5x the requested 4
    assert "VIDEO TYPE" in seen["ctx"] and "VIDEO TYPE" in seen["review_system"]


def test_consensus_falls_back_to_single_model_when_scout_fails(monkeypatch):
    _consensus_on(monkeypatch)
    events, calls = [], []
    monkeypatch.setattr(ai.log, "event", lambda kind, msg: events.append((kind, msg)))

    def fake(system, user, schema, effort="high", model=None, provider=None):
        calls.append(provider)
        if provider == "ollama":
            raise RuntimeError("Couldn't reach Ollama")
        return ai.MomentList(clips=[ai.Moment(**_cands()[0])])

    monkeypatch.setattr(ai, "_ask", fake)
    out = ai.find_moments(SEGS, 200.0, {}, 2, 15, 40)
    assert calls == ["ollama", None] and out[0]["title"] == "A" and "consensus" not in out[0]
    assert any("Ollama" in m for _, m in events)


def test_consensus_off_by_default_is_the_old_single_pass(monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "_ask", lambda system, user, schema, effort="high": calls.append(schema) or ai.MomentList(clips=[]))
    ai.find_moments(SEGS, 200.0, {}, 2, 15, 40)
    assert calls == [ai.MomentList]


def test_consensus_keeps_the_scout_picks_when_the_reviewer_fails(monkeypatch):
    _consensus_on(monkeypatch)

    def fake(system, user, schema, effort="high", model=None, provider=None):
        if schema is ai.MomentList:
            return ai.MomentList(clips=[ai.Moment(**c) for c in _cands()])
        raise RuntimeError("Claude is rate-limiting")

    monkeypatch.setattr(ai, "_ask", fake)
    out = ai.find_moments(SEGS, 200.0, {}, 2, 15, 40)
    assert [c["title"] for c in out] == ["A", "B"] and "consensus" not in out[0]


def test_base_url_with_credentials_is_refused():
    with pytest.raises(ValueError):
        providers.check_url("http://user:pass@127.0.0.1:11434")


# ---------- features routed to another provider don't need an Anthropic key ----------

def _ollama_only():
    config.save({"anthropic_api_key": "", "ai_providers": {"ollama": {"model": "llama3.1"}},
                 "ai_routes": {t: "ollama" for t in ai.TASKS}})
    assert not config.api_key() and all(ai.ready(t) for t in ai.TASKS)


def _lf_project():
    from reelwright import pipeline
    words = [{"s": float(i), "e": i + 0.5, "w": f"w{i}"} for i in range(200)]
    p = store.put("project", {"name": "Stream", "status": "ready", "meta": {"duration": 200.0, "width": 1920, "height": 1080,
                                                                           "fps": 30, "has_audio": True}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    pipeline.save_json(p["id"], "transcript.json", {"segments": [{"start": 0, "end": 200, "text": "x", "words": words}]})
    pipeline.save_json(p["id"], "signals.json", {"silences": []})
    return p


def test_ollama_only_runs_long_form_moments_and_chapter_titles(monkeypatch):
    from fastapi.testclient import TestClient
    from reelwright import longform, server
    _ollama_only()
    found, titled = [], []
    monkeypatch.setattr(ai, "find_moments", lambda *a, **k: found.append(1) or [{"start": 10.0, "end": 60.0, "score": 80}])
    monkeypatch.setattr(ai, "refine", lambda raw, *a, **k: raw)
    monkeypatch.setattr(ai, "longform_chapter_titles", lambda texts, g="": titled.append(1) or ["Local title"] * len(texts))
    p = _lf_project()
    assert longform._moments(p["id"], 120.0) and found  # not the offline heuristic
    d = longform.create(p["id"], "condensed", "Cut", [], [], status="capturing")
    longform.capture_job({"id": "j", "longform_id": d["id"]})
    assert titled and store.get("longform", d["id"])["chapters"][0]["title"] == "Local title"
    titled.clear()
    api = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    assert api.post(f"/api/longform/{d['id']}/chapters/auto").status_code == 200 and titled


def test_ollama_only_keeps_retraining_the_profile_and_reports_ready(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from reelwright import pipeline
    _ollama_only()
    for kind in ("profile", "feedback"):
        for x in store.find(kind):
            store.delete(kind, x["id"])
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    prof = store.put("profile", {"name": "Me", "active": True, "trained": {"theme": {}}, "trained_at": old})
    fb = [store.put("feedback", {"created": datetime.now(timezone.utc).timestamp(), "signal": 1}) for _ in range(10)]
    sub = []
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: sub.append((kind, kw)))
    monkeypatch.setattr(pipeline.platforms, "connected", lambda p: False)
    try:
        pipeline._learning_tick()
    finally:  # an active profile would restyle every later test's new clips
        store.delete("profile", prof["id"])
        for f in fb:
            store.delete("feedback", f["id"])
    assert ("train", {"profile_id": prof["id"], "auto": True}) in sub
    assert all(config.public()["ai_ready"][t] for t in ai.TASKS)  # the UI enables "Write with AI" / "Plan edits"


def test_long_video_chunks_keep_the_callers_context(monkeypatch):
    """A benchmark routes calls through a ContextVar; the chunked scan's worker threads must see it too,
    or long videos would silently go to the default (possibly paid) provider."""
    import contextvars
    probe = contextvars.ContextVar("probe", default="lost")
    seen = []
    monkeypatch.setattr(ai, "_find_pass", lambda *a, **k: seen.append(probe.get()) or [])
    segs = [{"start": float(t), "end": t + 5.0, "text": "x"} for t in range(0, 3 * 3600, 60)]
    token = probe.set("trial")
    try:
        ai._scan(segs, 3 * 3600.0, {}, 3, 20, 60, {}, provider="ollama")
    finally:
        probe.reset(token)
    assert seen and set(seen) == {"trial"}
