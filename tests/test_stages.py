"""Pipeline board (harvested → pre-clipped → long/short form → ready → uploaded) and the posted-clip edit lock."""
import os
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from reelwright import pipeline, server, store  # noqa: E402


def _ready_project(name="board"):
    p = pipeline.new_project(name)
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    return store.update("project", p["id"], {"meta": {"width": 1920, "height": 1080, "duration": 60.0, "fps": 30, "has_audio": True},
                                              "status": "ready", "source": "source.mp4"})


def _clip(pid, aspect="9:16", rev=1, **extra):
    c = pipeline.new_clip(pid, {"start": 5.0, "end": 15.0, "title": extra.pop("title", "Clip")})
    return store.update("clip", c["id"], fn=lambda d: (d["edit"].update(aspect=aspect), d.update(rev=rev, **extra)))


def _upload(cid, pid, status, **kw):
    return store.put("upload", {"platform": kw.pop("platform", "youtube"), "clip_id": cid, "project_id": pid, "status": status,
                                "publish_at": None, "upload_at": None, "video_id": None, "url": None, **kw})


def _stage_of(board, item_id):
    hits = [s["key"] for s in board["stages"] for i in s["items"] if i["id"] == item_id]
    assert len(hits) == 1, (item_id, hits)  # exactly one stage per item
    return hits[0]


def test_every_item_sits_in_the_furthest_stage_it_reached():
    importing = pipeline.new_project("still importing")
    empty = _ready_project("no clips yet")
    p = _ready_project()
    pre = _clip(p["id"], rev=1)
    longf = _clip(p["id"], aspect="16:9", rev=2)
    short = _clip(p["id"], aspect="9:16", rev=3)
    rendered = _clip(p["id"], rev=2)
    (pipeline.pdir(p["id"]) / "exports").mkdir(exist_ok=True)
    (pipeline.pdir(p["id"]) / "exports" / f"{rendered['id']}.mp4").write_bytes(b"x")
    store.update("clip", rendered["id"], {"export": {"status": "done", "rev": 2, "file": f"exports/{rendered['id']}.mp4"}})
    scheduled = _clip(p["id"], rev=1)
    _upload(scheduled["id"], p["id"], "scheduled", publish_at="2030-01-01T10:00:00+00:00")
    posted = _clip(p["id"], rev=4)
    _upload(posted["id"], p["id"], "done", video_id="abc", url="https://youtu.be/abc")
    _upload(posted["id"], p["id"], "held", platform="tiktok", upload_at="2030-01-02T10:00:00+00:00")

    board = server.pipeline_board()
    assert [s["key"] for s in board["stages"]] == ["harvested", "preclipped", "longform", "shortform", "ready", "uploaded"]
    assert _stage_of(board, importing["id"]) == "harvested" and _stage_of(board, empty["id"]) == "harvested"
    assert p["id"] not in [i["id"] for s in board["stages"] for i in s["items"]]  # a project with clips is represented by them
    assert [_stage_of(board, c["id"]) for c in (pre, longf, short, rendered, scheduled, posted)] == \
        ["preclipped", "longform", "shortform", "ready", "ready", "uploaded"]
    item = next(i for s in board["stages"] for i in s["items"] if i["id"] == posted["id"])
    assert item["locked"] and item["type"] == "clip" and item["edit_url"] == f"#/studio/{posted['id']}"
    [p0] = item["posts"]  # upload_id + account: the delete dialog names each post and its channel
    assert {k: p0[k] for k in ("platform", "url", "video_id", "publish_at", "account")} == \
        {"platform": "youtube", "url": "https://youtu.be/abc", "video_id": "abc", "publish_at": None, "account": None} and p0["upload_id"]
    assert item["pending"] == [{"platform": "tiktok", "status": "held", "when": "2030-01-02T10:00:00+00:00"}]
    assert item["duration"] == 10.0 and item["aspect"] == "9:16" and item["updated"]
    proj = next(i for s in board["stages"] for i in s["items"] if i["id"] == empty["id"])
    assert proj["type"] == "project" and proj["edit_url"] == f"#/editor/{empty['id']}" and not proj["locked"]


def test_posted_clips_are_locked_until_versioned(monkeypatch):
    p = _ready_project()
    c = _clip(p["id"], rev=2, title="Big moment")
    _upload(c["id"], p["id"], "done", video_id="v1")
    for call in (lambda: server.patch_clip(c["id"], {"edit": {"speed": 1.2}}), lambda: server.patch_clip(c["id"], {"start": 6}),
                 lambda: server.export_clip(c["id"]), lambda: server.track_clip(c["id"]),
                 lambda: server.assist(c["id"], {"instruction": "faster"}),
                 lambda: server.apply_plan(c["id"], {"plan_id": "x", "item_ids": []})):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 409 and "new version" in e.value.detail
    assert server.patch_clip(c["id"], {"title": "Renamed", "rating": 1})["title"] == "Renamed"  # metadata still editable

    v = server.version_clip(c["id"])
    assert v["id"] != c["id"] and v["version_of"] == c["id"] and v["title"] == "Renamed (v2)"
    assert v["edit"] == store.get("clip", c["id"])["edit"] and v["export"] is None and v["posts"] == {}
    assert not store.find("upload", clip_id=v["id"])
    assert server.patch_clip(v["id"], {"edit": {"speed": 1.2}})["edit"]["speed"] == 1.2  # the new version is editable
    assert server.version_clip(c["id"])["title"] == "Renamed (v3)"
