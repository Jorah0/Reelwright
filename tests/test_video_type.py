"""Video-type detection: classified early (before transcription), heuristic without a key, a per-type playbook in the
cached finder context and the judge prompt, a creator override that persists and is validated, old lessons still work."""
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from reelwright import ai, learn, pipeline, store  # noqa: E402

SEGS = [{"start": float(i * 5), "end": float(i * 5 + 4.8), "text": f"sentence number {i} here", "words": []} for i in range(40)]


def _fake_vt(**kw):
    return ai.VideoType(**{"type": "podcast", "sub_genre": None, "confidence": 0.9, "people_on_cam": 2, "facecam": False,
                           "language": "en", "reasons": ["two people at microphones"], **kw})


def test_classification_is_stored_and_runs_before_transcription(monkeypatch, tmp_path):
    order = []
    p = pipeline.new_project("Episode 12 with Dr. Smith")
    pid = p["id"]
    (pipeline.pdir(pid) / "source.mp4").write_bytes(b"x")
    store.update("project", pid, {"source": "source.mp4"})
    meta = {"duration": 600.0, "width": 1920, "height": 1080, "has_audio": True}
    monkeypatch.setattr(pipeline.media, "probe", lambda src: meta)
    monkeypatch.setattr(pipeline.media, "poster", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.media, "sprite", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.media, "audio_signals", lambda src, silences_out=None: ([1.0] * 100, []))
    monkeypatch.setattr(pipeline.media, "silences", lambda src: [])
    monkeypatch.setattr(pipeline.vision, "analyze", lambda src, meta, prog=None, faces=True, scenes=False, sprite=None:
                        order.append(f"vision faces={faces}") or {"motion": [], "face": [], "spikes": [], "scenes": []})
    monkeypatch.setattr(pipeline.vision, "frames", lambda src, a, b, n=4, width=384: order.append("frame") or [b"jpg"])
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "k")

    def ask(system, user, schema, effort="high", **kw):
        order.append("classify")
        assert schema is ai.VideoType and effort == "low"
        assert any(b.get("type") == "image" for b in user) and "Episode 12" in user[0]["text"]
        return _fake_vt()

    monkeypatch.setattr(ai, "_ask", ask)
    monkeypatch.setattr(pipeline.ai, "transcribe", lambda *a, **k: order.append("transcribe") or {"segments": SEGS, "language": "en"})
    seen = {}
    monkeypatch.setattr(pipeline, "_find", lambda pid, query, job=None, **k: seen.update(vt=store.get("project", pid)["video_type"]))
    monkeypatch.setattr(pipeline, "_autopilot", lambda pid: None)
    pipeline._analyze({"id": "j1", "project_id": pid})
    assert order.index("classify") < order.index("transcribe")
    assert order.count("classify") == 1  # confident: no second (refining) call after the transcript
    vt = store.get("project", pid)["video_type"]
    assert vt["type"] == "podcast" and vt["people_on_cam"] == 2 and vt["source"] == "ai" and seen["vt"]["type"] == "podcast"


def test_heuristic_without_a_key_uses_title_then_signals():
    vt = ai.heuristic_type("Valorant ranked clutch 1v4")
    assert vt["type"] == "gaming" and vt["sub_genre"] == "fps" and vt["source"] == "heuristic"
    assert ai.heuristic_type("Phasmophobia scary night")["sub_genre"] == "horror"
    busy = {"visual": {"motion": [0.08] * 300, "face": [0.0] * 300}}
    assert ai.heuristic_type("stream 4", busy, [], 300.0)["type"] == "gaming"
    cam = {"visual": {"motion": [0.005] * 300, "face": [1.0] * 300}}
    assert ai.heuristic_type("stream 4", cam, [], 300.0)["type"] == "just_chatting"
    talk = {"visual": {"motion": [0.005] * 200, "face": [0.0] * 200}}
    dense = [{"start": float(i), "end": i + 1.0, "text": "a b c d e f"} for i in range(200)]
    assert ai.heuristic_type("ep 3", talk, dense, 200.0)["type"] == "podcast"
    assert ai.heuristic_type("untitled")["type"] == "other" and ai.heuristic_type("untitled")["confidence"] < 0.5
    assert ai.heuristic_type("Elden Ring Episode 5")["type"] == "gaming"  # a series title isn't a podcast
    assert ai.heuristic_type("of course it broke")["type"] == "other"  # "course" only as a word


def test_a_failed_refine_keeps_the_earlier_ai_guess(monkeypatch):
    p = pipeline.new_project("stream 7")
    store.update("project", p["id"], {"video_type": {"type": "vtuber", "confidence": 0.5, "source": "ai"}})
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "k")
    monkeypatch.setattr(pipeline.vision, "frames", lambda *a, **k: [])
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Claude API error 529")))
    vt = pipeline._video_type(p["id"], "x.mp4", 100.0, {"visual": {"motion": [0.1] * 100}}, SEGS)
    assert vt["type"] == "vtuber" and store.get("project", p["id"])["video_type"]["source"] == "ai"


def test_no_key_analysis_falls_back_to_the_heuristic(monkeypatch):
    p = pipeline.new_project("My Minecraft speedrun attempt")
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "")
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: pytest.fail("no key: must not call Claude"))
    vt = pipeline._video_type(p["id"], "unused.mp4", 100.0)
    assert vt["type"] == "gaming" and vt["sub_genre"] == "speedrun"
    assert store.get("project", p["id"])["video_type"]["source"] == "heuristic"


def test_playbook_reaches_the_cached_context_not_the_task(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "_ask", lambda system, user, schema, effort="high", **k: seen.update(system=system, user=user)
                        or ai.MomentList(clips=[]))
    vt = {"type": "podcast", "sub_genre": None, "source": "ai"}
    ai.find_moments(SEGS, 200.0, {"scenes": [], "video_type": vt}, 3, 15, 40, query="funny")
    ctx, task = seen["user"]
    book = ai.PLAYBOOKS["podcast"]
    assert book in ctx and book not in task and "Podcast" in ctx
    # FPS sub-genre adds its own notes on top of the gaming playbook
    ai.find_moments(SEGS, 200.0, {"video_type": {"type": "gaming", "sub_genre": "fps"}}, 3, 15, 40)
    assert ai.SUB_PLAYBOOKS["fps"] in seen["user"][0] and ai.PLAYBOOKS["gaming"] in seen["user"][0]
    # the long-video judge gets it in its system prompt
    cands = [{"start": float(i * 10), "end": i * 10 + 8.0, "title": f"c{i}", "score": 50} for i in range(5)]
    monkeypatch.setattr(ai, "_ask", lambda system, user, schema, effort="high", **k: seen.update(jsys=system)
                        or ai.Ranking(ranked=[]))
    ai.rank_moments(cands, SEGS, 2, video_type=vt)
    assert book in seen["jsys"]
    ai.rank_moments(cands, SEGS, 2)  # no type: unchanged prompt
    assert book not in seen["jsys"]


def _client():
    from fastapi.testclient import TestClient

    from reelwright import server
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


def test_override_persists_is_validated_and_can_refind(monkeypatch):
    c = _client()
    p = pipeline.new_project("x")
    r = c.patch(f"/api/projects/{p['id']}", json={"video_type": "gaming", "sub_genre": "horror"})
    assert r.status_code == 200
    vt = store.get("project", p["id"])["video_type"]
    assert vt["type"] == "gaming" and vt["sub_genre"] == "horror" and vt["source"] == "user"
    assert c.patch(f"/api/projects/{p['id']}", json={"video_type": "banana"}).status_code == 400
    assert c.patch(f"/api/projects/{p['id']}", json={"video_type": "podcast", "sub_genre": "fps"}).status_code == 400
    assert store.get("project", p["id"])["video_type"]["sub_genre"] == "horror"  # a rejected change leaves it alone
    # a user-fixed type is never overwritten by detection
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "")
    assert pipeline._video_type(p["id"], "unused.mp4", 10.0)["source"] == "user"
    c.patch(f"/api/projects/{p['id']}", json={"video_type": "auto"})
    assert store.get("project", p["id"]).get("video_type") is None
    # re-find: untouched AI picks go, the clip finder runs again
    submitted = []
    monkeypatch.setattr(pipeline, "submit", lambda kind, **f: submitted.append((kind, f)) or {"id": "j"})
    store.update("project", p["id"], {"status": "ready"})
    pick = store.put("clip", {"project_id": p["id"], "start": 0, "end": 10, "source": "ai", "rev": 1, "rating": 0})
    kept = store.put("clip", {"project_id": p["id"], "start": 20, "end": 30, "source": "ai", "rev": 1, "rating": 1})
    assert c.patch(f"/api/projects/{p['id']}", json={"video_type": "comedy", "refind": True}).status_code == 200
    assert store.get("clip", pick["id"]) is None and store.get("clip", kept["id"])
    assert submitted[-1][0] == "find" and submitted[-1][1]["project_id"] == p["id"]


def test_upload_and_import_take_a_fixed_type(monkeypatch):
    c = _client()
    monkeypatch.setattr(pipeline, "submit", lambda kind, **f: {"id": "j"})
    r = c.post("/api/projects/url", json={"url": "https://example.com/v", "video_type": "tutorial"})
    assert store.get("project", r.json()["id"])["video_type"]["type"] == "tutorial"
    r = c.post("/api/projects/url", json={"url": "https://example.com/v"})  # default: auto-detect
    assert store.get("project", r.json()["id"]).get("video_type") is None
    assert c.post("/api/projects/url", json={"url": "https://example.com/v", "video_type": "nope"}).status_code == 400
    r = c.post("/api/projects", files={"file": ("a.mp4", b"x")}, data={"video_type": "vlog"})
    assert store.get("project", r.json()["id"])["video_type"]["type"] == "vlog"


def test_old_feedback_rows_still_work_and_type_splits_hints():
    old = [{"signal": 1, "why": "rated", "length": 30.0, "features": {"length": 0.5, "ai": 0.8}} for _ in range(6)]
    assert learn.samples(old)[0].shape == (6, len(learn.FEATURES))
    assert learn.hints(old, None, "podcast") == learn.hints(old, None)  # no typed lessons: no per-type line
    typed = old + [{"signal": 1, "why": "rated", "length": 55.0, "video_type": "podcast"} for _ in range(3)]
    assert "podcast" in learn.hints(typed, None, "podcast").lower()
    # imports keep a valid type and drop a bogus one
    rows = [{"id": "vt1", "created": 1.0, "signal": 1, "why": "rated", "video_type": "podcast"},
            {"id": "vt2", "created": 1.0, "signal": 1, "why": "rated", "video_type": "<script>"},
            {"id": "vt3", "created": 1.0, "signal": -1, "why": "dismissed"}]
    assert pipeline.import_learning({"format": pipeline.LEARN_FORMAT, "feedback": rows})["added"] == 3
    assert store.get("feedback", "vt1")["video_type"] == "podcast"
    assert "video_type" not in store.get("feedback", "vt2") and "video_type" not in store.get("feedback", "vt3")


def test_classify_retries_once_with_the_main_model_when_the_classify_model_fails(monkeypatch):
    s = {**ai.config.load(), "model": "claude-opus-5-5", "classify_model": "claude-haiku-5-5"}
    monkeypatch.setattr(ai.config, "load", lambda: s)
    calls, events = [], []

    def ask(system, user, schema, effort="high", model=None, provider=None):
        calls.append(model)
        if model == "claude-haiku-5-5":
            raise RuntimeError("Claude rejected the request: effort is not supported by this model")
        return _fake_vt()

    monkeypatch.setattr(ai, "_ask", ask)
    monkeypatch.setattr(ai.log, "event", lambda kind, msg: events.append((kind, msg)))
    vt = ai.classify_video("Episode 12", [])
    assert vt["type"] == "podcast" and vt["source"] == "ai"
    assert calls == ["claude-haiku-5-5", "claude-opus-5-5"]
    assert events and "claude-haiku-5-5" in events[0][1]
    # both failing: the error still reaches the caller (which falls back to the heuristic), after one retry only
    calls.clear()
    monkeypatch.setattr(ai, "_ask", lambda *a, model=None, **k: calls.append(model) or (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        ai.classify_video("Episode 12", [])
    assert len(calls) == 2


def test_classify_follows_the_provider_routing_and_the_models_release(monkeypatch):
    s = {**ai.config.load(), "model": "claude-opus-5-5", "classify_model": "claude-haiku-5-5"}
    monkeypatch.setattr(ai.config, "load", lambda: s)
    calls = []

    def ask(system, user, schema, effort="high", model=None, provider=None):
        calls.append(model)
        raise RuntimeError("down")

    monkeypatch.setattr(ai, "_ask", ask)
    # routed to another provider: one call there (its own model), never a retry dressed up as "the main Claude model"
    monkeypatch.setattr(ai, "_release", lambda task: None)
    s["ai_routes"] = {"classify": "ollama"}
    with pytest.raises(RuntimeError):
        ai.classify_video("Episode 12", [])
    assert calls == [None]
    # a Models-page release pins the anthropic model: Settings' classify_model must not override it
    s["ai_routes"], calls[:] = {}, []
    monkeypatch.setattr(ai, "_release", lambda task: {"provider": "anthropic", "model": "claude-rel"} if task == "classify" else None)
    monkeypatch.setattr(ai, "_ask", lambda *a, model=None, **k: calls.append(model) or _fake_vt())
    ai.classify_video("Episode 12", [])
    assert calls == [None]  # _ask then takes the release's model
