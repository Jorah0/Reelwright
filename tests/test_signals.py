"""WP1 signals: Whisper tuning (batched, hotwords, language pin, windowed RAM), "clip that" voice markers and the
markers.json sidecar, chat emote kinds (YouTube replay + live Twitch IRC capture), Twitch viewer clips, audio events,
the new transcript tags and learning features — and old signals/feedback still working. No network: Twitch HTTP and
IRC are fakes, Whisper and the audio tagger are fakes."""
import json
import os
import tempfile
import threading

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402

from reelwright import ai, chatlog, config, learn, media, pipeline, signals, store, twitch  # noqa: E402

SR = 16000


# ---------- Whisper ----------

class _Word:
    def __init__(self, s, e, w):
        self.start, self.end, self.word = s, e, w


class _Seg:
    def __init__(self, words):
        self.words = words
        self.start, self.end = words[0].start, words[-1].end
        self.text = "".join(w.word for w in words)


class _Info:
    language = "en"


def _settings(monkeypatch, **kw):
    base = config.load()
    monkeypatch.setattr(config, "load", lambda: {**base, **kw})


def test_transcribe_passes_hotwords_language_pin_and_batches_on_cuda(monkeypatch):
    calls = []

    class Model:
        def transcribe(self, audio, **kw):
            calls.append(("model", kw))
            return iter([_Seg([_Word(1.0, 1.5, " hi")])]), _Info()

    class Batched:
        def __init__(self, model):
            self.model = model

        def transcribe(self, audio, **kw):
            calls.append(("batched", kw))
            return iter([_Seg([_Word(1.0, 1.5, " hi")])]), _Info()

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "BatchedInferencePipeline", Batched)
    monkeypatch.setattr(ai, "_model", lambda size, device: Model())
    monkeypatch.setattr(media, "pcm", lambda src, start=0.0, dur=None, sr=SR: np.zeros(SR, np.float32))
    _settings(monkeypatch, whisper_device="cuda", whisper_model="large-v3-turbo", whisper_language="de")
    out = ai.transcribe("x.mp4", 60.0, vocab="Valorant, Jett, KEKW")
    kind, kw = calls[-1]
    assert kind == "batched" and kw["batch_size"] == ai.BATCH and kw["hotwords"] == "Valorant, Jett, KEKW"
    assert kw["language"] == "de" and kw["word_timestamps"] and kw["vad_filter"] and kw["beam_size"] == 2
    assert out["device"] == "cuda" and out["segments"][0]["words"][0] == {"s": 1.0, "e": 1.5, "w": " hi"}

    _settings(monkeypatch, whisper_device="cpu", whisper_model="small", whisper_language="")
    ai.transcribe("x.mp4", 60.0)
    kind, kw = calls[-1]
    assert kind == "model" and "batch_size" not in kw and kw["language"] is None and kw["hotwords"] is None
    assert "beam_size" not in kw  # only turbo is turned down to beam 2

    def unreadable(*a, **k):
        raise media.DecodeError("Couldn't read the audio at 0s")
    monkeypatch.setattr(media, "pcm", unreadable)
    broken = ai._cuda_broken
    _settings(monkeypatch, whisper_device="auto", whisper_model="small", whisper_language="")
    with pytest.raises(media.DecodeError):  # a bad file is not a GPU failure: no CPU retry, CUDA stays trusted
        ai.transcribe("x.mp4", 60.0)
    assert ai._cuda_broken == broken


def test_language_comes_from_the_first_window_with_speech(monkeypatch):
    langs = []

    class Info:
        def __init__(self, lang, p):
            self.language, self.language_probability = lang, p

    class Model:
        def transcribe(self, audio, **kw):
            langs.append(kw["language"])
            if len(langs) == 1:  # a music-only intro: nothing said, a shaky guess
                return iter([]), Info("en", 0.2)
            return iter([_Seg([_Word(1.0, 1.5, " hallo")])]), Info("de", 0.97)
    monkeypatch.setattr(ai, "_model", lambda size, device: Model())
    monkeypatch.setattr(media, "pcm", lambda *a, **k: np.zeros(10, np.float32))
    monkeypatch.setattr(ai, "WINDOW_S", 100.0)
    _settings(monkeypatch, whisper_device="cpu", whisper_model="small", whisper_language="")
    out = ai.transcribe("x.mp4", 350.0)
    assert langs == [None, None, "de", "de"] and out["language"] == "de"


def test_pcm_raises_on_an_unreadable_file(tmp_path):
    with pytest.raises(media.DecodeError):
        media.pcm(tmp_path / "missing.mp4", 0.0, 1.0)


def test_windowed_transcription_stitches_word_times_across_windows(monkeypatch):
    """A long file goes to Whisper in windows (never the whole clip as float32): words come back once each, in order,
    on the source's clock, even though each window segments the overlap differently."""
    dur = 350.0
    script = [(2.0 * i + 0.1, 2.0 * i + 1.0, f" w{i}") for i in range(int(dur // 2))]  # one word every 2 s
    pcm_calls = []

    def pcm(src, start=0.0, d=None, sr=SR):
        pcm_calls.append((start, d))
        return np.zeros(int((d or dur) * 100), np.float32)  # length doesn't matter to the fake model

    class Model:
        def transcribe(self, audio, **kw):
            off, length = pcm_calls[-1]
            length = dur - off if length is None else length  # the last window reads to the end of the file
            inside = [w for w in script if off <= w[0] and w[1] <= off + length]
            phase = len(pcm_calls) % 3  # each window groups words into segments differently
            groups, cur = [], []
            for i, (s, e, w) in enumerate(inside):
                cur.append(_Word(s - off, e - off, w))
                if (i + phase) % 4 == 3:
                    groups.append(cur)
                    cur = []
            groups += [cur] if cur else []
            return iter(_Seg(g) for g in groups), _Info()

    monkeypatch.setattr(ai, "_model", lambda size, device: Model())
    monkeypatch.setattr(media, "pcm", pcm)
    monkeypatch.setattr(ai, "WINDOW_S", 100.0)
    _settings(monkeypatch, whisper_device="cpu", whisper_model="small", whisper_language="")
    prog = []
    out = ai.transcribe("x.mp4", dur, prog.append)
    assert len(pcm_calls) == 4 and all(d <= 100 + ai.WINDOW_PRE_S + ai.WINDOW_POST_S for _, d in pcm_calls[:-1])
    assert pcm_calls[-1][1] is None  # the last window reads to the file's real end, even if the probed duration is short
    words = [w for s in out["segments"] for w in s["words"]]
    assert [w["w"] for w in words] == [w for _, _, w in script]  # every word exactly once, in order
    assert all(abs(a["s"] - b[0]) < 0.011 for a, b in zip(words, script))  # on the source's clock
    for s in out["segments"]:
        assert s["text"] == "".join(w["w"] for w in s["words"]).strip() and s["start"] == s["words"][0]["s"]
    assert prog == sorted(prog) and 0.9 <= prog[-1] <= 1.0


def test_vocab_merges_brand_terms_channel_tags_profile_and_caps_length():
    proj = {"source_info": {"channel": "ShroudTV", "tags": ["valorant", "Jett", "fps"], "categories": ["Gaming"]}}
    prof = {"trained": {"winning_topics": ["clutch rounds", "jett"]}}
    v = signals.vocab(proj, {"brand_vocab": "Reelwright, KEKW\nValorant"}, prof)
    assert v.startswith("Reelwright, KEKW, Valorant, ShroudTV")  # the creator's own terms first: they survive the cap
    assert v.lower().count("valorant") == 1 and v.lower().count("jett") == 1 and "clutch rounds" in v
    big = signals.vocab({}, {"brand_vocab": ", ".join(f"term{i}" for i in range(500))})
    assert len(big) <= signals.VOCAB_CHARS and not big.endswith(",")
    assert signals.vocab({}, {"brand_vocab": ""}) == ""


# ---------- markers ----------

def _segs(*lines):
    """[(start, "text"), ...] -> segments with one word per 0.4 s."""
    out = []
    for start, text in lines:
        words = [{"s": round(start + 0.4 * i, 2), "e": round(start + 0.4 * i + 0.3, 2), "w": " " + w} for i, w in enumerate(text.split())]
        out.append({"start": start, "end": words[-1]["e"], "text": text, "words": words})
    return out


def test_voice_markers_find_clip_phrases_on_word_times():
    segs = _segs((10.0, "that was so close"), (100.0, "Chat, clip that! Clip that"), (102.5, "an eclipse is not a clip"),
                 (200.0, "okay somebody clip"), (201.0, "it please"), (300.0, "That's a clip."))
    m = signals.voice_markers(segs)
    assert [x["t"] for x in m] == [100.4, 200.8, 300.0]  # repeats within 10 s merge; a phrase may span two lines
    assert all(x["kind"] == "voice" and x["label"] and 0 < x["strength"] <= 1 for x in m)
    assert [x["t"] for x in signals.voice_markers(segs, "so close, please")] == [10.8, 201.4]  # custom phrases replace
    assert signals.voice_markers([{"start": 5.0, "end": 7.0, "text": "clip it now", "words": []}]) == \
        [{"t": 5.0, "kind": "voice", "label": "clip it", "strength": 1.0}]  # old transcripts without word times


def test_markers_sidecar_merges_with_voice_markers(tmp_path):
    (tmp_path / "markers.json").write_text(json.dumps({"markers": [
        {"t": 50.0, "kind": "obs", "label": "Mark"}, {"t": "bad", "kind": "obs"}, {"t": -3, "kind": "obs"},
        {"t": 100.6, "kind": "voice"}, {"t": 70.0, "kind": "game:kill", "strength": 3, "d": 4}]}), "utf-8")
    sig = {"waveform": [], "loud": []}
    signals.enrich(tmp_path, {"meta": {"duration": 400.0}}, None, 400.0, sig, _segs((100.0, "clip that")), {"audio_events": False})
    assert [(m["t"], m["kind"]) for m in sig["markers"]] == [(50.0, "obs"), (70.0, "game:kill"), (100.0, "voice")]  # 100.6 repeats it
    assert sig["markers"][1]["strength"] == 1.0 and sig["markers"][1]["d"] == 4.0  # clamped; span kept
    (tmp_path / "markers.json").write_text(json.dumps([{"t": 12, "kind": "obs"}]), "utf-8")  # a bare list works too
    sig = {}
    signals.enrich(tmp_path, {}, None, 400.0, sig, [], {"audio_events": False})
    assert sig["markers"] == [{"t": 12.0, "kind": "obs", "label": "obs", "strength": 1.0}]
    (tmp_path / "markers.json").write_text("{not json", "utf-8")
    sig = {}
    signals.enrich(tmp_path, {}, None, 400.0, sig, [], {"audio_events": False})  # a broken sidecar never fails analysis
    assert sig.get("markers", []) == []


# ---------- chat ----------

def test_emote_table_classifies_reactions_with_channel_overrides():
    t = signals.emote_table()
    assert signals.classify("KEKW KEKW", t) == {"laugh"} and signals.classify("😂😂😂", t) == {"laugh"}
    assert signals.classify("hahaha", t) == {"laugh"} and signals.classify("Pog W", t) == {"hype"}
    assert signals.classify("monkaS", t) == {"scary"} and signals.classify("Sadge", t) == {"sad"}
    assert signals.classify("CLIP IT", t) == {"clip"} and signals.classify("hello there", t) == set()
    assert signals.classify("eclipse", t) == set() and signals.classify("lol 🔥", t) == {"laugh", "hype"}
    over = signals.parse_emote_text("laugh: myLaugh\n#foo hype: fooHype\n#foo: nope\ngarbage line\nscary:")
    assert over == {"*": {"laugh": ["mylaugh"]}, "foo": {"hype": ["foohype"]}}
    assert signals.classify("myLaugh", signals.emote_table("bar", over)) == {"laugh"}
    assert signals.classify("fooHype", signals.emote_table("bar", over)) == set()
    assert signals.classify("fooHype", signals.emote_table("Foo", over)) == {"hype"}
    assert signals.parse_emote_text(signals.emote_text(over)) == over
    junk = {"*": {"laugh": 5, "hype": ["ok"]}, "x": "nope"}  # a hand-edited settings.json: never a crash
    assert signals.emote_text(junk) == "hype: ok" and signals.classify("ok", signals.emote_table("", junk)) == {"hype"}


def test_youtube_chat_replay_becomes_reaction_kinds_per_10s(tmp_path):
    def item(ms, *runs):
        return {"replayChatItemAction": {"videoOffsetTimeMsec": str(ms), "actions": [{"addChatItemAction": {"item": {
            "liveChatTextMessageRenderer": {"message": {"runs": list(runs)}}}}}]}}
    lines = [item(1000, {"text": "LMAO"}), item(4000, {"emoji": {"emojiId": "😂", "shortcuts": [":joy:"]}}),
             item(12000, {"text": "W "}, {"emoji": {"emojiId": "UCx/abc", "shortcuts": [":_pog:"], "isCustomEmoji": True}}),
             item(25000, {"text": "nice"}), {"other": 1}]
    f = tmp_path / "source.live_chat.json"
    f.write_text("\n".join(map(json.dumps, lines)) + "\nnot json\n", "utf-8")
    msgs = signals.youtube_chat(f)
    assert msgs[1] == (4.0, "😂") and msgs[2] == (12.0, "W pog")
    kinds = signals.chat_kinds(msgs, 30.0, signals.emote_table())
    assert kinds == {"laugh": [2, 0, 0, 0], "hype": [0, 1, 0, 0]}  # kinds nobody used are left out


def test_irc_capture_parses_twitch_lines_and_aligns_to_the_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(chatlog, "DIR", tmp_path / "chatlogs")
    line = ("@badge-info=;color=#FF0000;display-name=Viewer;emotes=;tmi-sent-ts=1700000012500;user-id=1 "
            ":viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #streamer :KEKW that was insane")
    assert chatlog.parse_line(line) == (1700000012.5, "KEKW that was insane")
    assert chatlog.parse_line(":viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #streamer :hi") == (None, "hi")
    assert chatlog.parse_line("PING :tmi.twitch.tv") is None and chatlog.parse_line(":tmi.twitch.tv 001 justinfan1 :Welcome") is None

    t0 = 1700000000.0
    stop = threading.Event()
    sent = []

    class Sock:
        chunks = [(":tmi.twitch.tv 001 justinfan123 :Welcome\r\nPING :tmi.twitch.tv\r\n"
                   f"@tmi-sent-ts={int((t0 + 12) * 1000)} :a!a@a.tmi.twitch.tv PRIVMSG #streamer :KEKW\r\n@tmi-sent-").encode(),
                  f"ts={int((t0 + 15) * 1000)} :b!b@b.tmi.twitch.tv PRIVMSG #streamer :LUL LUL\r\n".encode(),
                  f"@tmi-sent-ts={int((t0 + 31) * 1000)} :c!c@c.tmi.twitch.tv PRIVMSG #streamer :Pog\r\n".encode(),
                  f"@tmi-sent-ts={int((t0 + 999) * 1000)} :d!d@d.tmi.twitch.tv PRIVMSG #streamer :after the VOD\r\n".encode()]

        def sendall(self, b):
            sent.append(b.decode())

        def recv(self, n):
            if self.chunks:
                return self.chunks.pop(0)
            stop.set()
            return b""

        def settimeout(self, t):
            pass

        def close(self):
            pass

    chatlog.run("Streamer", stop, connect=lambda: Sock())
    assert any(s.startswith("NICK justinfan") for s in sent) and "JOIN #streamer\r\n" in sent and "PONG :tmi.twitch.tv\r\n" in sent
    assert not any("PASS oauth" in s for s in sent)  # anonymous, read-only: no account token is ever sent
    for bad in ("../../x", "a\r\nPRIVMSG #x :hi", "C:\\x", "ab"):  # the name becomes a folder and an IRC JOIN
        assert chatlog.clean(bad) == "" and chatlog.messages(bad, t0, t0 + 60) == []
    chatlog.run("../evil", threading.Event(), connect=lambda: pytest.fail("must not connect"))
    assert chatlog.messages("streamer", t0, t0 + 60) == [(12.0, "KEKW"), (15.0, "LUL LUL"), (31.0, "Pog")]
    assert not any("viewer" in p.read_text("utf-8") or "\"a\"" in p.read_text("utf-8") for p in (tmp_path / "chatlogs").rglob("*.jsonl"))

    # an OBS recording made in that window: the slice lands on its clock and feeds chat counts + kinds
    src = tmp_path / "rec.mkv"
    src.write_bytes(b"x")
    os.utime(src, (t0 + 60, t0 + 60))  # OBS finished writing at t0+60 → a 60 s recording started at t0
    sig = {}
    proj = {"origin": {"obs": "Main PC"}, "source_info": {}}
    signals.enrich(tmp_path, proj, src, 60.0, sig, [], {"audio_events": False, "twitch_chat_channel": "streamer"})
    assert sig["chat_kinds"] == {"laugh": [0, 2, 0, 0, 0, 0, 0], "hype": [0, 0, 0, 1, 0, 0, 0]}
    assert sig["chat"] == [0, 2, 0, 1, 0, 0, 0]
    sig = {}  # someone else's video at the same time gets none of the streamer's chat
    signals.enrich(tmp_path, {"source_info": {"channel": "other"}, "recorded_at": t0}, src, 60.0, sig, [],
                   {"audio_events": False, "twitch_chat_channel": "streamer"})
    assert "chat_kinds" not in sig and "chat" not in sig


def test_import_records_when_the_stream_started(monkeypatch):
    p = pipeline.new_project("vod")
    pid = p["id"]

    def dl(url, outtmpl, hook=None):
        (pipeline.pdir(pid) / "source.mp4").write_bytes(b"x")
        return {"title": "VOD", "timestamp": 1700000000, "channel": "streamer"}
    monkeypatch.setattr(pipeline, "ytdlp_download", dl)
    monkeypatch.setattr(pipeline.media, "probe", lambda src: {"duration": 10.0})
    monkeypatch.setattr(pipeline, "submit", lambda *a, **k: None)
    pipeline._import_url({"id": "j", "project_id": pid, "url": "https://www.twitch.tv/videos/123"})
    assert store.get("project", pid)["recorded_at"] == 1700000000.0


# ---------- Twitch viewer clips ----------

def test_helix_clips_become_viewer_clips_on_the_vod_clock(monkeypatch, tmp_path):
    monkeypatch.setattr(twitch.platforms, "CONN_FILE", tmp_path / "connections.json")
    monkeypatch.setattr(twitch, "_token", {})
    twitch.set_app(" cid ", " sec ")
    assert twitch.platforms._load()["twitch"] == {"client_id": "cid", "client_secret": "sec"}
    calls = []

    def post(url, data=None, timeout=None):
        calls.append(("POST", url, data))
        return httpx.Response(200, json={"access_token": f"tok{len(calls)}", "expires_in": 5000000, "token_type": "bearer"})

    def get(url, params=None, headers=None, timeout=None):
        calls.append(("GET", url, dict(params or {}), headers["Authorization"]))
        if headers["Authorization"] == "Bearer tok1":
            return httpx.Response(401, json={"message": "Invalid OAuth token"})  # expired: one refresh, then retry
        if url.endswith("/videos"):
            return httpx.Response(200, json={"data": [{"id": "987", "user_id": "42", "created_at": "2026-11-01T18:00:00Z"}]})
        if (params or {}).get("after") is None:
            return httpx.Response(200, json={"data": [
                {"id": "A", "video_id": "987", "vod_offset": 3600, "duration": 30.0, "view_count": 120, "title": "insane"},
                {"id": "B", "video_id": "555", "vod_offset": 10, "duration": 20.0, "view_count": 5, "title": "other vod"},
                {"id": "C", "video_id": "987", "vod_offset": None, "duration": 20.0, "view_count": 5, "title": "no offset"}],
                "pagination": {"cursor": "next"}})
        return httpx.Response(200, json={"data": [
            {"id": "D", "video_id": "987", "vod_offset": 75, "duration": 26.5, "view_count": 9, "title": "early"}], "pagination": {}})
    monkeypatch.setattr(twitch.httpx, "post", post)
    monkeypatch.setattr(twitch.httpx, "get", get)
    assert twitch.vod_id("https://www.twitch.tv/videos/987?t=1h") == "987" and twitch.vod_id("https://youtu.be/x") is None
    clips = twitch.clips_for_vod("987")
    assert clips == [{"t": 75.0, "d": 26.5, "views": 9, "title": "early", "id": "D"},
                     {"t": 3600.0, "d": 30.0, "views": 120, "title": "insane", "id": "A"}]
    assert [c[1].rsplit("/", 1)[-1] for c in calls if c[0] == "POST"] == ["token", "token"]  # token refreshed once on 401
    assert calls[0][2] == {"client_id": "cid", "client_secret": "sec", "grant_type": "client_credentials"}
    clip_q = next(c[2] for c in calls if c[0] == "GET" and c[1].endswith("/clips"))
    assert clip_q["broadcaster_id"] == "42" and clip_q["started_at"] == "2026-11-01T18:00:00Z" and clip_q["first"] == 100

    sig = {}  # analysis of that VOD: saved as signals["viewer_clips"]
    signals.enrich(tmp_path, {"source_url": "https://www.twitch.tv/videos/987"}, None, 7200.0, sig, [], {"audio_events": False})
    assert [c["id"] for c in sig["viewer_clips"]] == ["D", "A"]
    twitch.set_app("", "")
    assert not twitch.has_app()
    sig = {}
    signals.enrich(tmp_path, {"source_url": "https://www.twitch.tv/videos/987"}, None, 7200.0, sig, [], {"audio_events": False})
    assert "viewer_clips" not in sig  # no app saved: skipped quietly


# ---------- audio events ----------

def test_audio_events_from_the_tagger_around_spikes(monkeypatch):
    class R:
        def __init__(self, name, prob):
            self.name, self.prob = name, prob

    class Stream:
        def accept_waveform(self, sample_rate, waveform):
            self.x = waveform

    class Tagger:
        def create_stream(self):
            return Stream()

        def compute(self, st, top_k=-1):
            t = float(st.x[0])  # the fake PCM carries its own time in every sample
            if 100 <= t < 103:
                return [R("Laughter", 0.8 if t >= 101 else 0.5), R("Speech", 0.9)]
            if 205 <= t < 206:
                return [R("Gunshot, gunfire", 0.7)]
            return [R("Speech", 0.9), R("Laughter", 0.1)]

    def pcm(src, start=0.0, d=None, sr=SR):
        return (start + np.arange(int(d * sr)) // sr).astype(np.float32)  # whole seconds since the file start
    monkeypatch.setattr(signals, "_tagger", lambda: Tagger())
    monkeypatch.setattr(media, "pcm", pcm)
    ev = signals.audio_events("x.mp4", [101.0, 205.0, 104.0], 400.0)
    assert [(e["kind"], round(e["t"])) for e in ev] == [("laughter", 102), ("gunshot", 206)]  # one event per burst, at its peak
    assert ev[0]["p"] == 0.8 and signals.audio_events("x.mp4", [], 400.0) == []


def test_audio_events_unavailable_without_sherpa(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "sherpa_onnx", None)  # import fails like on a build without the wheel
    monkeypatch.setattr(signals, "_TAGGER", {})
    st = signals.tagger_status()
    assert st["available"] is False and "Apache-2.0" in st["licence"] and st["sha256"]
    assert signals.audio_events("x.mp4", [10.0], 100.0) == []
    sig = {"loud": [{"t": 10.0, "energy": 1.0}]}
    signals.enrich(None, {}, "x.mp4", 100.0, sig, [], {"audio_events": True})
    assert "audio_events" not in sig  # analysis carries on without them


def test_tagger_download_is_pinned_by_sha256(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA", tmp_path)

    class R:
        def __init__(self, body):
            self.body = body

        def read(self, n=-1):  # fetch_model reads at most MODEL_MAX + 1
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: R(b"tampered"))
    with pytest.raises(RuntimeError, match="SHA-256"):
        media.fetch_model("t.onnx", "https://huggingface.co/x", sha256="0" * 64)
    assert not list((tmp_path / "models").iterdir())  # nothing half-written or unverified is kept
    import hashlib
    good = hashlib.sha256(b"ok").hexdigest()
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: R(b"ok"))
    assert media.fetch_model("t.onnx", "https://huggingface.co/x", sha256=good).read_bytes() == b"ok"


# ---------- consumers: tags, heuristic seeds, learning ----------

def test_tagged_lines_show_markers_viewer_clips_chat_kinds_and_audio_events():
    segs = [{"start": 5.0, "end": 9.0, "text": "no way"}, {"start": 20.0, "end": 24.0, "text": "clip that"},
            {"start": 41.0, "end": 45.0, "text": "he is behind me"}, {"start": 60.0, "end": 64.0, "text": "calm"}]
    sig = {"markers": [{"t": 21.0, "kind": "voice", "label": "clip that", "strength": 1.0}],
           "viewer_clips": [{"t": 2.0, "d": 30.0, "views": 50, "title": "a", "id": "1"}, {"t": 4.0, "d": 10.0, "views": 1, "title": "b", "id": "2"}],
           "chat_kinds": {"laugh": [0, 0, 0, 0, 9, 0, 0], "hype": [0, 0, 0, 0, 0, 0, 0]},
           "audio_events": [{"t": 43.0, "kind": "laughter", "p": 0.8}, {"t": 7.0, "kind": "gunshot", "p": 0.6}]}
    lines = ai.tagged_lines(segs, sig).splitlines()
    assert "VIEWER-CLIPPED ×2" in lines[0] and "GUNSHOT" in lines[0]
    assert "CLIP-MARKER" in lines[1] and "VIEWER-CLIPPED ×1" in lines[1]
    assert "CHAT: laughing" in lines[2] and "LAUGHTER" in lines[2] and "(" not in lines[3]
    assert "CLIP-MARKER" in ai.FIND_SYSTEM and "VIEWER-CLIPPED" in ai.FIND_SYSTEM and "LAUGHTER" in ai.FIND_SYSTEM


def test_heuristic_seeds_a_clip_before_a_voice_marker():
    segs = [{"start": float(i * 5), "end": float(i * 5 + 4.5), "text": "word " * (3 if i != 4 else 12)} for i in range(60)]
    plain = ai.heuristic_moments(segs, [], 300.0, 3, 20, 40)
    assert plain  # unchanged behaviour without marks
    marked = ai.heuristic_moments(segs, [], 300.0, 3, 20, 40, marks=[{"t": 200.0, "kind": "voice", "label": "clip that"},
                                                                     {"t": 100.0, "d": 25.0, "views": 40, "id": "v"}])
    top = marked[0]
    assert top["start"] <= 165.0 and 195.0 <= top["end"] <= 206.0 and "marker" in top["reason"].lower()
    assert any(c["start"] <= 100.5 and c["end"] >= 120.0 and "viewer" in c["reason"].lower() for c in marked)


def test_new_learning_features_and_old_feedback_rows_still_work():
    segs = [{"start": 10.0, "end": 14.0, "text": "what was that"}]
    old_sig = {"waveform": [0.1] * 600, "loud": [], "scenes": []}
    f_old = learn.features({"start": 10.0, "end": 40.0, "score": 70}, segs, old_sig, 60.0)
    assert set(f_old) == set(learn.FEATURES)
    new = ("markers", "viewer_clips", "chat_laugh", "chat_hype", "laugh")
    assert set(new) <= set(learn.FEATURES) and all(k in learn.NEUTRAL for k in new)
    assert all(f_old[k] == learn.NEUTRAL[k] for k in new)  # a clip without the signal looks like a lesson that predates it
    rich = {**old_sig, "markers": [{"t": 30.0, "kind": "voice"}], "viewer_clips": [{"t": 15.0, "d": 20.0, "views": 3}],
            "chat_kinds": {"laugh": [0, 0, 6, 0, 0, 0], "hype": [1, 1, 1, 1, 1, 1]},
            "audio_events": [{"t": 20.0, "kind": "laughter", "p": 0.9}, {"t": 22.0, "kind": "gunshot", "p": 0.9}]}
    f = learn.features({"start": 10.0, "end": 40.0, "score": 70}, segs, rich, 60.0)
    assert f["markers"] > 0 and f["viewer_clips"] > 0 and f["chat_laugh"] > 1 and f["laugh"] > 0 and f["chat_hype"] == 1.0
    old_rows = [{"signal": 1 if i % 2 else -1, "why": "rated", "features": {k: 0.5 for k in learn.FEATURES if k not in new}}
                for i in range(14)]
    X, y, w = learn.samples(old_rows)
    assert X.shape == (14, len(learn.FEATURES))
    assert all(X[0][learn.FEATURES.index(k)] == learn.NEUTRAL[k] for k in new)
    m = learn.train(old_rows + [{"signal": 1, "why": "rated", "features": f}] * 4)
    assert m is None or len(m["w"]) == len(learn.FEATURES)
    assert ai.tagged_lines(segs, old_sig) == "[10.0-14.0] what was that"  # old signals.json: no new tags, no crash


# ---------- routes ----------

def test_signals_settings_and_twitch_app_routes(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from reelwright import routes_signals
    monkeypatch.setattr(twitch.platforms, "CONN_FILE", tmp_path / "connections.json")
    monkeypatch.setattr(chatlog, "apply", lambda: None)
    app = FastAPI()  # the router on its own (server.py includes it with one line above its static mounts)
    app.include_router(routes_signals.router)
    c = TestClient(app)
    r = c.put("/api/signals", json={"whisper_language": "English!"})
    assert r.status_code == 400
    r = c.put("/api/signals", json={"whisper_language": "en", "brand_vocab": "Reelwright, KEKW", "voice_triggers": "clip that, mark it",
                                    "twitch_chat_channel": "Streamer", "chat_emotes": "laugh: myLaugh", "audio_events": False})
    assert r.status_code == 200, r.text
    s = config.load()
    assert s["whisper_language"] == "en" and s["twitch_chat_channel"] == "streamer" and s["chat_emotes"] == {"*": {"laugh": ["mylaugh"]}}
    assert s["audio_events"] is False and s["voice_triggers"] == "clip that, mark it"
    assert c.put("/api/signals", json={"twitch_chat_channel": "not a login!"}).status_code == 400
    assert c.put("/api/signals/twitch", json={"client_id": "cid", "client_secret": ""}).status_code == 400
    assert c.post("/api/signals/twitch/test").status_code == 400  # nothing saved yet
    assert c.put("/api/signals/twitch", json={"client_id": "cid", "client_secret": "topsecret"}).status_code == 200

    def refuse(force=False):
        raise RuntimeError("Twitch refused the app's Client ID / Client secret (400).")
    monkeypatch.setattr(twitch, "app_token", refuse)
    r = c.post("/api/signals/twitch/test")
    assert r.status_code == 400 and "refused" in r.json()["detail"]
    monkeypatch.setattr(twitch, "app_token", lambda force=False: "tok")
    assert c.post("/api/signals/twitch/test").json() == {"ok": True}
    body = c.get("/api/signals").json()
    assert body["twitch"]["has_app"] and "topsecret" not in json.dumps(body) and body["settings"]["chat_emotes"] == "laugh: mylaugh"
    assert body["audio"]["licence"] and "clip that" in body["default_triggers"]
    assert c.delete("/api/signals/twitch").status_code == 200 and not c.get("/api/signals").json()["twitch"]["has_app"]
    config.save({"whisper_language": "", "brand_vocab": "", "voice_triggers": "", "twitch_chat_channel": "", "chat_emotes": {},
                 "audio_events": True})
