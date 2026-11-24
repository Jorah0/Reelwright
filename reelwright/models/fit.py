"""FitAssessment: will a model variant run on this PC under a workload? Memory = weights + KV cache + workspace + margin,
as a lower-upper GiB range against the effective budget (free VRAM - reserve):

  fits          the upper bound fits        incompatible  even the lower bound is over
  borderline    the range crosses it        unknown       the metadata (size or KV shape) or GPU telemetry is missing

KV = 2 (K and V) x layers x KV heads x head dim x bytes per value x positions (context x concurrent sequences).
RAM, disk and runtime are separate checks, named even when VRAM fits. Training fit is its own formula (op)."""
from __future__ import annotations

import os

GIB = 1024 ** 3
KV_BYTES = {"f16": 2, "bf16": 2, "f32": 4, "q8_0": 1, "q4_0": 0.5}
WORKLOAD = {"op": "run", "context": 8192, "concurrency": 1, "kv_dtype": "f16", "placement": "gpu"}  # placement: gpu | split | cpu
OPS = ("run", "adapter-train", "full-train", "export")
# GiB of compute buffers (CUDA graphs, scratch): runtime- and batch-dependent, hence a range. Calibration knob: on an RTX 4090
# Ollama measured 0.14 GiB (qwen2.5-coder 1.5B) and 0.21 GiB (phi3 3.8B) at 8K context beyond weights + KV.
WORKSPACE = (0.1, 0.75)
WEIGHT_SLACK, MARGIN = 1.05, 1.05  # file size -> resident size (output layer copies, alignment); then a 5% safety margin


def default_kv_dtype() -> str:
    """Ollama's own KV cache type: OLLAMA_KV_CACHE_TYPE applies only with flash attention on, else f16."""
    t = (os.environ.get("OLLAMA_KV_CACHE_TYPE") or "").lower()
    return t if t in KV_BYTES and os.environ.get("OLLAMA_FLASH_ATTENTION", "").lower() in ("1", "true") else "f16"


def kv_bytes(layers, kv_heads, head_dim, bytes_per, positions) -> float:
    return 2 * layers * kv_heads * head_dim * bytes_per * positions


def assess(lo: float, hi: float, total_gib: float, budget_gib: float) -> dict:
    """The status of a lo-hi GiB need against a budget, with % of total VRAM and headroom (left) or shortfall (short)."""
    status = "fits" if hi <= budget_gib else "incompatible" if lo > budget_gib else "borderline"
    out = {"status": status, "range_gib": [round(lo, 2), round(hi, 2)], "budget_gib": round(budget_gib, 2), "total_gib": round(total_gib, 2),
           "pct_total": [round(lo / total_gib * 100), round(hi / total_gib * 100)] if total_gib else None}
    if hi <= budget_gib or lo <= budget_gib:
        out["left_gib"] = [round(max(0.0, budget_gib - hi), 1), round(budget_gib - lo, 1)]
    if hi > budget_gib:
        out["short_gib"] = [round(max(0.0, lo - budget_gib), 1), round(hi - budget_gib, 1)]
    return out


def _weights(v: dict) -> tuple[float, float, str] | None:
    """Resident weight GiB (lo, hi, source): the file size when known, else parameters x quantization bits."""
    if v.get("size_bytes"):
        s = v["size_bytes"] / GIB
        return s, s * WEIGHT_SLACK, "size"
    if v.get("params_b"):
        q = (v.get("quant") or "").upper()
        bits = 16 if q in ("F16", "BF16", "") else 32 if q == "F32" else 8.5 if q.startswith("Q8") else \
            6.6 if q.startswith("Q6") else 5.5 if q.startswith("Q5") else 4.5 if q.startswith(("Q4", "IQ4")) else 3.5
        s = v["params_b"] * 1e9 * bits / 8 / GIB
        return s * 0.9, s * 1.15, "params"
    return None


def _kv(v: dict, positions: int, dtype: str) -> float | None:
    if not (v.get("layers") and v.get("kv_heads") and v.get("head_dim")):
        return None
    return kv_bytes(v["layers"], v["kv_heads"], v["head_dim"], KV_BYTES.get(dtype, 2), positions) / GIB


def _need(v: dict, w: dict) -> tuple[float, float, str] | None:
    """(lo, hi, source) GiB of VRAM the workload needs, or None when unknown."""
    positions = int(w["context"]) * max(1, int(w["concurrency"]))
    kv = _kv(v, positions, w["kv_dtype"])
    m = v.get("measured") or {}
    if w["op"] == "run" and m.get("vram_gib"):  # measured on this PC: shift by the KV difference when the context differs
        delta = 0.0 if m.get("context") == positions else (kv - (_kv(v, int(m.get("context") or 0), w["kv_dtype"]) or 0)) if kv is not None else None
        if delta is not None:
            x = m["vram_gib"] + delta
            return x, x, "tested"
    wt = _weights(v)
    if wt is None or kv is None:
        return None
    lo, hi, _ = wt
    params = (v.get("params_b") or 0) * 1e9
    if w["op"] == "run":
        return lo + kv + WORKSPACE[0], (hi + kv + WORKSPACE[1]) * MARGIN, "estimated"
    if w["op"] == "adapter-train":  # LoRA on the (quantized) base: ~1% trainable params x 16 B of Adam state, activations ~2x KV
        extra = params * 0.01 * 16 / GIB
        return lo + extra + 2 * kv + WORKSPACE[0], (hi + extra + 3 * kv + 2 * WORKSPACE[1]) * 1.15, "estimated"
    if w["op"] == "full-train":  # fp16 weights + grads, fp32 master + Adam m, v = 16 B per parameter, plus activations
        if not params:
            return None
        base = params * 16 / GIB
        return base + 2 * kv, (base + 4 * kv) * 1.2, "estimated"
    return 0.0, 0.0, "estimated"  # export runs on the CPU


def estimate(v: dict, workload: dict | None = None, profile: dict | None = None) -> dict:
    """FitAssessment of variant v (a catalog entry) for a workload on a hardware profile (hardware.profile())."""
    from . import hardware

    w = {**WORKLOAD, "kv_dtype": default_kv_dtype(), **(workload or {})}
    if w["op"] not in OPS:
        raise ValueError(f"op is one of {', '.join(OPS)}")
    if v.get("provider") in ("anthropic", "openai"):  # runs in the provider's cloud: nothing to fit here
        return {"op": w["op"], "workload": w, "status": "inapplicable", "range_gib": None, "source": "inapplicable", "ok": True,
                "loaded": False, "checks": [{"name": "vram", "status": "inapplicable", "detail": "runs on the provider's servers"}]}
    p = profile or hardware.profile()
    need = _need(v, w)
    checks = []
    loaded = next((x for x in p.get("loaded") or [] if v.get("digest") and x.get("digest") == v["digest"]), None)
    gpus = [{**g, "budget_gib": g["budget_gib"] + (loaded or {}).get("size_vram_gib", 0)} for g in p["gpus"]]  # already resident
    out = {"op": w["op"], "workload": w, "profile": p.get("name"), "reserve_gib": p.get("reserve_gib"), "loaded": bool(loaded),
           "source": "unknown" if need is None else need[2], "measured_at": (v.get("measured") or {}).get("at") if need and need[2] == "tested" else None}
    if w["op"] == "export" or w["placement"] == "cpu":
        out.update(status="inapplicable" if need else "unknown", range_gib=None)
        checks.append({"name": "vram", "status": "inapplicable", "detail": "runs on the CPU"})
    elif need is None:
        out.update(status="unknown", range_gib=None)
        checks.append({"name": "vram", "status": "unknown", "detail": "size or KV shape (layers, KV heads, head dim) not known"})
    elif not gpus:
        out.update(status="unknown", range_gib=[round(need[0], 2), round(need[1], 2)])
        checks.append({"name": "vram", "status": "unknown",
                       "detail": "GPU telemetry was denied" if p.get("telemetry") == "denied" else "no NVIDIA GPU reading"})
    else:
        if w["placement"] == "split":
            total, budget, gpu = sum(g["total_gib"] for g in gpus), sum(g["budget_gib"] for g in gpus), None
        else:
            g = max(gpus, key=lambda g: g["budget_gib"])
            total, budget, gpu = g["total_gib"], g["budget_gib"], g["index"]
        out.update(assess(need[0], need[1], total, budget), gpu=gpu)
        checks.append({"name": "vram", "status": {"fits": "pass", "incompatible": "fail"}.get(out["status"], "borderline"),
                       "detail": f"{need[0]:.1f}-{need[1]:.1f} GiB of {budget:.1f} GiB budget"})
    # independent checks: named even when VRAM fits
    wt = _weights(v)
    if wt:
        cpu = w["placement"] == "cpu" or w["op"] == "export"
        ram_need = 1 + (wt[0] * (2 if w["op"] == "export" else 1) if cpu else wt[0] * 0.25)  # runtime + load staging (estimated)
        free = p.get("ram_free_gib")
        checks.append({"name": "ram", "status": "unknown" if free is None else "pass" if free >= ram_need else "fail",
                       "detail": f"needs ~{ram_need:.1f} GiB free RAM" + ("" if free is None else f", {free:.1f} GiB free")})
        if not v.get("installed"):
            disk = p.get("disk_free_gib")
            checks.append({"name": "disk", "status": "unknown" if disk is None else "pass" if disk >= wt[0] * 1.1 + 2 else "fail",
                           "detail": f"download ~{wt[0]:.1f} GiB" + ("" if disk is None else f", {disk:.1f} GiB free")})
    if v.get("setting"):  # Whisper sizes run in Reelwright's own transcriber (faster-whisper), not Ollama
        checks.append({"name": "runtime", "status": "pass", "detail": f"faster-whisper ({v['setting']})"})
    else:
        runnable = v.get("provider") == "ollama" or (v.get("source") == "hf" and v.get("format") == "gguf")
        checks.append({"name": "runtime", "status": "pass" if runnable else "fail",
                       "detail": "runs through Ollama" if runnable else "Ollama runs GGUF only: create an Ollama model from it first"})
    out["checks"] = checks
    out["ok"] = out["status"] in ("fits", "inapplicable") and all(c["status"] in ("pass", "inapplicable") for c in checks)
    return out
