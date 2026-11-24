"""Delete from anywhere on the Pipeline board: per-stage behaviour, pending posts canceled (and never posted after),
uploads in flight refused, opt-in YouTube removal with that post's own channel, TikTok/Instagram reported as manual,
per-item results and path safety. No network: YouTube is a fake service."""
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import accounts, pipeline, platforms, server, store, youtube  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "FILE", tmp_path / "accounts.json")
    monkeypatch.setattr(accounts, "LEGACY_CONN", tmp_path / "connections.json")
    monkeypatch.setattr(accounts, "LEGACY_YT", tmp_path / "youtube_token.json")
    monkeypatch.setattr(pipeline, "submit", lambda kind, **kw: {"id": "j"})
    monkeypatch.setattr(pipeline.config, "api_key", lambda: "")


@pytest.fixture
def api():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


def project():
    p = store.put("project", {"name": "p", "status": "ready", "meta": {"duration": 100.0, "width": 1920, "height": 1080}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    return p


def clip(p=None, rendered=False):
    p = p or project()
    c = pipeline.new_clip(p["id"], {"start": 0.0, "end": 30.0, "score": 80}, source="ai")
    if rendered:
        d = pipeline.pdir(p["id"]) / "exports"
        d.mkdir(exist_ok=True)
        (d / f"{c['id']}.mp4").write_bytes(b"x")
        (d / f"{c['id']}.jpg").write_bytes(b"x")
        c = store.update("clip", c["id"], {"export": {"status": "done", "rev": c["rev"], "file": f"exports/{c['id']}.mp4",
                                                       "cover": f"exports/{c['id']}.jpg"}})
    return c


def post(c, **fields):
    return store.put("upload", {"clip_id": c["id"], "project_id": c["project_id"], "platform": "youtube", "status": "queued",
                                "privacy": "private", "publish_at": None, "upload_at": None, "title": "t", **fields})


def delete(api, items, remote=None):
    r = api.post("/api/pipeline/delete", json={"items": items, "options": {"remote": remote or {}}})
    assert r.status_code == 200, r.text
    return r.json()["results"]


def board_ids(api):
    return {it["id"] for st in api.get("/api/pipeline").json()["stages"] for it in st["items"]}


# ---------------- per stage ----------------

def test_harvested_project_is_deleted_with_its_folder(api):
    p = project()
    (pipeline.pdir(p["id"]) / "source.mp4").write_bytes(b"x")
    assert p["id"] in board_ids(api)
    [res] = delete(api, [{"type": "project", "id": p["id"]}])
    assert res["ok"] and not store.get("project", p["id"]) and not pipeline.pdir(p["id"]).exists()
    assert p["id"] not in board_ids(api)


def test_clip_in_an_editing_stage_is_deleted_with_its_rendered_files(api):
    c = clip(rendered=True)
    folder = pipeline.pdir(c["project_id"])
    [res] = delete(api, [{"type": "clip", "id": c["id"]}])
    assert res["ok"] and not store.get("clip", c["id"])
    assert not (folder / "exports" / f"{c['id']}.mp4").exists() and not (folder / "exports" / f"{c['id']}.jpg").exists()
    assert store.get("project", c["project_id"]) and folder.exists()  # the project and its source stay


def test_ready_clip_cancels_its_pending_posts_and_says_how_many(api):
    c = clip(rendered=True)
    q, s = post(c), post(c, status="scheduled", upload_at="2999-01-01T00:00:00+00:00")
    [res] = delete(api, [{"type": "clip", "id": c["id"]}])
    assert res["ok"] and res["canceled"] == 2
    assert store.get("upload", q["id"])["status"] == "canceled" and store.get("upload", s["id"])["status"] == "canceled"
    assert c["id"] not in board_ids(api)


def test_a_canceled_post_of_a_deleted_clip_is_never_posted(api, monkeypatch):
    sent = []
    monkeypatch.setattr(pipeline.platforms, "publish", lambda *a, **k: sent.append(a) or {"video_id": "v"})
    monkeypatch.setattr(platforms, "account_connected", lambda a: True)
    c = clip(rendered=True)
    u = post(c)
    delete(api, [{"type": "clip", "id": c["id"]}])
    store.update("upload", u["id"], {"status": "queued"})  # even if something re-queued it, the clip is gone
    pipeline._upload({"upload_id": u["id"]})  # no crash, no post
    assert not sent and store.get("upload", u["id"])["status"] == "canceled"


def test_upload_whose_clip_vanished_is_canceled_not_failed_or_crashed(monkeypatch):
    sent = []
    monkeypatch.setattr(pipeline.platforms, "publish", lambda *a, **k: sent.append(a) or {"video_id": "v"})
    c = clip(rendered=True)
    u = post(c)
    store.delete("clip", c["id"])  # deleted by the old route, or mid-queue
    pipeline._upload({"upload_id": u["id"]})
    assert not sent and store.get("upload", u["id"])["status"] == "canceled"


def test_uploading_right_now_is_refused(api):
    c = clip(rendered=True)
    post(c, status="uploading")
    [res] = delete(api, [{"type": "clip", "id": c["id"]}])
    assert not res["ok"] and "wait for it to finish or let it fail" in res["error"]
    assert store.get("clip", c["id"])


def test_project_with_an_upload_in_flight_is_refused(api):
    c = clip(rendered=True)
    post(c, status="uploading")
    [res] = delete(api, [{"type": "project", "id": c["project_id"]}])
    assert not res["ok"] and store.get("project", c["project_id"])


# ---------------- posted clips ----------------

class FakeYT:
    def __init__(self, fail=None):
        self.deleted, self.fail = [], fail

    def videos(self):
        return self

    def delete(self, id):
        self.deleted.append(id)
        return self

    def execute(self):
        if self.fail:
            raise self.fail
        return ""


def posted(c, platform="youtube", **f):
    return post(c, platform=platform, status="done", video_id=f.pop("video_id", "vid1"), url=f.pop("url", "https://x/v"), **f)


def test_posted_clip_is_removed_locally_without_touching_youtube_by_default(api, monkeypatch):
    calls = []
    monkeypatch.setattr(youtube, "_service", lambda aid=None: calls.append(aid))
    c = clip(rendered=True)
    posted(c)
    [res] = delete(api, [{"type": "clip", "id": c["id"]}])
    assert res["ok"] and not calls and res["remote"] == [] and not store.get("clip", c["id"])
    assert c["id"] not in board_ids(api)


def test_youtube_video_is_deleted_only_when_ticked_and_with_that_posts_channel(api, monkeypatch):
    a1 = accounts.add("youtube", name="one", token={"scopes": youtube.SCOPES})
    a2 = accounts.add("youtube", name="two", token={"scopes": youtube.SCOPES})
    fake, used = FakeYT(), []
    monkeypatch.setattr(youtube, "_service", lambda aid=None: used.append(aid) or fake)
    c = clip(rendered=True)
    u1 = posted(c, account_id=a1["id"], video_id="v1")
    u2 = posted(c, account_id=a2["id"], video_id="v2")
    [res] = delete(api, [{"type": "clip", "id": c["id"]}], remote={u2["id"]: True, u1["id"]: False})
    assert res["ok"] and fake.deleted == ["v2"] and used == [a2["id"]]
    assert res["remote"] == [{"upload_id": u2["id"], "platform": "youtube", "ok": True, "manual": False, "url": "https://x/v", "error": None}]
    assert not store.get("upload", u2["id"]) and store.get("upload", u1["id"])  # u1 still feeds the results learning


def test_youtube_delete_needs_the_force_ssl_permission(api, monkeypatch):
    a = accounts.add("youtube", name="old", token={"scopes": youtube.SCOPES[:2]})
    monkeypatch.setattr(youtube, "_service", lambda aid=None: pytest.fail("must not call YouTube without the permission"))
    c = clip(rendered=True)
    u = posted(c, account_id=a["id"])
    [res] = delete(api, [{"type": "clip", "id": c["id"]}], remote={u["id"]: True})
    assert res["ok"] and not store.get("clip", c["id"])  # removing from Reelwright still happened
    [r] = res["remote"]
    assert not r["ok"] and "reconnect" in r["error"].lower()


def test_tiktok_and_instagram_are_reported_as_manual(api, monkeypatch):
    monkeypatch.setattr(youtube, "_service", lambda aid=None: pytest.fail("no YouTube call"))
    c = clip(rendered=True)
    t = posted(c, platform="tiktok", url="https://tiktok.com/@me/video/1")
    i = posted(c, platform="instagram", url="https://instagram.com/reel/1")
    [res] = delete(api, [{"type": "clip", "id": c["id"]}], remote={t["id"]: True, i["id"]: True})
    assert res["ok"]
    by = {r["platform"]: r for r in res["remote"]}
    assert by["tiktok"]["manual"] and by["instagram"]["manual"] and not by["tiktok"]["ok"]
    assert by["tiktok"]["url"] == "https://tiktok.com/@me/video/1" and "app" in by["instagram"]["error"]


def test_partial_failure_is_reported_per_item_and_does_not_block_others(api, monkeypatch):
    a = accounts.add("youtube", name="one", token={"scopes": youtube.SCOPES})
    monkeypatch.setattr(youtube, "_service", lambda aid=None: FakeYT(fail=RuntimeError("quota exceeded")))
    good, busy, bad = clip(rendered=True), clip(rendered=True), clip(rendered=True)
    post(busy, status="uploading")
    u = posted(bad, account_id=a["id"])
    res = delete(api, [{"type": "clip", "id": good["id"]}, {"type": "clip", "id": busy["id"]},
                       {"type": "clip", "id": "nope"}, {"type": "clip", "id": bad["id"]}], remote={u["id"]: True})
    assert [r["ok"] for r in res] == [True, False, False, True]
    assert "quota" in res[3]["remote"][0]["error"] and store.get("upload", u["id"])  # kept: the video is still online
    assert not store.get("clip", good["id"]) and store.get("clip", busy["id"]) and not store.get("clip", bad["id"])


# ---------------- safety ----------------

def test_ids_are_validated_and_paths_never_leave_the_projects_folder(api, tmp_path):
    outside = pipeline.config.projects_dir().parent / "keep.txt"
    outside.write_text("x")
    res = delete(api, [{"type": "project", "id": ".."}, {"type": "project", "id": "../.."}, {"type": "project", "id": ""},
                       {"type": "folder", "id": "x"}, {"type": "clip", "id": None}, "junk"])
    assert not any(r["ok"] for r in res) and outside.exists() and pipeline.config.projects_dir().exists()
    c = clip()  # a clip whose export points outside its folder: the file is never touched
    store.update("clip", c["id"], {"export": {"status": "done", "rev": 1, "file": "../../keep.txt", "cover": "../../keep.txt"}})
    [r] = delete(api, [{"type": "clip", "id": c["id"]}])
    assert r["ok"] and outside.exists()


def test_bad_body_is_a_400(api):
    assert api.post("/api/pipeline/delete", json={"items": "x"}).status_code == 400


def test_deleting_an_untouched_ai_pick_records_the_same_dismissed_lesson_as_the_clip_route(api, monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline, "remember", lambda c, why: seen.append((c["id"], why)))
    c = clip()
    delete(api, [{"type": "clip", "id": c["id"]}])
    assert seen == [(c["id"], "dismissed")]


def test_board_cards_carry_upload_ids_for_the_dialog(api):
    c = clip(rendered=True)
    u = posted(c)
    item = next(it for st in api.get("/api/pipeline").json()["stages"] for it in st["items"] if it["id"] == c["id"])
    assert item["posts"][0]["upload_id"] == u["id"]


def test_project_delete_refuses_while_a_job_runs_for_it_and_cancels_queued_ones():
    p = project()
    c = clip(p)
    running = store.put("job", {"kind": "export", "status": "running", "clip_id": c["id"]})
    res = pipeline.delete_item("project", p["id"])
    assert not res["ok"] and "running" in res["error"].lower()
    assert store.get("project", p["id"]) and pipeline.pdir(p["id"]).is_dir()  # nothing removed
    store.update("job", running["id"], {"status": "done"})
    queued = store.put("job", {"kind": "analyze", "status": "queued", "project_id": p["id"]})
    assert pipeline.delete_item("project", p["id"])["ok"]
    assert store.get("job", queued["id"])["status"] == "canceled"  # never recreates the folder afterwards
    assert not store.get("project", p["id"]) and not pipeline.pdir(p["id"]).exists()
