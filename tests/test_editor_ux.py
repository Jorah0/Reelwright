"""WP5 Editor UX: text-based cuts (validation, render, captions, bleeps), the triage queue, NLE export (FCPXML, EDL,
SRT) and the client-side undo stack (static/undo.js, run under node)."""
import json
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import config, media, nle, pipeline, server, store  # noqa: E402

STATIC = Path(server.__file__).parent / "static"
def _client():
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")  # the app only answers its local host


KINDS = ("project", "clip", "upload", "feedback", "longform", "job")


@pytest.fixture(autouse=True)
def clean():
    """Every doc (and project folder) a test makes is removed afterwards, so nothing leaks into other test files."""
    before = {k: {d["id"] for d in store.find(k)} for k in KINDS}
    yield
    for k in KINDS:
        for d in store.find(k):
            if d["id"] not in before[k]:
                store.delete(k, d["id"])
                if k == "project":
                    shutil.rmtree(pipeline.pdir(d["id"]), ignore_errors=True)


def _project(meta=None, words=None, name="wp5"):
    p = pipeline.new_project(name)
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    pipeline.save_json(p["id"], "transcript.json", {"segments": [{"start": 0, "end": 60, "text": "hello there", "words": words or []}]})
    return store.update("project", p["id"], {"source": "source.mp4", "status": "ready",
                                              "meta": meta or {"width": 1920, "height": 1080, "fps": 30, "duration": 60.0, "has_audio": True}})


def _clip(pid, start=10.0, end=20.0, **extra):
    edit = extra.pop("edit", {})
    c = pipeline.new_clip(pid, {"start": start, "end": end, "title": extra.pop("title", "Clip")})
    return store.update("clip", c["id"], fn=lambda d: (d["edit"].update(edit), d.update(**extra)))


# ---------- 12. text cuts: validation ----------

def test_cuts_are_validated_clamped_merged_and_sorted():
    clip = {"start": 10.0, "end": 20.0}
    assert server.clean_edit({"cuts": None}, clip) == {"cuts": None}
    assert server.clean_edit({"cuts": []}, clip) == {"cuts": None}
    got = server.clean_edit({"cuts": [[15, 16], [5, 11], [15.5, 17], [19.5, 30], [12, 12.5]]}, clip)["cuts"]
    assert got == [[10.0, 11.0], [12.0, 12.5], [15.0, 17.0], [19.5, 20.0]]  # clamped into the clip, merged, sorted
    assert server.clean_edit({"cuts": [[1, 2], [25, 30]]}, clip)["cuts"] is None  # all outside the clip: nothing to cut
    for bad in ("x", [[1]], [[12, 11]], [["a", "b"]], [[12, 13]] * 501, [[10, 19.8]]):
        with pytest.raises(HTTPException):  # not a list, not pairs, s >= e, too many, or nothing left of the clip
            server.clean_edit({"cuts": bad}, clip)


def test_posted_clips_refuse_cuts():
    p = _project()
    c = _clip(p["id"])
    store.put("upload", {"platform": "youtube", "clip_id": c["id"], "project_id": p["id"], "status": "done"})
    r = _client().patch(f"/api/clips/{c['id']}", json={"edit": {"cuts": [[12, 13]]}})
    assert r.status_code == 409
    assert not store.get("clip", c["id"])["edit"].get("cuts")
    c2 = _clip(p["id"])
    r = _client().patch(f"/api/clips/{c2['id']}", json={"edit": {"cuts": [[12, 13]]}})
    assert r.status_code == 200 and r.json()["edit"]["cuts"] == [[12.0, 13.0]]


# ---------- 12. text cuts: render ----------

def _need_ffmpeg():
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")


def _source(pid):
    out = pipeline.pdir(pid) / "source.mp4"
    subprocess.run([media.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=12:size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=f=300:duration=12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(out)], check=True)
    return out


WORDS = [{"s": 1.2, "e": 1.6, "w": " one"}, {"s": 2.4, "e": 2.8, "w": " gone"}, {"s": 3.5, "e": 3.9, "w": " two"},
         {"s": 5.5, "e": 5.9, "w": " shit"}, {"s": 6.2, "e": 6.6, "w": " three"}]


def test_render_with_cuts_has_the_right_duration_and_captions_follow(monkeypatch):
    _need_ffmpeg()
    p = _project({"width": 320, "height": 240, "fps": 30, "duration": 12.0, "has_audio": True}, WORDS)
    _source(p["id"])
    c = _clip(p["id"], 1.0, 7.0, edit={"aspect": "source", "loudnorm": False, "cuts": [[2.0, 3.0], [4.0, 5.0]]})
    seen, real = {}, media.render
    monkeypatch.setattr(media, "render", lambda *a, **kw: (seen.update(ass=a[6], keeps=a[8]), real(*a, **kw))[1])
    rel = pipeline._render_clip(c, lambda f: None)
    assert seen["keeps"] == [(1.0, 2.0), (3.0, 4.0), (5.0, 7.0)]
    out = media.probe(pipeline.pdir(p["id"]) / rel)
    assert abs(out["duration"] - 4.0) < 0.15  # 6 s clip minus two 1 s cuts
    ass = seen["ass"].upper()
    assert "GONE" not in ass and "TWO" in ass and "THREE" in ass  # the cut word is gone from the captions
    starts = {ln.split(",")[1] for ln in seen["ass"].splitlines() if ln.startswith("Dialogue:") and "}TWO{" in ln.upper()}
    assert starts == {"0:00:01.50"}  # spoken at 3.5 s in the source = 1 s kept + 0.5 s into the second keep
    starts = {ln.split(",")[1] for ln in seen["ass"].splitlines() if ln.startswith("Dialogue:") and "}THREE{" in ln.upper()}
    assert starts == {"0:00:03.20"}  # 6.2 s = 1 + 1 + 1.2 s of output


def test_bleeps_map_through_cuts(monkeypatch):
    _need_ffmpeg()
    p = _project({"width": 320, "height": 240, "fps": 30, "duration": 12.0, "has_audio": True}, WORDS)
    _source(p["id"])
    c = _clip(p["id"], 1.0, 7.0, edit={"aspect": "source", "loudnorm": False, "bleep": "mute", "cuts": [[2.0, 3.0]]})
    seen, real = {}, media.build_filters
    monkeypatch.setattr(media, "build_filters", lambda *a, **kw: (seen.update(bleeps=kw.get("bleeps")), real(*a, **kw))[1])
    config.save({"bleep_words": "shit"})
    try:
        pipeline._render_clip(c, lambda f: None)
    finally:
        config.save({"bleep_words": config.DEFAULTS["bleep_words"]})
    [(a, b)] = seen["bleeps"]
    assert abs(a - 3.5) < 1e-6 and abs(b - 3.9) < 1e-6  # 5.5-5.9 s in the source, after 1 s of kept and a 1 s cut


# ---------- 11. triage queue ----------

def test_triage_lists_unjudged_ai_picks_and_decisions_reach_feedback():
    p = _project()
    pid = p["id"]
    low = _clip(pid, 1, 9, source="ai", score=40, title="low")
    high = _clip(pid, 10, 19, source="ai", score=90, title="high")
    _clip(pid, 20, 29, source="manual", score=99)            # the creator's own clip: not a pick to judge
    rated = _clip(pid, 30, 39, source="ai", score=80, rating=1)
    posted = _clip(pid, 40, 49, source="auto", score=95)
    store.put("upload", {"platform": "youtube", "clip_id": posted["id"], "project_id": pid, "status": "done"})
    cl = _client()
    got = cl.get("/api/triage", params={"project": pid}).json()
    assert [x["id"] for x in got["clips"]] == [high["id"], low["id"]] and got["total"] == 2  # best first
    card = got["clips"][0]
    assert card["project"] == "wp5" and card["start"] == 10 and card["end"] == 19 and card["src"].endswith("/source.mp4")
    assert [x["id"] for x in cl.get("/api/triage", params={"project": pid, "min_score": 50}).json()["clips"]] == [high["id"]]
    assert [x["id"] for x in cl.get("/api/triage", params={"project": pid, "new_only": 0}).json()["clips"]] == [high["id"], rated["id"], low["id"]]
    assert high["id"] in {x["id"] for x in cl.get("/api/triage").json()["clips"]}  # across all projects by default
    # Y = keep: the same PATCH as 👍, so the learning memory records it; U = undo drops the lesson again
    assert cl.patch(f"/api/clips/{high['id']}", json={"rating": 1}).status_code == 200
    assert store.get("feedback", high["id"])["signal"] == 1
    assert [x["id"] for x in cl.get("/api/triage", params={"project": pid}).json()["clips"]] == [low["id"]]
    cl.patch(f"/api/clips/{low['id']}", json={"rating": -1})
    assert store.get("feedback", low["id"])["signal"] == -1
    cl.patch(f"/api/clips/{low['id']}", json={"rating": 0})
    assert store.get("feedback", low["id"]) is None


# ---------- 40. NLE export ----------

def _tl(fps=30.0, keeps=((10.0, 12.0), (13.0, 14.5)), markers=(), words=()):
    return {"name": "My clip", "src": Path(tempfile.gettempdir()) / "source.mp4", "fps": fps, "width": 1920, "height": 1080,
            "duration": 60.0, "has_audio": True, "keeps": list(keeps), "markers": list(markers), "words": list(words)}


def test_fcpxml_is_well_formed_with_frame_exact_timings():
    x = nle.fcpxml(_tl(markers=[(0.5, "Hype", "marker"), (2.5, "Part 2", "chapter")]))
    root = ET.fromstring(x)
    assert root.tag == "fcpxml" and root.get("version") == "1.10"
    fmt = root.find("resources/format")
    assert fmt.get("frameDuration") == "1/30s" and fmt.get("width") == "1920"
    assert root.find("resources/asset/media-rep").get("src").startswith("file:")
    clips = root.findall(".//spine/asset-clip")
    assert [(c.get("offset"), c.get("start"), c.get("duration")) for c in clips] == [("0/30s", "300/30s", "60/30s"), ("60/30s", "390/30s", "45/30s")]
    assert root.find(".//sequence").get("duration") == "105/30s"
    m = clips[0].find("marker")
    assert m.get("start") == "315/30s" and m.get("value") == "Hype"  # in the asset-clip's own (source) time
    ch = clips[1].find("chapter-marker")
    assert ch.get("start") == "405/30s" and ch.get("value") == "Part 2"  # 2.5 s on the timeline = 0.5 s into the 2nd clip


def test_fcpxml_ntsc_rate():
    root = ET.fromstring(nle.fcpxml(_tl(fps=29.97, keeps=[(10.0, 12.0)])))
    assert root.find("resources/format").get("frameDuration") == "1001/30000s"
    c = root.find(".//spine/asset-clip")
    assert c.get("start") == f"{300 * 1001}/30000s" and c.get("duration") == f"{60 * 1001}/30000s"  # frame 300 = 10.01 s


def test_edl_timecodes_at_30_and_29_97():
    assert nle.timecode(0, 30.0) == "00:00:00:00" and nle.timecode(30 * 3661 + 5, 30.0) == "01:01:01:05"
    assert nle.timecode(1798, 29.97) == "00:00:59;28"     # drop frame: ;00 and ;01 are skipped each minute...
    assert nle.timecode(1800, 29.97) == "00:01:00;02"
    assert nle.timecode(17982, 29.97) == "00:10:00;00"    # ...except every tenth
    assert nle.timecode(107892, 29.97) == "01:00:00;00"
    edl = nle.edl(_tl(markers=[(0.5, "Hype", "marker")]))
    lines = edl.splitlines()
    assert lines[0] == "TITLE: My clip" and lines[1] == "FCM: NON-DROP FRAME"
    events = [ln.split() for ln in lines if ln[:3].isdigit()]
    assert events[0] == ["001", "AX", "V", "C", "00:00:10:00", "00:00:12:00", "01:00:00:00", "01:00:02:00"]
    assert events[2] == ["003", "AX", "V", "C", "00:00:13:00", "00:00:14:15", "01:00:02:00", "01:00:03:15"]
    assert "* FROM CLIP NAME: source.mp4" in lines and any(ln.startswith("* LOC: 01:00:00:15") and "Hype" in ln for ln in lines)
    edl = nle.edl(_tl(fps=29.97, keeps=[(60.0, 600.0)]))
    assert "FCM: DROP FRAME" in edl
    ev = next(ln.split() for ln in edl.splitlines() if ln.startswith("001"))
    assert ev[4:8] == ["00:00:59;28", "00:10:00;00", "01:00:00;00", "01:09:00;02"]  # 16184 frames of 29.97 later


def test_srt_matches_the_remapped_captions():
    words = [{"s": 10.2, "e": 10.6, "w": "hello"}, {"s": 12.2, "e": 12.6, "w": "cut"}, {"s": 13.1, "e": 13.4, "w": "world"}]
    keeps = [(10.0, 12.0), (13.0, 14.5)]
    srt = nle.srt(_tl(keeps=keeps, words=words))
    blocks = [b.splitlines() for b in srt.strip().split("\n\n")]
    assert "cut" not in srt
    texts = " ".join(b[2] for b in blocks)
    assert texts == "hello world"
    first, last = blocks[0][1].split(" --> "), blocks[-1][1].split(" --> ")
    assert first[0] == nle.srt_time(media.remap(10.2, keeps)) == "00:00:00,200"
    assert last[1] == nle.srt_time(media.remap(13.4, keeps)) == "00:00:02,400"


def test_clip_export_routes_use_the_render_keeps_and_markers():
    words = [{"s": 11.0, "e": 11.4, "w": " hi"}]
    p = _project(words=words)
    pipeline.save_json(p["id"], "signals.json", {"markers": [{"t": 15.0, "kind": "voice", "label": "clip that"}]})
    c = _clip(p["id"], 10.0, 20.0, edit={"cuts": [[12.0, 13.0]]})
    tl = nle.clip_timeline(store.get("clip", c["id"]))
    assert tl["keeps"] == [(10.0, 12.0), (13.0, 20.0)] and tl["markers"] == [(4.0, "clip that", "marker")]
    cl = _client()
    store.update("clip", c["id"], {"title": "日本 clip ✦"})  # non-Latin titles: an ASCII filename plus filename*
    for fmt, ctype in (("fcpxml", "xml"), ("edl", "text"), ("srt", "text")):
        r = cl.get(f"/api/clips/{c['id']}/nle/{fmt}")
        assert r.status_code == 200 and ctype in r.headers["content-type"] and "attachment" in r.headers["content-disposition"]
    assert cl.get(f"/api/clips/{c['id']}/nle/exe").status_code == 404
    assert "hi" in cl.get(f"/api/clips/{c['id']}/nle/srt").text


def test_longform_export_has_segments_and_chapters():
    from reelwright import longform
    p = _project()
    d = longform.create(p["id"], "highlights", "LF", [[5.0, 10.0], [30.0, 33.0]], [{"t": 0.0, "title": "Intro"}, {"t": 5.0, "title": "Later"}])
    tl = nle.longform_timeline(store.get("longform", d["id"]))
    assert tl["keeps"] == [(5.0, 10.0), (30.0, 33.0)]
    assert [(t, lbl) for t, lbl, _ in tl["markers"]] == [(0.0, "Intro"), (5.0, "Later")]
    root = ET.fromstring(_client().get(f"/api/longform/{d['id']}/nle/fcpxml").text)
    assert len(root.findall(".//spine/asset-clip")) == 2 and len(root.findall(".//chapter-marker")) == 2


# ---------- 12. undo stack (client side, static/undo.js) ----------

@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_undo_stack_logic_under_node():
    script = r"""
const U = require(process.argv[1]);
const out = {};
const prev = { start: 10, end: 20, title: "T", rating: 0, edit: { zoom: 1, framing: null, track: { manual: [[1, 0.5, 0.5]] } } };  // no cuts yet: the first cut
out.inv = U.inverse({ start: 11, rating: 1, edit: { zoom: 1.5, framing: { zoom: 2 }, track_manual: [], cuts: [[12, 13]], newkey: 1 } }, prev);
out.none = U.inverse({ rating: 1 }, prev);
const h = U.History(3, 1000);
h.push("a", { edit: { zoom: 1 } }, { edit: { zoom: 1.2 } }, 0);
h.push("a", { edit: { zoom: 1.2 } }, { edit: { zoom: 1.4 } }, 500);    // a slider drag: one step
h.push("a", { start: 10 }, { start: 11 }, 3000);
out.u1 = h.undo("a"); out.u2 = h.undo("a"); out.u3 = h.undo("a");
out.r1 = h.redo("a"); out.r2 = h.redo("a"); out.r3 = h.redo("a");
h.undo("a"); h.push("a", { end: 20 }, { end: 19 }, 9000); out.redoCleared = h.redo("a");
for (let i = 0; i < 5; i++) h.push("a", { end: i }, { end: i + 1 }, 20000 + i * 5000);
let n = 0; while (h.undo("a")) n++; out.capped = n;
h.push("a", { end: 1 }, { end: 2 }, 90000);
out.otherClip = h.undo("b"); out.afterSwitch = h.undo("a");   // navigating to another clip clears the history
h.push("c", { end: 1 }, { end: 2 }, 0); h.clear(); out.cleared = h.undo("c");
console.log(JSON.stringify(out));
"""
    res = subprocess.run(["node", "-e", script, str(STATIC / "undo.js")], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    o = json.loads(res.stdout)
    assert o["inv"] == {"start": 10, "edit": {"zoom": 1, "framing": {"offset_y": 0, "zoom": 1}, "track_manual": [[1, 0.5, 0.5]], "cuts": None}}
    assert o["none"] is None  # a rating is feedback, not an edit: nothing to undo
    assert o["u1"] == {"start": 10} and o["u2"] == {"edit": {"zoom": 1}} and o["u3"] is None
    assert o["r1"] == {"edit": {"zoom": 1.4}} and o["r2"] == {"start": 11} and o["r3"] is None
    assert o["redoCleared"] is None and o["capped"] == 3
    assert o["otherClip"] is None and o["afterSwitch"] is None and o["cleared"] is None
