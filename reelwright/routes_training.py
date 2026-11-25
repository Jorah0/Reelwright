"""Model Lab: build the dataset, set up the training env, design a model for this PC (or a host on the network), train,
export to Ollama, evaluate against the active model, promote through the gate, roll back. See reelwright/training."""
import json
import shlex
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from . import ai, config, models, providers, store
from .training import dataset, design, release, runner

router = APIRouter()
TASKS = ("find", "judge", "meta")


def _err(fn, *a, **k):
    try:
        return fn(*a, **k)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/training/status")
def status():
    runner.recover()  # jobs a crash or restart left "running" show as interrupted
    ds = store.find("train_dataset")
    s = config.load()
    return {"env": runner.env_status(), "blocked": runner.block_reason(), "consent": dataset.consent(),
            "projects": [{"id": p["id"], "name": p.get("name"), "created": p.get("created")}
                         for p in store.find("project") if p.get("status") == "ready"],
            "dataset": ds[0] if ds else None, "jobs": store.find("train_job")[:20], "releases": store.find("train_release"),
            "defaults": {"arch": design.ARCH_DEFAULTS, "recipes": design.RECIPES, "tokenizers": design.PINNED_TOKENIZERS},
            "active": {t: {"provider": ai.route(t), "model": ai.provider_cfg(ai.route(t), t).get("model") or s["model"]} for t in TASKS},
            "settings": {k: s.get(k) for k in ("training_env_dir", "training_python", "training_wsl", "training_margin",
                                               "training_tolerance", "training_target")}}


@router.get("/api/training/active")
def active():
    """Queued, running or paused training jobs, slim: the status indicator's one extra request per poll (status() is heavy)."""
    def prog(j):
        step, total = (j.get("last") or {}).get("step"), ((j.get("spec") or {}).get("train") or {}).get("max_steps")
        return round(min(step / total, 1.0), 3) if step and total else None
    return [{"id": j["id"], "kind": j["kind"], "status": j["status"], "message": j.get("message") or "", "progress": prog(j)}
            for j in store.newest("train_job", 20) if j.get("status") in ("queued", "running", "paused")]


@router.get("/api/training/profile")
def profile():
    """This PC's training profile (another Reelwright that targets this one checks its fit against it)."""
    return design.local_profile()


@router.get("/api/training/bases")
def bases():
    """Bases for fine-tuning from the Models catalog: Hugging Face safetensors repos (trainable, pinned revision) and
    installed Ollama models (inference only: the designer says what's missing)."""
    out = []
    for v in models.catalog.entries():
        if "text-generation" not in (v.get("task") or []):
            continue
        if v["source"] == "hf" and v.get("format") == "safetensors":
            out.append({"source": "hf", "repo": v["repo"], "revision": v.get("revision") or "main", "name": v["name"],
                        "license": v.get("license"), "params_b": v.get("params_b")})
        elif v["source"] == "ollama" and v.get("installed"):
            out.append({"source": "ollama", "name": v["name"], "license": v.get("license"), "params_b": v.get("params_b")})
    return out


# ---------- environment ----------

@router.post("/api/training/env/setup")
def env_setup(body: dict):
    folder = str((body or {}).get("folder") or "").strip()
    if folder:
        p = Path(folder)
        if not p.is_absolute():
            raise HTTPException(400, "Pick a full folder path.")
        config.save({"training_env_dir": str(p), "training_python": [], "training_wsl": False})
    else:
        config.save({"training_python": [], "training_wsl": False})
    return _err(runner.submit, "setup", {"root": str(runner.env_root())}, gpu=False)


@router.post("/api/training/env/existing")
def env_existing(body: dict):
    cmd = str((body or {}).get("python") or "").strip()
    wsl = bool((body or {}).get("wsl"))
    argv = shlex.split(cmd, posix=wsl) if wsl else [cmd.strip('"')]
    _err(runner.use_existing, argv, wsl)
    return _err(runner.submit, "check", {}, gpu=False)


# ---------- dataset ----------

@router.post("/api/training/consent")
def set_consent(body: dict):
    b = body or {}
    return dataset.set_consent(b.get("all"), b.get("project"), bool(b.get("on", True)))


@router.post("/api/training/dataset/build")
def build():
    return dataset.build()


@router.get("/api/training/dataset/{h}/examples")
def examples(h: str):
    if not h.isalnum():
        raise HTTPException(400, "Bad dataset id.")
    try:
        return dataset.browse(h)
    except FileNotFoundError:
        raise HTTPException(404, "That dataset isn't on disk any more: build again.") from None


@router.post("/api/training/examples/{eid}/exclude")
def exclude(eid: str, body: dict):
    if not eid.isalnum():
        raise HTTPException(400, "Bad example id.")
    dataset.set_excluded(eid, bool((body or {}).get("on", True)))
    return {"ok": True}


@router.post("/api/training/teacher")
def teacher(body: dict):
    """Claude labels picked projects: without confirm, only the cost estimate comes back."""
    ids = [str(x) for x in (body or {}).get("projects") or []][:50]
    est = _err(dataset.teacher_label, ids, confirm=False)
    if not (body or {}).get("confirm"):
        return est
    if not est["parts"]:
        raise HTTPException(400, "None of those projects is confirmed for training.")
    return _err(runner.submit, "teacher", {"projects": ids}, gpu=False)


# ---------- designer ----------

def _design(b: dict) -> dict:
    mode = b.get("mode") or "scratch"
    out = {"mode": mode}
    p = b.get("pilot") or {}
    shape = {k: int(p.get(k) or d) for k, d in (("tokens", 50_000_000), ("seq", 1024), ("micro", 2), ("accum", 8))}
    if mode == "scratch":
        a = design.arch(b.get("arch"))
        tok = design.PINNED_TOKENIZERS.get(a["tokenizer"])
        if a["tokenizer"] != "bpe" and not tok:
            raise ValueError("Tokenizer: train a new BPE, or pick one of the pinned tokenizers.")
        measured = next(((j.get("result") or {}) for j in store.find("train_job", kind="pilot", status="done")
                         if (j.get("spec") or {}).get("arch") == a and (j.get("result") or {}).get("tok_s")), None)
        out.update(arch=a, params=design.param_count(a), errors=design.validate(a, tok and tok["vocab"]),
                   pilot=design.pilot(a, **shape, measured={"tok_s": measured["tok_s"], "peak_vram_gb": measured["peak_vram_gb"]}
                                      if measured else None), license=tok and tok["license"])
        need_vram, need_disk = out["pilot"]["vram_gb"], out["pilot"]["disk_gb"]
    else:
        r = design.recipe(mode, **(b.get("recipe") or {}))
        base = design.resolve_base(b.get("base") or {})
        v = next((x for x in models.catalog.entries() if x.get("repo") == base.get("repo")), {}) if base["trainable"] else {}
        est = design.lora_estimate(int((v.get("params_b") or 1.0) * 1e9), r)
        out.update(recipe=r, base=base, license=v.get("license"), params_b=v.get("params_b"), estimate=est, errors=[] if base["trainable"]
                   else [base["missing"]])
        need_vram, need_disk = est["vram_gb"], est["disk_gb"]
    target = str(b.get("target") or "").strip()
    prof = design.remote_profile(target) if target else design.local_profile()
    out.update(profile=prof, fit=design.fit(prof, need_vram, need_disk))
    return out


@router.post("/api/training/design")
def check_design(body: dict):
    return _err(_design, body or {})


@router.post("/api/training/jobs")
def start(body: dict):
    """kind "pilot" (20 steps: measures tok/s and peak VRAM) or "train"."""
    b = body or {}
    kind = b.get("kind")
    if kind not in ("pilot", "train"):
        raise HTTPException(400, "kind is pilot or train.")
    d = _err(_design, {**b, "target": ""})  # training runs on this PC's env: its fit is what counts
    if d["errors"]:
        raise HTTPException(400, " ".join(d["errors"]))
    if d["fit"]["fits"] is False:
        raise HTTPException(400, " ".join(d["fit"]["notes"]))
    if not runner.env_status()["ready"]:
        raise HTTPException(400, "Set up the training environment first.")
    p = b.get("pilot") or {}
    shape = d.get("recipe") or p  # LoRA/QLoRA: the recipe's own lr and batch shape; scratch: the designer's
    spec = {"mode": d["mode"], "arch": d.get("arch"), "base": d.get("base"), "recipe": d.get("recipe"), "license": d.get("license")}

    def train_shape():
        return {"seq": int(shape.get("seq") or 1024), "micro": int(shape.get("micro") or 2), "accum": int(shape.get("accum") or 8),
                "lr": float(shape.get("lr") or 3e-4), "tokens": int(p.get("tokens") or 50_000_000),
                "max_steps": 20 if kind == "pilot" else None, "warmup": 0.02, "save_every": 200}
    spec["train"] = _err(train_shape)
    return _err(runner.submit, kind, spec)


@router.post("/api/training/jobs/{jid}/{action}")
def job_action(jid: str, action: str, body: dict | None = None):
    if action == "export":
        src = store.get("train_job", jid)
        if not src or src["kind"] != "train" or src["status"] != "done":
            raise HTTPException(400, "Export a finished training job.")
        b, target = body or {}, str(config.load().get("training_target") or "")
        host = _err(design.check_target, target)["url"] if target else None  # validated again: settings can be edited by hand
        return _err(runner.submit, "export", {"train_job": jid, "eval_active": bool(b.get("eval_active")), "ollama_host": host})
    fn = {"pause": runner.pause, "resume": runner.resume, "cancel": runner.cancel}.get(action)
    if not fn:
        raise HTTPException(404, "Unknown action.")
    return _err(fn, jid)


def _gone(fn, *a):
    try:
        return _err(fn, *a)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None


@router.delete("/api/training/jobs/{jid}")
def delete_job(jid: str):
    """A finished job's folder (checkpoint, adapter, exports) and record; never a live job or the active/rollback model's."""
    return _gone(runner.delete_job, jid)


@router.delete("/api/training/releases/{rid}")
def delete_release(rid: str):
    return _gone(release.delete_release, rid)


@router.post("/api/training/releases/{rid}/promote")
def promote(rid: str, body: dict):
    rel = store.get("train_release", rid)
    if not rel:
        raise HTTPException(404, "No such candidate.")
    s, p = config.load(), ai.route("find")
    now = f"{p}:{ai.provider_cfg(p, 'find').get('model') or s['model']}"
    if rel.get("active_model") and rel["active_model"] != now:
        raise HTTPException(409, f"The active model changed since this candidate was scored ({rel['active_model']} → {now}): "
                                 "export again to compare on the same inputs.")
    return _err(release.promote, rid, (body or {}).get("task") or "find", rel.get("active_card"),
                float(s["training_margin"]), float(s["training_tolerance"]))


@router.post("/api/training/rollback")
def rollback(body: dict):
    return _err(release.rollback, (body or {}).get("task") or "find")


# ---------- job steps (run by runner's queue) ----------

def _conv(e: dict):
    return runner.wsl_path if e.get("wsl") else str


def _train_steps(job: dict) -> list:
    e, spec, jd = runner.env(), job["spec"], Path(job["dir"])
    conv = _conv(e)

    def prepare(j):
        b = dataset.build()
        if not b["counts"]["train"]:
            raise RuntimeError(b["note"] or "The training split is empty: review more clips or confirm more projects.")
        cfg = {**{k: spec[k] for k in ("mode", "arch", "base", "recipe", "train")}, "out": conv(jd), "dataset": b["hash"],
               "data": {"train": conv(Path(b["dir"]) / "train.jsonl"), "dev": conv(Path(b["dir"]) / "dev.jsonl")}}
        (jd / "config.json").write_text(json.dumps(cfg, indent=1), "utf-8")
        return {"dataset": b["hash"], "dataset_dir": b["dir"], "examples": b["counts"]["train"]}
    return [("Build the dataset", prepare), ("Train" if job["kind"] == "train" else "Pilot: 20 steps",
                                             runner.worker_argv(e, "train", "--config", jd / "config.json"))]


def _gpu_polite(asker):
    """Local inference between requests yields the GPU like training does: it waits while OBS records or a render runs."""
    def ask(system, user):
        while runner.block_reason():
            time.sleep(5)
        return asker(system, user)
    return ask


def _export_steps(job: dict) -> list:
    src = store.get("train_job", job["spec"]["train_job"])
    e, tdir, lora = runner.env(), Path(src["dir"]), src["spec"]["mode"] != "scratch"
    rid = release.next_id()
    tag = f"reelwright-specialist:{rid}"
    host = job["spec"].get("ollama_host")
    ocfg = {**config.provider("ollama"), "model": tag, **({"base_url": host} if host else {})}
    cmds = runner.export_commands(e, tdir, tag, lora)

    def modelfile(j):
        (tdir / "Modelfile").write_text(runner.modelfile("model-q4_k_m.gguf", scratch=not lora), "utf-8")

    def smoke(j):
        r = providers.ask("ollama", ocfg, ai.FIND_SYSTEM, "TRANSCRIPT:\n[0.0-9.0] Nobody expected the printer to explode.\n\n"
                          "TASK: Find the 1 best clips, each 5-30 seconds long, sorted best first.", ai.MomentList)
        return {"smoke": "ok", "smoke_clips": len(r.clips)}

    def evaluate(j):
        build = {"hash": src["result"]["dataset"], "dir": src["result"]["dataset_dir"]}
        cand = release.scorecard(_gpu_polite(release.asker_for("ollama", tag) if not host else
                                             (lambda system, user: providers.ask("ollama", ocfg, system, user, ai.MomentList).model_dump())),
                                 build)
        p = ai.route("find")
        model = ai.provider_cfg(p, "find").get("model") or config.load()["model"]
        cached = store.get("train_scorecard", f"{p}:{model}:{cand['inputs'][:16]}")
        active = cached and cached["card"]
        if not active and (p != "anthropic" or job["spec"].get("eval_active")):  # Claude costs money: only when asked
            active = release.scorecard(release.asker_for(p, model), build)
            store.put("train_scorecard", {"id": f"{p}:{model}:{cand['inputs'][:16]}", "card": active})
        return {"card": cand, "active_card": active, "active_model": f"{p}:{model}"}

    def record(j):
        res = j.get("result") or {}
        tags = providers.ollama_models(host or config.provider("ollama")["base_url"])
        digest = next((m["digest"] for m in tags if m.get("name") in (tag, f"{tag}:latest")), None)
        cfg = json.loads((tdir / "config.json").read_text("utf-8"))
        adapter = tdir / "adapter" / "adapter_model.safetensors"
        gguf = tdir / "model-q4_k_m.gguf"
        hashes = {"base": f"{cfg['base']['repo']}@{cfg['base']['revision']}" if lora else "scratch",
                  "adapter": release.sha256(adapter) if adapter.is_file() else None, "gguf": release.sha256(gguf),
                  "ollama_digest": digest, "dataset": cfg["dataset"], "config": release.config_hash(cfg)}
        a = cfg.get("arch") or {}
        info = {"params": (src.get("result") or {}).get("params") or (src.get("events") or {}).get("params", {}).get("count"),
                "context": a.get("context"), "layers": a.get("layers"), "kv_heads": a.get("kv_heads"),
                "head_dim": a["width"] // a["heads"] if a else None, "gguf_bytes": gguf.stat().st_size,
                "license": src["spec"].get("license")}
        man = release.manifest(hashes, release=rid, tag=tag, train_job=src["id"], card=res.get("card"),
                               active_card=res.get("active_card"), active_model=res.get("active_model"))
        (tdir / f"release-{rid}.json").write_text(json.dumps(man, indent=1), "utf-8")
        s = config.load()
        ok, why = release.gate(res.get("card") or {}, res.get("active_card"), float(s["training_margin"]), float(s["training_tolerance"]))
        release.record_release(rid, hashes, tag=tag, card=res.get("card"), digest=digest, model=info,
                               active_card=res.get("active_card"), active_model=res.get("active_model"), manifest=man,
                               reason=why, gate_ok=ok)
        return {"release": rid, "tag": tag, "digest": digest, "gate_ok": ok, "gate": why}

    steps = [(n, a) for n, a in cmds if n != "create"]
    return [*steps, ("Write the Modelfile", modelfile), *[(n, a) for n, a in cmds if n == "create"],
            ("Smoke test through the app's provider path", smoke), ("Evaluate against the active model", evaluate),
            ("Record the release", record),
            ("Free the training checkpoint", lambda j: {**runner.drop_checkpoint(store.get("train_job", src["id"])),
                                                         **runner.drop_export_intermediates(tdir, lora)})]


runner.STEPS.update({
    "setup": lambda job: runner.setup_steps(Path(job["spec"]["root"])),
    "check": lambda job: runner.check_steps(),
    "teacher": lambda job: [("Label with Claude", lambda j: dataset.teacher_label(job["spec"]["projects"], confirm=True))],
    "pilot": _train_steps, "train": _train_steps, "export": _export_steps,
})
