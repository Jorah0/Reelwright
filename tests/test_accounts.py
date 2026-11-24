"""Several linked accounts per platform, each with its own tokens and an explicit approval ("okayed") before it is
used. No network and no real tokens: every API call is answered by a fake."""
import json
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import httpx  # noqa: E402
import pytest  # noqa: E402

from reelwright import accounts, pipeline, platforms, server, store, youtube  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    """Every test gets its own accounts file and its own (absent) pre-accounts connection files."""
    monkeypatch.setattr(accounts, "FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "LEGACY_CONN", tmp_path / "connections.json")
    monkeypatch.setattr(accounts, "LEGACY_YT", tmp_path / "youtube_token.json")
    monkeypatch.setattr(platforms, "CONN_FILE", tmp_path / "connections.json")
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: {"id": "j"})
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "")
    yield tmp_path


def acct(platform, approved=True, autopilot=False, **fields):
    a = accounts.add(platform, name=fields.pop("name", f"{platform} acct"), access_token="T", **fields)
    return accounts.update(a["id"], {"approved": approved, "autopilot": autopilot})


def clip():
    p = store.put("project", {"name": "p", "meta": {"duration": 100.0, "width": 1920, "height": 1080}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    return pipeline.new_clip(p["id"], {"start": 0.0, "end": 30.0, "score": 80}, source="ai")


def post(c, **fields):
    return store.put("upload", {"clip_id": c["id"], "project_id": c["project_id"], "platform": "youtube", "status": "queued",
                                "privacy": "private", "publish_at": None, "upload_at": None, "title": "t", **fields})


@pytest.fixture
def publishing(monkeypatch):
    """Rendered clip, every account counts as signed in, and publish() only records which account it posted to."""
    sent = []
    monkeypatch.setattr(pipeline, "clip_export_current", lambda c: True)
    monkeypatch.setattr(platforms, "account_connected", lambda a: True)
    monkeypatch.setattr(pipeline.platforms, "publish",
                        lambda plat, path, u, *a, **k: sent.append((plat, u["account_id"])) or {"video_id": "v", "url": None})
    monkeypatch.setattr(pipeline, "remember", lambda *a, **k: None)
    return sent


def _with_export(c):
    return store.update("clip", c["id"], {"export": {"file": "x.mp4", "status": "done", "rev": c["rev"]}})


# ---------------- migration ----------------

def _legacy_files(tmp):
    (tmp / "youtube_token.json").write_text(json.dumps({"token": "YT", "refresh_token": "YR", "scopes": youtube.SCOPES}), "utf-8")
    (tmp / "connections.json").write_text(json.dumps({
        "tiktok": {"client_key": "k", "client_secret": "s", "access_token": "TT", "refresh_token": "TR", "expires_at": 9e12,
                   "open_id": "o1", "display_name": "Me on TikTok", "avatar": "https://a/x.jpg"},
        "instagram": {"access_token": "IG", "ig_user_id": "1784", "host": "instagram", "username": "me_ig", "followers": 5}}), "utf-8")


def test_single_connections_become_the_first_accounts_once_without_losing_tokens(fresh):
    _legacy_files(fresh)
    first = accounts.all_accounts()
    assert [a["platform"] for a in first] == ["youtube", "tiktok", "instagram"]
    yt, tt, ig = first
    assert yt["token"]["refresh_token"] == "YR"
    assert (tt["access_token"], tt["refresh_token"], tt["ext_id"], tt["name"]) == ("TT", "TR", "o1", "Me on TikTok")
    assert (ig["access_token"], ig["ig_user_id"], ig["host"], ig["name"]) == ("IG", "1784", "instagram", "me_ig")
    assert all(a["approved"] and a["autopilot"] and a["legacy"] for a in first)  # they were already in use: keep them working
    again = accounts.all_accounts()  # idempotent: no duplicates, same ids
    assert [a["id"] for a in again] == [a["id"] for a in first]
    assert len(json.loads(accounts.FILE.read_text("utf-8"))["accounts"]) == 3
    assert json.loads((fresh / "connections.json").read_text("utf-8"))["tiktok"]["access_token"] == "TT"  # old file kept


def test_failed_migration_keeps_reading_the_old_files(fresh, monkeypatch):
    _legacy_files(fresh)

    def broken(data):
        raise OSError("disk full")
    monkeypatch.setattr(accounts, "_write", broken)
    accs = accounts.all_accounts("tiktok")
    assert accs and accs[0]["access_token"] == "TT" and not accounts.FILE.exists()


def test_new_logins_are_unapproved_and_never_overwrite_another_account():
    a = accounts.link("tiktok", accounts.new_id("tiktok"), "o1", access_token="A1")
    assert not a["approved"] and not a["autopilot"]
    accounts.update(a["id"], {"approved": True})
    same = accounts.link("tiktok", accounts.new_id("tiktok"), "o1", access_token="A2")  # the same person again
    assert same["id"] == a["id"] and same["access_token"] == "A2" and same["approved"]
    other = accounts.link("tiktok", a["id"], "o2", access_token="B1")  # "Reconnect" but someone else signed in
    assert other["id"] != a["id"] and not other["approved"]
    assert accounts.get(a["id"])["access_token"] == "A2" and len(accounts.all_accounts("tiktok")) == 2


def test_the_browser_never_sees_tokens():
    a = accounts.link("youtube", accounts.new_id("youtube"), "UC1", token={"token": "secret", "refresh_token": "r"}, name="Ch")
    b = accounts.link("tiktok", accounts.new_id("tiktok"), "o1", access_token="x", refresh_token="y", expires_at=1)
    blob = json.dumps([accounts.public(a), accounts.public(b), platforms.status()["tiktok"]])
    assert "secret" not in blob and '"x"' not in blob and "refresh_token" not in blob and "Ch" in blob


# ---------------- approval enforced by the backend ----------------

def test_upload_refuses_unapproved_removed_and_autopilot_disallowed_accounts(publishing):
    c = _with_export(clip())
    a = acct("youtube", approved=False)
    u = post(c, account_id=a["id"])
    pipeline._upload({"upload_id": u["id"]})
    u = store.get("upload", u["id"])
    assert u["status"] == "failed" and "approved" in u["error"].lower() and not publishing
    accounts.update(a["id"], {"approved": True})
    auto = post(c, account_id=a["id"], auto=True)
    pipeline._upload({"upload_id": auto["id"]})
    assert store.get("upload", auto["id"])["status"] == "failed" and "autopilot" in store.get("upload", auto["id"])["error"].lower()
    gone = post(c, account_id="yt_doesnotexist")
    pipeline._upload({"upload_id": gone["id"]})
    assert store.get("upload", gone["id"])["status"] == "failed" and "removed" in store.get("upload", gone["id"])["error"]
    unchosen = post(c, account_id=None)  # moved to a platform without picking an account: never guessed
    pipeline._upload({"upload_id": unchosen["id"]})
    assert store.get("upload", unchosen["id"])["status"] == "failed" and not publishing
    ok = post(c, account_id=a["id"])
    pipeline._upload({"upload_id": ok["id"]})
    assert store.get("upload", ok["id"])["status"] == "done" and publishing == [("youtube", a["id"])]


def test_manual_publish_refuses_an_unapproved_account():
    c = clip()
    a = acct("tiktok", approved=False)
    with pytest.raises(RuntimeError, match="approved"):
        pipeline.request_upload(c["id"], {"platform": "tiktok", "account_id": a["id"]})


def test_one_clip_goes_to_two_youtube_channels_each_with_its_own_upload(publishing):
    c = _with_export(clip())
    a, b = acct("youtube", name="Main"), acct("youtube", name="Second")
    ups = server.upload_clip(c["id"], {"accounts": [a["id"], b["id"]], "title": "t", "privacy": "private"})
    assert sorted(u["account_id"] for u in ups) == sorted([a["id"], b["id"]]) and len({u["id"] for u in ups}) == 2
    for u in ups:
        pipeline._upload({"upload_id": u["id"]})
    assert sorted(publishing) == sorted([("youtube", a["id"]), ("youtube", b["id"])])


def test_old_posts_without_an_account_go_to_the_migrated_account(publishing, fresh):
    _legacy_files(fresh)
    legacy = accounts.all_accounts("youtube")[0]
    acct("youtube", name="New, approved")  # a second channel must not receive the old post
    u = post(_with_export(clip()))  # created before accounts existed: no account_id key at all
    pipeline._upload({"upload_id": u["id"]})
    assert publishing == [("youtube", legacy["id"])]


# ---------------- OAuth lands in the right account ----------------

def test_tiktok_sign_in_state_routes_the_login_to_the_right_account(monkeypatch):
    platforms.tiktok_set_app("k", "s")
    old = acct("tiktok", ext_id="o1", name="First")
    open_id = {"v": "o2"}

    def post_(url, **kw):
        assert "oauth/token" in url
        return httpx.Response(200, json={"access_token": f"NEW-{open_id['v']}", "open_id": open_id["v"], "expires_in": 86400,
                                         "refresh_token": "R"})

    def get_(url, **kw):
        return httpx.Response(200, json={"data": {"user": {"display_name": f"Name {open_id['v']}", "avatar_url": "https://a/b.jpg"}}})
    monkeypatch.setattr(platforms.httpx, "post", post_)
    monkeypatch.setattr(platforms.httpx, "get", get_)

    url = platforms.tiktok_auth_url(8765)  # "+ Add another account"
    q = httpx.URL(url).params
    assert q["disable_auto_auth"] == "1"  # TikTok always shows its authorization page
    new = platforms.tiktok_callback("code", q["state"], 8765)
    assert new["id"] != old["id"] and new["name"] == "Name o2" and not new["approved"]
    assert accounts.get(old["id"])["access_token"] == "T"  # the first account's sign-in is untouched

    open_id["v"] = "o1"  # Reconnect the first account
    url = platforms.tiktok_auth_url(8765, account_id=old["id"])
    again = platforms.tiktok_callback("code", httpx.URL(url).params["state"], 8765)
    assert again["id"] == old["id"] and accounts.get(old["id"])["access_token"] == "NEW-o1" and accounts.get(old["id"])["approved"]
    assert len(accounts.all_accounts("tiktok")) == 2
    with pytest.raises(RuntimeError, match="start the TikTok connection again"):
        platforms.tiktok_callback("code", "forged-state", 8765)


def test_youtube_sign_in_goes_to_a_new_account_and_offers_the_account_picker(monkeypatch, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text("{}", "utf-8")
    monkeypatch.setattr(youtube, "YT_SECRET", secret)
    first = accounts.link("youtube", accounts.new_id("youtube"), "UC1", token={"token": "one"}, name="First")
    seen = {}

    class Creds:
        def to_json(self):
            return json.dumps({"token": "two", "refresh_token": "r2", "scopes": youtube.SCOPES})

    class Flow:
        def run_local_server(self, **kw):
            seen.update(kw)
            return Creds()

    import google_auth_oauthlib.flow as gflow
    monkeypatch.setattr(gflow.InstalledAppFlow, "from_client_secrets_file", classmethod(lambda cls, *a, **k: Flow()))
    monkeypatch.setattr(youtube, "_channel_info", lambda creds: {"ext_id": "UC2", "name": "Second channel", "avatar": None})

    class Now:  # run the background sign-in inline
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(youtube.threading, "Thread", Now)
    youtube.connect()
    assert seen["prompt"] == "select_account consent"
    yts = accounts.all_accounts("youtube")
    new = next(a for a in yts if a["id"] != first["id"])
    assert seen["state"].startswith(new["id"] + ".") and new["name"] == "Second channel" and not new["approved"]
    assert accounts.get(first["id"])["token"] == {"token": "one"}


# ---------------- removal, rescheduling, Autopilot, results ----------------

def test_removing_an_account_holds_its_queued_posts_and_never_moves_them(publishing):
    c = _with_export(clip())
    a, b = acct("youtube"), acct("youtube")
    q, s, done, other = (post(c, account_id=a["id"]), post(c, account_id=a["id"], status="scheduled"),
                         post(c, account_id=a["id"], status="done"), post(c, account_id=b["id"]))
    assert pipeline.remove_account(a["id"]) == 2
    for x in (q, s):
        held = store.get("upload", x["id"])
        assert held["status"] == "failed" and "removed" in held["error"] and held["account_id"] == a["id"]
    assert store.get("upload", done["id"])["status"] == "done" and store.get("upload", other["id"])["status"] == "queued"
    assert accounts.get(a["id"]) is None
    pipeline.retry_upload(q["id"])  # retrying cannot post it anywhere else
    pipeline._upload({"upload_id": q["id"]})
    assert publishing == [] and store.get("upload", q["id"])["status"] == "failed"


def test_removing_the_migrated_account_also_deletes_its_old_token_file(fresh):
    _legacy_files(fresh)
    yt = accounts.all_accounts("youtube")[0]
    pipeline.remove_account(yt["id"])
    assert not (fresh / "youtube_token.json").exists()
    tt = accounts.all_accounts("tiktok")[0]
    pipeline.remove_account(tt["id"])
    conn = json.loads((fresh / "connections.json").read_text("utf-8"))
    assert "access_token" not in conn["tiktok"] and conn["tiktok"]["client_key"] == "k"  # the app keys stay


def test_reschedule_moves_a_post_only_to_another_approved_account():
    c = clip()
    a, b, unapproved, tt = acct("youtube"), acct("youtube"), acct("youtube", approved=False), acct("tiktok")
    u = post(c, account_id=a["id"], status="scheduled", publish_at="2031-01-01T10:00:00+00:00")
    out = pipeline.reschedule(u["id"], account_id=b["id"])
    assert out["account_id"] == b["id"] and out["publish_at"] == "2031-01-01T10:00:00+00:00"
    with pytest.raises(RuntimeError, match="approved"):
        pipeline.reschedule(u["id"], account_id=unapproved["id"])
    with pytest.raises(RuntimeError):
        pipeline.reschedule(u["id"], platform="youtube", account_id=tt["id"])
    moved = pipeline.reschedule(u["id"], account_id=tt["id"])  # an account implies its platform
    assert moved["platform"] == "tiktok" and moved["account_id"] == tt["id"] and moved["upload_at"] == "2031-01-01T10:00:00+00:00"


def test_autopilot_only_posts_to_accounts_approved_and_allowed_for_autopilot(monkeypatch):
    monkeypatch.setattr(pipeline, "paused", lambda: False)
    ok, manual_only, unapproved = acct("youtube", autopilot=True), acct("youtube"), acct("youtube", approved=False, autopilot=True)
    for ap, expect in (({"platforms": ["youtube"]}, [ok["id"]]),                         # old saved forms: by platform
                       ({"accounts": [ok["id"], manual_only["id"], unapproved["id"]]}, [ok["id"]])):
        c = clip()
        store.update("project", c["project_id"], {"autopilot": {"enabled": True, "top_n": 1, **ap}})
        pipeline.save_json(c["project_id"], "transcript.json", {"segments": []})
        pipeline._autopilot(c["project_id"])
        ups = store.find("upload", clip_id=c["id"])
        assert [u["account_id"] for u in ups] == expect and all(u["auto"] for u in ups)


def test_results_are_fetched_with_each_channels_own_credentials(fresh, monkeypatch):
    _legacy_files(fresh)
    legacy = accounts.all_accounts("youtube")[0]
    a, b = acct("youtube"), acct("youtube")
    for d in store.find("upload"):
        store.delete("upload", d["id"])
    c = clip()
    post(c, account_id=a["id"], status="done", video_id="v1")
    post(c, account_id=b["id"], status="done", video_id="v2")
    post(c, status="done", video_id="v3")  # from before accounts: the migrated channel's
    asked = {}

    class Svc:
        def __init__(self, aid):
            self.aid = aid

        def videos(self):
            return self

        def list(self, part, id):
            asked[self.aid] = id
            self.ids = id.split(",")
            return self

        def execute(self):
            return {"items": [{"id": i, "statistics": {"viewCount": "10", "likeCount": "1"}} for i in self.ids]}
    monkeypatch.setattr(youtube, "_service", lambda account_id=None: Svc(account_id))
    monkeypatch.setattr(platforms, "account_connected", lambda acc: True)
    perf = pipeline._performance()
    assert asked == {a["id"]: "v1", b["id"]: "v2", legacy["id"]: "v3"} and len(perf) == 3


def test_schedule_shows_the_account_name_on_each_post():
    a = acct("youtube", name="Gaming channel")
    post(clip(), account_id=a["id"], status="scheduled", publish_at="2031-01-01T10:00:00+00:00")
    items = server.schedule_view()["items"]
    assert any(it["account"] == "Gaming channel" and it["account_id"] == a["id"] for it in items)


def test_upload_clip_validates_every_account_before_queueing_and_dedupes(publishing):
    c = _with_export(clip())
    a, b = acct("youtube", name="Ok"), acct("youtube", name="Not approved", approved=False)
    with pytest.raises(RuntimeError):
        server.upload_clip(c["id"], {"accounts": [a["id"], b["id"]], "title": "t", "privacy": "private"})
    assert not store.find("upload", clip_id=c["id"])  # nothing queued, so a retry can't post A twice
    ups = server.upload_clip(c["id"], {"accounts": [a["id"], a["id"]], "title": "t", "privacy": "private"})
    assert len(ups) == 1 and len(store.find("upload", clip_id=c["id"])) == 1


@pytest.mark.parametrize("bad", ["{not json", ""])
def test_a_corrupt_accounts_file_is_backed_up_and_never_overwritten_or_remigrated(fresh, bad):
    _legacy_files(fresh)
    accounts.FILE.write_text(bad, "utf-8")
    with pytest.raises(RuntimeError, match="accounts.json"):
        accounts.all_accounts()
    with pytest.raises(RuntimeError):
        accounts.add("youtube", name="New", access_token="T")
    assert accounts.FILE.read_text("utf-8") == bad  # not replaced by a fresh migration (which would re-approve legacy ones)
    backups = list(fresh.glob("accounts.json.bad-*"))
    assert len(backups) == 1 and backups[0].read_text("utf-8") == bad
    with pytest.raises(RuntimeError):
        accounts.all_accounts()
    assert len(list(fresh.glob("accounts.json.bad-*"))) == 1  # one backup, not one per read
