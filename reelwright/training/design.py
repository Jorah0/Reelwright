"""Model designer arithmetic: parameter count, validation, pilot estimates, LoRA recipes, base resolution and fit checks.
Pure Python (no torch): the app shows these live; the worker re-checks the count against LlamaForCausalLM."""
import ipaddress
import math
import re
from urllib.parse import urlsplit

import httpx

from .. import providers

ARCH_DEFAULTS = {"vocab": 8192, "layers": 8, "width": 512, "mlp": 1376, "heads": 8, "kv_heads": 2, "context": 1024,
                 "tied": True, "seed": 1234, "tokenizer": "bpe"}  # tokenizer: "bpe" (train a new one) or a pinned repo
PINNED_TOKENIZERS = {"HuggingFaceTB/SmolLM2-135M": {"revision": "main", "vocab": 49152, "license": "apache-2.0"}}
RECIPES = {
    "lora": {"quant": None, "r": 16, "alpha": 32, "dropout": 0.05, "lr": 1e-4, "epochs": 1, "seq": 2048, "micro": 1, "accum": 8},
    "qlora": {"quant": "nf4", "r": 16, "alpha": 32, "dropout": 0.05, "lr": 1e-4, "epochs": 1, "seq": 2048, "micro": 1, "accum": 8},
}
GPU_TFLOPS = 165.0  # RTX 4090 dense bf16; ponytail: one figure for every GPU until a pilot measures the real one
MFU = 0.25          # share of peak a small-model training loop reaches (estimate; the pilot replaces it)
_HF_REPO = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}$")
_REV = re.compile(r"^[\w.-]{1,100}$")


def arch(a: dict) -> dict:
    out = {**ARCH_DEFAULTS, **(a or {})}
    for k in ("vocab", "layers", "width", "mlp", "heads", "kv_heads", "context", "seed"):
        out[k] = int(out[k])
    out["tied"] = bool(out["tied"])
    return out


def param_count(a: dict) -> int:
    """Llama-style decoder: V·d + L(2d² + 2dk + 3dm + 2d) + d, k = kv_heads·head_dim; untied output adds V·d."""
    a = arch(a)
    v, n, d, m = a["vocab"], a["layers"], a["width"], a["mlp"]
    k = a["kv_heads"] * (d // a["heads"])
    return v * d + n * (2 * d * d + 2 * d * k + 3 * d * m + 2 * d) + d + (0 if a["tied"] else v * d)


def validate(a: dict, tokenizer_vocab: int | None = None) -> list[str]:
    a = arch(a)
    errs = [f"{k} must be at least 1." for k in ("vocab", "layers", "width", "mlp", "heads", "kv_heads", "context") if a[k] < 1]
    if errs:
        return errs
    if a["width"] % a["heads"]:
        errs.append(f"Width {a['width']} must be divisible by heads {a['heads']}.")
    elif (a["width"] // a["heads"]) % 2:
        errs.append(f"Head dimension {a['width'] // a['heads']} must be even (rotary embeddings rotate pairs).")
    if a["heads"] % a["kv_heads"]:
        errs.append(f"Heads {a['heads']} must be divisible by KV heads {a['kv_heads']}.")
    if tokenizer_vocab is not None and tokenizer_vocab != a["vocab"]:
        errs.append(f"Vocab {a['vocab']} doesn't match the tokenizer's {tokenizer_vocab} tokens.")
    return errs


def pilot(a: dict, tokens: int = 50_000_000, seq: int = 1024, micro: int = 2, accum: int = 8, measured: dict | None = None) -> dict:
    """Tokens/steps/time/disk/VRAM for a from-scratch run. Estimated from FLOPs until a pilot run measures tok/s and VRAM."""
    a = arch(a)
    n = param_count(a)
    per = seq * micro * accum
    flops_tok = 6 * n + 12 * a["layers"] * a["width"] * seq  # weights + attention, forward + backward
    tok_s = (measured or {}).get("tok_s") or GPU_TFLOPS * 1e12 * MFU / flops_tok
    act = micro * seq * (a["layers"] * a["width"] * 34 + a["vocab"] * 8)  # bf16 activations + fp32 logits and their grads
    vram = (measured or {}).get("peak_vram_gb") or (n * 16 + act) / 2**30 + 0.6  # weights+grads+Adam (fp32) + CUDA context
    return {"params": n, "tokens": tokens, "tokens_per_update": per, "updates": math.ceil(tokens / per),
            "tok_s": round(tok_s), "hours": tokens / tok_s / 3600, "vram_gb": round(vram, 2),
            "disk_gb": round(n * (12 + 2 + 2 + 0.6) / 2**30, 3),  # checkpoint (fp32 + Adam), bf16 model, f16 + Q4 GGUF
            "label": "measured" if measured else "estimated"}


def lora_estimate(base_params: int, r: dict, seq: int | None = None) -> dict:
    """QLoRA/LoRA VRAM: the frozen base (4-bit ≈ 0.56 B/param, bf16 2 B) + adapters + activations (rough)."""
    seq = seq or r["seq"]
    base = base_params * (0.56 if r.get("quant") else 2)
    return {"vram_gb": round((base + base_params * 0.01 * 16 + r["micro"] * seq * 4096 * 64) / 2**30 + 1.0, 1),
            "disk_gb": round(base_params * (2 + 2 + 0.6) / 2**30, 1), "label": "estimated"}


def recipe(kind: str, **edits) -> dict:
    if kind not in RECIPES:
        raise ValueError(f"Unknown recipe {kind!r}: pick lora or qlora.")
    out = {**RECIPES[kind], **{k: v for k, v in edits.items() if k in RECIPES[kind] and v is not None}}
    if not (0 < float(out["lr"]) < 1 and 1 <= int(out["r"]) <= 512 and 0 <= float(out["dropout"]) < 1 and int(out["epochs"]) >= 1):
        raise ValueError("Recipe out of range: lr 0-1, r 1-512, dropout 0-1, epochs ≥ 1.")
    return out


def resolve_base(spec: dict) -> dict:
    """Where trainable weights and a tokenizer come from. An Ollama model is a quantized GGUF: inference only."""
    if spec.get("source") == "ollama":
        name = str(spec.get("name") or "")
        return {"trainable": False, "name": name,
                "missing": (f"{name} is an Ollama model: a quantized GGUF file, which runs but can't be fine-tuned. Tuning needs "
                            "the original Hugging Face weights (safetensors) and its tokenizer: pick the Hugging Face repo and "
                            "revision it was made from (same family and size), and the specialist is exported back to Ollama.")}
    repo, rev = str(spec.get("repo") or ""), str(spec.get("revision") or "main")
    if not _HF_REPO.match(repo) or ".." in repo or not _REV.match(rev):
        raise ValueError("A Hugging Face base is owner/name plus a revision (branch, tag or commit).")
    return {"trainable": True, "repo": repo, "revision": rev, "download": f"https://huggingface.co/{repo}"}


def check_target(url: str) -> dict:
    """A remote Ollama / training host: http(s) only; says whether data stays on the private network."""
    base = providers.check_url(url)  # ValueError on a bad scheme or credentials in the URL
    host = urlsplit(base).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
        private = ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        private = host == "localhost" or host.endswith((".local", ".lan", ".home.arpa")) or "." not in host
    warn = ("" if private else f"{host} is outside your private network: training data (transcripts and your decisions) and "
            "model files would leave it. Only use a host you control.")
    return {"url": base, "private": private, "warning": warn or "A host on your network: make sure you trust it."}


def fit(profile: dict, need_vram_gb: float, need_disk_gb: float) -> dict:
    """Does a job fit a host? None when the host didn't report its VRAM (unknown isn't yes)."""
    notes, fits = [], True
    vram, disk = profile.get("vram_gb"), profile.get("disk_free_gb")
    if vram is None:
        return {"fits": None, "notes": ["This host didn't report its GPU memory: the fit can't be checked."]}
    if need_vram_gb > vram:
        fits = False
        notes.append(f"Needs about {need_vram_gb:.1f} GB VRAM; the host has {vram:.1f} GB. Shrink the model, sequence or "
                     "micro-batch, or use QLoRA.")
    if disk is not None and need_disk_gb > disk:
        fits = False
        notes.append(f"Needs about {need_disk_gb:.1f} GB disk; {disk:.1f} GB free.")
    return {"fits": fits, "notes": notes or [f"Fits: ~{need_vram_gb:.1f} of {vram:.1f} GB VRAM."]}


def local_profile() -> dict:
    """This PC as the Models package sees it: the best GPU's budget (free VRAM minus the hardware profile's reserve,
    e.g. "OBS live" keeps 3 GiB back), RAM and free disk."""
    from .. import models
    p = models.profile()
    best = max(p.get("gpus") or [{}], key=lambda g: g.get("budget_gib") or 0)
    return {"host": "this PC", "gpu": best.get("name"), "vram_gb": p.get("budget_gib"), "vram_total_gb": best.get("total_gib"),
            "ram_gb": p.get("ram_total_gib"), "disk_free_gb": p.get("disk_free_gib"), "profile": p.get("name")}


def remote_profile(url: str) -> dict:
    """A remote host's reported profile: a Reelwright there answers /api/training/profile; plain Ollama only says it's up."""
    t = check_target(url)
    with httpx.Client(transport=providers.TRANSPORT, timeout=10) as c:
        try:
            r = c.get(f"{t['url']}/api/training/profile")
            if r.status_code == 200:
                p = r.json()
                return {**{k: p.get(k) for k in ("gpu", "vram_gb", "ram_gb", "disk_free_gb")}, "host": t["url"], "target": t}
        except (httpx.HTTPError, ValueError):
            pass
        try:
            ver = c.get(f"{t['url']}/api/version").json().get("version")
        except (httpx.HTTPError, ValueError):
            raise RuntimeError(f"Couldn't reach {t['url']}.") from None
    return {"host": t["url"], "ollama": ver, "gpu": None, "vram_gb": None, "target": t}
