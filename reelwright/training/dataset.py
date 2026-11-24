"""Training data from the creator's reviewed decisions, in the exact shape the app asks its models at inference.

SFT (chat JSONL): system + user = ai._find_pass's prompt for that video (or window of a long one), target = the MomentList
of clips the creator kept (👍) or published, with their own trims and titles. Also "judge" (ai.rank_moments' prompt →
Ranking) and "meta" (ai.youtube_meta's prompt → the title/description/tags the creator actually published). Claude can
add "teacher" labels on request (estimate first, explicit confirm). DPO pairs: chosen = the creator's version, rejected =
the AI's own picks for the same window (with its original bounds, including the ones the creator 👎'd).

Every example has a sidecar (sources, revisions, schema/feature version, reviewer) and a split: grouped by project so
one VOD never spans splits, with a frozen later-time test set. Only confirmed projects are used; deleted clips, removed
projects and excluded examples drop out of the next build; lineage says which datasets/releases used a clip.
Same inputs → same bytes → same hash.
"""
import hashlib
import json
import math
import time
from pathlib import Path

from .. import ai, config, log, pipeline, store, usage

SCHEMA_VERSION = 1    # bump when the example format changes
FEATURE_VERSION = 1   # bump when the prompt-building mirror changes
TEST_SHARE = 0.2      # the latest 20% of projects (at freeze time) are the test set, forever
DEV_MOD = 10          # ~1 in 10 of the other projects is dev (hash of the project id)
AI_SOURCES = ("ai", "auto", "search")


def root() -> Path:
    return config.DATA / "training" / "datasets"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ---------- consent and exclusions ----------

def consent() -> dict:
    return store.get("pref", "training_consent") or {"all": False, "projects": []}


def set_consent(all_projects: bool | None = None, project: str | None = None, on: bool = True) -> dict:
    c = consent()
    if all_projects is not None:
        c["all"] = bool(all_projects)
    if project:
        c["projects"] = sorted(set(c["projects"]) | {project} if on else set(c["projects"]) - {project})
    return store.put("pref", {"id": "training_consent", "created": 0, **{k: c[k] for k in ("all", "projects")}})


def excluded() -> set[str]:
    return set((store.get("pref", "training_excluded") or {}).get("ids") or [])


def set_excluded(eid: str, on: bool) -> None:
    ids = excluded() | {eid} if on else excluded() - {eid}
    store.put("pref", {"id": "training_excluded", "created": 0, "ids": sorted(ids)})


def eligible(proj: dict) -> bool:
    c = consent()
    return proj.get("training") is not False and (c["all"] or proj["id"] in c["projects"])


# ---------- the app's prompts, mirrored (tests compare them with ai._find_pass / rank_moments / youtube_meta) ----------

def target_len(s: dict) -> float:
    return pipeline.lengths(s)[1]


def find_prompt(segments, duration, signals, count, min_len, max_len, target=None, window=None) -> tuple[str, str]:
    """ai._find_pass's (system, user) with no search query, style profile or taste (the weights learn those)."""
    aim = (f" Aim for about {target:.0f}s each (shorter is fine when the moment is complete); never longer than {max_len:.0f}s."
           if target else f" Never longer than {max_len:.0f}s.")
    task = f"Find the {count} best clips, each {min_len:.0f}-{max_len:.0f} seconds long, sorted best first.{aim}"
    if window:
        task += f" This is the part of the video from {window[0]:.0f}s to {window[1]:.0f}s; only pick moments inside it."
    chat = ai.chat_spikes(signals.get("chat") or [])
    vspikes = [(m["t"], m["kind"], m["strength"]) for m in (signals.get("visual") or {}).get("spikes", [])
               if not window or window[0] <= m["t"] < window[1]]
    book = ai.playbook(signals.get("video_type"))
    context = ((f"{book}\n\n" if book else "") + f"Video duration: {duration:.1f}s\n"
               f"Scene cuts (s): {ai.even_sample(signals.get('scenes', []), 400)}\n"
               f"Audio energy spikes (t, relative energy): {[(m['t'], m['energy']) for m in signals.get('loud', [])]}\n"
               + (f"Visual spikes (t, kind, strength): {vspikes}\n" if vspikes else "")
               + (f"Live-chat activity spikes (s): {[t for t in chat if not window or window[0] <= t < window[1]]}\n" if chat else "")
               + f"\nTRANSCRIPT (tags show audio/chat at that line):\n{ai.tagged_lines(segments, signals)}")
    return ai.FIND_SYSTEM, context + "\n\nTASK: " + task


def windows(segments, duration, signals, count) -> list[tuple]:
    """ai._scan's split: (segments, signals, count, window) per part; one part for a short video."""
    if duration <= ai.CHUNK_S * 1.5:
        return [(segments, signals, count, None)]
    per = max(2, math.ceil(count * 2 * ai.CHUNK_S / duration))
    out = []
    for a in range(0, int(duration), ai.CHUNK_S):
        b = min(a + ai.CHUNK_S + ai.OVERLAP_S, duration)
        segs = [s for s in segments if a <= s["start"] < b]
        if segs:
            sig = {**signals, "scenes": [t for t in signals.get("scenes", []) if a <= t < b],
                   "loud": [m for m in signals.get("loud", []) if a <= m["t"] < b]}
            out.append((segs, sig, per, (segs[0]["start"], segs[-1]["end"])))
    return out


def judge_prompt(cands, segments, count, video_type=None) -> tuple[str, str]:
    """ai.rank_moments' (system, user), text only (no frames, profile or taste)."""
    entries = [f"#{i} [{c['start']:.0f}-{c['end']:.0f}s] {c.get('title', '')} — hook: {c.get('hook', '')}\n"
               f"{' '.join(s['text'] for s in segments if c['start'] <= s['start'] < c['end'])[:700]}"
               for i, c in enumerate(cands)]
    system = ("You are the final editor choosing which candidate clips to publish. Candidates were found in separate "
              "parts of one long video by different passes, so their original scores are not comparable: rescore them "
              "on one honest scale." + (f"\n\n{book}" if (book := ai.playbook(video_type)) else ""))
    listing = "\n\n".join(entries)
    return system, f"CANDIDATES:\n{listing}\n\nPick the {count} strongest for short-form (Shorts/TikTok/Reels), best first."


META_SYSTEM = "You are a short-form growth strategist writing searchable, honest, high-CTR titles and captions for YouTube, TikTok and Reels."


def meta_prompt(clip, segments, vertical: bool) -> tuple[str, str]:
    return META_SYSTEM, (f"Write YouTube metadata for this {'YouTube Short (vertical)' if vertical else 'video'}.\n"
                         f"Working title: {clip.get('title')}\nHook: {clip.get('hook')}\n"
                         f"Transcript:\n{ai.transcript_lines(segments, clip['start'], clip['end'])}")


# ---------- examples ----------

def _moment(c: dict, bounds=None, score=90) -> dict:
    a, b = bounds or (c["start"], c["end"])
    return {"start": round(float(a), 2), "end": round(float(b), 2), "title": c.get("title") or "", "hook": c.get("hook") or "",
            "reason": c.get("reason") or "The creator kept this clip.", "score": score, "hashtags": list(c.get("hashtags") or [])}


def _verdict(c: dict) -> int:
    """+1 kept (👍) or published, -1 skipped (👎), 0 undecided."""
    return 1 if c.get("rating") == 1 or c.get("posts") else -1 if c.get("rating") == -1 else 0


def _side(task, group, sources, revisions, reviewer, schema, **extra) -> dict:
    return {"task": task, "group": group, "sources": sorted(sources), "revisions": revisions, "reviewer": reviewer,
            "schema": f"{schema}@v{SCHEMA_VERSION}", "feature_version": FEATURE_VERSION, **extra}


def _ex(side: dict, system: str, user: str, target: dict) -> dict:
    eid = _sha(_canon([side["task"], side["group"], side.get("window"), side["reviewer"]]))[:16]
    return {"id": eid, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user},
                                    {"role": "assistant", "content": _canon(target)}], "side": {"id": eid, **side}}


def find_examples(proj: dict, segments: list, signals: dict, clips: list | None = None) -> list[dict]:
    """One SFT example per video part that holds a kept/published clip; a DPO pair rides along as "dpo" when the AI's
    picks there differ from the creator's version."""
    s = config.load()
    lo, target, hi = pipeline.lengths(s)
    clips = [c for c in (clips if clips is not None else store.find("clip", project_id=proj["id"])) if not c.get("parts")]
    dur = float((proj.get("meta") or {}).get("duration") or (segments[-1]["end"] if segments else 0))
    out = []
    for segs, sig, count, win in windows(segments, dur, signals, s["clip_count"]):
        inside = [c for c in clips if not win or win[0] <= c["start"] < win[1]]
        good = sorted((c for c in inside if _verdict(c) > 0), key=lambda c: (not c.get("posts"), -(c.get("lift") or 0), c["start"]))
        if not good:
            continue
        system, user = find_prompt(segs, dur, sig, count, lo, hi, target=target, window=win)
        chosen = {"clips": [_moment(c, score=95 if c.get("posts") else 90) for c in good]}
        side = _side("find", proj["id"], [c["id"] for c in inside if _verdict(c)], {c["id"]: c.get("rev", 1) for c in good},
                     "user", "MomentList", window=list(win) if win else None)
        ex = _ex(side, system, user, chosen)
        picks = sorted((c for c in inside if c.get("source") in AI_SOURCES), key=lambda c: (c.get("ai_bounds") or [c["start"]])[0])
        rejected = {"clips": [_moment(c, c.get("ai_bounds"), int(c.get("ai_score") or c.get("score") or 50)) for c in picks]}
        if picks and _canon(rejected) != _canon(chosen):
            ex["dpo"] = {"id": ex["id"], "prompt": ex["messages"][:2], "chosen": _canon(chosen), "rejected": _canon(rejected)}
        out.append(ex)
    return out


def judge_examples(proj, segments, clips) -> list[dict]:
    cands = sorted((c for c in clips if c.get("source") in AI_SOURCES and _verdict(c) and not c.get("parts")), key=lambda c: c["start"])
    good = [i for i, c in enumerate(cands) if _verdict(c) > 0]
    if len(cands) < 2 or not good or len(good) == len(cands):
        return []
    shown = [{**c, "start": (c.get("ai_bounds") or [c["start"], c["end"]])[0], "end": (c.get("ai_bounds") or [c["start"], c["end"]])[1]}
             for c in cands]
    system, user = judge_prompt(shown, segments, len(good), proj.get("video_type"))
    good.sort(key=lambda i: (not cands[i].get("posts"), cands[i]["start"]))
    target = {"ranked": [{"index": i, "score": 95 - k, "reason": "The creator kept this clip."} for k, i in enumerate(good)]}
    return [_ex(_side("judge", proj["id"], [c["id"] for c in cands], {c["id"]: c.get("rev", 1) for c in cands}, "user",
                      "Ranking"), system, user, target)]


def meta_examples(proj, segments, clips) -> list[dict]:
    out, by = [], {c["id"]: c for c in clips}
    for u in sorted(store.find("upload", project_id=proj["id"]), key=lambda u: u["id"]):
        c = by.get(u.get("clip_id"))
        if not c or u.get("status") != "done" or not u.get("title"):
            continue
        system, user = meta_prompt(c, segments, (c.get("edit") or {}).get("aspect") == "9:16")
        target = {"title": u["title"], "description": u.get("description") or "", "tags": list(u.get("tags") or []),
                  "category_id": str(u.get("category_id") or "22"), "social_caption": u.get("caption") or u["title"]}
        side = _side("meta", proj["id"], [c["id"], u["id"]], {c["id"]: c.get("rev", 1)}, "user", "YouTubeMeta", window=[u["id"]])
        out.append(_ex(side, system, user, target))
    return out


def teacher_examples(proj, segments, signals, user_windows: set) -> list[dict]:
    """Claude's labels for parts the creator hasn't decided on (the creator's own picks always win)."""
    out = []
    for lab in sorted(store.find("train_label", project_id=proj["id"]), key=lambda x: x["id"]):
        win = tuple(lab["window"]) if lab.get("window") else None
        if win in user_windows or not lab.get("clips"):
            continue
        side = _side("find", proj["id"], [lab["id"]], {}, "teacher", "MomentList", window=list(win) if win else None,
                     teacher=lab.get("model"))
        out.append(_ex(side, lab["system"], lab["user"], {"clips": lab["clips"]}))
    return out


def project_examples(proj: dict) -> list[dict]:
    try:
        segs = (pipeline.load_json(proj["id"], "transcript.json", {}) or {}).get("segments") or []
        signals = pipeline.load_json(proj["id"], "signals.json", {}) or {}
    except (OSError, ValueError):
        return []  # storage drive away or a bad id: nothing from this project this time
    if not segs:
        return []
    if proj.get("video_type"):
        signals["video_type"] = proj["video_type"]
    signals.pop("video", None)
    clips = [c for c in store.find("clip", project_id=proj["id"]) if not c.get("parts")]
    finds = find_examples(proj, segs, signals, clips)
    wins = {tuple(e["side"]["window"]) if e["side"].get("window") else None for e in finds}
    return finds + judge_examples(proj, segs, clips) + meta_examples(proj, segs, clips) + teacher_examples(proj, segs, signals, wins)


# ---------- splits ----------

def frozen_test(projects: list[dict]) -> set[str]:
    """The test set: the latest TEST_SHARE of projects when first frozen (≥3 projects), then never changed."""
    doc = store.get("pref", "training_split")
    if doc:
        return set(doc["test"])
    if len(projects) < 3:
        return set()
    late = sorted(projects, key=lambda p: (p.get("created") or 0, p["id"]))[-max(1, round(len(projects) * TEST_SHARE)):]
    store.put("pref", {"id": "training_split", "created": 0, "test": sorted(p["id"] for p in late), "frozen_at": time.time()})
    return {p["id"] for p in late}


def split_of(group: str, test: set) -> str:
    return "test" if group in test else "dev" if int(_sha(group)[:8], 16) % DEV_MOD == 0 else "train"


# ---------- build ----------

def collect() -> tuple[list[dict], dict]:
    """(examples, stats) from every confirmed project; nothing is written."""
    projs = [p for p in store.find("project") if p.get("status") == "ready"]
    ok = [p for p in projs if eligible(p)]
    exs, skip = [], excluded()
    for p in sorted(ok, key=lambda p: p["id"]):
        exs += project_examples(p)
    dropped = {e["id"] for e in exs if e["id"] in skip or skip & set(e["side"]["sources"])}
    exs = [e for e in exs if e["id"] not in dropped]
    have = {c["id"] for c in store.find("clip")}
    gone = sum(1 for f in store.find("feedback") if f["id"] not in have)
    return exs, {"projects": len(projs), "confirmed": len(ok), "excluded_examples": len(dropped), "deleted_clips_skipped": gone}


def build() -> dict:
    exs, stats = collect()
    test = frozen_test([p for p in store.find("project") if eligible(p) and p.get("status") == "ready"]) if exs else set()
    files = {k: [] for k in ("train", "dev", "test", "dpo", "sidecar")}
    for e in sorted(exs, key=lambda e: e["id"]):
        side = {**e["side"], "split": split_of(e["side"]["group"], test)}
        files[side["split"]].append(_canon({"id": e["id"], "messages": e["messages"]}))
        files["sidecar"].append(_canon(side))
        if e.get("dpo") and side["split"] == "train":
            files["dpo"].append(_canon(e["dpo"]))
    blob = {k: "".join(x + "\n" for x in v) for k, v in files.items()}
    h = _sha(_canon(blob))
    out = root() / h[:16]
    out.mkdir(parents=True, exist_ok=True)
    for k, text in blob.items():
        (out / f"{k}.jsonl").write_text(text, "utf-8", newline="\n")
    sides = [json.loads(x) for x in files["sidecar"]]
    counts = {"sft": len(sides), "dpo": len(files["dpo"]), **{s: len(files[s]) for s in ("train", "dev", "test")},
              "by_task": {t: sum(x["task"] == t for x in sides) for t in ("find", "judge", "meta")},
              "teacher": sum(x["reviewer"] == "teacher" for x in sides)}
    sources = sorted({s for x in sides for s in x["sources"]})
    (out / "manifest.json").write_text(_canon({"hash": h, "counts": counts, "schema": SCHEMA_VERSION,
                                               "feature_version": FEATURE_VERSION, "sources": sources}), "utf-8")
    if not store.get("train_dataset", h):
        store.put("train_dataset", {"id": h, "dir": str(out), "counts": counts, "sources": sources})
    note = ("" if consent()["all"] or consent()["projects"] else
            "Nothing is used until you confirm which projects' decisions may be trained on.") if not sides else ""
    if not sides and not note:
        note = "No reviewed decisions yet: 👍, 👎 and publish clips in confirmed projects to make examples."
    log.event("training", f"dataset {h[:12]}: {counts['sft']} examples, {counts['dpo']} DPO pairs")
    return {"hash": h, "dir": str(out), "counts": counts, "stats": stats, "note": note}


def lineage(source_id: str) -> dict:
    """Which datasets (oldest first) and releases included a clip, upload or label."""
    ds = sorted((d for d in store.find("train_dataset") if source_id in d.get("sources", [])), key=lambda d: d["created"])
    hashes = [d["id"] for d in ds]
    rel = sorted(r["id"] for r in store.find("train_release") if (r.get("hashes") or {}).get("dataset") in hashes)
    return {"datasets": hashes, "releases": rel}


def browse(h: str, limit: int = 200) -> list[dict]:
    """A dataset's examples for the browser: sidecar + a short view of the prompt and the full target."""
    d = root() / h[:16]
    side = {s["id"]: s for s in map(json.loads, (d / "sidecar.jsonl").read_text("utf-8").splitlines())}
    skip, out = excluded(), []
    for split in ("train", "dev", "test"):
        for line in (d / f"{split}.jsonl").read_text("utf-8").splitlines():
            e = json.loads(line)
            out.append({**side[e["id"]], "prompt": e["messages"][1]["content"][-600:], "target": e["messages"][2]["content"],
                        "excluded": e["id"] in skip})
    return out[:limit]


# ---------- Claude as teacher ----------

def teacher_label(project_ids: list[str], confirm: bool = False) -> dict:
    """Claude labels confirmed projects through the app's own find pass. Without confirm: only the cost estimate."""
    s = config.load()
    lo, target, hi = pipeline.lengths(s)
    plan = []
    for pid in project_ids:
        p = store.get("project", pid)
        if not p or not eligible(p):
            continue
        segs = (pipeline.load_json(pid, "transcript.json", {}) or {}).get("segments") or []
        signals = pipeline.load_json(pid, "signals.json", {}) or {}
        if p.get("video_type"):
            signals["video_type"] = p["video_type"]
        signals.pop("video", None)
        dur = float((p.get("meta") or {}).get("duration") or 0)
        plan += [(p, segs, sig, count, win, dur) for segs, sig, count, win in windows(segs, dur, signals, s["clip_count"])]
    chars = sum(len(find_prompt(segs, dur, sig, count, lo, hi, target, win)[1]) + len(ai.FIND_SYSTEM)
                for _, segs, sig, count, win, dur in plan)
    tin, tout = chars / 4, 300 * len(plan) + sum(150 * x[3] for x in plan)
    price = usage.PRICES.get(s["model"])
    usd = (tin * price[0] + tout * price[1]) / 1e6 if price else None
    est = {"parts": len(plan), "tokens_in": round(tin), "tokens_out": round(tout), "model": s["model"], "usd": usd,
           "line": f"{len(plan)} video part(s), ~{tin:,.0f} input + ~{tout:,.0f} output tokens on {s['model']}"
                   + (f": about ${usd:.2f} (rough, ±50%)" if usd is not None else ": no price known")}
    if not confirm:
        return {**est, "confirm_needed": True}
    done = 0
    for p, segs, sig, count, win, dur in plan:
        system, user = find_prompt(segs, dur, sig, count, lo, hi, target, win)
        picks = ai._find_pass(segs, dur, sig, count, lo, hi, target=target, window=win, provider="anthropic")
        store.put("train_label", {"id": _sha(_canon([p["id"], win]))[:16], "project_id": p["id"], "window": list(win) if win else None,
                                  "clips": [_moment(c, score=int(c.get("score") or 50)) for c in picks], "system": system,
                                  "user": user, "model": s["model"]})
        done += 1
    return {**est, "labelled": done}
