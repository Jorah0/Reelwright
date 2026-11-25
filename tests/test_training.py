"""Custom-model training (reelwright/training): dataset builder, model designer arithmetic, GPU lease, worker progress,
export commands and the promote gate. No torch and no network: every outside process and model is a fake. The one
real-torch check (the parameter count against LlamaForCausalLM) runs only when REELWRIGHT_TRAIN_PY names a training env."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from reelwright import ai, config, obs, pipeline, store  # noqa: E402
from reelwright.training import dataset, design, release, runner  # noqa: E402

ARCH = {"vocab": 8192, "layers": 8, "width": 512, "mlp": 1376, "heads": 8, "kv_heads": 2, "context": 1024, "tied": True}


# ---------- model designer ----------

def test_param_count_matches_the_handbook_and_untying_adds_vd():
    assert design.param_count(ARCH) == 26_354_176
    assert design.param_count({**ARCH, "tied": False}) == 26_354_176 + 8192 * 512


def test_validation_names_each_broken_rule():
    assert design.validate(ARCH, tokenizer_vocab=8192) == []
    errs = " | ".join(design.validate({**ARCH, "width": 500}))
    assert "divisible by heads" in errs
    assert "divisible by KV heads" in " ".join(design.validate({**ARCH, "kv_heads": 3}))
    assert "even" in " ".join(design.validate({**ARCH, "width": 520}))  # 520 / 8 heads = 65
    assert "tokenizer" in " ".join(design.validate(ARCH, tokenizer_vocab=32000))


def test_pilot_arithmetic_is_labelled_estimated_until_measured():
    p = design.pilot(ARCH, tokens=50_000_000, seq=1024, micro=2, accum=8)
    assert p["tokens_per_update"] == 16_384 and p["updates"] == 3_052 and p["label"] == "estimated"
    assert p["disk_gb"] > 0 and p["vram_gb"] > 0 and p["hours"] > 0
    m = design.pilot(ARCH, tokens=50_000_000, seq=1024, micro=2, accum=8, measured={"tok_s": 100_000, "peak_vram_gb": 3.2})
    assert m["label"] == "measured" and m["vram_gb"] == 3.2 and abs(m["hours"] - 50_000_000 / 100_000 / 3600) < 1e-6


def test_lora_recipe_defaults_and_ollama_only_base_says_whats_missing():
    r = design.recipe("qlora")
    assert (r["quant"], r["r"], r["alpha"], r["dropout"], r["lr"], r["epochs"]) == ("nf4", 16, 32, 0.05, 1e-4, 1)
    b = design.resolve_base({"source": "ollama", "name": "llama3.2:latest"})
    assert not b["trainable"] and "Hugging Face" in b["missing"] and "GGUF" in b["missing"]
    assert design.resolve_base({"source": "hf", "repo": "Qwen/Qwen2.5-0.5B-Instruct", "revision": "main"})["trainable"]
    with pytest.raises(ValueError):
        design.resolve_base({"source": "hf", "repo": "../etc", "revision": "main"})


def test_network_target_is_validated_and_warns():
    with pytest.raises(ValueError):
        design.check_target("ftp://box:11434")
    assert design.check_target("http://192.168.1.20:11434")["private"]
    pub = design.check_target("https://gpu.example.com")
    assert not pub["private"] and "leave" in pub["warning"]
    fit = design.fit({"vram_gb": 24, "disk_free_gb": 100}, need_vram_gb=30, need_disk_gb=1)
    assert not fit["fits"] and "VRAM" in fit["notes"][0]
    assert design.fit({}, need_vram_gb=1, need_disk_gb=1)["fits"] is None  # an unknown host profile isn't a yes


# ---------- dataset ----------

def _project(name, created, n=6):
    p = store.put("project", {"name": name, "status": "ready", "created": created, "meta": {"duration": 60.0 * n}})
    pipeline.pdir(p["id"]).mkdir(parents=True, exist_ok=True)
    segs = [{"start": i * 10.0, "end": i * 10.0 + 9.5, "text": f"{name} line {i} says something memorable.", "words": []}
            for i in range(n * 6)]
    pipeline.save_json(p["id"], "transcript.json", {"segments": segs})
    return p


def _clip(p, start, end, rating=0, posted=False, ai_bounds=None, title="t"):
    c = pipeline.new_clip(p["id"], {"start": start, "end": end, "title": title, "hook": "h", "score": 70,
                                    "ai_bounds": ai_bounds}, source="ai")
    c = store.update("clip", c["id"], {"rating": rating, "posts": {"youtube": {"id": "x"}} if posted else {}})
    if rating:
        pipeline.remember(c, "rated")
    if posted:
        pipeline.remember(c, "published")
    return c


KINDS = ("project", "clip", "feedback", "pref", "train_dataset", "train_label", "train_release", "upload", "train_job", "train_scorecard",
         "model_selection", "model_release", "model_variant")


@pytest.fixture
def lib(monkeypatch):
    """A clean library for one test; every store doc of these kinds and the settings file are put back afterwards."""
    saved = {k: store.find(k) for k in KINDS}
    settings = config.SETTINGS_FILE.read_bytes() if config.SETTINGS_FILE.exists() else None
    for kind in KINDS:
        for d in store.find(kind):
            store.delete(kind, d["id"])
    yield from _library()
    for kind in KINDS:
        for d in store.find(kind):
            store.delete(kind, d["id"])
        for d in saved[kind]:
            store.put(kind, d)
    if settings is None:
        config.SETTINGS_FILE.unlink(missing_ok=True)
    else:
        config.SETTINGS_FILE.write_bytes(settings)


def _library():
    ps = [_project(f"p{i}", 1000.0 + i) for i in range(6)]
    clips = []
    for i, p in enumerate(ps):
        clips.append(_clip(p, 20.0, 50.0, rating=1, ai_bounds=[18.0, 52.0], title=f"good {i}"))
        clips.append(_clip(p, 100.0, 130.0, rating=-1, title=f"bad {i}"))
        clips.append(_clip(p, 200.0, 230.0, posted=True, title=f"posted {i}"))
    dataset.set_consent(all_projects=True)
    yield ps, clips


def test_nothing_is_trained_on_without_consent(lib):
    dataset.set_consent(all_projects=False)
    b = dataset.build()
    assert b["counts"]["sft"] == 0 and "confirm" in b["note"].lower()


def test_same_inputs_give_the_same_dataset_hash_and_bytes(lib):
    a, b = dataset.build(), dataset.build()
    assert a["hash"] == b["hash"] and a["counts"]["sft"] > 0
    for f in ("train.jsonl", "dev.jsonl", "test.jsonl", "dpo.jsonl", "sidecar.jsonl"):
        assert (Path(a["dir"]) / f).read_bytes() == (Path(b["dir"]) / f).read_bytes()


def test_split_is_grouped_by_project_with_a_frozen_later_test_set(lib):
    ps, _ = lib
    b = dataset.build()
    side = [json.loads(x) for x in (Path(b["dir"]) / "sidecar.jsonl").read_text("utf-8").splitlines()]
    where = {}
    for s in side:
        where.setdefault(s["group"], set()).add(s["split"])
    assert all(len(v) == 1 for v in where.values()), "one VOD's examples leaked across splits"
    test_groups = {g for g, v in where.items() if "test" in v}
    assert test_groups and test_groups <= {ps[-1]["id"], ps[-2]["id"]}  # the latest projects by time
    late = _project("p-new", 99999.0)  # a newer project after freezing never joins (and so never changes) the test set
    _clip(late, 20.0, 50.0, rating=1)
    side2 = [json.loads(x) for x in (Path(dataset.build()["dir"]) / "sidecar.jsonl").read_text("utf-8").splitlines()]
    assert {s["group"] for s in side2 if s["split"] == "test"} == test_groups


def test_sidecar_records_sources_revision_versions_and_reviewer(lib):
    b = dataset.build()
    sides = [json.loads(x) for x in (Path(b["dir"]) / "sidecar.jsonl").read_text("utf-8").splitlines()]
    assert {x["task"] for x in sides} == {"find", "judge"}
    s = next(x for x in sides if x["task"] == "find")
    assert {"id", "task", "group", "split", "sources", "revisions", "schema", "feature_version", "reviewer"} <= set(s)
    assert s["reviewer"] == "user" and s["schema"].startswith("MomentList")


def test_sft_example_uses_the_apps_own_find_prompt(lib, monkeypatch):
    ps, _ = lib
    seen = {}

    def capture(system, user, schema, **k):
        seen.update(system=system, user=user)
        return ai.MomentList(clips=[])

    monkeypatch.setattr(ai, "_ask", capture)
    segs = pipeline.load_json(ps[0]["id"], "transcript.json")["segments"]
    s = config.load()
    ai._find_pass(segs, 360.0, {"video_type": None}, s["clip_count"], s["clip_min"], s["clip_max"],
                  target=dataset.target_len(s))
    ex = dataset.find_examples(ps[0], segs, {})[0]
    assert ex["messages"][0]["content"] == seen["system"]
    assert ex["messages"][1]["content"] == seen["user"][0] + "\n\n" + seen["user"][1]
    target = json.loads(ex["messages"][2]["content"])
    assert {(c["start"], c["end"]) for c in target["clips"]} == {(20.0, 50.0), (200.0, 230.0)}  # kept + published, not 👎


def test_dpo_pair_prefers_the_users_trim_over_the_ai_bounds(lib):
    b = dataset.build()
    pairs = [json.loads(x) for x in (Path(b["dir"]) / "dpo.jsonl").read_text("utf-8").splitlines()]
    assert pairs
    chosen, rejected = json.loads(pairs[0]["chosen"]), json.loads(pairs[0]["rejected"])
    assert [20.0, 50.0] in [[c["start"], c["end"]] for c in chosen["clips"]]
    assert [18.0, 52.0] in [[c["start"], c["end"]] for c in rejected["clips"]]


def test_a_clip_deleted_later_leaves_the_next_build_and_lineage_remembers(lib):
    ps, clips = lib
    first = dataset.build()
    release.record_release("rc001", {"dataset": first["hash"]})
    gone = clips[0]
    pipeline.delete_clip(gone["id"])
    second = dataset.build()
    assert second["hash"] != first["hash"]
    side = (Path(second["dir"]) / "sidecar.jsonl").read_text("utf-8")
    assert gone["id"] not in side
    assert dataset.lineage(gone["id"]) == {"datasets": [first["hash"]], "releases": ["rc001"]}


def test_excluding_an_example_drops_it(lib):
    b = dataset.build()
    s = json.loads((Path(b["dir"]) / "sidecar.jsonl").read_text("utf-8").splitlines()[0])
    dataset.set_excluded(s["id"], True)
    assert s["id"] not in (Path(dataset.build()["dir"]) / "sidecar.jsonl").read_text("utf-8")


def test_teacher_labels_need_a_confirmed_estimate(lib, monkeypatch):
    called = []
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: called.append(1) or ai.MomentList(clips=[]))
    est = dataset.teacher_label([lib[0][0]["id"]], confirm=False)
    assert not called and est["confirm_needed"] and "usd" in est
    res = dataset.teacher_label([lib[0][0]["id"]], confirm=True)
    assert called and res["labelled"] == 1


# ---------- GPU lease, worker progress ----------

def test_gpu_lease_refuses_while_obs_records_and_pauses_a_running_job(monkeypatch, tmp_path):
    monkeypatch.setattr(obs, "recording", lambda: [{"name": "Main PC"}])
    assert "recording" in runner.block_reason().lower()
    with pytest.raises(RuntimeError):
        runner.lease("job1")
    job = {"id": "j", "status": "running", "dir": str(tmp_path), "paused_by": None}
    assert runner.watchdog(job)["status"] == "paused" and (tmp_path / "control" / "pause").exists()
    monkeypatch.setattr(obs, "recording", lambda: [])
    monkeypatch.setattr(runner, "_rendering", lambda: False)
    assert runner.watchdog({**job, "status": "paused", "paused_by": "auto"})["status"] == "running"
    assert not (tmp_path / "control" / "pause").exists()
    manual = {**job, "status": "paused", "paused_by": "user"}  # a manual pause outlasts the block
    assert runner.watchdog(manual)["status"] == "paused"


def test_one_training_job_at_a_time(monkeypatch):
    monkeypatch.setattr(obs, "recording", lambda: [])
    monkeypatch.setattr(runner, "_rendering", lambda: False)
    runner.lease("a")
    try:
        with pytest.raises(RuntimeError, match="already"):
            runner.lease("b")
    finally:
        runner.release_lease("a")
    runner.lease("b")
    runner.release_lease("b")


def test_qlora_and_lora_take_the_adapter_path_in_the_worker():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rw_worker", runner.worker_file())
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)  # no torch at import time
    assert w.is_lora("qlora") and w.is_lora("lora") and not w.is_lora("scratch")


def test_existing_env_must_be_a_python_or_a_wsl_command(tmp_path):
    for argv, wsl in ((["cmd.exe", "/c", "x"], False), (["//server/share/python.exe"], False), (["notepad.exe"], True)):
        with pytest.raises(ValueError):
            runner.use_existing(argv, wsl)


def test_unzip_refuses_paths_outside_the_folder(tmp_path):
    import zipfile
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../escape.txt", "x")
    with pytest.raises(RuntimeError, match="Unsafe"):
        runner._extract(z, tmp_path / "out", strip=False)
    assert not (tmp_path / "escape.txt").exists()


def test_worker_progress_lines_are_parsed_and_noise_is_kept_as_log():
    assert runner.parse_progress('{"event": "step", "step": 3, "loss": 2.5, "tok_s": 51000}') == \
        {"event": "step", "step": 3, "loss": 2.5, "tok_s": 51000}
    assert runner.parse_progress("Loading checkpoint shards: 100%") == {"event": "log", "text": "Loading checkpoint shards: 100%"}
    assert runner.parse_progress("[1, 2]")["event"] == "log"
    assert runner.parse_progress("\x1b[1Gwriting manifest \x1b[K") == {"event": "log", "text": "writing manifest"}
    job = {"metrics": [], "log": []}
    for line in ('{"event": "step", "step": 1, "loss": 3.0, "tok_s": 10}', '{"event": "done", "peak_vram_gb": 1.5}', "warn"):
        runner.apply_progress(job, runner.parse_progress(line))
    assert job["metrics"] == [{"step": 1, "loss": 3.0, "tok_s": 10}] and job["result"]["peak_vram_gb"] == 1.5
    assert job["log"] == ["warn"]


# ---------- export ----------

def test_export_commands(tmp_path):
    env = {"python": [str(tmp_path / "venv" / "Scripts" / "python.exe")], "root": str(tmp_path)}
    cmds = dict(runner.export_commands(env, tmp_path / "job", "reelwright-specialist:rc003", lora=True))
    assert list(cmds) == ["merge", "convert", "quantize", "create"]
    conv = cmds["convert"]
    assert conv[1].endswith("worker.py") and "convert" in conv and "--outtype" in conv and "f16" in conv
    assert any(a.endswith("convert_hf_to_gguf.py") for a in conv)
    q = cmds["quantize"]
    assert q[0].endswith("llama-quantize.exe") and q[-1] == "Q4_K_M" and q[1].endswith("model-f16.gguf")
    assert cmds["create"][:3] == ["ollama", "create", "reelwright-specialist:rc003"] and cmds["create"][-1].endswith("Modelfile")
    assert "merge" not in dict(runner.export_commands(env, tmp_path / "job", "x:rc1", lora=False))
    wsl = {"python": ["wsl", "-d", "Ubuntu", "--", "/home/u/venv/bin/python"], "root": "C:\\rw\\env", "wsl": True}
    conv = dict(runner.export_commands(wsl, Path("C:/rw/job"), "x:rc1", lora=False))["convert"]
    assert "/mnt/c/rw/job/merged" in conv


def test_a_scratch_models_merged_folder_is_the_model_and_stays(tmp_path):
    for lora in (True, False):
        d = tmp_path / str(lora)
        (d / "merged").mkdir(parents=True)
        (d / "merged" / "model.safetensors").write_bytes(b"w" * 10)
        (d / "model-f16.gguf").write_bytes(b"f" * 20)
        (d / "model-q4_k_m.gguf").write_bytes(b"q")
        assert runner.drop_export_intermediates(d, lora) == {"export_freed": 20 + (10 if lora else 0)}
        assert not (d / "model-f16.gguf").exists() and (d / "model-q4_k_m.gguf").exists()
        assert (d / "merged").exists() is not lora  # scratch: the worker trained straight into merged/, a re-export needs it


def test_worker_progress_is_batched_into_few_store_writes(monkeypatch, tmp_path):
    """Each write rewrites the whole job doc (2000 metrics + 200 log lines): one per stdout line was ~2 a second."""
    j = store.put("train_job", {"kind": "train", "status": "running", "gpu": False, "dir": str(tmp_path), "spec": {}})
    lines = [json.dumps({"event": "step", "step": i, "loss": 1.0, "tok_s": 5}) + "\n" for i in range(300)] + ["warn\n"]

    class Proc:
        stdout = iter(lines)

        def wait(self):
            return 0
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: Proc())
    writes, real = [], store.update
    monkeypatch.setattr(store, "update", lambda kind, id, *a, **k: (writes.append(kind) if id == j["id"] else None) or real(kind, id, *a, **k))
    runner._subprocess(j["id"], ["python", "worker.py"])
    done = store.get("train_job", j["id"])
    assert len(done["metrics"]) == 300 and done["log"] == ["warn"]  # nothing lost: the last batch is always written
    assert len(writes) <= 3
    store.delete("train_job", j["id"])


def test_modelfile_for_a_scratch_model_carries_its_chat_template():
    mf = runner.modelfile("model-q4_k_m.gguf", scratch=True)
    assert mf.startswith("FROM ./model-q4_k_m.gguf") and "<|assistant|>" in mf and 'PARAMETER stop "<|end|>"' in mf
    assert "TEMPLATE" not in runner.modelfile("m.gguf", scratch=False)


def test_requirements_are_pinned_exactly():
    reqs = runner.requirements_text()
    pkgs = [x for x in reqs.splitlines() if x and not x.startswith(("#", "--"))]
    assert pkgs and all("==" in x for x in pkgs)
    assert "download.pytorch.org" in reqs and any(x.startswith("torch==") for x in pkgs)


def test_python_detection_prefers_a_version_with_cuda_torch_wheels(monkeypatch):
    out = " -V:3.14 *        C:\\Python314\\python.exe\n -V:3.12          C:\\Py312\\python3.12.exe\n"
    monkeypatch.setattr(runner, "_run_text", lambda argv, **k: out)
    assert runner.pick_python()["path"].endswith("python3.12.exe")
    monkeypatch.setattr(runner, "_run_text", lambda argv, **k: " -V:3.9 *  C:\\Py39\\python.exe\n")
    with pytest.raises(RuntimeError, match="3.12"):
        runner.pick_python()


# ---------- evaluate & release ----------

def _card(evals, test, n_test=10):
    return {"evals_summary": {"precision_at_k": evals}, "test_summary": {"precision_at_k": test},
            "test": {f"e{i}": {} for i in range(n_test)}, "evals": {"case1": {"precision_at_k": evals}}, "inputs": "h"}


def test_promote_gate():
    ok, why = release.gate(_card(0.6, 0.70), _card(0.6, 0.60), margin=0.05, tolerance=0.02)
    assert ok, why
    ok, why = release.gate(_card(0.6, 0.62), _card(0.6, 0.60), margin=0.05, tolerance=0.02)
    assert not ok and "margin" in why
    ok, why = release.gate(_card(0.5, 0.80), _card(0.6, 0.60), margin=0.05, tolerance=0.02)
    assert not ok and "regress" in why
    ok, why = release.gate(_card(0.6, 0.90, n_test=3), _card(0.6, 0.60, n_test=3), margin=0.05, tolerance=0.02)
    assert not ok and "evals" in why  # 3 test examples are noise: the evals decide, and they're tied
    assert release.next_id() == "rc001"


def test_a_failed_candidate_keeps_the_active_release_and_rollback_restores(lib):
    config.save({"ai_routes": {}, "ai_providers": {"ollama": {"model": "qwen3:8b"}}})
    release.record_release("rc001", {"dataset": "d1"}, tag="reelwright-specialist:rc001", digest="a" * 64, card=_card(0.6, 0.7),
                           model={"params": 26_354_176, "context": 32768})  # the find workload needs a long context
    assert release.promote("rc001", task="find", active_card=_card(0.6, 0.6), margin=0.05, tolerance=0.02)["promoted"]
    assert ai.route("find") == "ollama" and ai.provider_cfg("ollama", "find")["model"] == "reelwright-specialist:rc001"
    release.record_release("rc002", {"dataset": "d2"}, tag="reelwright-specialist:rc002", digest="b" * 64, card=_card(0.6, 0.71))
    r = release.promote("rc002", task="find", active_card=_card(0.6, 0.7), margin=0.05, tolerance=0.02)
    assert not r["promoted"] and r["reason"]
    assert ai.provider_cfg("ollama", "find")["model"] == "reelwright-specialist:rc001"  # the active release stays
    assert store.get("train_release", "rc002")["status"] == "candidate" and store.get("train_release", "rc002")["reason"]
    release.rollback("find")
    assert ai.route("find") == "anthropic" and ai.provider_cfg("ollama", "find")["model"] == "qwen3:8b"
    assert store.get("train_release", "rc001")["status"] == "rolled_back"


def test_release_manifest_records_every_hash(lib, tmp_path):
    f = tmp_path / "m.gguf"
    f.write_bytes(b"gguf")
    m = release.manifest({"base": "b" * 64, "adapter": None, "gguf": release.sha256(f), "ollama_digest": "sha256:abc",
                          "dataset": "d" * 64, "config": release.config_hash({"r": 16})})
    assert set(m["hashes"]) == {"base", "adapter", "gguf", "ollama_digest", "dataset", "config"}
    assert len(m["hashes"]["gguf"]) == 64 and m["pins"] and m["llama_cpp"]["tag"]


def test_scorecard_runs_evals_and_frozen_test_with_the_same_inputs(lib, monkeypatch):
    b = dataset.build()
    calls = []

    def asker(system, user):
        calls.append((system, user))
        return {"clips": [{"start": 20.0, "end": 50.0, "title": "x", "hook": "", "reason": "", "score": 90, "hashtags": []}]}

    a, c = release.scorecard(asker, b), release.scorecard(asker, b)
    assert a["inputs"] == c["inputs"] and a["test_summary"]["precision_at_k"] is not None
    assert set(a["evals"]) and calls


# ---------- privacy, ownership ----------

def test_privacy_policy_discloses_every_training_download_host():
    import re
    root = Path(runner.__file__).parent
    code = "\n".join(p.read_text("utf-8") for p in root.glob("*.py"))
    policy = (root.parent / "static" / "legal" / "privacy.html").read_text("utf-8").lower()
    hosts = set(re.findall(r"https?://([a-z0-9.-]+\.[a-z]{2,})", code)) - {"localhost"}
    assert {"download.pytorch.org", "github.com"} <= hosts  # the pinned torch index and llama.cpp
    missing = [h for h in hosts | {"pypi.org", "huggingface.co"} if h not in policy]  # pip and HF downloads name no URL
    assert not missing, f"privacy.html doesn't disclose: {missing}"


def test_the_app_never_imports_torch_for_training():
    for p in Path(runner.__file__).parent.glob("*.py"):
        if p.name == "worker.py":
            continue
        src = p.read_text("utf-8")
        assert "import torch" not in src and "import transformers" not in src and "import peft" not in src, p.name


# ---------- real torch (only with a training env) ----------

@pytest.mark.skipif(not os.environ.get("REELWRIGHT_TRAIN_PY"), reason="set REELWRIGHT_TRAIN_PY to a training env's python")
def test_param_count_matches_llama_for_causal_lm_in_the_worker_env(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"arch": ARCH}), "utf-8")
    out = subprocess.run([os.environ["REELWRIGHT_TRAIN_PY"], str(Path(runner.__file__).with_name("worker.py")), "params",
                          "--config", str(cfg)], capture_output=True, text=True, timeout=300)
    lines = [runner.parse_progress(x) for x in out.stdout.splitlines()]
    assert {"event": "params", "count": 26_354_176} in [{k: x.get(k) for k in ("event", "count")} for x in lines]


def test_settings_route_cannot_point_training_at_a_program():
    """Which program training runs may only be set through the validated training routes."""
    from fastapi.testclient import TestClient
    from reelwright import config
    from reelwright.server import PORT, app
    before = {k: config.load()[k] for k in ("training_python", "training_wsl", "training_env_dir")}
    c = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
    c.put("/api/settings", json={"training_python": ["C:/evil.exe"], "training_wsl": True, "training_env_dir": "C:/x"})
    after = {k: config.load()[k] for k in before}
    assert after == before


def test_wsl_command_must_be_system_wsl_distro_and_an_absolute_python():
    import pytest
    from reelwright.training import runner
    for bad in (["wsl", "-e", "sh", "-c", "rm -rf /"], ["C:/tmp/wsl.exe", "--", "/usr/bin/python3"],
                ["wsl", "--", "python3"], ["wsl", "-d", "Ubu;ntu", "--", "/usr/bin/python3"],
                ["wsl", "--exec", "/usr/bin/python3"], ["wsl", "--", "/usr/bin/python3", "-c", "x"]):
        with pytest.raises(ValueError):
            runner.use_existing(bad, wsl=True)
    assert runner.wsl_argv("Ubuntu-22.04", "/home/me/rw/bin/python3.12")[-3:] == ["Ubuntu-22.04", "--", "/home/me/rw/bin/python3.12"]
