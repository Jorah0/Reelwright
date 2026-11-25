"""The full mobile app's API (remote.py): every new endpoint is signed, allowlisted and goes through the same server paths
and gates as the desktop (patch_clip's validation and posted-clip lock, presets, account approval, reschedule rules).
Also the simulator's loopback-only pairing shortcut and demo mode's no-real-uploads guard. No browser needed."""
import json
import re
import shutil
import time
from datetime import datetime, timedelta, timezone

import pytest
import test_remote  # first: it points REELWRIGHT_DATA at a temporary folder before reelwright loads
from test_remote import ai_clip, call, get

from reelwright import accounts, config, pipeline, platforms, remote, store, youtube

clean, phone = test_remote.clean, test_remote.phone  # its fixtures here too (clean is autouse: a clean slate per test)


def soon(hours=30):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@pytest.fixture
def dev_sim():
    """The developer toggle on; put back afterwards."""
    old = config.load()["remote_dev_sim"]
    config.save({"remote_dev_sim": True})
    yield
    config.save({"remote_dev_sim": old})


def approved(platform="youtube", name="Main"):
    return accounts.update(accounts.add(platform, name=name, access_token="T")["id"], {"approved": True})


# ---------------- every new endpoint is signed and allowlisted ----------------

NEW = [("GET", "clips/{c}/editor"), ("POST", "clips/{c}/edit"), ("POST", "clips/{c}/preset"), ("POST", "clips/{c}/media"),
       ("POST", "clips/{c}/meta"), ("GET", "clips/{c}/publish"), ("GET", "schedule"), ("POST", "uploads/{u}/move"),
       ("GET", "longform"), ("POST", "longform/{l}/render"), ("POST", "longform/{l}/publish"), ("POST", "projects/{p}/analyze")]


def test_every_new_endpoint_is_in_the_allowlist_and_refuses_unsigned_or_wrong_key_requests(phone):
    port, did, secret = phone
    routes = {(m, rx) for m, rx, _ in remote.API}
    for m, path in NEW:
        p = path.format(c="abc", u="abc", l="abc", p="abc")
        assert any(m == rm and re.fullmatch(rx, p) for rm, rx in routes), path
        assert call(port, did, None, m, f"/remote/api/{p}", {} if m == "POST" else None)[0] == 401, path  # unsigned
        assert call(port, did, "wrong", m, f"/remote/api/{p}", {} if m == "POST" else None)[0] == 401, path
        assert call(port, did, secret, "PUT", f"/remote/api/{p}")[0] in (404, 405), path  # no other verbs


# ---------------- clip editing ----------------

def test_editor_lists_trim_bounds_options_and_presets_without_secrets(phone):
    port, did, secret = phone
    c = ai_clip(rendered=b"v")
    store.put("preset", {"name": "Bold captions", "parts": {"captions": {"caption_style": "boxed"}}, "scope": "global"})
    st, js, _ = call(port, did, secret, "GET", f"/remote/api/clips/{c['id']}/editor")
    assert st == 200 and js["start"] == 0.0 and js["end"] == 30.0 and js["source_duration"] == 100.0 and js["fps"] >= 1
    assert "split" in js["options"]["layouts"] and "boxed" in js["options"]["caption_styles"] and "auto" in js["options"]["themes"]
    assert any(p["name"] == "Bold captions" for p in js["presets"]) and js["locked"] is False
    assert set(js["edit"]) >= {"layout", "captions", "caption_style", "headline", "streamer_card", "cuts", "overlays"}
    assert "kits" in js  # media kits hook (routes_media.py): None until that API exists
    assert str(config.DATA) not in json.dumps(js) and "/files/" not in json.dumps(js)


def test_edit_trims_changes_layout_headline_captions_card_and_cuts_through_patch_clip(phone):
    port, did, secret = phone
    c = ai_clip()
    body = {"start": 2.0, "end": 20.0, "edit": {"layout": "split", "captions": False, "caption_style": "neon",
                                                 "headline": {"on": True, "text": "Huge play", "theme": "auto"},
                                                 "streamer_card": {"on": True}, "cuts": [[5.0, 6.0]]}}
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/edit", body)
    assert st == 200, js
    got = store.get("clip", c["id"])
    e = got["edit"]
    assert (got["start"], got["end"]) == (2.0, 20.0) and got["rev"] == c["rev"] + 1
    assert e["layout"] == "split" and e["captions"] is False and e["caption_style"] == "neon"
    assert e["headline"]["text"] == "Huge play" and e["headline"]["on"] and e["streamer_card"]["on"] and e["cuts"] == [[5.0, 6.0]]


@pytest.mark.parametrize("bad", [
    {"start": "x"}, {"start": float("nan")}, {"start": 10, "end": 5}, {"edit": {"layout": "nope"}},
    {"edit": {"caption_style": "comic"}}, {"edit": {"headline": {"text": "x" * 500}}}, {"edit": {"cuts": [[0, 100]]}},
    {"edit": {"volume": 5}}, {"edit": {"music": "music/x.mp3"}}, {"edit": {"headline": {"font": "Arial"}}}, {"edit": "split"}, {},
])
def test_edit_refuses_bad_values_and_keys_the_phone_may_not_change(phone, bad):
    port, did, secret = phone
    c = ai_clip()
    st, _, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/edit", bad)  # NaN travels as JSON NaN
    assert st == 400, bad
    assert store.get("clip", c["id"])["rev"] == c["rev"]


def test_a_posted_clip_is_locked_for_edits_presets_media_and_rerender(phone):
    port, did, secret = phone
    c = ai_clip(rendered=b"v")
    store.put("upload", {"clip_id": c["id"], "project_id": c["project_id"], "platform": "youtube", "status": "done"})
    p = store.put("preset", {"name": "P", "parts": {"layout": {"layout": "split"}}, "scope": "global"})
    for path, body in ((f"clips/{c['id']}/edit", {"edit": {"layout": "split"}}), (f"clips/{c['id']}/preset", {"preset_id": p["id"]}),
                       (f"clips/{c['id']}/render", {}), (f"clips/{c['id']}/media", {"asset_id": "x"})):
        st, js, _ = call(port, did, secret, "POST", f"/remote/api/{path}", body)
        assert st == 409 and "posted" in js["detail"].lower(), path
    assert call(port, did, secret, "GET", f"/remote/api/clips/{c['id']}/editor")[1]["locked"] is True


def test_preset_applies_clip_parts_only_never_app_settings(phone):
    port, did, secret = phone
    c = ai_clip()
    before = config.load()["clip_min"]
    p = store.put("preset", {"name": "Everything", "scope": "global",
                             "parts": {"layout": {"layout": "pip"}, "captions": {"caption_style": "clean"}, "app": {"clip_min": 7}}})
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/preset", {"preset_id": p["id"]})
    assert st == 200, js
    e = store.get("clip", c["id"])["edit"]
    assert e["layout"] == "pip" and e["caption_style"] == "clean" and config.load()["clip_min"] == before
    assert call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/preset", {"preset_id": "missing"})[0] == 404


def test_media_adds_a_library_sound_or_sticker_at_the_playhead_and_removes_it(phone):
    port, did, secret = phone
    c = ai_clip()
    snd = store.put("asset", {"kind": "sound", "name": "Boom", "file": "x.mp3", "duration": 1.5})
    vid = store.put("asset", {"kind": "video", "name": "Clip", "file": "x.mp4", "duration": 3})
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/media", {"asset_id": snd["id"], "t": 4.0})
    assert st == 200, js
    ov = store.get("clip", c["id"])["edit"]["overlays"]
    assert [(o["kind"], o["asset_id"], o["t"]) for o in ov] == [("sound", snd["id"], 4.0)]
    assert call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/media", {"asset_id": vid["id"]})[0] == 400  # sounds and stickers only
    assert call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/media", {"remove": ov[0]["id"]})[0] == 200
    assert not store.get("clip", c["id"])["edit"]["overlays"]


def test_rerender_goes_through_the_desktop_export_path(phone, monkeypatch):
    port, did, secret = phone
    c = ai_clip()
    seen = []
    monkeypatch.setattr(pipeline, "request_export", lambda cid: seen.append(cid) or {"id": "job1"})
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/render", {})
    assert st == 200 and js["job"] == "job1" and seen == [c["id"]]


# ---------------- publish ----------------

def test_publish_info_marks_unapproved_accounts_and_previews_the_description_template(phone):
    port, did, secret = phone
    c = ai_clip()
    store.update("clip", c["id"], {"hook": "The comeback"})
    ok = approved(name="Main")
    no = accounts.add("youtube", name="Second", access_token="T")
    accounts.update(ok["id"], {"description_template": "{hook}\nfollow for more"})
    st, js, _ = call(port, did, secret, "GET", f"/remote/api/clips/{c['id']}/publish")
    assert st == 200
    acc = {a["id"]: a for a in js["accounts"]}
    assert acc[ok["id"]]["approved"] is True and acc[no["id"]]["approved"] is False
    assert "follow for more" in acc[ok["id"]]["template"] and "The comeback" in acc[ok["id"]]["template"]
    assert "access_token" not in json.dumps(js)
    assert js["ai"]["provider"] and "ready" in js["ai"]
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/publish", {"accounts": [no["id"]], "title": "t"})
    assert st == 400 and "approved" in js["detail"]  # the account approval gate, from the phone too


def test_ai_meta_calls_the_existing_route_and_reports_claudes_cost(phone, monkeypatch):
    port, did, secret = phone
    c = ai_clip()
    from reelwright import ai, usage

    def fake(clip, segs, vertical, guidance=""):
        usage.record("YouTubeMeta", "claude-haiku-5-5", {"input_tokens": 1000, "output_tokens": 200})
        return {"title": "AI title", "description": "AI words", "tags": ["a", "b"]}
    monkeypatch.setattr(ai, "youtube_meta", fake)
    monkeypatch.setattr(ai, "route", lambda task: "anthropic")
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/meta", {})
    assert st == 200 and js["title"] == "AI title" and js["provider"] == "anthropic" and js["cost"] == pytest.approx(0.0002)


def test_publish_carries_the_phone_description_and_queues_through_upload_clip(phone):
    port, did, secret = phone
    c = ai_clip()
    ok = approved()
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/clips/{c['id']}/publish",
                     {"accounts": [ok["id"]], "title": "T", "description": "From the phone", "when": soon()})
    assert st == 200, js
    u = store.find("upload", clip_id=c["id"])[0]
    assert "From the phone" in u["description"] and u["publish_at"]


# ---------------- schedule ----------------

def test_schedule_lists_posts_and_moves_follow_the_reschedule_rules(phone):
    port, did, secret = phone
    c = ai_clip(rendered=b"v")
    ok = approved()
    up = pipeline.request_upload(c["id"], {"platform": "youtube", "account_id": ok["id"], "title": "T", "publish_at": soon(30)})
    st, js, _ = call(port, did, secret, "GET", "/remote/api/schedule")
    assert st == 200 and any(i["id"] == up["id"] for i in js["items"]) and "/files/" not in json.dumps(js)
    later = soon(80)
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/uploads/{up['id']}/move", {"when": later})
    assert st == 200, js
    got = store.get("upload", up["id"])
    assert abs(datetime.fromisoformat(got["publish_at"]).timestamp() - datetime.fromisoformat(later).timestamp()) < 2 and got["auto"] is False
    assert call(port, did, secret, "POST", f"/remote/api/uploads/{up['id']}/move", {"when": "2001-01-01T00:00:00+00:00"})[0] == 400
    assert call(port, did, secret, "POST", f"/remote/api/uploads/{up['id']}/move", {"when": "soon"})[0] == 400
    no = accounts.add("youtube", name="Unapproved")
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/uploads/{up['id']}/move", {"account_id": no["id"]})
    assert st == 400 and "approved" in js["detail"]
    store.update("upload", up["id"], {"status": "uploading"})
    assert call(port, did, secret, "POST", f"/remote/api/uploads/{up['id']}/move", {"when": later})[0] == 400  # the worker has it
    assert call(port, did, secret, "POST", "/remote/api/uploads/nosuch/move", {"when": later})[0] == 400


# ---------------- long form, library ----------------

def test_longform_list_render_and_publish(phone, monkeypatch):
    port, did, secret = phone
    from reelwright import longform
    c = ai_clip()
    d = longform.create(c["project_id"], "clips", "Best of", [[0.0, 20.0]], [], {"clip_ids": [c["id"]]}, 0)
    st, js, _ = call(port, did, secret, "GET", "/remote/api/longform")
    assert st == 200 and [x["title"] for x in js["items"] if x["id"] == d["id"]] == ["Best of"]
    monkeypatch.setattr(longform, "request_export", lambda lid: {"id": "lfjob"})
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/longform/{d['id']}/render", {})
    assert st == 200 and js["job"] == "lfjob"
    no = accounts.add("youtube", name="Unapproved")
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/longform/{d['id']}/publish", {"accounts": [no["id"]], "title": "x"})
    assert st == 400 and "approved" in js["detail"]
    ok = approved()
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/longform/{d['id']}/publish", {"accounts": [ok["id"]], "title": "Best of"})
    assert st == 200 and store.find("upload", longform_id=d["id"])


def test_analyze_starts_the_desktop_analysis_and_is_off_in_demo_mode(phone, monkeypatch):
    port, did, secret = phone
    c = ai_clip()
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/projects/{c['project_id']}/analyze", {})
    assert st == 200 and store.get("project", c["project_id"])["status"] == "queued"
    monkeypatch.setattr(config, "DEMO", True)
    st, js, _ = call(port, did, secret, "POST", f"/remote/api/projects/{c['project_id']}/analyze", {})
    assert st == 400 and "demo" in js["detail"].lower()


# ---------------- simulator: loopback-only pairing shortcut ----------------

def test_simulator_shortcut_is_off_by_default_and_needs_the_listener(phone):
    assert config.DEFAULTS["remote_dev_sim"] is False
    with pytest.raises(ValueError):
        remote.sim_link()
    st = remote.put_remote({"dev_sim": True})
    try:
        link = remote.sim_link()
        assert link["url"].startswith(f"http://127.0.0.1:{phone[0]}/sim.html#pair=") and st["dev_sim"] is True
        assert next(d for d in config.load()["remote_devices"] if d["id"] == link["device_id"])["sim"] is True
    finally:
        remote.put_remote({"dev_sim": False})
    with pytest.raises(Exception):
        remote.put_remote({"dev_sim": "yes"})


def test_simulator_devices_and_pages_work_from_loopback_only(phone, dev_sim):
    port, _, _ = phone
    link = remote.sim_link()
    did, code = link["url"].split("#pair=")[1].split(".", 1)
    assert remote.device_allowed({"sim": True}, "127.0.0.1") and remote.device_allowed({"sim": True}, "::1")
    for ip in ("192.168.1.5", "10.0.0.2", "100.64.1.1", "::ffff:192.168.1.5"):
        assert not remote.device_allowed({"sim": True}, ip), ip
        assert remote.device_allowed({}, ip)  # a normal phone still works over Wi-Fi
    st, js, _ = call(port, did, code, "POST", "/remote/api/pair", {"name": "Simulator", "pn": "ab" * 16})
    assert st == 200
    st, body, hdrs = get(port, "/sim.html")
    assert st == 200 and b"<iframe" in body and "frame-ancestors 'self'" in hdrs["Content-Security-Policy"]
    assert hdrs["X-Frame-Options"] == "SAMEORIGIN" and get(port, "/sim.js")[0] == 200
    config.save({"remote_dev_sim": False})
    assert get(port, "/sim.html")[0] == 404 and get(port, "/sim.js")[0] == 404
    assert "frame-ancestors 'none'" in get(port, "/")[2]["Content-Security-Policy"]


def test_the_main_app_route_makes_a_loopback_link_and_refuses_when_off(phone, dev_sim):
    out = remote.post_simulator({})
    assert out["url"].startswith("http://127.0.0.1:") and out["window"] is False  # no desktop window in tests
    config.save({"remote_dev_sim": False})
    with pytest.raises(Exception) as e:
        remote.post_simulator({})
    assert getattr(e.value, "status_code", None) == 400


# ---------------- demo mode: real uploads are impossible ----------------

def test_demo_mode_never_reaches_a_platform_and_only_sandbox_accounts_look_connected(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEMO", True)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network upload in demo mode"))  # noqa: E731
    for name in ("upload", "set_thumbnail", "set_publish_at", "delete_video"):
        monkeypatch.setattr(youtube, name, boom)
    monkeypatch.setattr(platforms, "tiktok_upload", boom)
    monkeypatch.setattr(platforms, "instagram_upload", boom)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    for plat in ("youtube", "tiktok", "instagram"):
        res = platforms.publish(plat, f, {"id": "u1", "title": "t", "description": "", "tags": [], "category_id": "20",
                                          "privacy": "private", "publish_at": None, "made_for_kids": False, "account_id": "a"})
        assert res["demo"] is True and res["url"].startswith("https://demo.invalid/")
    assert platforms.account_connected({"platform": "youtube", "id": "x", "sandbox": True})
    assert not platforms.account_connected({"platform": "tiktok", "id": "y", "access_token": "REAL"})  # a real token: never used
    assert youtube._creds("anything") is None


KINDS = ("project", "clip", "upload", "preset", "asset", "longform", "job")


@pytest.fixture
def restore_store():
    """Docs the seed adds are removed again: other tests count projects, clips and posts."""
    before = {k: {d["id"] for d in store.find(k)} for k in KINDS}
    yield
    for k in KINDS:
        for d in store.find(k):
            if d["id"] not in before[k]:
                store.delete(k, d["id"])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_demo_seed_builds_a_sandbox_library(monkeypatch, tmp_path, restore_store):
    from reelwright import demo
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(accounts, "FILE", tmp_path / "accounts.json")
    t0 = time.time()
    ids = demo.seed(small=True)
    assert time.time() - t0 < 60
    accs = accounts.all_accounts()
    assert accs and all(a.get("sandbox") for a in accs) and not any(a.get("access_token") or a.get("token") for a in accs)
    assert any(not a["approved"] for a in accs) and ids["unapproved"] in [a["id"] for a in accs]
    review = store.get("clip", ids["review"][0])
    assert review and not review.get("rating")
    edit = store.get("clip", ids["edit"])
    assert pipeline.clip_export_current(edit)
    assert [u for u in store.find("upload") if u.get("status") == "scheduled"]
    with pytest.raises(SystemExit):
        demo.check_data_dir(config.ROOT / "data")  # never the real library


def test_demo_removes_the_temporary_folder_it_made_but_never_a_chosen_one(monkeypatch, tmp_path):
    from pathlib import Path

    from reelwright import demo
    made = tmp_path / "reelwright-demo-x"
    monkeypatch.delenv("REELWRIGHT_DEMO", raising=False)
    monkeypatch.setattr(demo.tempfile, "mkdtemp", lambda prefix="": (made.mkdir(), str(made))[1])
    seen = []
    monkeypatch.setattr(demo.subprocess, "call", lambda argv, env: (seen.append(env["REELWRIGHT_DATA"]), (Path(env["REELWRIGHT_DATA"]) / "store.db").write_text("x"), 0)[-1])
    assert demo.main([]) == 0 and seen == [str(made)] and not made.exists()  # one leftover folder per run, before
    mine = tmp_path / "my-demo"
    mine.mkdir()
    assert demo.main(["--data", str(mine)]) == 0 and (mine / "store.db").exists()  # a folder the user named stays
