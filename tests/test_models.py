"""Model marketplace (reelwright/models): catalog, hardware fit, versioned selection and release gates. No network:
Ollama and Hugging Face go through an httpx.MockTransport, nvidia-smi through a fake subprocess runner."""
import json
import os
import subprocess
import tempfile

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import httpx  # noqa: E402
import pytest  # noqa: E402

from reelwright import ai, config, store  # noqa: E402
from reelwright import models  # noqa: E402
from reelwright.models import catalog, fit, hardware, selections as selection  # noqa: E402

GIB = 1024 ** 3


def hw(gpus=((24, 18),), ram=(64, 40), disk=200, loaded=()):
    """A probed machine: gpus as (total, free) GiB."""
    return {"gpus": [{"index": i, "name": "GPU", "total_gib": t, "free_gib": f, "used_gib": t - f} for i, (t, f) in enumerate(gpus)],
            "ram_total_gib": ram[0], "ram_free_gib": ram[1], "disk_free_gib": disk, "driver": "1", "telemetry": "ok",
            "loaded": list(loaded), "at": 1e12}


def variant(**kw):
    v = {"id": "ollama:abc", "source": "ollama", "name": "m:latest", "digest": "abc", "provider": "ollama", "installed": True,
         "size_bytes": 4 * GIB, "layers": 32, "kv_heads": 8, "head_dim": 128, "context": 131072, "task": ["text-generation"],
         "inputs": ["text"], "outputs": ["text"], "family": "llama", "quant": "Q4_K_M", "params_b": 8.0}
    v.update(kw)
    return v


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("OLLAMA_KV_CACHE_TYPE", raising=False)  # fixtures assume Ollama's default f16 KV cache
    keep = config.load()

    def wipe():  # before and after: an activated release left in the shared store would reroute other modules' tests
        for kind in ("model_variant", "model_selection", "model_release", "hf_cache"):
            for d in store.find(kind):
                store.delete(kind, d["id"])
    wipe()
    yield
    wipe()
    config.save({k: keep[k] for k in ("ai_routes", "ai_providers", "model_hw_profiles", "model_hw_profile", "model")})


@pytest.fixture
def net(monkeypatch):
    sent, box = [], {}

    def handler(req):
        sent.append(str(req.url))
        out = box["fn"](req, json.loads(req.content or b"{}") if req.method == "POST" else {})
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)

    monkeypatch.setattr(catalog, "TRANSPORT", httpx.MockTransport(handler))
    return type("Net", (), {"sent": sent, "reply": staticmethod(lambda fn: box.update(fn=fn))})


# ---------- the handbook's acceptance fixture ----------

def test_acceptance_fixture_budget_percent_headroom_and_status():
    prof = hardware.profile(hw=hw(), reserve_gib=3)
    assert prof["budget_gib"] == 15
    a = fit.assess(8, 10, total_gib=24, budget_gib=15)
    assert a["status"] == "fits" and a["pct_total"] == [33, 42] and a["left_gib"] == [5, 7]
    assert fit.assess(14, 17, total_gib=24, budget_gib=15)["status"] == "borderline"
    c = fit.assess(26, 30, total_gib=24, budget_gib=15)
    assert c["status"] == "incompatible" and c["pct_total"] == [108, 125] and c["short_gib"] == [11, 15]


def test_kv_cache_example_is_one_gib():
    assert fit.kv_bytes(layers=32, kv_heads=8, head_dim=128, bytes_per=2, positions=8192) == GIB


def test_kv_dtype_follows_ollamas_own_setting(monkeypatch):
    v, prof = variant(size_bytes=4 * GIB), hardware.profile(hw=hw(), reserve_gib=0)
    f16 = fit.estimate(v, {"context": 8192}, prof)["range_gib"][0]
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q8_0")
    monkeypatch.setenv("OLLAMA_FLASH_ATTENTION", "1")
    assert fit.estimate(v, {"context": 8192}, prof)["range_gib"][0] == pytest.approx(f16 - 0.5)  # 1 GiB of f16 KV -> 0.5


def test_missing_metadata_is_unknown():
    e = fit.estimate(variant(size_bytes=None, params_b=None), profile=hardware.profile(hw=hw(), reserve_gib=3))
    assert e["status"] == "unknown"


# ---------- estimator ----------

def test_changing_reserve_context_or_concurrency_recomputes():
    v = variant(size_bytes=12 * GIB)
    base = fit.estimate(v, {"context": 8192}, hardware.profile(hw=hw(), reserve_gib=0))
    assert base["status"] == "fits"
    more_reserve = fit.estimate(v, {"context": 8192}, hardware.profile(hw=hw(), reserve_gib=6))
    assert more_reserve["budget_gib"] == 12 and more_reserve["status"] != "fits"
    longer = fit.estimate(v, {"context": 32768}, hardware.profile(hw=hw(), reserve_gib=0))
    wider = fit.estimate(v, {"context": 8192, "concurrency": 4}, hardware.profile(hw=hw(), reserve_gib=0))
    assert longer["range_gib"][0] == pytest.approx(base["range_gib"][0] + 3)  # 4x the positions: +3 GiB of KV
    assert wider["range_gib"] == longer["range_gib"]


def test_telemetry_denied_reads_as_unknown(monkeypatch):
    def denied(*a, **k):
        raise PermissionError("access denied")
    monkeypatch.setattr(hardware, "RUN", denied)
    p = hardware.probe()
    assert p["telemetry"] == "denied" and p["gpus"] == []
    e = fit.estimate(variant(), profile=hardware.profile(hw=p, reserve_gib=0))
    assert e["status"] == "unknown" and any(c["name"] == "vram" and c["status"] == "unknown" for c in e["checks"])


def test_probe_parses_nvidia_smi(monkeypatch):
    out = "0, NVIDIA GeForce RTX 4090, 24576, 6144, 18432, 621.01\n"
    monkeypatch.setattr(hardware, "RUN", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    p = hardware.probe()
    assert p["gpus"][0]["total_gib"] == 24 and p["gpus"][0]["free_gib"] == 18 and p["driver"] == "621.01" and p["at"]


def test_already_loaded_model_is_not_counted_twice():
    v = variant(size_bytes=14 * GIB)
    cold = fit.estimate(v, {"context": 8192}, hardware.profile(hw=hw(gpus=((24, 6),)), reserve_gib=0))
    warm = fit.estimate(v, {"context": 8192},
                        hardware.profile(hw=hw(gpus=((24, 6),), loaded=[{"digest": "abc", "size_vram_gib": 15}]), reserve_gib=0))
    assert cold["status"] == "incompatible"
    assert warm["status"] == "fits" and warm["budget_gib"] == 21 and warm["loaded"]


def test_two_gpus_with_asymmetric_free_memory():
    prof = hardware.profile(hw=hw(gpus=((24, 4), (24, 20))), reserve_gib=3)
    e = fit.estimate(variant(size_bytes=10 * GIB), {"context": 8192}, prof)
    assert e["status"] == "fits" and e["gpu"] == 1 and e["budget_gib"] == 17
    big = variant(size_bytes=16 * GIB)
    assert fit.estimate(big, {"context": 8192}, prof)["status"] == "incompatible"
    split = fit.estimate(big, {"context": 8192, "placement": "split"}, prof)
    assert split["budget_gib"] == 18 and split["status"] in ("fits", "borderline")


def test_enough_vram_but_not_enough_ram_names_the_failing_check():
    e = fit.estimate(variant(size_bytes=6 * GIB), {"context": 8192}, hardware.profile(hw=hw(ram=(8, 2)), reserve_gib=0))
    assert e["status"] == "fits"
    ram = next(c for c in e["checks"] if c["name"] == "ram")
    assert ram["status"] == "fail" and not e["ok"]


def test_training_fit_is_separate_from_inference_fit():
    v = variant(size_bytes=5 * GIB, params_b=8.0)
    prof = hardware.profile(hw=hw(), reserve_gib=3)
    run = fit.estimate(v, {"op": "run", "context": 4096}, prof)
    full = fit.estimate(v, {"op": "full-train", "context": 4096}, prof)
    assert run["status"] == "fits" and full["status"] == "incompatible" and full["op"] == "full-train"


def test_measured_fit_is_labelled_tested():
    v = variant(measured={"vram_gib": 7.5, "context": 8192, "at": "2026-11-24T10:00:00+00:00"})
    e = fit.estimate(v, {"context": 8192}, hardware.profile(hw=hw(), reserve_gib=3))
    assert e["source"] == "tested" and e["range_gib"] == [7.5, 7.5] and e["measured_at"].startswith("2026-11-24")


# ---------- catalog ----------

def ollama_fake(tags, shows):
    def fn(req, body):
        if req.url.path == "/api/tags":
            return {"models": tags}
        if req.url.path == "/api/show":
            return shows[body["model"]]
        if req.url.path == "/api/ps":
            return {"models": []}
        return httpx.Response(404)
    return fn


TAGS = [{"name": "llama3.2:latest", "digest": "a80c4f17acd5" + "0" * 52, "size": 2019393189,
         "details": {"format": "gguf", "family": "llama", "parameter_size": "3.2B", "quantization_level": "Q4_K_M"}},
        {"name": "giant:405b", "digest": "fe" * 32, "size": 230 * GIB,
         "details": {"format": "gguf", "family": "llama", "parameter_size": "405B", "quantization_level": "Q4_K_M"}}]
SHOW = {"llama3.2:latest": {"capabilities": ["completion", "tools"], "model_info": {
    "general.architecture": "llama", "llama.block_count": 28, "llama.attention.head_count_kv": 8, "llama.attention.key_length": 128,
    "llama.context_length": 131072}},
        "giant:405b": {"capabilities": ["completion", "vision"], "model_info": {
    "general.architecture": "llama", "llama.block_count": 126, "llama.attention.head_count_kv": 8, "llama.attention.key_length": 128,
    "llama.context_length": 131072}}}


def test_ollama_sync_reads_digest_shape_and_capabilities(net):
    net.reply(ollama_fake(TAGS, SHOW))
    vs = catalog.sync_ollama()
    v = next(x for x in vs if x["name"] == "llama3.2:latest")
    assert v["id"] == "ollama:" + TAGS[0]["digest"] and v["layers"] == 28 and v["kv_heads"] == 8 and v["head_dim"] == 128
    assert v["installed"] and "tools" in v["capabilities"] and v["src"]["layers"] == "publisher"
    giant = next(x for x in vs if x["name"] == "giant:405b")
    assert "image" in giant["inputs"] and "image" not in giant["outputs"]  # accepts images, doesn't generate them


def test_incompatible_model_is_searchable_and_opens_but_the_fit_filter_excludes_it(net):
    net.reply(ollama_fake(TAGS, SHOW))
    catalog.sync_ollama()
    prof = hardware.profile(hw=hw(), reserve_gib=3)
    found = catalog.search("giant", profile=prof)
    assert [e["name"] for e in found] == ["giant:405b"] and found[0]["fit"]["status"] == "incompatible"
    assert catalog.get(found[0]["id"])["name"] == "giant:405b"
    assert catalog.search("giant", {"fit": True}, profile=prof) == []
    assert [e["name"] for e in catalog.search("", {"fit": True}, profile=prof)] == ["llama3.2:latest"]


def test_search_exact_alias_first_then_typo_tolerant(net):
    net.reply(ollama_fake(TAGS, SHOW))
    catalog.sync_ollama()
    assert catalog.search("llama3.2:latest")[0]["name"] == "llama3.2:latest"
    assert catalog.search("lamma3.2")[0]["name"] == "llama3.2:latest"  # a typo still finds it
    assert {e["name"] for e in catalog.search("", {"input": "image"})} == {"giant:405b"}
    assert catalog.search("", {"output": "image"}) == []


def test_hf_search_is_paginated_cached_and_labelled(net):
    calls = []

    def fn(req, body):
        calls.append(dict(req.url.params))
        return httpx.Response(200, json=[{"id": "Qwen/Qwen3-8B-GGUF", "sha": "deadbeef", "pipeline_tag": "text-generation",
                                          "tags": ["gguf", "base_model:Qwen/Qwen3-8B"], "downloads": 10, "likes": 2,
                                          "siblings": [{"rfilename": "Qwen3-8B-Q4_K_M.gguf"}, {"rfilename": "Qwen3-8B-Q8_0.gguf"}]}],
                              headers={"Link": '<https://huggingface.co/api/models?search=qwen&cursor=abc>; rel="next"'})
    net.reply(fn)
    r = catalog.sync_hf(search="qwen", pipeline_tag="text-generation", limit=5)
    assert r["next"] and r["synced_at"] and len(r["entries"]) == 2
    assert calls[0]["search"] == "qwen" and calls[0]["limit"] == "5" and calls[0]["pipeline_tag"] == "text-generation"
    q4 = next(e for e in r["entries"] if e["quant"] == "Q4_K_M")
    assert q4["id"] == "hf:Qwen/Qwen3-8B-GGUF@deadbeef/Qwen3-8B-Q4_K_M.gguf" and q4["revision"] == "deadbeef" and not q4["installed"]
    assert q4["src"]["size_bytes"] == "unknown"
    catalog.sync_hf(search="qwen", pipeline_tag="text-generation", limit=5)
    assert len(calls) == 1  # served from the cache
    with pytest.raises(ValueError):
        catalog.sync_hf(next_url="https://evil.example/api/models")


def test_manifest_ships_curated_choices():
    m = catalog.manifest()
    tasks = {t for e in m for t in e["task"]}
    assert {"text-generation", "image-text-to-text", "feature-extraction", "automatic-speech-recognition"} <= tasks


def test_import_local_gguf_reads_header(tmp_path):
    import struct

    def s(x):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kv = [("general.architecture", 8, s("llama")), ("llama.block_count", 4, struct.pack("<I", 16)),
          ("llama.attention.head_count_kv", 4, struct.pack("<I", 4)), ("llama.attention.head_count", 4, struct.pack("<I", 16)),
          ("llama.embedding_length", 4, struct.pack("<I", 2048)), ("llama.context_length", 4, struct.pack("<I", 4096))]
    body = b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv)) + b"".join(s(k) + struct.pack("<I", t) + v for k, t, v in kv)
    p = tmp_path / "tiny.gguf"
    p.write_bytes(body + b"\0" * 1000)
    v = catalog.import_gguf(str(p))
    assert v["source"] == "gguf" and v["layers"] == 16 and v["kv_heads"] == 4 and v["head_dim"] == 128 and v["digest"]
    assert catalog.get(v["id"])["context"] == 4096
    with pytest.raises(ValueError):
        catalog.import_gguf(str(tmp_path / "missing.gguf"))


# ---------- scoring ----------

HANDBOOK = [{"name": "quality", "worst": 0.70, "best": 0.90, "weight": 0.5},
            {"name": "support", "worst": 0.90, "best": 1.00, "weight": 0.3},
            {"name": "latency", "worst": 2.8, "best": 1.2, "weight": 0.2}]


def test_appropriateness_score_regression_fixture():
    a = selection.score({"quality": 0.84, "support": 0.96, "latency": 1.2}, HANDBOOK)
    b = selection.score({"quality": 0.88, "support": 0.98, "latency": 2.8}, HANDBOOK)
    assert (a, b) == (73, 69)
    ranked = selection.rank({"a": {"quality": 0.84, "support": 0.96, "latency": 1.2},
                             "b": {"quality": 0.88, "support": 0.98, "latency": 2.8},
                             "c": {"quality": 0.95, "support": None, "latency": 1.0}}, HANDBOOK)
    assert [r["id"] for r in ranked] == ["a", "b", "c"] and ranked[2]["rank"] is None and ranked[2]["score"] is None


def test_invalid_bounds_and_weights_are_rejected():
    with pytest.raises(ValueError):
        selection.score({"quality": 0.8}, [{"name": "quality", "worst": 0.9, "best": 0.9, "weight": 1}])
    with pytest.raises(ValueError):
        selection.score({"quality": 0.8, "support": 0.9}, [{"name": "quality", "worst": 0.7, "best": 0.9, "weight": 0.5},
                                                          {"name": "support", "worst": 0.9, "best": 1, "weight": 0.4}])


# ---------- selection & release ----------

def test_migration_keeps_the_current_routing():
    config.save({"ai_routes": {"find": "ollama"}, "ai_providers": {"ollama": {"model": "llama3.2:latest"}}})
    before = {t: ai.route(t) for t in ai.TASKS}
    selection.migrate()
    for task in selection.TASKS:
        sel = models.selection(task)
        assert sel["mode"]["kind"] == "existing" and sel["origin"] == "settings"
    assert models.selection("find")["provider"] == "ollama" and models.selection("find")["model"] == "llama3.2:latest"
    assert {t: ai.route(t) for t in ai.TASKS} == before and selection.routing("find") is None
    selection.migrate()  # idempotent
    assert len(store.find("model_selection", task="find")) == 1


def _installed(digest="d1", name="llama3.2:latest", **kw):
    return catalog.put(variant(**{"id": f"ollama:{digest}", "digest": digest, "name": name, "size_bytes": 2 * GIB, **kw}))


def test_activation_routes_the_task_and_rollback_restores(monkeypatch):
    monkeypatch.setattr(selection, "_profile", lambda: hardware.profile(hw=hw(), reserve_gib=3))
    config.save({"ai_routes": {}, "ai_providers": {"ollama": {"model": ""}}})
    selection.migrate()
    v = _installed()
    cand = models.propose("find", v["id"], workload={"context": 8192})
    assert cand["digest"] == "d1" and cand["hardware"]["budget_gib"] == 15 and cand["gates"]["fit"] == "pass"
    rel = models.release("find")
    out = models.activate("find", cand["id"], expected_rev=rel["rev"])
    assert out["ok"] and models.release("find")["active"] == cand["id"]
    assert ai.route("find") == "ollama" and ai.provider_cfg("ollama", "find")["model"] == "llama3.2:latest" and ai.ready("find")
    assert ai.route("classify") == "anthropic"  # other tasks untouched
    with pytest.raises(selection.Conflict):
        models.activate("find", cand["id"], expected_rev=rel["rev"])  # stale revision
    back = models.rollback("find")
    assert back["active"] != cand["id"] and ai.route("find") == "anthropic" and selection.routing("find") is None


def test_failed_candidate_keeps_the_active_release(monkeypatch):
    monkeypatch.setattr(selection, "_profile", lambda: hardware.profile(hw=hw(), reserve_gib=3))
    selection.migrate()
    good = models.propose("find", _installed()["id"], workload={"context": 8192})
    models.activate("find", good["id"])
    huge = models.propose("find", _installed("d2", "giant:405b", **{"size_bytes": 230 * GIB})["id"], workload={"context": 8192})
    assert huge["gates"]["fit"] == "fail"
    out = models.activate("find", huge["id"])
    rel = models.release("find")
    assert not out["ok"] and rel["active"] == good["id"] and rel["candidate"] == huge["id"] and rel["candidate_status"] == "failed"
    forced = models.activate("find", huge["id"], override_reason="I'll close OBS first")
    assert forced["ok"] and store.get("model_selection", huge["id"])["choice"]["override_reason"] == "I'll close OBS first"


def test_selection_needs_a_digest_and_the_scratch_mode_is_a_tagged_union():
    nameless = catalog.put(variant(id="manifest:x", source="manifest", digest=None, installed=False))
    with pytest.raises(ValueError):
        models.propose("find", nameless["id"])
    base = _installed()
    s = models.propose("specialist", base["id"], mode={"kind": "scratch", "recipe": {"dataset": "clips-v1"}})
    assert s["mode"] == {"kind": "scratch", "base": "d1", "recipe": {"dataset": "clips-v1"}}
    with pytest.raises(ValueError):
        models.propose("find", base["id"], mode={"kind": "sideways"})


def test_benchmark_records_a_measured_fit_with_a_date(net, monkeypatch):
    v = _installed()

    def fn(req, body):
        if req.url.path == "/api/generate":
            return {"response": "ok", "eval_count": 40, "eval_duration": 2_000_000_000, "total_duration": 3_000_000_000}
        if req.url.path == "/api/ps":
            return {"models": [{"name": "llama3.2:latest", "digest": "d1", "size_vram": int(3.5 * GIB), "size": int(3.5 * GIB)}]}
        return httpx.Response(404)
    net.reply(fn)
    monkeypatch.setattr(hardware, "used_gib", lambda: [6.0])
    m = catalog.benchmark(v["id"], context=8192)
    assert m["vram_gib"] == 3.5 and m["tokens_per_s"] == 20 and m["at"]
    assert catalog.get(v["id"])["measured"]["vram_gib"] == 3.5 and catalog.get(v["id"])["src"]["vram"] == "tested"


def test_pull_checks_disk_space_first(net, monkeypatch):
    monkeypatch.setattr(hardware, "disk_free_gib", lambda: 1.0)
    with pytest.raises(ValueError, match="disk"):
        catalog.pull_check(size_bytes=5 * GIB)
    catalog.pull_check(size_bytes=None)  # unknown size: checked again when Ollama reports the total


# ---------- HTTP routes ----------

def test_routes_catalog_hardware_activation_and_paid_benchmark_confirm(net, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from reelwright import routes_models

    net.reply(ollama_fake(TAGS, SHOW))
    monkeypatch.setattr(hardware, "probe", lambda: hw())
    monkeypatch.setattr(selection, "_profile", lambda: hardware.profile(hw=hw(), reserve_gib=3))
    app = FastAPI()
    app.include_router(routes_models.router)
    c = TestClient(app)
    r = c.get("/api/models/catalog", params={"fit": "1"}).json()
    names = [v["name"] for g in r["groups"] for v in g["variants"]]  # first visit: installed + curated picks + API models
    assert "llama3.2:latest" in names and "giant:405b" not in names and r["profile"]["budget_gib"] == 18
    assert c.get("/api/models/catalog", params={"q": "giant"}).json()["count"] == 1  # all-models view still finds it
    assert c.put("/api/models/hardware", json={"profiles": [{"name": "Idle", "reserve_gib": 0}, {"name": "Idle", "reserve_gib": 2}]}).status_code == 400
    assert c.put("/api/models/hardware", json={"profiles": [{"name": "Idle", "reserve_gib": 0}, {"name": "Stream", "reserve_gib": 4}],
                                               "active": "Stream"}).json()["profile"]["reserve_gib"] == 4
    vid = "ollama:" + TAGS[0]["digest"]
    sel = c.post("/api/models/propose", json={"task": "find", "id": vid, "workload": {"context": 8192}}).json()
    rev = c.get("/api/models/tasks").json()["tasks"][0]["release"]["rev"]
    assert c.post("/api/models/activate", json={"task": "find", "candidate": sel["id"], "rev": rev}).json()["ok"]
    assert c.post("/api/models/activate", json={"task": "find", "candidate": sel["id"], "rev": rev}).status_code == 409
    config.save({"model": "claude-opus-5-5"})
    catalog.sync_remote()
    paid = c.post("/api/models/propose", json={"task": "meta", "id": "anthropic:claude-opus-5-5"}).json()
    r = c.post("/api/models/measure", json={"selection": paid["id"]}).json()
    assert r["needs_confirm"] and r["cost"]["paid"]  # nothing ran: a paid benchmark waits for the click


def test_activation_rechecks_runtime_and_it_cannot_be_overridden(monkeypatch):
    monkeypatch.setattr(selection, "_profile", lambda: hardware.profile(hw=hw(), reserve_gib=3))
    sel = models.propose("find", _installed()["id"], workload={"context": 8192})
    catalog.put({**catalog.get(sel["variant_id"]), "installed": False})  # uninstalled after it was proposed
    out = models.activate("find", sel["id"], override_reason="please")
    assert not out["ok"] and "runtime" in out["failed_gates"] and models.release("find")["active"] != sel["id"]


def test_nan_weights_are_rejected():
    with pytest.raises(ValueError):
        selection.score({"quality": 0.8}, [{"name": "quality", "worst": 0.7, "best": 0.9, "weight": float("nan")}])
