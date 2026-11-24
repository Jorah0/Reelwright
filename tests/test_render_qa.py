"""WP2 Render & QA: music mix order + ducking, profanity bleep, animated captions, the render queue, the encoder chain
(and LGPL filter substitutes), the pre-publish QA lint, preview proxies and the LGPL ffmpeg fetch."""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reelwright import config, doctor, learn, longform, media, pipeline, qa, server, store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def need_ffmpeg():
    if subprocess.run([media.FFMPEG, "-version"], capture_output=True).returncode:
        pytest.skip("no ffmpeg")


def ff(*args):
    subprocess.run([media.FFMPEG, "-v", "error", "-y", *args], check=True)


def band_db(path, a: float, b: float, f: int) -> float:
    """RMS level (dB) of one frequency band of a file's audio over [a, b] s (astats)."""
    r = subprocess.run([media.FFMPEG, "-hide_banner", "-i", str(path), "-af",
                        f"atrim={a}:{b},bandpass=f={f}:width_type=q:w=20,astats=metadata=0", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return float(re.findall(r"RMS level dB: (\S+)", r.stderr)[-1])


def speech_video(path, seconds=4, speech="lt(t,2)", size="320x180"):
    """A test picture with a 300 Hz 'voice' at a speaking level (0.3 peak) while `speech` (an ffmpeg expression) holds."""
    ff("-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=10",
       "-f", "lavfi", "-i", f"sine=f=300:r=48000:d={seconds},volume='if({speech},2.4,0)':eval=frame",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", str(path))
    return path


def music_file(path, seconds=12):
    ff("-f", "lavfi", "-i", f"sine=f=1000:r=48000:d={seconds},volume=2", "-ac", "2", str(path))  # 0.25 peak, 1 kHz
    return path


META = {"width": 320, "height": 180, "fps": 10, "has_audio": True}


# ---------- 1. mix order and music ducking ----------

def test_music_is_mixed_first_then_loudness_and_fades_over_the_whole_mix():
    g, _ = media.build_filters({"aspect": "9:16", "fade_in": 0.5, "fade_out": 1.0}, 1920, 1080, 10, False, music=True)
    audio = g.split("[v];", 1)[1]
    assert audio.index("amix") < audio.index("loudnorm") < audio.index("afade=t=in") < audio.index("afade=t=out")
    assert "sidechaincompress=threshold=0.03:ratio=8:attack=15:release=350" in audio  # music_duck defaults on
    assert audio.count("amix") == 1 and audio.endswith("[a]")
    ovs = [{"kind": "sound", "a": 1.0, "b": 2.0, "d": 1.0, "input": 2, "volume": 0.8}]
    g2, _ = media.build_filters({"aspect": "9:16"}, 1920, 1080, 10, False, music=True, overlays=ovs)
    assert g2.count("amix") == 1 and "amix=inputs=3" in g2 and g2.index("amix") < g2.index("loudnorm")


def test_music_duck_off_mixes_without_a_sidechain():
    g, _ = media.build_filters({"aspect": "9:16", "music_duck": False}, 1920, 1080, 10, False, music=True)
    assert "sidechaincompress" not in g and g.index("amix") < g.index("loudnorm")


def test_music_only_clip_is_normalised_and_faded():
    g, _ = media.build_filters({"aspect": "9:16", "fade_out": 1.0}, 1920, 1080, 10, False, has_audio=False, music=True)
    assert g.endswith("asetpts=PTS-STARTPTS,loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st=9.00:d=1.00[a]")


def test_graphs_without_the_new_options_are_byte_identical():
    assert media.build_filters({"aspect": "9:16", "fade_out": 1.0}, 1920, 1080, 10, True)[0] == (
        "[0:v]crop=608:1080:656:0,scale=1080:1920,setsar=1,fade=t=out:st=9.00:d=1.00,subtitles=captions.ass,format=yuv420p[v];"
        "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st=9.00:d=1.00[a]")
    ovs = [{"kind": "sound", "a": 1.0, "b": 2.0, "d": 1.0, "input": 1, "volume": 0.8}]
    assert media.build_filters({"aspect": "9:16", "fade_in": 0.5}, 1920, 1080, 10, False, overlays=ovs)[0] == (
        "[0:v]crop=608:1080:656:0,scale=1080:1920,setsar=1,fade=t=in:st=0:d=0.50,format=yuv420p[v];"
        "[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];[1:a]atrim=0:1.000,asetpts=PTS-STARTPTS,aresample=48000,"
        "aformat=channel_layouts=stereo,volume=0.80,adelay=1000:all=1[s0];[a0][s0]amix=inputs=2:duration=first:"
        "dropout_transition=0:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.50[a]")
    assert media.build_filters({"aspect": "9:16", "contrast": 1.2, "fit": "blur"}, 1920, 1080, 10, False)[0] == (
        "[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:2[bg];"
        "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,"
        "eq=brightness=0.000:contrast=1.200:saturation=1.000:gamma=1.000,format=yuv420p[v];[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]")


def test_ducking_lowers_the_music_under_speech(tmp_path):
    need_ffmpeg()
    src, mus = speech_video(tmp_path / "src.mp4"), music_file(tmp_path / "m.wav")
    out = {}
    for duck in (True, False):
        out[duck] = tmp_path / f"o{duck}.mp4"
        media.render(src, out[duck], 0.0, 4.0, {"aspect": "9:16", "loudnorm": False, "music_volume": 1.0, "music_duck": duck},
                     META, None, music=mus)
    under, gap = band_db(out[True], 0.4, 1.6, 1000), band_db(out[True], 2.7, 3.7, 1000)
    assert gap - under >= 6, (under, gap)  # the music dips while someone talks...
    plain_under, plain_gap = band_db(out[False], 0.4, 1.6, 1000), band_db(out[False], 2.7, 3.7, 1000)
    assert abs(plain_gap - plain_under) < 2, (plain_under, plain_gap)  # ...and only because of the duck
    assert abs(gap - plain_gap) < 2  # back to its set level in the pauses


def test_fades_apply_to_the_music(tmp_path):
    need_ffmpeg()
    src, mus = speech_video(tmp_path / "src.mp4"), music_file(tmp_path / "m.wav")
    out = tmp_path / "o.mp4"
    media.render(src, out, 0.0, 4.0, {"aspect": "9:16", "music_volume": 1.0, "fade_out": 1.0}, META, None, music=mus)
    mid, tail = band_db(out, 2.4, 2.9, 1000), band_db(out, 3.8, 3.97, 1000)
    assert mid - tail >= 12, (mid, tail)  # before the fix the music played on at full level to the last frame


def test_longform_join_ducks_and_fades_the_music_over_the_programme(tmp_path, monkeypatch):
    need_ffmpeg()
    parts = []
    for i in range(2):
        p = tmp_path / f"part_{i}.mp4"
        speech_video(p, 2, "lt(t,1)")
        parts.append(p)
    mus = music_file(tmp_path / "m.wav")
    e = {**media.DEFAULT_EDIT, "music_volume": 1.0, "fade_out": 0.8}
    seen = []
    real = subprocess.run
    monkeypatch.setattr(longform.subprocess, "run", lambda args, **kw: seen.append(args) or real(args, **kw))
    longform._join(parts, tmp_path / "lf.mp4", e, mus, True, 4.0)
    g = seen[-1][seen[-1].index("-filter_complex") + 1]
    assert "sidechaincompress" in g and g.index("amix") < g.index("loudnorm") < g.index("afade=t=out:st=3.20:d=0.80")
    assert band_db(tmp_path / "lf.mp4", 1.5, 1.9, 1000) - band_db(tmp_path / "lf.mp4", 3.85, 3.98, 1000) >= 12


# ---------- 26. profanity bleep ----------

def test_bleep_words_match_whole_words_and_wildcards():
    words = [{"s": 1.0, "e": 1.4, "w": " What"}, {"s": 1.5, "e": 1.9, "w": " the"}, {"s": 2.0, "e": 2.4, "w": " FUCKING,"},
             {"s": 2.5, "e": 2.9, "w": " shitake"}, {"s": 3.0, "e": 3.4, "w": " shit!"}, {"s": 9.0, "e": 9.4, "w": " shit"}]
    bad = media.bleep_list("fuck*, Shit ,, ")
    assert bad == ["fuck*", "shit"]
    assert media.bleep_spans(words, 0.0, 5.0, bad) == [(2.0, 2.4), (3.0, 3.4)]  # whole words only; inside the clip
    censored = media.censor(words, bad)
    assert [w["w"] for w in censored][2:5] == [" F******,", " shitake", " s***!"] and words[2]["w"] == " FUCKING,"


def test_bleep_beeps_or_mutes_exactly_over_the_word(tmp_path):
    need_ffmpeg()
    src = speech_video(tmp_path / "src.mp4", 4, "1")  # talking the whole time
    for mode in ("beep", "mute"):
        out = tmp_path / f"{mode}.mp4"
        # source 1.0-1.5 s = 0.5-1.0 s of a clip starting at 0.5
        media.render(src, out, 0.5, 3.5, {"aspect": "9:16", "loudnorm": False, "bleep": mode}, META, None, bleeps=[(1.0, 1.5)])
        assert band_db(out, 0.6, 0.9, 300) < -45  # the word is gone...
        assert band_db(out, 1.3, 2.3, 300) > -25  # ...the rest of the speech isn't
        if mode == "beep":
            assert band_db(out, 0.6, 0.9, 1000) > -30 and band_db(out, 1.3, 2.3, 1000) < -45  # a beep only over the word
        else:
            assert band_db(out, 0.6, 0.9, 1000) < -45


def test_bleep_off_or_no_spans_leaves_the_graph_alone():
    plain = media.build_filters({"aspect": "9:16"}, 1920, 1080, 10, False)[0]
    assert media.build_filters({"aspect": "9:16", "bleep": "off"}, 1920, 1080, 10, False, bleeps=[(1, 2)])[0] == plain
    assert media.build_filters({"aspect": "9:16", "bleep": "beep"}, 1920, 1080, 10, False, bleeps=[])[0] == plain
    g = media.build_filters({"aspect": "9:16", "bleep": "beep"}, 1920, 1080, 10, False, bleeps=[(1, 1.5)])[0]
    assert "volume=0:enable='between(t,1.000,1.500)'" in g and "sine=f=1000" in g and g.index("amix") < g.index("loudnorm")


def _project_with_clip(tmp_path, edit, words):
    p = pipeline.new_project("wp2")
    src = pipeline.pdir(p["id"]) / "source.mp4"
    src.write_bytes(b"x")
    store.update("project", p["id"], {"source": "source.mp4", "status": "ready",
                                      "meta": {"width": 1920, "height": 1080, "fps": 30, "duration": 60, "has_audio": True}})
    pipeline.save_json(p["id"], "transcript.json", {"segments": [{"start": 0, "end": 60, "text": "", "words": words}]})
    return p, store.put("clip", {"project_id": p["id"], "start": 10.0, "end": 20.0, "title": "t", "edit": edit, "rev": 1})


def test_render_clip_bleeps_spoken_words_and_censors_captions(tmp_path, monkeypatch):
    words = [{"s": 11.0, "e": 11.4, "w": " oh"}, {"s": 11.5, "e": 11.9, "w": " shit"}, {"s": 12.0, "e": 12.4, "w": " wow"}]
    _, clip = _project_with_clip(tmp_path, {"aspect": "9:16", "bleep": "beep", "caption_anim": "pop"}, words)
    seen = {}
    monkeypatch.setattr(media, "render", lambda *a, **kw: seen.update(ass=a[6], kw=kw))
    config.save({"bleep_words": "shit"})
    try:
        pipeline._render_clip(clip, lambda f: None)
    finally:
        config.save({"bleep_words": config.DEFAULTS["bleep_words"]})
    assert seen["kw"]["bleeps"] == [(11.5, 11.9)]
    assert "S***" in seen["ass"] and "SHIT" not in seen["ass"] and "\\t(" in seen["ass"]  # censored, and animated


# ---------- 13. animated captions ----------

WORDS = [{"s": 10.0 + i * 0.5, "e": 10.4 + i * 0.5, "w": w} for i, w in enumerate([" We", " made", " 100", " INSANE", " unbelievable", " plays."])]


def _cap_lines(ass):
    return [line for line in ass.splitlines() if line.startswith("Dialogue:") and ",Cap," in line]


def test_highlight_is_the_default_and_unchanged():
    base = media.build_ass(WORDS, 10.0, 13.0, 1.0, (1080, 1920))
    assert media.build_ass(WORDS, 10.0, 13.0, 1.0, (1080, 1920), anim="highlight", emphasis=False) == base
    assert "Dialogue: 0,0:00:00.00,0:00:00.50,Cap,,0,0,0,,{\\c&H0000D4FF&}WE{\\c&H00FFFFFF&} MADE 100" in base


@pytest.mark.parametrize("anim,tag", [("pop", "\\fscx125\\fscy125\\t(0,120,\\fscx100\\fscy100)"),
                                      ("bounce", "\\fscx70\\fscy70\\t(0,90,\\fscx120\\fscy120)\\t(90,180,\\fscx100\\fscy100)")])
def test_pop_and_bounce_animate_the_spoken_word(anim, tag):
    lines = _cap_lines(media.build_ass(WORDS, 10.0, 13.0, 1.0, (1080, 1920), anim=anim))
    assert len(lines) == 6 and all(line.count(tag) == 1 for line in lines)  # one animated word per event: the spoken one
    assert "{\\c&H0000D4FF&" + tag + "}WE{\\c&H00FFFFFF&\\fscx100\\fscy100} MADE" in lines[0]


def test_reveal_hides_words_until_they_are_spoken():
    lines = _cap_lines(media.build_ass(WORDS, 10.0, 13.0, 1.0, (1080, 1920), anim="reveal"))
    assert "{\\c&H00FFFFFF&}{\\alpha&HFF&} MADE 100" in lines[0] and "\\alpha&HFF&" not in lines[-1]


def test_box_captions_draw_a_box_behind_the_spoken_word_only():
    ass = media.build_ass(WORDS, 10.0, 13.0, 1.0, (1080, 1920), anim="box")
    cap = next(line for line in ass.splitlines() if line.startswith("Style: Cap,")).split(",")
    box = next(line for line in ass.splitlines() if line.startswith("Style: CapBox,")).split(",")
    assert box[1:3] == cap[1:3] and box[11:15] == cap[11:15] and box[18:] == cap[18:]  # same font, size, scale, spacing, place
    assert box[15] == "3" and box[5] == "&H0000D4FF&"  # an opaque box in the highlight colour
    boxes = [line for line in ass.splitlines() if ",CapBox," in line]
    assert len(boxes) == 6 and all(line.startswith("Dialogue: 0,") for line in boxes)
    assert all(line.startswith("Dialogue: 1,") for line in ass.splitlines() if ",Cap," in line)  # text over its box
    assert "{\\1a&HFF&\\3a&H00&}MADE{\\alpha&HFF&}" in boxes[1] and boxes[1].count("\\alpha&HFF&") == 2


def test_caption_emphasis_marks_numbers_shouting_and_long_words():
    ass = media.build_ass(WORDS, 10.0, 13.0, 1.0, (1080, 1920), emphasis=True)
    first, last = _cap_lines(ass)[0], _cap_lines(ass)[-1]  # three-word chunks: "WE MADE 100", "INSANE UNBELIEVABLE PLAYS."
    emph = "{\\c&H0000D4FF&\\fscx112\\fscy112}"
    assert f"{emph}100{{\\c&H00FFFFFF&\\fscx100\\fscy100}}" in first and f"{emph}INSANE" in last and f"{emph}UNBELIEVABLE" in last
    assert " MADE " in first and f"{emph}MADE" not in first and f"{emph}PLAYS." not in last  # ordinary words stay plain


def test_animated_captions_render_through_libass(tmp_path):
    need_ffmpeg()
    src = speech_video(tmp_path / "s.mp4", 3)
    for anim in ("pop", "box"):
        ass = media.build_ass(WORDS, 10.0, 13.0, 1.0, (1080, 1920), anim=anim, emphasis=True)
        out = tmp_path / f"{anim}.mp4"
        media.render(src, out, 0.0, 3.0, {"aspect": "9:16", "loudnorm": False}, META, ass)
        assert media.probe(out)["height"] == 1920


def test_new_edit_keys_validate_at_the_api():
    clip = {"start": 0, "end": 10, "project_id": ""}
    out = server.clean_edit({"caption_anim": "bounce", "caption_emphasis": 1, "music_duck": 0, "bleep": "mute"}, clip)
    assert out == {"caption_anim": "bounce", "caption_emphasis": True, "music_duck": False, "bleep": "mute"}
    for bad in ({"caption_anim": "wiggle"}, {"bleep": "loud"}):
        with pytest.raises(HTTPException):
            server.clean_edit(bad, clip)


def test_new_style_keys_are_learned_and_old_lessons_still_load():
    e = {"caption_anim": "pop", "caption_emphasis": True, "bleep": "beep", "music_duck": False}
    assert learn.clean_edit({**e, "aspect": "9:16"}) == {**e, "aspect": "9:16"}
    assert learn.clean_edit({"caption_anim": "wiggle", "bleep": 3}) == {}
    fb = [{"signal": 1, "edit": {"caption_anim": "box", "aspect": "9:16"}} for _ in range(3)] + [{"signal": 1, "edit": {"aspect": "9:16"}}]
    assert learn.house_style(fb)["caption_anim"] == "box"
    assert "caption_anim" not in learn.house_style([{"signal": 1, "edit": {"aspect": "9:16"}}] * 4)  # old lessons: default


# ---------- 2. render queue beside analysis ----------

def _queue_rig(monkeypatch, parallel):
    heavy, render = __import__("queue").Queue(), __import__("queue").Queue()
    monkeypatch.setattr(pipeline, "_heavy", heavy)
    monkeypatch.setattr(pipeline, "_render", render)
    for k in ("export", "longform_export", "proxy", "track"):
        monkeypatch.setitem(pipeline.QUEUE_OF, k, render)
    monkeypatch.setattr(pipeline, "parallel_render", lambda: parallel)
    release, events = threading.Event(), []
    monkeypatch.setitem(pipeline.HANDLERS, "analyze", lambda job: (events.append("analyze start"), release.wait(10), events.append("analyze end")))
    monkeypatch.setitem(pipeline.HANDLERS, "export", lambda job: events.append("render"))
    for q, gate in ((heavy, pipeline._analysis_gate), (render, pipeline._render_gate)):
        threading.Thread(target=pipeline._worker, args=(q, gate), daemon=True).start()
    return release, events


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_render_queue_runs_a_render_while_analysis_runs(monkeypatch):
    release, events = _queue_rig(monkeypatch, parallel=True)
    pipeline.submit("analyze", project_id="p1")
    assert _wait(lambda: "analyze start" in events)
    pipeline.submit("export", clip_id="c1")
    assert _wait(lambda: "render" in events)  # done while the analysis is still running
    assert "analyze end" not in events
    release.set()
    assert _wait(lambda: "analyze end" in events)


def test_render_waits_for_analysis_when_parallel_render_is_off(monkeypatch):
    release, events = _queue_rig(monkeypatch, parallel=False)
    pipeline.submit("analyze", project_id="p1")
    assert _wait(lambda: "analyze start" in events)
    pipeline.submit("export", clip_id="c1")
    time.sleep(0.3)
    assert "render" not in events
    release.set()
    assert _wait(lambda: events[-1] == "render") and events.index("analyze end") < events.index("render")


def test_render_kinds_route_to_the_render_queue_and_parallel_auto(monkeypatch):
    for k in ("export", "longform_export", "proxy", "track"):
        assert pipeline.QUEUE_OF[k] is pipeline._render
    assert pipeline.QUEUE_OF.get("analyze", pipeline._heavy) is pipeline._heavy
    assert config.DEFAULTS["parallel_render"] == "auto"
    monkeypatch.setattr(media, "_has_nvenc", lambda: False)
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 4)
    assert pipeline.parallel_render() is False
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 16)
    assert pipeline.parallel_render() is True
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(media, "_has_nvenc", lambda: True)
    assert pipeline.parallel_render() is True
    config.save({"parallel_render": "off"})
    try:
        assert pipeline.parallel_render() is False
    finally:
        config.save({"parallel_render": "auto"})


# ---------- 15. encoder chain, LGPL filters, doctor ----------

def test_encoder_chain_order_and_fallback():
    all_names = {"h264_nvenc", "h264_qsv", "h264_amf", "libx264", "libopenh264"}
    assert [n for n, _ in media._chain(all_names, lambda n: True)] == ["h264_nvenc", "h264_qsv", "h264_amf", "libx264", "libopenh264"]
    # hardware listed but its 1-frame test encode fails (no such GPU / old driver): skipped; software trusted when listed
    assert [n for n, _ in media._chain(all_names, lambda n: n == "h264_amf")] == ["h264_amf", "libx264", "libopenh264"]
    assert [n for n, _ in media._chain({"h264_qsv", "libopenh264"}, lambda n: False)] == ["libopenh264"]  # an LGPL build
    assert media._chain({"h264_mf"}, lambda n: True) == ()


def test_encode_without_any_encoder_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "encoders", lambda: ())
    with pytest.raises(RuntimeError, match="No usable H.264 encoder"):
        media._encode([media.FFMPEG], tmp_path / "o.mp4", tmp_path, 1.0, None)


def test_encode_falls_through_to_the_next_encoder(tmp_path, monkeypatch):
    need_ffmpeg()
    src = speech_video(tmp_path / "s.mp4", 1)
    monkeypatch.setattr(media, "encoders", lambda: (("broken", ["-c:v", "no_such_encoder"]), ("mpeg4", ["-c:v", "mpeg4"])))
    media.render(src, tmp_path / "o.mp4", 0, 1, {"aspect": "source", "loudnorm": False}, META, None)
    assert media.probe(tmp_path / "o.mp4")["duration"] > 0.5


def test_nvenc_probe_is_a_real_one_frame_encode(monkeypatch):
    calls = []
    monkeypatch.setattr(media, "_run", lambda args, **kw: calls.append(args))
    media._probes.clear()
    try:
        assert media._encoder_works("h264_nvenc") is True
        assert "-frames:v" in calls[0] and calls[0][calls[0].index("-frames:v") + 1] == "1" and "h264_nvenc" in calls[0]
        monkeypatch.setattr(media, "_run", lambda args, **kw: (_ for _ in ()).throw(RuntimeError("no device")))
        media._probes.clear()
        assert media._encoder_works("h264_nvenc") is False
        monkeypatch.setattr(media, "_run", lambda args, **kw: calls.append(args))
        assert media._encoder_works("h264_nvenc") is False  # a failure is remembered for a while...
        media._probes["h264_nvenc"] = (False, time.monotonic() - media.PROBE_RETRY - 1)
        assert media._encoder_works("h264_nvenc") is True  # ...then probed again (a busy GPU, a driver reset)
    finally:
        media._probes.clear()


def test_lgpl_builds_get_substitutes_for_gpl_only_filters(tmp_path, monkeypatch):
    need_ffmpeg()
    lgpl = media._filter_names() - {"boxblur", "eq"}
    monkeypatch.setattr(media, "_filter_names", lambda: lgpl)
    g, _ = media.build_filters({"aspect": "9:16", "fit": "blur", "contrast": 1.2, "gamma": 1.1}, 320, 180, 2, False)
    assert "boxblur" not in g and "gblur=sigma=20.0" in g and "eq=" not in g and "lutyuv=" in g
    src = speech_video(tmp_path / "s.mp4", 1)
    media.render(src, tmp_path / "o.mp4", 0, 1, {"aspect": "9:16", "fit": "blur", "contrast": 1.2, "loudnorm": False}, META, None)
    assert media.probe(tmp_path / "o.mp4")["height"] == 1920


def test_doctor_reports_usable_encoders_licence_and_missing_filters(monkeypatch):
    monkeypatch.setattr(media, "encoders", lambda: (("libopenh264", []),))
    st, detail, _ = doctor._encoders()
    assert st == doctor.WARN and "libopenh264" in detail
    monkeypatch.setattr(media, "encoders", lambda: (("h264_nvenc", []), ("libx264", [])))
    st, detail, _ = doctor._encoders()
    assert st == doctor.OK and "h264_nvenc" in detail and "libx264" in detail
    monkeypatch.setattr(media, "encoders", lambda: ())
    assert doctor._encoders()[0] == doctor.FAIL
    monkeypatch.setattr(media, "_filter_names", lambda: {"subtitles", "loudnorm"})
    st, detail, fix = doctor._ffmpeg()
    assert st == doctor.FAIL and "sidechaincompress" in detail and fix


def test_lgpl_fetch_verifies_the_checksum_and_extracts_the_tools(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_exe
    z = tmp_path / "src" / "ff.zip"
    z.parent.mkdir()
    with zipfile.ZipFile(z, "w") as f:
        for n in ("ffmpeg", "ffprobe", "ffplay"):
            f.writestr(f"ffmpeg-x-win64-lgpl/bin/{n}.exe", n)
        f.writestr("ffmpeg-x-win64-lgpl/LICENSE.txt", "LGPL")
    good = hashlib.sha256(z.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="checksum"):
        build_exe.fetch_lgpl(tmp_path / "cache", z.as_uri(), "0" * 64)
    assert not list((tmp_path / "cache").glob("*.zip"))  # a bad download is never kept
    tools = build_exe.fetch_lgpl(tmp_path / "cache", z.as_uri(), good)
    assert sorted(p.name for p in tools) == ["LICENSE.txt", "ffmpeg.exe", "ffprobe.exe"]
    assert build_exe.fetch_lgpl(tmp_path / "cache", "file:///nowhere.zip", good) == tools  # cached: no second download


# ---------- 8. pre-publish QA lint ----------

def _video(path, vf="null", af="anull", seconds=4, size="1080x1920"):
    ff("-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size={size}:rate=10", "-f", "lavfi",
       "-i", f"sine=f=440:r=48000:d={seconds}", "-vf", vf, "-af", af, "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(path))
    return path


def _codes(issues):
    return {i["code"] for i in issues}


def test_qa_catches_a_black_segment_and_clipping(tmp_path):
    need_ffmpeg()
    bad = _video(tmp_path / "bad.mov", vf="drawbox=enable='between(t,1.5,2.6)':color=black:t=fill", af="volume=10")
    clip = {"edit": {"aspect": "9:16", "captions": False}, "project_id": ""}
    issues = qa.lint(clip, bad)
    assert {"black", "clipping"} <= _codes(issues), issues
    assert all({"code", "level", "msg", "fix", "platform"} <= i.keys() for i in issues)
    good = _video(tmp_path / "good.mov", af="volume=2")
    assert not {"black", "clipping", "frozen"} & _codes(qa.lint(clip, good))


def test_qa_flags_a_frozen_picture_and_a_silent_opening(tmp_path):
    need_ffmpeg()
    v = _video(tmp_path / "v.mov", vf="trim=0:0.5,tpad=stop_mode=clone:stop_duration=4", af="volume='if(lt(t,2.2),0,1)':eval=frame")
    codes = _codes(qa.lint({"edit": {"captions": False}, "project_id": ""}, v))
    assert {"frozen", "no_hook"} <= codes


def test_qa_platform_length_and_size_limits():
    codes = lambda **kw: {(i["code"], i["platform"]) for i in qa.limits(**kw)}  # noqa: E731
    assert ("too_long", "youtube") in codes(duration=200, size=1e6, vertical=True)
    assert ("too_long", "youtube") not in codes(duration=200, size=1e6, vertical=False)  # not a Short: a normal upload
    assert ("too_short", "instagram") in codes(duration=2, size=1e6, vertical=True)
    assert ("too_big", "instagram") in codes(duration=60, size=301e6, vertical=True)
    assert ("too_long", "tiktok") in codes(duration=90, size=1e6, vertical=True, account={"max_video_post_duration_sec": 60})
    assert not codes(duration=40, size=20e6, vertical=True)
    assert all(i["level"] == "error" for i in qa.limits(duration=2, size=301e6, vertical=True))


def test_qa_safe_zones_flag_text_under_the_app_ui():
    ok = qa.safe_zones({"edit": {"aspect": "9:16"}, "project_id": ""}, 1080, 1920)
    assert not ok  # captions low (24 % up) clear every app's bottom UI
    hit = qa.safe_zones({"edit": {"aspect": "9:16", "sticker_text": "@me", "sticker_pos": "top-right"}, "project_id": ""}, 1080, 1920)
    assert {i["platform"] for i in hit if i["code"] == "safe_zone"} >= {"instagram"}
    assert not qa.safe_zones({"edit": {"aspect": "16:9", "sticker_text": "@me"}, "project_id": ""}, 1920, 1080)  # not vertical


def test_export_runs_the_lint_and_stores_it(tmp_path, monkeypatch):
    need_ffmpeg()
    _, clip = _project_with_clip(tmp_path, {"aspect": "9:16"}, [])

    def fake_render(c, prog):
        rel = f"exports/{c['id']}.mp4"
        (pipeline.pdir(c["project_id"]) / "exports").mkdir(exist_ok=True)
        _video(pipeline.pdir(c["project_id"]) / rel.replace(".mp4", ".mov"), vf="drawbox=enable='between(t,1,2.2)':color=black:t=fill")
        os.replace(pipeline.pdir(c["project_id"]) / rel.replace(".mp4", ".mov"), pipeline.pdir(c["project_id"]) / rel)
        return rel
    monkeypatch.setattr(pipeline, "_render_clip", fake_render)
    job = store.put("job", {"kind": "export", "clip_id": clip["id"], "status": "running"})
    pipeline._export(job)
    store.update("job", job["id"], {"status": "done"})
    exp = store.get("clip", clip["id"])["export"]
    assert exp["status"] == "done" and "black" in _codes(exp["qa"])


# ---------- 18. preview proxies ----------

def test_needs_proxy_for_hard_to_scrub_sources():
    base = {"vcodec": "h264", "container": "mov,mp4,m4a,3gp,3g2,mj2", "width": 1920, "height": 1080, "fps": 30, "duration": 600}
    assert not media.needs_proxy(base)
    for k, v in (("vcodec", "hevc"), ("vcodec", "av1"), ("container", "matroska,webm"), ("height", 2160), ("fps", 120),
                 ("duration", 3 * 3600)):
        assert media.needs_proxy({**base, k: v}), k
    assert media.needs_proxy({**base, "width": 1080, "height": 1920}) is False  # vertical 1080p is fine


def test_proxy_is_540p_with_a_one_second_gop_and_no_b_frames(tmp_path):
    need_ffmpeg()
    src = speech_video(tmp_path / "s.mp4", 4, size="1280x720")
    out = tmp_path / "proxy.mp4"
    media.proxy(src, out)
    m = media.probe(out)
    assert m["height"] == 540 and m["has_audio"] and m["vcodec"] == "h264"
    probe = lambda *a: subprocess.run([media.FFPROBE, "-v", "error", "-select_streams", "v:0", *a, "-of", "csv=p=0", str(out)],  # noqa: E731
                                      capture_output=True, text=True).stdout.split()
    keys = [float(t.strip(",")) for t in probe("-skip_frame", "nokey", "-show_entries", "frame=pts_time")]
    assert len(keys) >= 4 and max(b - a for a, b in zip(keys, keys[1:])) <= 1.05  # a keyframe every second: instant seeks
    assert probe("-show_entries", "stream=has_b_frames") == ["0"]


def test_proxy_route_queues_one_job_and_the_editor_gets_the_proxy(tmp_path):
    need_ffmpeg()
    p = pipeline.new_project("proxy")
    src = speech_video(pipeline.pdir(p["id"]) / "source.mkv", 2, size="640x360")
    store.update("project", p["id"], {"source": "source.mkv", "status": "ready", "meta": {**media.probe(src)}})
    c = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    for j in store.find("job"):  # other tests leave renders queued (no worker runs here): a proxy never jumps ahead of them
        if j["kind"] in ("export", "longform_export") and j["status"] in ("queued", "running"):
            store.update("job", j["id"], {"status": "done"})
    render = store.put("job", {"kind": "export", "clip_id": "x", "status": "queued"})
    assert c.post(f"/api/projects/{p['id']}/proxy").json() == {"proxy": None, "needed": True}  # waits for the render
    store.update("job", render["id"], {"status": "done"})
    r = c.post(f"/api/projects/{p['id']}/proxy").json()
    assert r["needed"] and r["job"]["kind"] == "proxy"
    assert c.post(f"/api/projects/{p['id']}/proxy").json()["job"]["id"] == r["job"]["id"]  # not queued twice
    pipeline.HANDLERS["proxy"](r["job"])
    proj = store.get("project", p["id"])
    assert proj["proxy"] == "proxy.mp4" and media.probe(pipeline.pdir(p["id"]) / "proxy.mp4")["height"] == 360
    assert c.post(f"/api/projects/{p['id']}/proxy").json() == {"proxy": "proxy.mp4", "needed": True}
    small = pipeline.new_project("plain")
    speech_video(pipeline.pdir(small["id"]) / "source.mp4", 1)
    store.update("project", small["id"], {"source": "source.mp4", "status": "ready", "meta": media.probe(pipeline.pdir(small["id"]) / "source.mp4")})
    assert c.post(f"/api/projects/{small['id']}/proxy").json() == {"proxy": None, "needed": False}
