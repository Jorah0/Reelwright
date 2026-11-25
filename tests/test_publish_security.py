"""WP3 publishing & security: DPAPI-sealed secrets (migration, backups, another-PC lock-out, rollback), crash-safe uploads
(resume or poll, never a second post), AI-content disclosure and description templates. No network: every platform call
is answered by a fake. On Windows the vault round-trips through real DPAPI; the plain-text fallback is tested too."""
import json
import os
import sys
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import httpx  # noqa: E402
import pytest  # noqa: E402

from reelwright import accounts, config, pipeline, platforms, remote, store, vault, youtube  # noqa: E402

WIN = pytest.mark.skipif(not vault.AVAILABLE, reason="DPAPI is Windows-only")
PLAIN = {"anthropic_api_key": "sk-ant-PLAINKEY", "lan_token": "rwLANPLAIN", "gsi_token": "GSIPLAIN", "vts_token": "VTSPLAIN",
         "ai_providers": {"openai": {"api_key": "sk-OPENAIPLAIN", "model": "m"}},
         "remote_devices": [{"id": "d1", "secret": "PHONEKEYPLAIN", "name": "Phone", "created": 1.0, "paired_at": 2.0}]}
SECRETS = ("PLAINKEY", "LANPLAIN", "GSIPLAIN", "VTSPLAIN", "OPENAIPLAIN", "PHONEKEYPLAIN", "YTREFRESHPLAIN", "TTACCESSPLAIN",
           "IGTOKPLAIN", "TTAPPSECRETPLAIN", "TWITCHSECRETPLAIN", "LEGACYTT")


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    """Own settings, accounts and connection files, and its own vault state, per test."""
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(accounts, "FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "LEGACY_CONN", tmp_path / "connections.json")
    monkeypatch.setattr(accounts, "LEGACY_YT", tmp_path / "youtube_token.json")
    monkeypatch.setattr(platforms, "CONN_FILE", tmp_path / "connections.json")
    monkeypatch.setattr(vault, "STATE", tmp_path / "vault.json")
    monkeypatch.setattr(vault, "OFF", tmp_path / "vault-off")
    monkeypatch.setattr(platforms, "BACKOFF", 0)
    monkeypatch.setattr(youtube, "BACKOFF", 0)
    monkeypatch.setattr(platforms.time, "sleep", lambda s: None)
    return tmp_path


def _old_version_files(tmp):
    """What a Reelwright from before the vault left on disk: every secret in plain text."""
    (tmp / "settings.json").write_text(json.dumps({**PLAIN, "clip_count": 5}), "utf-8")
    (tmp / "accounts.json").write_text(json.dumps({"accounts": [
        {"id": "youtube_legacy", "platform": "youtube", "legacy": True, "approved": True, "name": "Main",
         "token": {"token": "a", "refresh_token": "YTREFRESHPLAIN", "client_secret": "cs"}},
        {"id": "tiktok_legacy", "platform": "tiktok", "legacy": True, "approved": True, "access_token": "TTACCESSPLAIN",
         "refresh_token": "TTR", "expires_at": 9e12, "open_id": "o1"},
        {"id": "ig_1", "platform": "instagram", "approved": True, "access_token": "IGTOKPLAIN", "ig_user_id": "17"}]}), "utf-8")
    (tmp / "connections.json").write_text(json.dumps({
        "tiktok": {"client_key": "ck", "client_secret": "TTAPPSECRETPLAIN", "access_token": "LEGACYTT", "refresh_token": "LEGACYTT2"},
        "twitch": {"client_id": "tw", "client_secret": "TWITCHSECRETPLAIN"}}), "utf-8")
    (tmp / "youtube_token.json").write_text(json.dumps({"token": "a", "refresh_token": "LEGACYTT"}), "utf-8")


def _disk(tmp) -> str:
    return "".join(p.read_text("utf-8") for p in tmp.glob("*.json"))


# ---------------- the vault itself ----------------

@WIN
def test_dpapi_round_trip_seals_any_json_value_once():
    for v in ("sk-ant-x", {"token": "t", "n": [1, 2]}, [{"id": "d", "secret": "s"}]):
        sealed = vault.seal(v)
        assert isinstance(sealed, str) and sealed.startswith("dpapi:") and "sk-ant" not in sealed
        assert vault.unseal(sealed) == v and vault.seal(sealed) == sealed  # sealing twice changes nothing
    assert vault.seal("") == "" and vault.seal(None) is None and vault.seal([]) == []  # nothing to hide: "not set"
    with pytest.raises(vault.Locked):
        vault.unseal("dpapi:AAAAbm90IGEgcmVhbCBibG9i")  # another PC's or user's blob (or a damaged one)
    assert vault.status()["encrypted"]


def test_without_dpapi_secrets_stay_plain_and_status_says_so(monkeypatch):
    monkeypatch.setattr(vault, "AVAILABLE", False)
    assert vault.seal("sk-ant-x") == "sk-ant-x" and vault.unseal("sk-ant-x") == "sk-ant-x"
    st = vault.status()
    assert not st["encrypted"] and "plain text" in st["scheme"]
    config.save({"anthropic_api_key": "sk-ant-FALLBACK"})
    assert config.load()["anthropic_api_key"] == "sk-ant-FALLBACK"
    with pytest.raises(vault.Locked):
        vault.unseal("dpapi:AAAA")  # a sealed value can't be opened without DPAPI: it reads as "reconnect"


# ---------------- migration ----------------

@WIN
def test_migration_seals_every_secret_keeps_them_working_and_backs_up_first(files):
    _old_version_files(files)
    before = {p.name: p.read_text("utf-8") for p in files.glob("*.json")}
    vault.migrate()
    disk = _disk(files)
    for s in SECRETS:
        assert s not in disk, s
    for name, text in before.items():  # the plain-text originals, untouched, beside each file
        assert (files / f"{name}.pre-vault.bak").read_text("utf-8") == text
    assert not (files / "youtube_token.json").exists()  # pre-accounts copy: held by the migrated account now
    s = config.load()
    assert {k: s[k] for k in PLAIN if k != "ai_providers"} == {k: v for k, v in PLAIN.items() if k != "ai_providers"}
    assert config.provider("openai")["api_key"] == "sk-OPENAIPLAIN" and s["clip_count"] == 5
    yt, tt, ig = accounts.all_accounts()
    assert yt["token"]["refresh_token"] == "YTREFRESHPLAIN" and tt["access_token"] == "TTACCESSPLAIN" and ig["access_token"] == "IGTOKPLAIN"
    assert platforms._load()["tiktok"]["client_secret"] == "TTAPPSECRETPLAIN" and platforms._load()["twitch"]["client_secret"] == "TWITCHSECRETPLAIN"
    conn = json.loads((files / "connections.json").read_text("utf-8"))
    assert "access_token" not in conn["tiktok"] and conn["tiktok"]["client_key"] == "ck"  # stale legacy sign-in dropped
    sealed_once = {p.name: p.read_text("utf-8") for p in files.glob("*.json") if p.name != "vault.json"}
    vault.migrate()  # idempotent: nothing left to seal, so nothing is rewritten; this second clean start drops the backups
    assert {p.name: p.read_text("utf-8") for p in files.glob("*.json") if p.name != "vault.json"} == sealed_once
    assert not list(files.glob("*.pre-vault.bak"))


@WIN
def test_backups_go_after_clean_starts_and_rollback_writes_plain_text(files):
    _old_version_files(files)
    for _ in range(vault.KEEP_STARTS):
        vault.migrate()
        assert list(files.glob("*.pre-vault.bak")) or _ == vault.KEEP_STARTS - 1
    assert not list(files.glob("*.pre-vault.bak"))  # N clean starts in a row: the plain-text copies are deleted
    vault.rollback()  # back to plain files (an older Reelwright, or moving PCs); sealing stays off
    disk = _disk(files)
    assert "TTACCESSPLAIN" in disk and "PLAINKEY" in disk and "dpapi:" not in disk
    assert not vault.status()["encrypted"] and accounts.all_accounts("tiktok")[0]["access_token"] == "TTACCESSPLAIN"
    vault.migrate()
    assert "PLAINKEY" in _disk(files)  # off means off, also at the next start
    vault.enable()
    assert "PLAINKEY" not in _disk(files) and config.api_key() == "sk-ant-PLAINKEY"


@WIN
def test_secrets_sealed_on_another_pc_read_as_reconnect_and_are_never_blanked(files, monkeypatch):
    foreign = "dpapi:AAAAbm90IGEgcmVhbCBibG9i"  # what another Windows user's blob looks like here: it won't open
    (files / "settings.json").write_text(json.dumps({"anthropic_api_key": foreign, "remote_devices": foreign, "lan_token": foreign,
                                                     "ai_providers": {"openai": {"api_key": foreign, "model": "m"}}}), "utf-8")
    (files / "accounts.json").write_text(json.dumps({"accounts": [
        {"id": "tt1", "platform": "tiktok", "approved": True, "name": "Me", "access_token": foreign, "refresh_token": foreign}]}), "utf-8")
    (files / "connections.json").write_text(json.dumps({"tiktok": {"client_key": "ck", "client_secret": foreign}}), "utf-8")
    s = config.load()
    assert s["anthropic_api_key"] == "" and s["remote_devices"] == [] and config.provider("openai")["api_key"] == ""
    pub = config.public()
    assert set(pub["vault_locked"]) >= {"anthropic_api_key", "remote_devices", "lan_token", "ai_providers.openai.api_key"}
    acc = accounts.get("tt1")
    assert acc["locked"] and acc["access_token"] is None and not platforms.account_connected(acc)
    assert platforms.status()["tiktok"]["accounts"][0]["locked"]
    assert any("TikTok" in x["what"] for x in vault.status()["locked"])
    vault.migrate()  # never crashes, never "fixes" by blanking
    config.save({"clip_count": 3, "remote_devices": [], "anthropic_api_key": ""})  # empties never replace a locked value
    remote.revoke("nope")
    accounts.update("tt1", {"approved": True})
    raw = json.loads((files / "settings.json").read_text("utf-8"))
    assert raw["anthropic_api_key"] == foreign and raw["remote_devices"] == foreign and raw["ai_providers"]["openai"]["api_key"] == foreign
    assert json.loads(accounts.FILE.read_text("utf-8"))["accounts"][0]["access_token"] == foreign
    assert json.loads((files / "connections.json").read_text("utf-8"))["tiktok"]["client_secret"] == foreign
    # a posting attempt asks to reconnect instead of crashing
    c = _clip()
    u = store.put("upload", {"clip_id": c["id"], "project_id": c["project_id"], "platform": "tiktok", "account_id": "tt1",
                             "status": "queued", "title": "t", "privacy": "private", "publish_at": None, "upload_at": None})
    monkeypatch.setattr(pipeline, "clip_export_current", lambda c: True)
    pipeline._upload({"upload_id": u["id"]})
    assert store.get("upload", u["id"])["status"] == "needs_auth"
    config.save({"anthropic_api_key": "sk-ant-NEW"})  # entering it again replaces it
    assert config.api_key() == "sk-ant-NEW" and "anthropic_api_key" not in config.public()["vault_locked"]
    vault.forget_locked()  # "Forget them": the unreadable leftovers go
    assert config.public()["vault_locked"] == []


@WIN
def test_a_passing_dpapi_hiccup_is_not_remembered_as_locked(files, monkeypatch):
    config.save({"anthropic_api_key": "sk-ant-HICCUP"})
    vault._plain.cache_clear()
    real = vault._crypt
    monkeypatch.setattr(vault, "_crypt", lambda data, protect: (_ for _ in ()).throw(OSError(5, "not ready")))
    assert config.load()["anthropic_api_key"] == ""  # this read fails...
    monkeypatch.setattr(vault, "_crypt", real)
    assert config.api_key() == "sk-ant-HICCUP"  # ...but the next one tries again
    with pytest.raises(PermissionError):
        vault.forget_locked(confirm="")  # forgetting needs an explicit confirm


@WIN
def test_backups_stay_while_a_file_could_not_be_sealed(files, monkeypatch):
    _old_version_files(files)
    monkeypatch.setattr(platforms, "reseal", lambda: (_ for _ in ()).throw(OSError("disk")))
    for _ in range(vault.KEEP_STARTS + 1):
        vault.migrate()
    assert (files / "settings.json.pre-vault.bak").exists() and vault._state()["clean_starts"] == 0
    assert "TTAPPSECRETPLAIN" in (files / "connections.json").read_text("utf-8")  # still plain: so nothing counted as clean


def test_secrets_never_reach_public_settings_or_the_accounts_api(files):
    config.save({**PLAIN})
    accounts.add("youtube", name="Ch", token={"token": "a", "refresh_token": "YTREFRESHPLAIN"})
    accounts.add("tiktok", name="T", access_token="TTACCESSPLAIN", refresh_token="x")
    platforms.tiktok_set_app("ck", "TTAPPSECRETPLAIN")
    from reelwright import server
    blob = json.dumps([config.public(), server.connections(), vault.status(), [accounts.public(a) for a in accounts.all_accounts()]])
    for s in SECRETS[:8] + ("TTAPPSECRETPLAIN",):
        assert s not in blob, s
    assert "dpapi:" not in blob


@WIN
def test_the_phone_remote_still_pairs_and_verifies_after_migration(files):
    _old_version_files(files)
    vault.migrate()
    path = "/remote/api/dashboard"
    dev, _ = remote.verify(remote.sign("d1", "PHONEKEYPLAIN", "GET", path), "GET", path, b"")
    assert dev["id"] == "d1"
    new = remote.add_device()  # a new phone pairs as before; its key is sealed on disk
    remote._pair(new, {"name": "Second phone", "pn": "ab" * 16})
    paired = remote.verify_device(new["id"])
    assert paired["paired_at"] and paired["secret"] != new["secret"]
    assert paired["secret"] not in (files / "settings.json").read_text("utf-8")


# ---------------- crash-safe uploads ----------------

def _clip(tmp=None):
    p = store.put("project", {"name": "p", "meta": {"duration": 100.0, "width": 1920, "height": 1080},
                              "source_url": "https://www.twitch.tv/videos/123456", "source_info": {"channel": "Streamer"}})
    folder = pipeline.pdir(p["id"])
    (folder / "exports").mkdir(parents=True, exist_ok=True)
    c = pipeline.new_clip(p["id"], {"start": 3723.0, "end": 3750.0, "score": 80, "title": "Big play"}, source="ai")
    (folder / "exports" / "x.mp4").write_bytes(b"\0" * 1000)
    return store.update("clip", c["id"], {"export": {"file": "exports/x.mp4", "status": "done", "rev": c["rev"]}, "hook": "what a play"})


@pytest.fixture
def post(monkeypatch):
    """An approved account of the platform and a queued post of a rendered clip to it; returns a maker."""
    monkeypatch.setattr(pipeline, "remember", lambda *a, **k: None)
    monkeypatch.setattr(platforms, "_duration", lambda path: 27.0)

    def make(platform, **fields):
        acc = accounts.add(platform, name=f"{platform} acct", access_token="T", refresh_token="R", expires_at=9e12, ig_user_id="17",
                           token={"token": "t"}, approved=True)
        c = _clip()
        u = store.put("upload", {"clip_id": c["id"], "project_id": c["project_id"], "platform": platform, "account_id": acc["id"],
                                 "status": "queued", "title": "Big play", "description": "d", "caption": "c", "tags": [],
                                 "category_id": "22", "privacy": "private", "publish_at": None, "upload_at": None,
                                 "made_for_kids": False, **fields})
        return acc, u
    return make


def _crash_and_restart(uid):
    """The app died mid-upload: the post is left "uploading"; the next start recovers it."""
    store.update("upload", uid, {"status": "uploading"})
    pipeline._recover_uploads()
    assert store.get("upload", uid)["status"] == "queued"
    pipeline._upload({"upload_id": uid})
    return store.get("upload", uid)


class FakeYT:
    """videos().insert(...).next_chunk() shaped like googleapiclient's resumable upload: 3 chunks; crash_after stops the
    process after that many; a request given a saved session URI in error state first asks how far it got."""

    def __init__(self, crash_after=None, session_alive=True, uploads_playlist=()):
        self.sessions, self.bodies, self.probes, self.crash_after = 0, [], [], crash_after
        self.alive, self.playlist, self.chunks = session_alive, list(uploads_playlist), 0

    def videos(self):
        return self

    def channels(self):
        return self

    def playlistItems(self):
        return self

    def list(self, **kw):
        self.listed = kw
        return self

    def execute(self):
        if "mine" in self.listed:
            return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
        return {"items": self.playlist}

    def insert(self, part, body, media_body):
        self.bodies.append(body)
        fake = self

        class Req:
            resumable_uri, _in_error_state, resumable_progress = None, False, 0

            def next_chunk(self):
                from googleapiclient.errors import HttpError
                if self.resumable_uri is None:
                    fake.sessions += 1
                    self.resumable_uri = f"https://upload/session{fake.sessions}"
                elif self._in_error_state:
                    fake.probes.append(self.resumable_uri)
                    if not fake.alive:
                        raise HttpError(type("R", (), {"status": 404, "reason": "gone"})(), b"{}")
                    self._in_error_state = False
                if fake.crash_after is not None and fake.chunks >= fake.crash_after:
                    raise SystemExit("power cut")
                fake.chunks += 1
                self.resumable_progress += 400
                if self.resumable_progress >= 1000:
                    return None, {"id": "VID1"}
                return type("S", (), {"progress": lambda s: 0.4})(), None
        return Req()


def test_youtube_resumes_its_saved_session_after_a_crash_without_a_second_video(post, monkeypatch):
    acc, u = post("youtube")
    fake = FakeYT(crash_after=1)
    monkeypatch.setattr(youtube, "_service", lambda account_id=None: fake)
    monkeypatch.setattr(platforms, "account_connected", lambda a: True)
    with pytest.raises(SystemExit):
        pipeline._upload({"upload_id": u["id"]})
    saved = store.get("upload", u["id"])["resume"]
    assert saved["uri"] == "https://upload/session1" and saved["offset"] == 400 and saved["account_id"] == acc["id"]
    fake.crash_after = None
    done = _crash_and_restart(u["id"])
    assert done["status"] == "done" and done["video_id"] == "VID1" and done.get("resume") is None
    assert fake.sessions == 1 and fake.probes == ["https://upload/session1"]  # continued the same upload


def test_youtube_with_its_session_gone_finds_the_finished_video_instead_of_posting_again(post, monkeypatch):
    acc, u = post("youtube", resume={"platform": "youtube", "account_id": None, "started": 0.0})
    store.update("upload", u["id"], fn=lambda d: d["resume"].update(account_id=d["account_id"], uri="https://upload/old", size=1000))
    fake = FakeYT(session_alive=False, uploads_playlist=[{"snippet": {"title": "Big play", "publishedAt": "2030-01-01T00:00:00Z",
                                                                      "resourceId": {"videoId": "VIDOLD"}}}])
    monkeypatch.setattr(youtube, "_service", lambda account_id=None: fake)
    monkeypatch.setattr(platforms, "account_connected", lambda a: True)
    done = _crash_and_restart(u["id"])
    assert done["status"] == "done" and done["video_id"] == "VIDOLD" and fake.sessions == 0


class Net:
    """Records every request and answers from a route table: (method, url-substring) -> list of (status, json) or an
    exception to raise; the last answer repeats."""

    def __init__(self, monkeypatch, routes):
        self.calls, self.routes = [], routes
        for m in ("get", "post", "put"):
            monkeypatch.setattr(platforms.httpx, m, self._handler(m.upper()))

    def _handler(self, method):
        def call(url, **kw):
            self.calls.append((method, url, kw))
            for (m, frag), answers in self.routes.items():
                if m == method and frag in url:
                    a = answers.pop(0) if len(answers) > 1 else answers[0]
                    if isinstance(a, BaseException):
                        raise a
                    return httpx.Response(a[0], json=a[1])
            raise AssertionError(f"unexpected {method} {url}")
        return call

    def find(self, method, frag):
        return [c for c in self.calls if c[0] == method and frag in c[1]]


OK = {"error": {"code": "ok", "message": ""}}
CREATOR = {"creator_username": "me", "privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 600}


def test_tiktok_restart_polls_the_saved_publish_and_never_posts_again(post, monkeypatch):
    platforms._save_platform("tiktok", {"client_key": "k", "client_secret": "s"})
    acc, u = post("tiktok", tiktok={"privacy_level": "SELF_ONLY"})
    net = Net(monkeypatch, {("POST", "creator_info"): [(200, {"data": CREATOR, **OK})],
                            ("POST", "video/init"): [(200, {"data": {"publish_id": "P1", "upload_url": "https://open-upload.tiktokapis.com/u"}, **OK})],
                            ("PUT", "open-upload"): [SystemExit("power cut")],
                            ("POST", "status/fetch"): [(200, {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [77]}, **OK})]})
    with pytest.raises(SystemExit):
        pipeline._upload({"upload_id": u["id"]})
    assert store.get("upload", u["id"])["resume"]["publish_id"] == "P1"
    done = _crash_and_restart(u["id"])
    assert done["status"] == "done" and done["url"] == "https://www.tiktok.com/@me/video/77"
    assert len(net.find("POST", "video/init")) == 1  # polled the saved publish; no second post


def test_tiktok_publish_that_never_got_its_file_is_started_over_once(post, monkeypatch):
    platforms._save_platform("tiktok", {"client_key": "k", "client_secret": "s"})
    acc, u = post("tiktok", tiktok={"privacy_level": "SELF_ONLY"})
    store.update("upload", u["id"], {"resume": {"platform": "tiktok", "account_id": acc["id"], "started": 0.0, "publish_id": "P0"}})
    net = Net(monkeypatch, {("POST", "creator_info"): [(200, {"data": CREATOR, **OK})],
                            ("POST", "video/init"): [httpx.ConnectError("blip"), (200, {"data": {"publish_id": "P2", "upload_url": "https://open-upload.tiktokapis.com/u"}, **OK})],
                            ("PUT", "open-upload"): [(201, {})],
                            ("POST", "status/fetch"): [(200, {"data": {"status": "FAILED", "fail_reason": "file_format_check_failed"}, **OK}),
                                                       (200, {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [78]}, **OK})]})
    done = _crash_and_restart(u["id"])
    assert done["status"] == "done" and "78" in done["url"]
    assert len(net.find("POST", "video/init")) == 2  # one network blip retried with backoff; the failed P0 never posted


def test_instagram_restart_checks_the_container_and_publishes_exactly_once(post, monkeypatch):
    acc, u = post("instagram")
    net = Net(monkeypatch, {("POST", "/17/media_publish"): [httpx.ReadTimeout("lost answer")],
                            ("POST", "/17/media"): [(200, {"id": "C1"})],
                            ("POST", "rupload.facebook.com"): [(200, {"success": True})],
                            ("GET", "/C1"): [(200, {"status_code": "FINISHED"}), (200, {"status_code": "FINISHED"}),
                                             (200, {"status_code": "FINISHED"}), (200, {"status_code": "PUBLISHED"})]})
    monkeypatch.setattr(platforms, "PUBLISH_TRIES", 2)
    with pytest.raises(httpx.TransportError):  # media_publish's answer is lost twice (the job fails; it may have posted)
        pipeline._upload({"upload_id": u["id"]})
    assert store.get("upload", u["id"])["resume"]["container"] == "C1"
    done = _crash_and_restart(u["id"])  # the container says PUBLISHED: done, no new container, no second publish
    assert done["status"] == "done" and len([c for c in net.calls if c[1].endswith("/17/media")]) == 1
    assert len(net.find("POST", "media_publish")) == 2


def test_instagram_uploaded_container_is_published_after_a_restart(post, monkeypatch):
    acc, u = post("instagram")
    store.update("upload", u["id"], {"resume": {"platform": "instagram", "account_id": acc["id"], "started": 0.0, "container": "C1",
                                                "uploaded": True}})
    net = Net(monkeypatch, {("POST", "/17/media_publish"): [(200, {"id": "999"})],
                            ("GET", "/C1"): [(200, {"status_code": "FINISHED"})],
                            ("GET", "/999"): [(200, {"permalink": "https://www.instagram.com/reel/x/"})]})
    done = _crash_and_restart(u["id"])
    assert done["status"] == "done" and done["video_id"] == "999" and not net.find("POST", "/17/media?")
    assert len(net.find("POST", "media_publish")) == 1


# ---------------- AI-content disclosure ----------------

def test_ai_disclosure_reaches_youtube_and_tiktok_and_follows_the_clip(post, monkeypatch):
    fake = FakeYT()
    monkeypatch.setattr(youtube, "_service", lambda account_id=None: fake)
    youtube.upload(pipeline.pdir(_clip()["project_id"]) / "exports" / "x.mp4", "t", "d", [], "22", "private", None, ai_content=True)
    assert fake.bodies[0]["status"]["containsSyntheticMedia"] is True
    platforms._save_platform("tiktok", {"client_key": "k", "client_secret": "s"})
    acc, u = post("tiktok", tiktok={"privacy_level": "SELF_ONLY"}, ai_content=True)
    net = Net(monkeypatch, {("POST", "creator_info"): [(200, {"data": CREATOR, **OK})],
                            ("POST", "video/init"): [(200, {"data": {"publish_id": "P1", "upload_url": "https://open-upload.tiktokapis.com/u"}, **OK})],
                            ("PUT", "open-upload"): [(201, {})],
                            ("POST", "status/fetch"): [(200, {"data": {"status": "PUBLISH_COMPLETE"}, **OK})]})
    pipeline._upload({"upload_id": u["id"]})
    assert net.find("POST", "video/init")[0][2]["json"]["post_info"]["is_aigc"] is True
    # moving a scheduled YouTube video keeps the label (videos.list doesn't echo it back, so it is sent from the post)
    sent = {}

    class Svc:
        def videos(self):
            return self

        def list(self, **kw):
            return self

        def update(self, part, body):
            sent.update(body)
            return self

        def execute(self):
            return {"items": [{"status": {"privacyStatus": "private"}}]}
    monkeypatch.setattr(youtube, "_service", lambda account_id=None: Svc())
    monkeypatch.setattr(youtube, "missing_scopes", lambda account_id=None: [])
    youtube.set_publish_at("v", "2031-01-01T10:00:00Z", ai_content=True)
    assert sent["status"]["containsSyntheticMedia"] is True
    # the post records it: from the dialog's switch, else from the clip (AI-generated media in it)
    c = _clip()
    yt = accounts.add("youtube", name="Y", token={"token": "t"}, approved=True)
    assert pipeline.request_upload(c["id"], {"account_id": yt["id"], "ai_content": True})["ai_content"] is True
    assert pipeline.request_upload(c["id"], {"account_id": yt["id"]})["ai_content"] is False
    store.update("clip", c["id"], {"ai_media": ["tts"]})
    assert pipeline.request_upload(c["id"], {"account_id": yt["id"]})["ai_content"] is True


# ---------------- source link + description templates ----------------

def test_vod_links_carry_the_clip_start():
    assert platforms.vod_link("https://www.twitch.tv/videos/123456", 3723.4) == "https://www.twitch.tv/videos/123456?t=1h02m03s"
    assert platforms.vod_link("https://www.youtube.com/watch?v=abc", 75) == "https://www.youtube.com/watch?v=abc&t=75s"
    assert platforms.vod_link("https://youtu.be/abc", 75) == "https://youtu.be/abc?t=75"
    assert platforms.vod_link("", 5) == ""


def test_templates_fill_variables_drop_empty_lines_and_apply_per_account():
    tpl = "{description}\n\nFull stream ({vod_time}): {source_url}\nBy {channel}\n{hashtags}\nMissing: {nope}"
    v = {"description": "Watch this", "source_url": "https://x/v?t=1s", "vod_time": "1:02:03", "channel": "", "hashtags": "#a #b"}
    assert platforms.render_template(tpl, v) == "Watch this\n\nFull stream (1:02:03): https://x/v?t=1s\n#a #b\nMissing: {nope}"
    assert platforms.render_template("Source: {source_url}", {"description": "D", "source_url": ""}) == "D"  # footer, no link: dropped
    assert platforms.render_template("Source: {source_url}", {"description": "D", "source_url": "u"}) == "D\n\nSource: u"
    c = _clip()
    yt = accounts.add("youtube", name="Y", token={"token": "t"}, approved=True)
    tt = accounts.add("tiktok", name="T", access_token="x", approved=True, description_template="{hook} | {source_url}")
    up = pipeline.request_upload(c["id"], {"account_id": yt["id"], "description": "My desc", "tags": ["gg"]})
    assert up["source_link"] == "https://www.twitch.tv/videos/123456?t=1h02m03s"
    assert up["description"] == "My desc\n\nFull stream: https://www.twitch.tv/videos/123456?t=1h02m03s"  # the default template
    up = pipeline.request_upload(c["id"], {"platform": "tiktok", "account_id": tt["id"], "caption": "cap", "tiktok": {"privacy_level": "SELF_ONLY"}})
    assert up["caption"] == "cap\n\nwhat a play | https://www.twitch.tv/videos/123456?t=1h02m03s"
    up = pipeline.request_upload(c["id"], {"account_id": yt["id"], "description": "Plain", "template": False})
    assert up["description"] == "Plain"


def test_routes_are_mounted_before_the_static_ui():
    from reelwright import server
    paths = [getattr(r, "path", "") for r in server.app.routes]
    assert paths.index("/api/vault") < len(paths) - 1 and server.app.routes[-1].name == "ui" and "/api/publish/template" in paths


if sys.platform != "win32":  # the vault must not be needed off Windows
    def test_vault_reports_plain_text_off_windows():
        assert not vault.status()["encrypted"]


# ---------------- the QA gate at upload ----------------

TOO_SHORT = {"code": "too_short", "level": "error", "msg": "Instagram Reels must be at least 3 s (this is 2.0 s)",
             "fix": "Make the clip longer.", "platform": "instagram"}
LOUD = {"code": "loudness", "level": "warn", "msg": "Loudness -20 LUFS", "fix": "Normalize.", "platform": None}


def _qa(c, *issues):
    return store.update("clip", c["id"], fn=lambda d: d["export"].update(qa=list(issues)))


def test_upload_is_blocked_by_a_platform_error_and_never_posted(post, monkeypatch):
    sent, events = [], []
    monkeypatch.setattr(platforms, "publish", lambda *a, **k: sent.append(a[0]) or {"url": "https://x"})
    monkeypatch.setattr(pipeline.log, "event", lambda kind, msg: events.append((kind, msg)))
    acc, u = post("instagram", auto=True)
    accounts.update(acc["id"], {"autopilot": True})
    _qa(pipeline._target(u), TOO_SHORT, LOUD)
    pipeline._upload({"upload_id": u["id"]})
    u = store.get("upload", u["id"])
    assert u["status"] == "failed" and "at least 3 s" in u["error"] and not sent
    assert any(k == "autopilot" and "at least 3 s" in m for k, m in events)  # Autopilot skips it and says why
    # the same clip with only a warning, or an error for another platform, still goes out
    acc, u2 = post("tiktok")
    _qa(pipeline._target(u2), TOO_SHORT, LOUD)
    pipeline._upload({"upload_id": u2["id"]})
    assert store.get("upload", u2["id"])["status"] == "done" and sent == ["tiktok"]


def test_a_manual_post_of_a_blocked_render_is_refused_up_front(post):
    acc, u = post("instagram")
    c = _qa(pipeline._target(u), TOO_SHORT)
    with pytest.raises(RuntimeError, match="at least 3 s"):
        pipeline.request_upload(c["id"], {"platform": "instagram", "account_id": acc["id"], "title": "t"})
    _qa(c, LOUD)  # warnings never block: the dialog shows them with "Post anyway"
    assert pipeline.request_upload(c["id"], {"platform": "instagram", "account_id": acc["id"], "title": "t"})["status"]


def test_a_qa_error_for_one_chosen_account_refuses_the_whole_publish(post):
    from fastapi import HTTPException

    from reelwright import server
    ig, u = post("instagram")
    tt, _ = post("tiktok")
    c = _qa(pipeline._target(u), TOO_SHORT)
    before = len(store.find("upload"))
    with pytest.raises(HTTPException) as ex:
        server.upload_clip(c["id"], {"accounts": [tt["id"], ig["id"]], "title": "t"})
    assert ex.value.status_code == 400 and "at least 3 s" in ex.value.detail
    assert len(store.find("upload")) == before  # not even the TikTok post went into the queue


@WIN
def test_plain_text_backups_live_only_until_the_next_clean_start(files):
    _old_version_files(files)
    vault.migrate()  # the sealing start keeps the way back
    assert list(files.glob("*.pre-vault.bak"))
    vault.migrate()  # the next start reopened everything sealed: the plain-text copies go
    assert not list(files.glob("*.pre-vault.bak"))
