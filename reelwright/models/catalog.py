"""The model catalog: one list of exact variants (a quant/size of a family, identified by digest or revision, never by
name alone) from four sources, none of which downloads weights:

  ollama    installed models: GET /api/tags + POST /api/show (digest, family, size, quant, context, KV shape, capabilities)
  hf        Hugging Face Hub search (public https://huggingface.co/api/models; no key), cached with its sync time
  manifest  static/models/manifest.json: good picks for Reelwright's tasks (an install suggestion until pulled)
  gguf      a local .gguf file the creator points at (its header is read; nothing is copied)

Each field's provenance is in v["src"][field]: publisher (reported by the model/hub), tested (measured on this PC),
estimated, or unknown. Entries are store documents (kind "model_variant"); search is in Python (a few hundred rows)."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from .. import config, log, store
from . import fit, hardware

GIB = 1024 ** 3
KIND = "model_variant"
HF_API = "https://huggingface.co/api/models"
HF_TTL = 3600  # a cached Hub search is reused for an hour unless refreshed
TRANSPORT = None  # tests swap in an httpx.MockTransport
MANIFEST = config.STATIC / "models" / "manifest.json"  # under static/: the .exe bundles that folder
PIPELINE_IO = {  # pipeline tag -> (inputs, outputs): "accepts images" is not "generates images"
    "text-generation": (["text"], ["text"]), "image-text-to-text": (["text", "image"], ["text"]),
    "image-to-text": (["image"], ["text"]), "feature-extraction": (["text"], ["embedding"]),
    "sentence-similarity": (["text"], ["embedding"]), "automatic-speech-recognition": (["audio"], ["text"]),
    "text-to-image": (["text"], ["image"]), "image-to-image": (["image"], ["image"])}
QUANT_RE = re.compile(r"(IQ\d_[A-Z]+|Q\d_K_[SML]|Q\d_K|Q\d_\d|BF16|F16|F32)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _client(timeout=20) -> httpx.Client:
    return httpx.Client(transport=TRANSPORT, timeout=timeout, follow_redirects=False)


def _ollama() -> str:
    return config.provider("ollama")["base_url"].rstrip("/")


def _params(s) -> float | None:
    m = re.search(r"([\d.]+)\s*([BMK])", str(s or ""), re.I)
    return None if not m else float(m[1]) * {"B": 1, "M": 1e-3, "K": 1e-6}[m[2].upper()]


def _n(x):
    return max(x) if isinstance(x, list) and x else x  # per-layer lists (hybrid attention): plan for the largest


# ---------- storage ----------

def get(vid: str) -> dict | None:
    return store.get(KIND, vid)


def put(v: dict) -> dict:
    """Insert or merge a variant (a re-sync keeps what was measured on this PC)."""
    old = get(v["id"]) or {}
    v = {**old, **v, "src": {**old.get("src", {}), **v.get("src", {})}}
    if old.get("measured") and not v.get("measured"):
        v["measured"] = old["measured"]
    return store.put(KIND, v)


def entries() -> list[dict]:
    return store.find(KIND)


# ---------- Ollama ----------

def loaded() -> list[dict]:
    """Models Ollama holds in memory right now (GET /api/ps), [] when it isn't running."""
    try:
        with _client(5) as c:
            ms = c.get(f"{_ollama()}/api/ps").json().get("models") or []
    except (httpx.HTTPError, ValueError):
        return []
    return [{"name": m.get("name"), "digest": m.get("digest"), "size_vram_gib": (m.get("size_vram") or 0) / GIB} for m in ms]


def current_profile(name: str | None = None) -> dict:
    return hardware.profile(name, hw={**hardware.probe(), "loaded": loaded()})


def _from_ollama(m: dict, show: dict) -> dict:
    d, info = m.get("details") or {}, show.get("model_info") or {}
    arch = info.get("general.architecture") or d.get("family") or ""
    g = lambda k: _n(info.get(f"{arch}.{k}"))  # noqa: E731
    caps = show.get("capabilities") or m.get("capabilities") or []
    heads, emb = g("attention.head_count"), g("embedding_length")
    head_dim = g("attention.key_length") or (emb // heads if emb and heads else None)
    embed = "embedding" in caps
    inputs = ["text"] + (["image"] if "vision" in caps else [])
    task = ["feature-extraction"] if embed else ["text-generation"] + (["image-text-to-text"] if "vision" in caps else [])
    v = {"id": f"ollama:{m['digest']}", "source": "ollama", "provider": "ollama", "name": m["name"], "pull": m["name"],
         "digest": m["digest"], "family": d.get("family") or arch or m["name"].split(":")[0], "format": d.get("format") or "gguf",
         "params_b": _params(d.get("parameter_size")), "quant": d.get("quantization_level"), "size_bytes": m.get("size"),
         "context": g("context_length") or d.get("context_length"), "layers": g("block_count"),
         "kv_heads": g("attention.head_count_kv") or heads, "head_dim": head_dim, "capabilities": caps, "installed": True,
         "task": task, "inputs": inputs, "outputs": ["embedding"] if embed else ["text"], "aliases": [m["name"].removesuffix(":latest")]}
    v["src"] = {k: "publisher" if v.get(k) is not None else "unknown"
                for k in ("params_b", "quant", "size_bytes", "context", "layers", "kv_heads", "head_dim")}
    return v


def sync_ollama() -> list[dict]:
    """Installed Ollama models -> variants (one /api/show per digest not seen before). Uninstalled ones stay listed."""
    with _client(30) as c:
        tags = c.get(f"{_ollama()}/api/tags").json().get("models") or []
        out = []
        for m in tags:
            old = get(f"ollama:{m['digest']}")
            show = {} if old and old.get("layers") else c.post(f"{_ollama()}/api/show", json={"model": m["name"]}).json()
            out.append(put(_from_ollama(m, show) if show or not old else {**old, "installed": True, "name": m["name"]}))
    have = {v["id"] for v in out}
    for v in entries():
        if v["source"] == "ollama" and v["id"] not in have and v.get("installed"):
            put({**v, "installed": False})
    _mark_installed({v.get("pull") for v in out})
    return out


def _mark_installed(names: set) -> None:
    for v in entries():
        if v["source"] in ("manifest", "hf") and v.get("installed") != (v.get("pull") in names):
            put({**v, "installed": v.get("pull") in names})


def sync_remote() -> list[dict]:
    """The API models set up in Settings → AI, as variants (an API model id is its revision) so they can be candidates."""
    s, oa = config.load(), config.provider("openai")
    pairs = {("anthropic", s["model"]), ("anthropic", s.get("classify_model") or ""), ("openai", oa.get("model") or "")}
    return [put({"id": f"{p}:{m}", "source": "api", "provider": p, "name": m, "pull": m, "revision": m, "digest": None, "family": p,
                 "installed": True, "task": ["text-generation", "image-text-to-text"], "outputs": ["text"],
                 "inputs": ["text", "image"] if p == "anthropic" or oa.get("vision", True) else ["text"], "src": {"revision": "publisher"}})
            for p, m in sorted(pairs) if m]


# ---------- Hugging Face ----------

def _hf_variants(m: dict) -> list[dict]:
    repo, sha = m["id"], m.get("sha")
    tags = m.get("tags") or []
    base = next((t.split(":", 1)[1] for t in tags if t.startswith("base_model:") and t.count(":") == 1), None)
    inputs, outputs = PIPELINE_IO.get(m.get("pipeline_tag") or "", (["text"], ["text"]))
    common = {"source": "hf", "repo": repo, "revision": sha, "digest": None, "family": (base or repo).split("/")[-1],
              "task": [m["pipeline_tag"]] if m.get("pipeline_tag") else [], "inputs": inputs, "outputs": outputs,
              "downloads": m.get("downloads"), "likes": m.get("likes"), "installed": False, "provider": None,
              "params_b": _params(re.search(r"\d+(\.\d+)?[BM]\b", repo)[0]) if re.search(r"\d+(\.\d+)?[BM]\b", repo) else None}
    src = {"params_b": "estimated" if common["params_b"] else "unknown", "size_bytes": "unknown", "layers": "unknown",
           "revision": "publisher" if sha else "unknown"}
    ggufs = [s["rfilename"] for s in m.get("siblings") or [] if s["rfilename"].endswith(".gguf")
             and not re.search(r"-0000[2-9]-of-", s["rfilename"]) and "mmproj" not in s["rfilename"].lower()]
    if not ggufs:
        return [{**common, "id": f"hf:{repo}@{sha}", "name": repo, "format": "safetensors", "src": src}]
    out = []
    for f in ggufs:
        q = QUANT_RE.search(f)
        quant = q[1].upper() if q else None
        out.append({**common, "id": f"hf:{repo}@{sha}/{f}", "name": f"{repo} · {quant or f}", "file": f, "format": "gguf", "quant": quant,
                    "pull": f"hf.co/{repo}:{quant}" if quant else None, "src": {**src, "quant": "publisher" if quant else "unknown"}})
    return out


def sync_hf(search: str = "", filter: str = "", pipeline_tag: str = "", sort: str = "downloads", limit: int = 20,
            next_url: str = "", refresh: bool = False) -> dict:
    """One page of a Hub model search -> variants (a GGUF repo gives one per quant file). -> {entries, next, synced_at}"""
    if next_url and not next_url.startswith(HF_API + "?"):
        raise ValueError("next must be a huggingface.co model-search link")
    params = {k: v for k, v in {"search": search, "filter": filter, "pipeline_tag": pipeline_tag, "sort": sort,
                                "limit": str(max(1, min(int(limit), 100))), "full": "true"}.items() if v}
    if next_url:
        params = {k: v[0] for k, v in parse_qs(urlsplit(next_url).query).items()}
    key = "hf:" + hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    cached = store.get("hf_cache", key)
    if cached and not refresh and time.time() - cached["t"] < HF_TTL:
        return {"entries": [get(i) or {"id": i} for i in cached["ids"]], "next": cached["next"], "synced_at": cached["synced_at"]}
    with _client() as c:
        r = c.get(HF_API, params=params)
    r.raise_for_status()
    nxt = (r.links.get("next") or {}).get("url") or ""
    out = [put(v) for m in r.json() for v in _hf_variants(m)]
    names = {v.get("pull") for v in entries() if v["source"] == "ollama" and v.get("installed")}
    out = [put({**v, "installed": True}) if v.get("pull") in names else v for v in out]
    synced = _now()
    store.put("hf_cache", {"id": key, "ids": [v["id"] for v in out], "next": nxt, "synced_at": synced, "t": time.time()})
    return {"entries": out, "next": nxt, "synced_at": synced}


def hf_card(vid: str) -> dict:
    """Fills a Hub variant's file size, license and GGUF facts from its model card (GET /api/models/{repo}?blobs=true)."""
    v = get(vid)
    if not v or v["source"] != "hf":
        raise ValueError("Not a Hugging Face entry.")
    with _client() as c:
        r = c.get(f"{HF_API}/{v['repo']}", params={"blobs": "true", "revision": v["revision"]} if v.get("revision") else {"blobs": "true"})
    r.raise_for_status()
    m = r.json()
    size = next((s.get("size") for s in m.get("siblings") or [] if s.get("rfilename") == v.get("file")), None)
    gg, card = m.get("gguf") or {}, m.get("cardData") or {}
    patch = {"size_bytes": size, "license": card.get("license"), "context": gg.get("context_length") or v.get("context"),
             "params_b": (gg.get("total") or 0) / 1e9 or v.get("params_b"), "card_at": _now()}
    src = {"size_bytes": "publisher" if size else "unknown", "context": "publisher" if gg.get("context_length") else v["src"].get("context", "unknown")}
    if gg.get("total"):
        src["params_b"] = "publisher"
    return put({**v, **{k: x for k, x in patch.items() if x is not None}, "src": src})


# ---------- curated manifest ----------

def manifest() -> list[dict]:
    return json.loads(MANIFEST.read_text("utf-8"))["models"]


def load_manifest() -> list[dict]:
    out = []
    names = {v.get("pull") for v in entries() if v["source"] == "ollama" and v.get("installed")}
    for m in manifest():
        inputs, outputs = PIPELINE_IO.get(m["task"][0], (["text"], ["text"]))
        v = {"id": f"manifest:{m['key']}", "source": "manifest", "digest": None, "revision": m.get("revision"), "installed": m.get("pull") in names,
             "inputs": inputs, "outputs": outputs, "provider": "ollama" if m.get("pull") else None, "format": "gguf" if m.get("pull") else None, **m}
        v["src"] = {k: "estimated" if m.get(k) is not None else "unknown"  # hand-entered from model cards
                    for k in ("params_b", "quant", "size_bytes", "context", "layers", "kv_heads", "head_dim")}
        out.append(put(v))
    return out


# ---------- local GGUF ----------

_GG = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


def read_gguf(path: Path) -> dict:
    """The key/value header of a GGUF file (tensor data is never read)."""
    with open(path, "rb") as f:
        rd = lambda fmt: struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]  # noqa: E731
        string = lambda: f.read(rd("<Q")).decode("utf-8", "replace")  # noqa: E731

        def value(t):
            if t == 8:
                return string()
            if t == 9:
                it, n = rd("<I"), rd("<Q")
                vals = [value(it) for _ in range(n)]
                return vals if it != 8 else None  # token lists: skipped, not kept
            return rd(_GG[t])
        if f.read(4) != b"GGUF":
            raise ValueError("Not a GGUF file.")
        rd("<I")
        rd("<Q")
        return {string(): value(rd("<I")) for _ in range(rd("<Q"))}


def import_gguf(path: str) -> dict:
    p = Path(str(path).strip().strip('"'))
    if p.suffix.lower() != ".gguf" or not p.is_file():
        raise ValueError("Pick an existing .gguf file.")
    try:
        kv = read_gguf(p)
    except (OSError, struct.error, KeyError, ValueError) as e:
        raise ValueError(f"Couldn't read that GGUF header: {e}") from None
    arch = kv.get("general.architecture", "")
    g = lambda k: _n(kv.get(f"{arch}.{k}"))  # noqa: E731
    heads, emb = g("attention.head_count"), g("embedding_length")
    h = hashlib.sha256()
    with open(p, "rb") as f:  # first 16 MiB + size: identifies the file without hashing gigabytes
        h.update(f.read(16 * 1024 * 1024))
    h.update(str(p.stat().st_size).encode())
    q = QUANT_RE.search(p.name)
    v = {"id": f"gguf:{h.hexdigest()[:24]}", "source": "gguf", "digest": "partial-sha256:" + h.hexdigest(), "path": str(p.resolve()),
         "name": p.name, "family": kv.get("general.basename") or arch, "format": "gguf", "provider": None, "installed": False,
         "size_bytes": p.stat().st_size, "layers": g("block_count"), "kv_heads": g("attention.head_count_kv") or heads,
         "head_dim": g("attention.key_length") or (emb // heads if emb and heads else None), "context": g("context_length"),
         "params_b": (kv.get("general.parameter_count") or 0) / 1e9 or _params(kv.get("general.size_label")), "quant": q[1].upper() if q else None,
         "task": ["text-generation"], "inputs": ["text"], "outputs": ["text"]}
    v["src"] = {k: "publisher" if v.get(k) is not None else "unknown"
                for k in ("params_b", "quant", "size_bytes", "context", "layers", "kv_heads", "head_dim")}
    return put(v)


# ---------- search ----------

def _score(v: dict, q: str) -> float:
    if not q:
        return 0.0
    names = [x.lower() for x in (v["id"], v.get("name") or "", v.get("pull") or "", *(v.get("aliases") or [])) if x]
    if q in names:
        return 3.0
    hay = " ".join(names + [(v.get("family") or "").lower(), (v.get("repo") or "").lower()])
    if q in hay:
        return 2.0
    tokens = set(re.split(r"[\s:/@·_\-]+", hay)) | set(names)
    best = max((difflib.SequenceMatcher(None, q, t).ratio() for t in tokens if t), default=0.0)
    return 1.0 + best if best >= 0.75 else 0.0


SORTS = {"size": lambda v: v.get("size_bytes") or 0, "params": lambda v: v.get("params_b") or 0,
         "context": lambda v: v.get("context") or 0, "downloads": lambda v: v.get("downloads") or 0}


def search(q: str = "", facets: dict | None = None, profile: dict | None = None, workload: dict | None = None,
           sort: str = "relevance") -> list[dict]:
    """Exact id/name/alias first, then substring, then typo-tolerant (difflib). Facets: task, input, output, installed,
    quant, min_ctx, min_params, max_params, source, fit (True = only "fits"). Incompatible entries stay searchable."""
    f, q = facets or {}, (q or "").strip().lower()
    out = []
    for v in entries():
        s = _score(v, q)
        if q and not s:
            continue
        if (f.get("task") and f["task"] not in (v.get("task") or [])) or (f.get("input") and f["input"] not in (v.get("inputs") or [])) \
                or (f.get("output") and f["output"] not in (v.get("outputs") or [])) or (f.get("source") and v["source"] != f["source"]) \
                or (f.get("installed") is not None and bool(v.get("installed")) != bool(f["installed"])) \
                or (f.get("quant") and (v.get("quant") or "").upper() != str(f["quant"]).upper()) \
                or (f.get("min_ctx") and (v.get("context") or 0) < float(f["min_ctx"])) \
                or (f.get("min_params") and (v.get("params_b") or 0) < float(f["min_params"])) \
                or (f.get("max_params") and (v.get("params_b") is None or v["params_b"] > float(f["max_params"]))):
            continue
        out.append({**v, "_score": s})
    if profile is not None or f.get("fit"):
        profile = profile or current_profile()
        for v in out:
            v["fit"] = fit.estimate(v, workload, profile)
        if f.get("fit"):
            out = [v for v in out if v["fit"]["status"] in ("fits", "inapplicable")]
    key = SORTS.get(sort)
    out.sort(key=(lambda v: (-key(v), v.get("name") or "")) if key else (lambda v: (v.get("name") or "").lower()) if sort == "name"
             else (lambda v: (-v["_score"], not v.get("installed"), (v.get("name") or "").lower())))
    return out


def groups(vs: list[dict]) -> list[dict]:
    """Variants grouped by family, in result order."""
    out: dict[str, list] = {}
    for v in vs:
        out.setdefault((v.get("family") or v.get("name") or "?").lower(), []).append(v)
    return [{"family": k, "variants": x} for k, x in out.items()]


# ---------- install (Ollama pull) ----------

PULLS: dict[str, dict] = {}
_pull_lock = threading.Lock()


def pull_check(size_bytes: int | None) -> None:
    free = hardware.disk_free_gib()
    if size_bytes and free is not None and free < size_bytes / GIB * 1.1 + 2:
        raise ValueError(f"Not enough disk space for this model: needs ~{size_bytes / GIB * 1.1 + 2:.1f} GiB free, "
                         f"{free:.1f} GiB free in {hardware.models_dir()}.")


def pull(name: str, size_bytes: int | None = None) -> dict:
    """Starts `ollama pull name` in the background (progress in PULLS[name]; cancel() stops it)."""
    if not re.fullmatch(r"[\w.\-/:@]{1,200}", name or ""):
        raise ValueError("That isn't a model name Ollama can pull.")
    pull_check(size_bytes)
    with _pull_lock:
        if PULLS.get(name, {}).get("state") == "running":
            return PULLS[name]
        PULLS[name] = st = {"name": name, "state": "running", "status": "starting", "completed": 0, "total": 0, "error": "", "cancel": False}
    threading.Thread(target=_pull_run, args=(st,), daemon=True, name=f"pull {name}").start()
    return st


def _pull_run(st: dict) -> None:
    checked = False
    try:
        with _client(None) as c, c.stream("POST", f"{_ollama()}/api/pull", json={"model": st["name"], "stream": True}) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if st["cancel"]:
                    st.update(state="canceled", status="canceled")
                    return
                if not line.strip():
                    continue
                m = json.loads(line)
                if m.get("error"):
                    raise RuntimeError(m["error"])
                st.update(status=m.get("status", ""), completed=m.get("completed", st["completed"]), total=m.get("total", st["total"]))
                if m.get("total") and not checked:
                    checked = True
                    pull_check(m["total"])  # the real size, once Ollama says it
        st.update(state="done", status="installed")
        sync_ollama()
    except Exception as e:
        st.update(state="failed", error=str(e)[:300])
        log.event("models", f"pull {st['name']} failed: {e}")


def cancel(name: str) -> bool:
    st = PULLS.get(name)
    if st and st["state"] == "running":
        st["cancel"] = True
        return True
    return False


# ---------- benchmark on this PC ----------

BENCH_PROMPT = ("Here is a stream transcript excerpt:\n[12.0-18.5] no way, he just hit that from across the map\n"
                "[18.5-25.0] chat is going crazy right now\nReply with JSON {\"title\": ...} naming the best clip in 8 words.")


def benchmark(vid: str, context: int = 8192) -> dict:
    """Loads an installed Ollama variant, runs a representative prompt, and records the VRAM it really took
    (Ollama's /api/ps size_vram) and the nvidia-smi peak: the fit is then "tested" on this PC, with the date."""
    v = get(vid)
    if not v or v.get("provider") != "ollama" or not v.get("installed"):
        raise ValueError("Only an installed Ollama model can be benchmarked here.")
    base, peak, stop = hardware.used_gib(), [], threading.Event()

    def sample():
        while not stop.is_set():
            peak.append(sum(hardware.used_gib()))
            stop.wait(0.25)
    t = threading.Thread(target=sample, daemon=True)
    t.start()
    try:
        with _client(600) as c:
            r = c.post(f"{_ollama()}/api/generate", json={"model": v["name"], "prompt": BENCH_PROMPT, "stream": False,
                                                          "options": {"num_ctx": int(context), "num_predict": 64}, "keep_alive": "2m"}).json()
            row = None
            for _ in range(6):  # another app reloading the same model can briefly take it out of /api/ps
                ps = c.get(f"{_ollama()}/api/ps").json().get("models") or []
                row = next((m for m in ps if m.get("digest") == v["digest"] or m.get("name") == v["name"]), None)
                if row or r.get("error"):
                    break
                stop.wait(0.5)
    finally:
        stop.set()
        t.join(2)
    if r.get("error"):
        raise RuntimeError(f"Ollama: {r['error']}")
    if not row:
        raise RuntimeError("The model wasn't loaded after the run (Ollama /api/ps doesn't list it).")
    context = int(row.get("context_length") or context)  # what Ollama really loaded (another app may hold it at its own)
    secs = (r.get("eval_duration") or 0) / 1e9
    m = {"vram_gib": round(row.get("size_vram", 0) / GIB, 2), "ram_gib": round(max(0, row.get("size", 0) - row.get("size_vram", 0)) / GIB, 2),
         "peak_delta_gib": round(max(0.0, max(peak or [0]) - sum(base)), 2) if base else None, "context": int(context),
         "tokens_per_s": round(r.get("eval_count", 0) / secs, 1) if secs else None,
         "load_s": round((r.get("load_duration") or 0) / 1e9, 2), "at": _now()}
    put({**v, "measured": m, "src": {"vram": "tested"}})
    return m
