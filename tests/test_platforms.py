"""TikTok Content Posting API and Instagram Reels publishing against fakes shaped like the official docs, plus the
Autopilot pause switch. No network: every httpx call is answered by a recorder."""
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import httpx  # noqa: E402
import pytest  # noqa: E402

from reelwright import config, pipeline, platforms, store  # noqa: E402


class Net:
    """Records every request and answers from a route table: (method, url-substring) -> (status, json) or a list of them."""

    def __init__(self, monkeypatch, routes):
        self.calls, self.routes = [], routes
        for m in ("get", "post", "put"):
            monkeypatch.setattr(platforms.httpx, m, self._handler(m.upper()))

    def _handler(self, method):
        def call(url, **kw):
            self.calls.append((method, url, kw))
            for (m, frag), resp in self.routes.items():
                if m == method and frag in url:
                    status, body = resp.pop(0) if isinstance(resp, list) and len(resp) > 1 else (resp[0] if isinstance(resp, list) else resp)
                    return httpx.Response(status, json=body)
            raise AssertionError(f"unexpected {method} {url}")
        return call

    def find(self, method, frag):
        return [c for c in self.calls if c[0] == method and frag in c[1]]


@pytest.fixture
def video(tmp_path, monkeypatch):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\0" * 1000)
    monkeypatch.setattr(platforms, "_duration", lambda path: 30.0)
    return p


# ---------------- Instagram ----------------

@pytest.fixture
def ig(monkeypatch):
    platforms._save_platform("instagram", {"access_token": "IGTOKEN", "ig_user_id": "1784"})
    monkeypatch.setattr(platforms.time, "sleep", lambda s: None)
    yield
    platforms._save_platform("instagram", None)


def ig_routes(container=None):
    return {("POST", "/1784/media_publish"): (200, {"id": "999"}),
            ("POST", "/1784/media"): (200, container or {"id": "C1"}),
            ("POST", "rupload.facebook.com"): (200, {"success": True, "message": "Upload successful."}),
            ("GET", "/C1"): [(200, {"status_code": "IN_PROGRESS"}), (200, {"status_code": "FINISHED"})],
            ("GET", "/999"): (200, {"permalink": "https://www.instagram.com/reel/abc/"})}


def test_instagram_reel_flow_builds_rupload_url_when_response_has_no_uri(ig, video, monkeypatch):
    net = Net(monkeypatch, ig_routes())  # the content-publishing guide shows only {"id"}
    tags = " ".join(f"#t{i}" for i in range(40))
    res = platforms.instagram_upload(video, f"Great clip {tags}", cover_t=2.5)
    assert res == {"video_id": "999", "url": "https://www.instagram.com/reel/abc/"}
    create = net.find("POST", "/1784/media")[0][2]["params"]
    assert create["media_type"] == "REELS" and create["upload_type"] == "resumable" and create["thumb_offset"] == 2500
    assert create["caption"].count("#") == 30  # Instagram's 30-hashtag limit, extra ones dropped
    _, url, kw = net.find("POST", "rupload.facebook.com")[0]
    assert url == f"https://rupload.facebook.com/ig-api-upload/{platforms.IG_VERSION}/C1"
    assert kw["headers"]["Authorization"] == "OAuth IGTOKEN" and kw["headers"]["offset"] == "0" and kw["headers"]["file_size"] == "1000"


def test_instagram_uses_returned_uri_when_present(ig, video, monkeypatch):
    net = Net(monkeypatch, ig_routes({"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v27.0/C1?x=1"}))
    platforms.instagram_upload(video, "hi")
    assert net.find("POST", "rupload")[0][1].endswith("C1?x=1")


def test_instagram_login_tokens_use_graph_instagram_and_find_the_account_id(video, monkeypatch):
    monkeypatch.setattr(platforms.time, "sleep", lambda s: None)
    net = Net(monkeypatch, {("GET", "graph.instagram.com/v27.0/me"): (200, {"user_id": "1784", "username": "me_ig"}),
                            ("GET", "graph.instagram.com/v27.0/1784"): (200, {"username": "me_ig"})})
    info = platforms.instagram_set("IGAA" + "x" * 120, "")  # Instagram-Login token, no account id typed
    assert info["username"] == "me_ig" and platforms._load()["instagram"]["host"] == "instagram"
    assert platforms._load()["instagram"]["ig_user_id"] == "1784"
    routes = {k: v for k, v in ig_routes().items()}
    routes = {(m, "graph.instagram.com/v27.0" + f if f.startswith("/") else f): v for (m, f), v in routes.items()}
    net = Net(monkeypatch, routes)
    platforms.instagram_upload(video, "hi")
    assert all("graph.facebook.com" not in c[1] for c in net.calls)  # every Graph call went to the Instagram host
    platforms._save_platform("instagram", None)


def test_connecting_survives_accounts_without_optional_profile_fields(monkeypatch):
    def handler(method):
        def call(url, **kw):
            fields = (kw.get("params") or {}).get("fields", "")
            if "/me" in url:
                return httpx.Response(200, json={"user_id": "1784", "username": "me_ig"})
            if "profile_picture_url" in fields:  # what Meta returned for this account
                return httpx.Response(400, json={"error": {"message": "(#100) Tried accessing nonexisting field (profile_picture_url)",
                                                           "type": "OAuthException", "code": 100}})
            if fields == "followers_count":
                return httpx.Response(200, json={"followers_count": 42})
            return httpx.Response(200, json={"username": "me_ig"})
        return call
    for m in ("get", "post"):
        monkeypatch.setattr(platforms.httpx, m, handler(m))
    info = platforms.instagram_set("IGAA" + "x" * 120, "")
    saved = platforms._load()["instagram"]
    assert info["username"] == "me_ig" and saved["ig_user_id"] == "1784" and saved["followers"] == 42 and saved["avatar"] is None
    platforms._save_platform("instagram", None)


FB_TOKEN = "EAA" + "y" * 120


def _fb_net(monkeypatch, pages):
    def handler(method):
        def call(url, **kw):
            fields = (kw.get("params") or {}).get("fields", "")
            if "graph.instagram.com" in url:
                return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token - Cannot parse access token", "code": 190}})
            if url.endswith("/me/accounts"):
                return httpx.Response(200, json={"data": pages})
            if url.endswith("/555") and fields == "username":  # 555 = the person's Facebook user id
                return httpx.Response(400, json={"error": {"message": "(#12) username field is deprecated for versions v2.0 and higher", "code": 12}})
            if url.endswith("/17841400000000001"):
                return httpx.Response(200, json={"username": "my_ig"})
            return httpx.Response(400, json={"error": {"message": "unexpected", "code": 100}})
        return call
    for m in ("get", "post"):
        monkeypatch.setattr(platforms.httpx, m, handler(m))


def test_facebook_login_finds_the_instagram_account_itself(monkeypatch):
    pages = [{"id": "p1", "name": "No IG page"}, {"id": "p2", "name": "My Page", "instagram_business_account": {"id": "17841400000000001"}}]
    _fb_net(monkeypatch, pages)
    for typed in ("", "555"):  # blank, or the Facebook user id by mistake
        info = platforms.instagram_set(FB_TOKEN, typed)
        saved = platforms._load()["instagram"]
        assert info["username"] == "my_ig" and saved["ig_user_id"] == "17841400000000001" and saved["host"] == "facebook"
    platforms._save_platform("instagram", None)


def test_facebook_login_without_a_linked_instagram_says_what_to_fix(monkeypatch):
    _fb_net(monkeypatch, [{"id": "p1", "name": "My Page"}])
    with pytest.raises(RuntimeError, match="isn't linked to an Instagram"):
        platforms.instagram_set(FB_TOKEN, "555")


def _fb_net2(monkeypatch, perms, businesses=None):
    def call(url, **kw):
        if "graph.instagram.com" in url:
            return httpx.Response(400, json={"error": {"message": "Cannot parse access token", "code": 190}})
        if url.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": []})
        if url.endswith("/me/permissions"):
            return httpx.Response(200, json={"data": [{"permission": p, "status": s} for p, s in perms.items()]})
        if url.endswith("/me/businesses"):
            return httpx.Response(200, json={"data": businesses or []})
        if url.endswith("/17841400000000002"):
            return httpx.Response(200, json={"username": "biz_ig"})
        return httpx.Response(400, json={"error": {"message": "unexpected", "code": 100}})
    for m in ("get", "post"):
        monkeypatch.setattr(platforms.httpx, m, lambda url, _c=call, **kw: _c(url, **kw))


def test_no_pages_names_the_exact_missing_permission(monkeypatch):
    _fb_net2(monkeypatch, {"instagram_basic": "granted", "instagram_content_publish": "granted", "pages_show_list": "declined"})
    with pytest.raises(RuntimeError) as e:
        platforms.instagram_set(FB_TOKEN, "")
    msg = str(e.value)
    assert "pages_show_list" in msg and "pages_read_engagement" in msg and "Generate Access Token" in msg


def test_pages_in_a_business_portfolio_are_found(monkeypatch):
    perms = {p: "granted" for p in ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement",
                                    "business_management")}
    biz = [{"id": "b1", "owned_pages": {"data": [{"id": "p9", "name": "Biz Page", "instagram_business_account": {"id": "17841400000000002"}}]}}]
    _fb_net2(monkeypatch, perms, biz)
    assert platforms.instagram_set(FB_TOKEN, "")["username"] == "biz_ig"
    platforms._save_platform("instagram", None)


def test_all_permissions_granted_but_no_page_selected(monkeypatch):
    perms = {p: "granted" for p in ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement")}
    _fb_net2(monkeypatch, perms)
    with pytest.raises(RuntimeError, match="business_management"):
        platforms.instagram_set(FB_TOKEN, "")


def test_pasted_instagram_tokens_are_cleaned_and_wrong_values_explained():
    ig, fb = "IGAA" + "x" * 120, "EAA" + "y" * 120  # real tokens are 100+ characters
    assert platforms.clean_ig_token(f'  "access_token={ig}&expires_in=5183944"\n') == ig
    assert platforms.clean_ig_token(f"Bearer {fb}") == fb
    with pytest.raises(RuntimeError, match="App ID"):
        platforms.clean_ig_token("1234567890123456")
    with pytest.raises(RuntimeError, match="App Secret"):
        platforms.clean_ig_token("0123456789abcdef0123456789abcdef")
    with pytest.raises(RuntimeError, match="sign-in code"):
        platforms.clean_ig_token("AQBx-code-from-redirect#_")
    with pytest.raises(RuntimeError, match="too short"):
        platforms.clean_ig_token("IGAAshort")


def test_facebook_login_token_falls_back_cleanly(video, monkeypatch):
    Net(monkeypatch, {("GET", "graph.facebook.com/v27.0/1784"): (200, {"username": "fb_ig"}),
                      ("GET", "graph.instagram.com/v27.0/me"): (400, {"error": {"message": "Cannot parse access token", "code": 190}})})
    assert platforms.instagram_set("EAA" + "y" * 120, "1784")["username"] == "fb_ig"
    assert platforms._load()["instagram"]["host"] == "facebook"  # (blank ID: found via the Page, see the tests below)
    platforms._save_platform("instagram", None)


def test_instagram_expired_token_asks_to_reconnect(ig, video, monkeypatch):
    Net(monkeypatch, {("POST", "/1784/media"): (400, {"error": {"message": "Error validating access token", "type": "OAuthException", "code": 190}})})
    with pytest.raises(platforms.AuthError, match="reconnect"):
        platforms.instagram_upload(video, "hi")


def test_instagram_rejects_out_of_spec_files_before_uploading(ig, video, monkeypatch):
    net = Net(monkeypatch, ig_routes())
    monkeypatch.setattr(platforms, "_duration", lambda p: 2.0)
    with pytest.raises(RuntimeError, match="3 seconds"):
        platforms.instagram_upload(video, "hi")
    monkeypatch.setattr(platforms, "_duration", lambda p: 16 * 60.0)
    with pytest.raises(RuntimeError, match="15 minutes"):
        platforms.instagram_upload(video, "hi")
    assert not net.calls


# ---------------- TikTok ----------------

CREATOR = {"creator_username": "clipper", "creator_nickname": "Clipper",
           "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
           "comment_disabled": False, "duet_disabled": True, "stitch_disabled": False, "max_video_post_duration_sec": 600}


@pytest.fixture
def tt(monkeypatch):
    platforms._save_platform("tiktok", {"client_key": "k", "client_secret": "s", "access_token": "TT", "refresh_token": "R",
                                        "expires_at": 9e12})
    monkeypatch.setattr(platforms.time, "sleep", lambda s: None)
    yield
    platforms._save_platform("tiktok", None)


def tt_routes(creator=None, status=None):
    ok = {"error": {"code": "ok", "message": ""}}
    return {("POST", "creator_info/query"): (200, {"data": {**CREATOR, **(creator or {})}, **ok}),
            ("POST", "video/init"): (200, {"data": {"publish_id": "v_pub_1", "upload_url": "https://open-upload.tiktokapis.com/upload?x=1"}, **ok}),
            ("PUT", "open-upload.tiktokapis.com"): (201, {}),
            ("POST", "status/fetch"): [(200, {"data": {"status": "PROCESSING_UPLOAD"}, **ok}),
                                       (200, {"data": status or {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300]}, **ok})]}


def test_tiktok_post_respects_creator_settings_and_links_to_the_post(tt, video, monkeypatch):
    net = Net(monkeypatch, tt_routes())
    res = platforms.tiktok_upload(video, "caption #fyp", {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True,
                                                          "allow_duet": True, "allow_stitch": False}, cover_t=1.5)
    assert res["url"] == "https://www.tiktok.com/@clipper/video/7300" and res["privacy"] == "PUBLIC_TO_EVERYONE"
    body = net.find("POST", "video/init")[0][2]["json"]
    pi = body["post_info"]
    assert pi["disable_comment"] is False   # creator allows comments and the user turned them on
    assert pi["disable_duet"] is True       # creator has Duet off: never re-enabled
    assert pi["disable_stitch"] is True     # off unless the user turns it on
    assert pi["brand_content_toggle"] is False and pi["brand_organic_toggle"] is False and pi["video_cover_timestamp_ms"] == 1500
    assert body["source_info"] == {"source": "FILE_UPLOAD", "video_size": 1000, "chunk_size": 1000, "total_chunk_count": 1}
    put = net.find("PUT", "open-upload")[0][2]["headers"]
    assert put["Content-Range"] == "bytes 0-999/1000" and put["Content-Type"] == "video/mp4"


def test_tiktok_interactions_default_off_and_privacy_falls_back_safely(tt, video, monkeypatch):
    net = Net(monkeypatch, tt_routes({"privacy_level_options": ["SELF_ONLY"]}))  # unaudited apps: private only
    res = platforms.tiktok_upload(video, "x", {"privacy_level": "PUBLIC_TO_EVERYONE"})
    pi = net.find("POST", "video/init")[0][2]["json"]["post_info"]
    assert pi["privacy_level"] == "SELF_ONLY" and res["privacy"] == "SELF_ONLY"
    assert pi["disable_comment"] and pi["disable_duet"] and pi["disable_stitch"]


def test_tiktok_blocks_too_long_videos_and_private_branded_content(tt, video, monkeypatch):
    Net(monkeypatch, tt_routes({"max_video_post_duration_sec": 20}))
    with pytest.raises(RuntimeError, match="20 s"):
        platforms.tiktok_upload(video, "x", {"privacy_level": "SELF_ONLY"})
    net = Net(monkeypatch, tt_routes())
    with pytest.raises(RuntimeError, match="Branded content can't be private"):
        platforms.tiktok_upload(video, "x", {"privacy_level": "SELF_ONLY", "brand_content": True})
    assert not net.find("POST", "video/init")


def test_tiktok_errors_map_to_actions(tt, video, monkeypatch):
    Net(monkeypatch, {("POST", "creator_info/query"): (401, {"error": {"code": "access_token_invalid", "message": "expired"}})})
    with pytest.raises(platforms.AuthError):
        platforms.tiktok_upload(video, "x", {})
    routes = tt_routes()
    routes[("POST", "video/init")] = (403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": ""}})
    Net(monkeypatch, routes)
    with pytest.raises(RuntimeError, match="set your TikTok account to private"):
        platforms.tiktok_upload(video, "x", {})


def test_tiktok_large_file_chunking_matches_docs():
    mb = 1024 * 1024
    assert platforms.tiktok_chunks(4 * mb) == [(0, 4 * mb - 1)]  # under 5 MB: one whole chunk
    ch = platforms.tiktok_chunks(50_000_123, 10_000_000)  # the docs' own example (any size >64 MB uses the same rule)
    assert len(ch) == 1  # 50 MB is under the 64 MB single-chunk limit
    big = 200_000_123
    ch = platforms.tiktok_chunks(big, 10_000_000)
    assert len(ch) == 20 and ch[-1] == (190_000_000, big - 1) and ch[-1][1] - ch[-1][0] + 1 <= 128 * mb


def test_publish_passes_tiktok_options_through(monkeypatch, video):
    seen = {}
    monkeypatch.setattr(platforms, "tiktok_upload",
                        lambda p, cap, opts, prog=None, cover_t=None: seen.update(opts=opts, cover_t=cover_t) or {})
    platforms.publish("tiktok", video, {"title": "t", "privacy": "public", "tiktok": {"privacy_level": "SELF_ONLY"}, "cover_t": 3.0})
    assert seen == {"opts": {"privacy_level": "SELF_ONLY"}, "cover_t": 3.0}
    platforms.publish("tiktok", video, {"title": "t", "privacy": "private"})  # Autopilot: maps the generic privacy
    assert seen["opts"]["privacy_level"] == "SELF_ONLY"


# ---------------- best times & rescheduling ----------------

def _clear(kind):
    for d in store.find(kind):
        store.delete(kind, d["id"])


def test_best_times_learn_from_your_own_results(monkeypatch):
    from datetime import datetime, timedelta
    from reelwright import schedule
    _clear("upload"), _clear("feedback"), _clear("profile")
    assert schedule.slot_scores("tiktok")[1] == "default"
    base = datetime(2030, 1, 7).astimezone()  # a Monday, local time
    for week in range(5):  # Mondays 20:00 get 50k views, Mondays 08:00 get 200
        for hour, views in ((20, 50_000), (8, 200)):
            t = (base + timedelta(weeks=week, hours=hour)).isoformat()
            cid = f"c{week}{hour}"
            store.put("upload", {"platform": "tiktok", "status": "done", "uploaded_at": t, "clip_id": cid, "category_id": "20"})
            store.put("feedback", {"id": cid, "signal": 1, "why": "published", "views": views, "created": 1.0})
    scores, src = schedule.slot_scores("tiktok")
    assert src == "yours" and scores[(0, 20)] > 0.9 and scores[(0, 8)] < 0.2
    nxt = schedule.suggest("tiktok", 2, after=datetime(2031, 1, 1).astimezone(), taken=[])
    assert all(datetime.fromisoformat(t).astimezone().hour in (19, 20, 21) for t in nxt)
    a, b = (datetime.fromisoformat(t) for t in nxt)
    assert abs((b - a).total_seconds()) >= 3 * 3600  # spaced out


def test_research_slots_drive_suggestions_when_you_have_no_data():
    from datetime import datetime
    from reelwright import schedule
    _clear("upload"), _clear("feedback"), _clear("profile")
    store.put("profile", {"name": "p", "active": True, "playbooks": {"instagram": {"posting_slots": [{"weekday": 2, "hour": 13}]}}})
    scores, src = schedule.slot_scores("instagram")
    assert src == "research" and scores[(2, 13)] == 1.0
    t = datetime.fromisoformat(schedule.suggest("instagram", 1, after=datetime(2031, 1, 1).astimezone(), taken=[])[0]).astimezone()
    assert (t.weekday(), t.hour) == (2, 13)


def test_reschedule_moves_time_and_platform_and_never_posts_early(monkeypatch):
    submitted = []
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: submitted.append(kind) or {"id": "j"})
    p = store.put("project", {"name": "p", "meta": {"duration": 100.0, "width": 1920, "height": 1080}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    c = pipeline.new_clip(p["id"], {"start": 0.0, "end": 30.0, "score": 80}, source="ai")
    u = store.put("upload", {"clip_id": c["id"], "project_id": p["id"], "platform": "youtube", "status": "scheduled",
                             "publish_at": "2030-01-01T10:00:00+00:00", "upload_at": None})
    out = pipeline.reschedule(u["id"], when="2030-02-02T18:00:00+00:00", platform="tiktok")
    assert out["platform"] == "tiktok" and out["upload_at"] == "2030-02-02T18:00:00+00:00" and out["publish_at"] is None
    store.update("upload", u["id"], {"status": "queued"})
    pipeline._upload({"upload_id": u["id"]})  # a job queued before the move must not post ahead of the new time
    assert store.get("upload", u["id"])["status"] == "scheduled"
    store.update("upload", u["id"], {"status": "uploading"})
    with pytest.raises(RuntimeError, match="uploading"):
        pipeline.reschedule(u["id"], when="2030-03-03T18:00:00+00:00")


def _post(monkeypatch, **fields):
    p = store.put("project", {"name": "p", "meta": {"duration": 100.0, "width": 1920, "height": 1080}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    c = pipeline.new_clip(p["id"], {"start": 0.0, "end": 30.0, "score": 80}, source="ai")
    return store.put("upload", {"clip_id": c["id"], "project_id": p["id"], "platform": "youtube", "privacy": "public",
                                "publish_at": None, "upload_at": None, **fields})


def test_a_queued_post_is_never_published_twice(monkeypatch):
    jobs = []
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: jobs.append(kw) or {"id": "j"})
    u = _post(monkeypatch, status="queued")
    pipeline.reschedule(u["id"], when=None)  # "post as soon as ready" on a post whose job is already waiting
    assert jobs == []  # no second job
    store.update("upload", u["id"], {"status": "done"})
    called = []
    monkeypatch.setattr(pipeline.platforms, "publish", lambda *a, **k: called.append(1) or {})
    pipeline._upload({"upload_id": u["id"]})  # a stray duplicate job finds it done and stops
    assert not called


def test_moving_to_tiktok_never_picks_public_for_you(monkeypatch):
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: {"id": "j"})
    u = _post(monkeypatch, status="scheduled", publish_at="2031-01-01T10:00:00+00:00")
    out = pipeline.reschedule(u["id"], platform="tiktok")
    assert out["tiktok"]["privacy_level"] == "SELF_ONLY" and not out["tiktok"]["allow_comment"]


def test_manually_moving_a_held_autopilot_post_takes_it_over(monkeypatch):
    jobs = []
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: jobs.append(kind) or {"id": "j"})
    monkeypatch.setattr(pipeline, "clip_export_current", lambda c: True)
    config.save({"autopilot_paused": True})
    u = _post(monkeypatch, status="held", auto=True)
    out = pipeline.reschedule(u["id"], when=None)
    assert out["auto"] is False and out["status"] == "queued" and jobs == ["upload"]
    config.save({"autopilot_paused": False})


def test_suggestions_keep_local_hours_across_daylight_saving(monkeypatch):
    from datetime import datetime, timezone
    from reelwright import schedule
    _clear("upload"), _clear("feedback"), _clear("profile")
    # a span crossing a DST change in most zones that have one; every suggestion must land on a default hour locally
    times = schedule.suggest("youtube", 12, after=datetime(2030, 3, 1, tzinfo=timezone.utc), taken=[], horizon_days=60)
    for t in times:
        assert datetime.fromisoformat(t).astimezone().hour in schedule.DEFAULT_HOURS


def test_instagram_caption_limits_count_tags_on_new_lines():
    cap = "hook line\n" + "\n".join(f"#tag{i}" for i in range(40)) + "\n" + " ".join(f"@u{i}" for i in range(25))
    out = platforms.ig_caption(cap)
    assert out.count("#") == 30 and out.count("@") == 20 and "hook line\n#tag0" in out


def test_tiktok_cover_at_zero_seconds_is_respected(tt, video, monkeypatch):
    net = Net(monkeypatch, tt_routes())
    platforms.tiktok_upload(video, "x", {"privacy_level": "SELF_ONLY"}, cover_t=0.0)
    assert net.find("POST", "video/init")[0][2]["json"]["post_info"]["video_cover_timestamp_ms"] == 0


# ---------------- Autopilot pause ----------------

def test_autopilot_pause_holds_everything_and_resume_releases(monkeypatch):
    submitted = []
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: submitted.append(kind) or {"id": "j"})
    p = store.put("project", {"name": "p", "meta": {"duration": 100.0, "width": 1920, "height": 1080},
                              "autopilot": {"enabled": True, "top_n": 1, "platforms": ["youtube"]}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    pipeline.save_json(p["id"], "transcript.json", {"segments": []})
    c = pipeline.new_clip(p["id"], {"start": 0.0, "end": 30.0, "score": 80}, source="ai")
    config.save({"autopilot_paused": True})
    pipeline._autopilot(p["id"])
    assert not store.find("upload", clip_id=c["id"])  # paused: nothing new gets scheduled
    u = store.put("upload", {"clip_id": c["id"], "platform": "youtube", "status": "scheduled", "upload_at": None, "auto": True})
    pipeline._queue_or_schedule(u)
    assert store.get("upload", u["id"])["status"] == "held" and "upload" not in submitted
    manual = store.put("upload", {"clip_id": c["id"], "platform": "youtube", "status": "scheduled", "upload_at": None})
    pipeline._queue_or_schedule(manual)  # posts you scheduled yourself are not affected
    assert store.get("upload", manual["id"])["status"] == "queued"
    config.save({"autopilot_paused": False})
    pipeline._scheduler_tick()
    assert store.get("upload", u["id"])["status"] == "queued"
