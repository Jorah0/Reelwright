"""Judgment & learning: the setup guard and context cards, post results (48 h / 7 d snapshots, lift against the account's own
posts), per-account taste models, Thompson-sampling experiments and retention curves. No network: every platform is a fake."""
import math
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

from reelwright import accounts, ai, context, experiments, learn, metrics, pipeline, platforms, store, youtube  # noqa: E402


def _segs(rows):
    return [{"start": a, "end": b, "text": t, "words": []} for a, b, t in rows]


SEGS = _segs([(0.0, 4.0, "Let me tell you about my landlord."),
               (4.2, 9.0, "My landlord calls me at three in the morning."),
               (9.2, 14.0, "He says the basement is full of frogs."),
               (14.2, 20.0, "And I just hang up the phone."),
               (20.2, 26.0, "That was the best decision of my life.")])
# a 3.5 s pause before "And then the canoe flipped": nothing before it belongs to this story
LAKE = _segs([(0.0, 5.0, "We spent the whole day at the lake."), (5.2, 10.0, "The water was freezing cold."),
              (13.5, 18.0, "And then the canoe flipped over."), (18.2, 24.0, "Everything we owned went straight into the lake."),
              (24.2, 30.0, "Including the car keys, of course."), (30.2, 36.0, "We waited four hours for a locksmith.")])


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Own accounts file; no jobs actually queued."""
    monkeypatch.setattr(accounts, "FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "LEGACY_CONN", tmp_path / "connections.json")
    monkeypatch.setattr(accounts, "LEGACY_YT", tmp_path / "youtube_token.json")
    monkeypatch.setattr(platforms, "CONN_FILE", tmp_path / "connections.json")
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: {"id": "j"})
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "")  # never a real AI call (titles, cards)
    for kind in ("upload", "feedback"):
        for d in store.find(kind):
            store.delete(kind, d["id"])
    return tmp_path


def _acct(platform, **fields):
    a = accounts.add(platform, name=fields.pop("name", platform), access_token="T", **fields)
    return accounts.update(a["id"], {"approved": True, "autopilot": True})


def _project(segs, dur=300.0):
    p = store.put("project", {"name": "p", "meta": {"duration": dur, "width": 1920, "height": 1080}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    pipeline.save_json(p["id"], "transcript.json", {"segments": segs})
    pipeline.save_json(p["id"], "signals.json", {})
    return p


# ---------------- setup guard + context card ----------------

def test_dangling_openers():
    for t in ("And then it exploded.", "He says the basement is full of frogs.", "That's when it happened.",
              "One night he calls me.", "So we ran."):
        assert context.dangling(t), t
    for t in ("My landlord calls me at three in the morning.", "Let me tell you about my landlord.", "", "No way, my whole team died."):
        assert not context.dangling(t), t


def test_guard_extends_a_clip_that_lacks_its_setup_and_leaves_a_complete_one_alone():
    late, whole = {"start": 9.2, "end": 26.0, "score": 80}, {"start": 4.2, "end": 26.0, "score": 70}
    fixed, kept = context.guard([late, whole], SEGS, max_len=30)
    assert fixed["start"] == 4.2 and fixed["end"] == 26.0 and fixed["context"] == {"extended_from": 9.2}
    assert not fixed.get("needs_context")
    assert kept == whole  # already starts at its setup: untouched
    [tight] = context.guard([late], SEGS, max_len=18)  # the setup would push it past the max length
    assert tight["start"] == 9.2 and tight["needs_context"]
    [cut] = context.guard([late], SEGS, max_len=30, signals={"scenes": [9.1]})  # a hard cut is an arc boundary
    assert cut["start"] == 9.2 and cut["needs_context"]
    [paused] = context.guard([{"start": 13.5, "end": 36.0, "score": 60}], LAKE, max_len=60)  # a long pause is too
    assert paused["start"] == 13.5 and paused["needs_context"]


def test_context_card_is_written_by_a_cheap_call_and_shown_as_a_short_headline(monkeypatch):
    seen = {}

    def ask(system, user, schema, effort="high", model=None, provider=None):
        seen.update(user=user, effort=effort)
        return schema(needed=True, card="Their canoe trip at the lake")
    monkeypatch.setattr(ai, "_ask", ask)
    card = context.card({"start": 13.5, "end": 36.0}, LAKE)
    assert card == "Earlier: Their canoe trip at the lake" and seen["effort"] == "low"
    assert "freezing cold" in seen["user"]  # what came before the clip is in the prompt
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: context.ContextCard(needed=False, card=""))
    assert context.card({"start": 13.5, "end": 36.0}, LAKE) is None
    patch = context.card_edit({"headline": None}, card)["headline"]
    assert patch["on"] and patch["text"] == card and patch["show"] == "first_n_seconds" and 1 <= patch["seconds"] <= 2.5
    assert context.card_edit({"headline": {"on": True, "text": "Mine"}}, card) is None  # never replaces the creator's headline


def test_find_stores_the_card_on_a_pick_whose_setup_cannot_fit(monkeypatch):
    p = _project(LAKE)
    store.delete("pref", "headline")
    monkeypatch.setattr(pipeline, "feedback", lambda limit=200: [])  # no learned re-trim from other tests' lessons
    monkeypatch.setattr(ai, "find_moments", lambda *a, **k: [{"start": 13.5, "end": 36.0, "score": 90, "title": "Canoe"}])
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "k")
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: context.ContextCard(needed=True, card="A day at the lake"))
    pipeline._find(p["id"], "", count=1)
    [c] = store.find("clip", project_id=p["id"])
    assert c["start"] == 13.5 and c["context_card"] == "Earlier: A day at the lake"
    assert c["edit"]["headline"]["on"] and c["edit"]["headline"]["show"] == "first_n_seconds"


# ---------------- metrics: snapshots, lift, platforms ----------------

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)


def _up(hours, **m):
    return {"uploaded_at": (NOW - timedelta(hours=hours)).isoformat(), "metrics": m}


def test_snapshots_are_taken_at_48h_and_7d():
    assert metrics.due(_up(10), NOW) is None
    assert metrics.due(_up(50), NOW) == "48h"
    assert metrics.due(_up(50, **{"48h": {"views": 1}}), NOW) is None
    assert metrics.due(_up(170, **{"48h": {"views": 1}}), NOW) == "7d"
    assert metrics.due(_up(400), NOW) == "7d"  # missed the 48 h window: only the 7 d one is taken, never a late "48 h"
    assert metrics.due(_up(400, **{"7d": {"views": 1}}), NOW) is None
    later = {"uploaded_at": (NOW - timedelta(hours=100)).isoformat(), "publish_at": (NOW - timedelta(hours=20)).isoformat()}
    assert metrics.due(later, NOW) is None  # a YouTube post scheduled for later counts from when it went public
    assert metrics.due({"metrics": {}}, NOW) is None
    # hourly pulls only in the day after a snapshot falls due: an unreadable post can't keep them going
    assert metrics.due_soon(_up(50), NOW) and not metrics.due_soon(_up(80), NOW) and not metrics.due_soon(_up(400), NOW)


def test_lift_is_against_the_same_accounts_posts_of_the_same_age():
    ups = [{"id": f"a{i}", "account_id": "A", "platform": "youtube", "metrics": {"48h": {"views": v}}}
           for i, v in enumerate((100, 100, 100, 400))]
    ups += [{"id": f"b{i}", "account_id": "B", "platform": "youtube", "metrics": {"48h": {"views": 10_000}}} for i in range(4)]
    ups += [{"id": "old", "account_id": "A", "platform": "youtube", "metrics": {"48h": {"views": 50}, "7d": {"views": 900}}}]
    ups += [{"id": f"c{i}", "account_id": "C", "platform": "tiktok", "metrics": {"48h": {"views": 5}}} for i in range(2)]
    ups += [{"id": "pre", "account_id": "A", "metrics": {"48h": {"views": 100}}}]  # from before accounts: no platform key
    L = metrics.lifts(ups)
    assert L["a3"] == {"label": "48h", "value": pytest.approx(math.log2(401 / 101), abs=1e-3)}
    assert L["b0"]["value"] == 0.0  # 10k views is just a normal day for the big channel
    assert L["old"]["label"] == "48h" and L["old"]["value"] < 0  # no other 7 d posts yet: compared at 48 h instead
    assert "c0" not in L  # fewer than 3 posts to compare with: no lift
    assert L["pre"]["value"] == 0.0  # a pre-accounts post is one of the channel's YouTube posts, not a group of its own


def test_tiktok_and_instagram_results_or_which_permission_to_add(monkeypatch):
    monkeypatch.setattr(platforms, "_tiktok_access", lambda aid=None: "T")
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        if kw["json"]["filters"]["video_ids"] == ["denied"]:
            return httpx.Response(401, json={"error": {"code": "scope_not_authorized", "message": "no"}})
        return httpx.Response(200, json={"data": {"videos": [{"id": "7", "view_count": 50, "like_count": 4, "comment_count": 1,
                                                              "share_count": 2}]}, "error": {"code": "ok"}})
    monkeypatch.setattr(metrics.httpx, "post", post)
    tt = {"id": "tt1", "platform": "tiktok", "access_token": "T"}
    assert metrics.fetch(tt, ["7", "v_pub_file~x"])["7"]["views"] == 50
    assert calls[0][1]["json"]["filters"]["video_ids"] == ["7"]  # a publish id (not public yet) is not a video id
    with pytest.raises(metrics.MissingScope, match="video.list"):
        metrics.fetch(tt, ["denied"])

    def get(url, params=None, **kw):
        if "gone" in url:  # a deleted Reel: Meta rejects just that one
            return httpx.Response(400, json={"error": {"code": 100, "message": "Unsupported get request"}})
        if "denied" in url:
            return httpx.Response(400, json={"error": {"code": 10, "message": "(#10) Application does not have permission"}})
        return httpx.Response(200, json={"data": [{"name": "views", "values": [{"value": 321}]},
                                                  {"name": "ig_reels_avg_watch_time", "values": [{"value": 4200}]}]})
    monkeypatch.setattr(metrics.httpx, "get", get)
    ig = {"id": "ig1", "platform": "instagram", "access_token": "T", "host": "instagram"}
    got = metrics.fetch(ig, ["gone", "m1"])["m1"]  # one bad Reel doesn't cost the others
    assert got["views"] == 321 and got["ig_reels_avg_watch_time"] == 4200
    with pytest.raises(metrics.MissingScope, match="insights"):
        metrics.fetch(ig, ["denied"])


def test_results_take_snapshots_compute_lift_and_tag_lessons(isolated, monkeypatch):
    a, tt = _acct("youtube", name="Main"), _acct("tiktok", name="Tok")
    p = _project(SEGS)
    clips = [pipeline.new_clip(p["id"], {"start": 0.0, "end": 26.0, "score": 70}, source="ai") for _ in range(4)]
    posted = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    for i, c in enumerate(clips):
        store.put("upload", {"clip_id": c["id"], "project_id": p["id"], "platform": "youtube", "account_id": a["id"],
                             "status": "done", "video_id": f"v{i}", "uploaded_at": posted, "title": f"t{i}"})
    store.put("upload", {"clip_id": clips[0]["id"], "project_id": p["id"], "platform": "tiktok", "account_id": tt["id"],
                         "status": "done", "video_id": "77", "uploaded_at": posted, "title": "tok"})
    views = {"v0": 50, "v1": 100, "v2": 100, "v3": 1000}

    def fetch(acc, ids):
        if acc["platform"] == "tiktok":
            raise metrics.MissingScope("video.list", "Tok: TikTok view counts need the video.list permission.")
        return {i: {"views": views[i], "likes": 1} for i in ids}
    monkeypatch.setattr(metrics, "fetch", fetch)
    monkeypatch.setattr(platforms, "account_connected", lambda acc: True)
    pipeline._results()
    ups = {u["video_id"]: u for u in store.find("upload", platform="youtube")}
    assert ups["v3"]["metrics"]["48h"]["views"] == 1000 and ups["v3"]["metrics"]["lift"]["value"] > 3
    f = store.get("feedback", clips[3]["id"])
    assert f["account_id"] == a["id"] and f["platform"] == "youtube" and f["lift"] > 3 and f["views"] == 1000
    assert store.get("feedback", clips[0]["id"])["lift"] < 0
    assert "video.list" in store.get("pref", "metrics_notes")["notes"][tt["id"]]
    pipeline._results()  # the 48 h snapshot is kept, not overwritten by a later pull
    assert store.get("upload", ups["v3"]["id"])["metrics"]["48h"]["at"] == ups["v3"]["metrics"]["48h"]["at"]


# ---------------- learning: lift labels, per-account taste ----------------

def _fb(signal, account_id=None, **feat):
    return {"signal": signal, "why": "rated", "features": {**{k: 0.5 for k in learn.FEATURES}, **feat}, "length": 30.0,
            **({"account_id": account_id, "platform": "youtube"} if account_id else {})}


def test_lift_labels_replace_raw_views_and_old_rows_still_work():
    base = {k: 0.5 for k in learn.FEATURES}
    rows = [{"signal": 1, "why": "published", "features": base, "views": v, "lift": lf}
            for v, lf in ((100_000, -1.0), (50, 1.5), (900, 0.2))]
    _, y, w = learn.samples(rows)
    assert list(y) == [0.0, 1.0, 1.0] and list(w) == [1.0, 1.0, 1.0]  # a big number below that channel's normal is a miss
    old = [{"signal": 1, "why": "published", "features": base, "views": v} for v in (100, 200, 5000, 8000)]
    assert list(learn.samples(old)[1]) == [0.0, 0.0, 1.0, 1.0]  # rows from before lift: still labelled by views
    assert list(learn.samples(rows + old)[1]) == [0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 1.0]
    legacy = [{"signal": -1, "why": "dismissed", "features": {"length": 0.5}}]  # no account, no lift, few features
    assert learn.samples(legacy)[0].shape == (1, len(learn.FEATURES))
    assert learn.model_for(legacy, None, account_id="A") is None


def test_per_account_models_shrink_to_the_global_one_when_data_is_small():
    rng = np.random.default_rng(5)
    everyone = [_fb(1 if x > 1.5 else -1, loud=x) for x in rng.uniform(0.5, 2.5, 60)]  # overall: loud clips win
    g = learn.train(everyone)
    few = [_fb(1 if x < 1.5 else -1, "B", loud=x) for x in (0.6, 0.8, 2.2, 2.4)]  # account B: 4 lessons against
    mb = learn.model_for(everyone + few, g, account_id="B")
    assert mb is not g and mb["n"] == 4 and mb["shrunk"] and mb["useful"] == g["useful"]
    assert mb["weights"]["loud"] > 0  # 4 lessons can't overturn what 60 taught
    many = [_fb(1 if x < 1.5 else -1, "C", loud=x) for x in rng.uniform(0.5, 2.5, 80)]  # account C: quiet clips, a lot
    gc = learn.train(everyone + many)
    mc = learn.model_for(everyone + many, gc, account_id="C")
    assert mc["weights"]["loud"] < 0 and mc["useful"] and mc["n"] == 80
    assert learn.model_for(everyone, g, account_id="nobody") is g  # no lessons of its own: the global model
    assert learn.model_for(everyone + few, g, platform="youtube")["n"] == 4  # per platform works the same way


# ---------------- experiments ----------------

def test_thompson_sampling_picks_only_allowed_options_and_learns_from_lift():
    cfg = {"enabled": True, "hook": ["question", "statement"], "length": ["short"], "slot": ["evening", "bogus", "night"]}
    opts = experiments.allowed(cfg, slot=True)
    assert opts == {"hook": ["question", "statement"], "slot": ["evening", "night"]}  # one option is no experiment; junk ignored
    assert experiments.allowed({**cfg, "enabled": False}, slot=True) == {} and experiments.allowed(None) == {}
    assert "slot" not in experiments.allowed(cfg, slot=False)  # a fixed posting time is the creator's own choice
    ups = [{"account_id": "A", "arm": {"hook": "question"}, "metrics": {"lift": {"value": 1.0}}} for _ in range(20)]
    ups += [{"account_id": "A", "arm": {"hook": "statement"}, "metrics": {"lift": {"value": -1.0}}} for _ in range(20)]
    ups += [{"account_id": "B", "arm": {"hook": "statement"}, "metrics": {"lift": {"value": 1.0}}} for _ in range(20)]
    ups += [{"account_id": "A", "arm": {"hook": "statement"}}]  # no result yet: not counted
    st = experiments.stats(ups, "A")
    assert st["hook"] == {"question": [20, 0], "statement": [0, 20]}
    rng = random.Random(1)
    picks = [experiments.choose({"hook": ["question", "statement"]}, st, [], rng)["hook"] for _ in range(200)]
    assert picks.count("question") > 190
    fresh = [experiments.choose(opts, {}, [], rng) for _ in range(300)]
    assert {a["hook"] for a in fresh} == {"question", "statement"} and {a["slot"] for a in fresh} == {"evening", "night"}
    close = {"hook": {"question": [3, 2], "statement": [2, 3]}}
    capped = [experiments.choose({"hook": ["question", "statement"]}, close, [True] * 5 + [False] * 5, rng) for _ in range(50)]
    assert all(a["hook"] == "question" and not a["explore"] for a in capped)  # 50% explored lately: over the 25% cap


def test_autopilot_ranks_clips_per_account_and_records_the_arm(isolated, monkeypatch):
    a, b = _acct("youtube", name="Loud fans"), _acct("youtube", name="Calm fans")
    p = _project(SEGS)
    loud = pipeline.new_clip(p["id"], {"start": 0.0, "end": 26.0, "score": 80,
                                       "features": {**{k: 0.5 for k in learn.FEATURES}, "loud": 2.4, "question": 0.0}}, source="ai")
    calm = pipeline.new_clip(p["id"], {"start": 0.0, "end": 26.0, "score": 79,
                                       "features": {**{k: 0.5 for k in learn.FEATURES}, "loud": 0.6, "question": 0.0}}, source="ai")
    rng = np.random.default_rng(2)
    fb = [_fb(1 if x > 1.5 else -1, a["id"], loud=x) for x in rng.uniform(0.5, 2.5, 40)]
    fb += [_fb(1 if x < 1.5 else -1, b["id"], loud=x) for x in rng.uniform(0.5, 2.5, 40)]
    monkeypatch.setattr(pipeline, "feedback", lambda limit=200: fb)
    real = pipeline.config.load()
    monkeypatch.setattr(pipeline.config, "load", lambda: {**real, "experiments": {"enabled": True, "hook": ["question", "statement"]}})
    monkeypatch.setattr(pipeline, "paused", lambda: False)
    store.update("project", p["id"], {"autopilot": {"enabled": True, "top_n": 1, "accounts": [a["id"], b["id"]]}})
    pipeline._autopilot(p["id"])
    by = {u["account_id"]: u for u in store.find("upload", project_id=p["id"])}
    assert by[a["id"]]["clip_id"] == loud["id"] and by[b["id"]]["clip_id"] == calm["id"]
    assert all(u["arm"]["hook"] == "statement" for u in by.values())  # what was actually posted (neither opens with a question)


# ---------------- retention ----------------

def _curve(length=30.0):
    """Shorts-like retention: a steep exit in the first 2 s, a plateau, a fall-off over the last quarter."""
    rows = []
    for i in range(1, 101):
        t = i / 100 * length
        w = 1.0 if t < 0.5 else 0.55 if t <= 2.0 else 0.5 if t < 0.75 * length else 0.5 - 0.4 * (t - 0.75 * length) / (0.25 * length)
        rows.append([i / 100, w])
    return rows


def test_retention_drop_offs_feed_trim_bias_and_hook_hints():
    res = metrics.retention(_curve(), 30.0)
    assert res["available"] and res["hold_3s"] < 0.65 and 0 < res["drop_s"] < 30
    assert 0 < res["trim"][0] <= 3 and res["trim"][1] < 0  # start later, end earlier
    fb = [{"signal": 1, "retention": res} for _ in range(5)]
    assert learn.trim_bias(fb)["start"] > 0  # five posts' drop-offs teach the same as five manual re-trims
    assert "drop" in learn.hints(fb).lower()
    assert learn.trim_bias([{"signal": 1, "retention": {"available": False}}] * 5) is None
    habit = [{"signal": 1, "trim": [1.5, 0.0]} for _ in range(5)]
    healthy = [{"signal": 1, "retention": {**res, "trim": [0.0, 0.0]}} for _ in range(6)]
    assert learn.trim_bias(habit + healthy)["start"] == 1.5  # posts that held their viewers don't vote the habit away


def test_missing_retention_is_detected_and_explained(monkeypatch):
    assert "Shorts" in metrics.retention([], 28.0)["reason"] and not metrics.retention([], 28.0)["available"]
    assert "Shorts" not in metrics.retention([], 600.0)["reason"]

    class Svc:
        def reports(self):
            return self

        def query(self, **kw):
            self.kw = kw
            return self

        def execute(self):
            return {"columnHeaders": [], "rows": []}  # what YouTube answers for most Shorts
    svc = Svc()
    monkeypatch.setattr(metrics, "_analytics", lambda aid: svc)
    monkeypatch.setattr(youtube, "missing_scopes", lambda aid=None: [])
    out = metrics.youtube_retention("A", "vid", "2026-11-01", 25.0)
    assert not out["available"] and "Shorts" in out["reason"] and svc.kw["filters"] == "video==vid"
    monkeypatch.setattr(youtube, "missing_scopes", lambda aid=None: [metrics.YT_ANALYTICS])
    with pytest.raises(metrics.MissingScope, match="yt-analytics.readonly"):
        metrics.youtube_retention("A", "vid", "2026-11-01", 25.0)


# ---------------- old data, export/import, routes ----------------

def test_lessons_keep_lift_and_account_through_rerating_and_import(isolated):
    p = _project(SEGS)
    c = pipeline.new_clip(p["id"], {"start": 0.0, "end": 26.0, "score": 70}, source="ai")
    pipeline.remember(c, "published")
    store.update("feedback", c["id"], {"lift": 1.2, "account_id": "yo_1", "platform": "youtube", "views": 9})
    pipeline.remember({**c, "rating": 1}, "rated")  # an explicit 👍 keeps the measured results
    f = store.get("feedback", c["id"])
    assert f["why"] == "rated" and f["lift"] == 1.2 and f["account_id"] == "yo_1" and f["views"] == 9
    rows = [{"id": "lift1", "created": 1.0, "signal": 1, "why": "published", "lift": 0.7, "account_id": "yo_1", "platform": "youtube"},
            {"id": "lift2", "created": 1.0, "signal": 1, "why": "published", "lift": "huge", "platform": "<b>"}]
    pipeline.import_learning({"format": pipeline.LEARN_FORMAT, "feedback": rows})
    assert store.get("feedback", "lift1")["lift"] == 0.7 and store.get("feedback", "lift1")["platform"] == "youtube"
    assert "lift" not in store.get("feedback", "lift2") and "platform" not in store.get("feedback", "lift2")
    assert {"lift", "account_id", "platform"} <= pipeline.LEARN_FIELDS


def test_lab_routes(isolated):
    from fastapi.testclient import TestClient
    from reelwright import server
    api = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    r = api.get("/api/learn/insights")
    assert r.status_code == 200 and {"global", "platforms", "accounts", "experiments", "retention", "notes"} <= set(r.json())
    r = api.get("/api/learn/evals")
    assert r.status_code == 200 and "baseline" in r.json()


def test_tiktok_view_counts_are_requested_only_when_turned_on(isolated, monkeypatch):
    platforms.tiktok_set_app("key", "secret")
    assert "video.list" not in platforms.tiktok_auth_url(38765)  # an app without the Display API can still connect
    real = platforms.config.load()
    monkeypatch.setattr(platforms.config, "load", lambda: {**real, "tiktok_metrics": True})
    assert "video.list" in platforms.tiktok_auth_url(38765)
    assert metrics.YT_ANALYTICS in youtube.SCOPES  # YouTube asks for retention with every (re)connect
