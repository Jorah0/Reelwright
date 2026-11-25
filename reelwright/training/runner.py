"""The external training environment, the GPU lease and the job queue.

torch/transformers/peft never enter the .exe: "Set up training environment" makes a venv (Python 3.12 by default, which has
CUDA torch wheels) in a folder the creator picks, pip-installs PINS, checks torch.cuda, and fetches a pinned llama.cpp
(the GGUF converter script and llama-quantize). Or point at an existing env, or a WSL2 command. Jobs run worker.py there
as a subprocess that prints one JSON object per line; one at a time (the GPU lease), paused while OBS records or streams
or a render runs, with a manual pause/resume that the worker honours at its next step boundary (it checkpoints first).
"""
import atexit
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx

from .. import config, log, obs, store

TORCH_INDEX = "https://download.pytorch.org/whl/cu130"  # CUDA 13.0 wheels (driver 580+)
PINS = ("torch==2.14.1+cu130", "transformers==4.57.6", "tokenizers==0.22.2", "peft==0.18.1", "accelerate==1.12.0",
        "bitsandbytes==0.49.1", "safetensors==0.7.0", "sentencepiece==0.2.1", "numpy==2.2.6", "protobuf==4.25.8",
        "huggingface-hub==0.36.2")  # transformers 4.57.6 = what the pinned llama.cpp converter expects
PYTHONS = ("3.12", "3.13", "3.11", "3.14")  # CUDA torch wheels exist for these; 3.12 first (the best-trodden)
LLAMA_CPP = {"tag": "b12206",  # 2026-11-08
             "source": "https://github.com/ggml-org/llama.cpp/archive/refs/tags/b12206.zip",
             "bin": "https://github.com/ggml-org/llama.cpp/releases/download/b12206/llama-b12206-bin-win-cpu-x64.zip",
             "bin_sha256": "48a19456881db8051b1fab8cbd60ca916e142bbe66d1a2dc97c5f7aad32d580b",
             "source_sha256": "5595781cb4fa7d961c2f0f165fc2d33eede3c7d73176afd62f79ae2c36892b7d"}  # its code runs in the env
MAX_UNZIPPED = 2 << 30  # an archive that expands past 2 GiB is refused (zip bomb)
RENDER_KINDS = {"export", "longform_export", "proxy", "track"}  # pipeline's render queue: GPU encodes and tracking
GPU_KINDS = RENDER_KINDS | {"analyze"}  # analysis runs Whisper (and vision) on the GPU too: training must not share it
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
QUANT = "Q4_K_M"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ---------- the environment ----------

def requirements_text() -> str:
    return (f"# Reelwright training environment (pinned; written by Set up training environment)\n"
            f"--extra-index-url {TORCH_INDEX}\n" + "\n".join(PINS) + "\n")


def _run_text(argv: list[str], timeout: float = 30) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW).stdout


def pick_python() -> dict:
    """An installed Python with CUDA torch wheels, via the py launcher (3.12 preferred)."""
    try:
        listing = _run_text(["py", "-0p"])
    except (OSError, subprocess.SubprocessError):
        listing = ""
    found = {m.group(1): m.group(2).strip() for m in re.finditer(r"-V:(\d+\.\d+)\S*\s+\*?\s*(\S.*)", listing)}
    for v in PYTHONS:
        if v in found:
            return {"version": v, "path": found[v]}
    raise RuntimeError("Training needs Python 3.12 (or 3.11/3.13/3.14), which has CUDA builds of PyTorch. Install Python 3.12 "
                       "from python.org (tick 'py launcher') or run: winget install Python.Python.3.12 — then set up again. "
                       f"Found: {', '.join(found) or 'none'}.")


def env_root() -> Path:
    return Path(config.load().get("training_env_dir") or config.DATA / "training-env")


def env() -> dict:
    """How to run the worker: {"python": argv, "root": tools folder, "wsl": bool}. An existing env or WSL command wins."""
    s, root = config.load(), env_root()
    custom = s.get("training_python") or []
    if custom:
        return {"python": list(custom), "root": str(root), "wsl": bool(s.get("training_wsl"))}
    return {"python": [str(root / "venv" / "Scripts" / "python.exe")], "root": str(root), "wsl": False}


def env_status() -> dict:
    """The env's recorded check (reelwright-env.json) plus whether its python still exists."""
    e = env()
    try:
        info = json.loads((Path(e["root"]) / "reelwright-env.json").read_text("utf-8"))
    except (OSError, ValueError):
        info = {}
    exists = e["wsl"] or Path(e["python"][0]).is_file()
    return {**info, "root": e["root"], "python": e["python"], "wsl": e["wsl"], "exists": exists,
            "ready": bool(exists and info.get("torch")), "pins": list(PINS), "llama_cpp_tag": LLAMA_CPP["tag"],
            "tools": (Path(e["root"]) / "llama-bin" / "llama-quantize.exe").is_file()}


def wsl_path(p) -> str:
    """C:\\x\\y -> /mnt/c/x/y (the WSL view of a Windows path)."""
    s = str(p).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else s


def worker_file() -> Path:
    """worker.py ships as a data file next to the frozen package (it runs in the training env, not in the .exe)."""
    base = Path(getattr(sys, "_MEIPASS", "")) / "reelwright" / "training" if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / "worker.py"


def worker_argv(e: dict, mode: str, *args) -> list[str]:
    conv = wsl_path if e.get("wsl") else str
    return [*e["python"], conv(worker_file()), mode, *[conv(a) if isinstance(a, Path) else str(a) for a in args]]


def _download(url: str, dest: Path, sha256: str | None = None) -> str:
    h = hashlib.sha256()
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, connect=15)) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                h.update(chunk)
    digest = h.hexdigest()
    if sha256 and digest != sha256:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{url} didn't match its pinned SHA-256 (got {digest[:12]}…): not used.")
    return digest


def _extract(zf: Path, dest: Path, strip: bool) -> None:
    """Unzip an untrusted archive: every member must land inside dest (no ../, no absolute paths)."""
    dest = dest.resolve()
    with zipfile.ZipFile(zf) as z:
        if sum(m.file_size for m in z.infolist()) > MAX_UNZIPPED:
            raise RuntimeError(f"{zf.name} expands to more than {MAX_UNZIPPED >> 30} GiB: refused.")
        for m in z.infolist():
            name = m.filename.split("/", 1)[1] if strip and "/" in m.filename else m.filename
            if not name or m.is_dir():
                continue
            target = (dest / name).resolve()
            if dest not in target.parents:
                raise RuntimeError(f"Unsafe path in {zf.name}: {m.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(m))


def setup_steps(root: Path) -> list:
    """Create the venv, pip-install the pins, check CUDA, fetch llama.cpp. Each step: (label, argv) or (label, callable)."""
    venv_py = root / "venv" / "Scripts" / "python.exe"

    def make_venv(job):
        if venv_py.is_file():
            return {"note": "venv already there"}
        py = pick_python()
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run([py["path"], "-m", "venv", str(root / "venv")], check=True, creationflags=NO_WINDOW, timeout=600)
        return {"python": py}

    def write_reqs(job):
        (root / "requirements-train.txt").write_text(requirements_text(), "utf-8")

    pip = [str(venv_py), "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "off",
           "-r", str(root / "requirements-train.txt")]
    return [("Create the virtual environment", make_venv), ("Write requirements-train.txt", write_reqs),
            ("Install PyTorch, transformers, PEFT (several GB)", pip),
            ("Check CUDA", [str(venv_py), str(worker_file()), "check"]),
            ("Fetch llama.cpp (GGUF converter + llama-quantize)", lambda job: fetch_tools(root)),
            ("Record the environment", record_env)]


def check_steps() -> list:
    """An existing env or WSL command: check torch/CUDA, fetch the llama.cpp tools if missing, record."""
    e = env()
    steps = [("Check the environment", worker_argv(e, "check"))]
    if not (Path(e["root"]) / "llama-bin" / "llama-quantize.exe").is_file():
        steps.append(("Fetch llama.cpp (GGUF converter + llama-quantize)", lambda job: fetch_tools(Path(e["root"]))))
    return [*steps, ("Record the environment", record_env)]


def fetch_tools(root: Path) -> dict:
    """The pinned llama.cpp: its source (convert_hf_to_gguf.py + gguf-py) and the Windows CPU build (llama-quantize)."""
    dl = root / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    src = _download(LLAMA_CPP["source"], dl / f"llama.cpp-{LLAMA_CPP['tag']}.zip", LLAMA_CPP["source_sha256"])
    shutil.rmtree(root / "llama.cpp", ignore_errors=True)
    _extract(dl / f"llama.cpp-{LLAMA_CPP['tag']}.zip", root / "llama.cpp", strip=True)
    binsha = _download(LLAMA_CPP["bin"], dl / "llama-bin.zip", LLAMA_CPP["bin_sha256"])
    shutil.rmtree(root / "llama-bin", ignore_errors=True)
    _extract(dl / "llama-bin.zip", root / "llama-bin", strip=False)
    return {"llama_cpp": {"tag": LLAMA_CPP["tag"], "source_sha256": src, "bin_sha256": binsha}}


def record_env(job: dict) -> dict:
    """Write reelwright-env.json from the job's torch check and the env's exact installed versions (pip freeze)."""
    e = env()
    check = (job.get("events") or {}).get("check") or {}
    if not check.get("torch"):
        raise RuntimeError("The env's torch check didn't report back.")
    try:
        old = json.loads((Path(e["root"]) / "reelwright-env.json").read_text("utf-8"))
    except (OSError, ValueError):
        old = {}
    freeze = _run_text([*e["python"], "-m", "pip", "freeze", "--disable-pip-version-check"], timeout=120).split()
    info = {**old, "python": e["python"], "torch": check["torch"], "cuda": check.get("cuda"), "gpu": check.get("gpu"),
            "cuda_version": check.get("cuda_version"), "pins": list(PINS), "freeze": freeze,
            "llama_cpp": (job.get("result") or {}).get("llama_cpp") or old.get("llama_cpp"), "checked": time.time()}
    Path(e["root"]).mkdir(parents=True, exist_ok=True)
    (Path(e["root"]) / "reelwright-env.json").write_text(json.dumps(info, indent=1), "utf-8")
    return {"env": {k: info[k] for k in ("torch", "cuda", "gpu", "cuda_version")}}


def wsl_argv(distro: str | None, python: str) -> list[str]:
    """The WSL command, always the system wsl.exe (never a user-supplied path)."""
    exe = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe")
    return [exe, *(["-d", distro] if distro else []), "--", python]


def use_existing(python_argv: list[str], wsl: bool = False) -> dict:
    """Point at an existing env (a python.exe) or a WSL2 command (e.g. wsl -d Ubuntu -- ~/rw/bin/python)."""
    argv = [str(a) for a in python_argv if str(a).strip()]
    if not argv:
        raise ValueError("Give the python of an existing env, or a WSL command.")
    if wsl:  # only: wsl [-d <distro>] -- /absolute/linux/python ; rebuilt server-side around the system wsl.exe
        ok = argv[0] in ("wsl", "wsl.exe")
        rest = argv[1:]
        distro = None
        if ok and rest[:1] == ["-d"] and len(rest) >= 2:
            distro, rest = rest[1], rest[2:]
            ok = bool(re.fullmatch(r"[\w.-]{1,64}", distro))
        if not (ok and len(rest) == 2 and rest[0] == "--" and re.fullmatch(r"/[\w./-]*/python[\d.]*", rest[1])):
            raise ValueError("A WSL command is: wsl [-d <distro>] -- /home/me/rw/bin/python (nothing else)")
        argv = wsl_argv(distro, rest[1])
    if not wsl:
        if len(argv) != 1 or argv[0].startswith(("\\\\", "//")) or not re.fullmatch(r"python[\d.]*(\.exe)?", Path(argv[0]).name.lower()):
            raise ValueError("Pick the python.exe of an environment on this PC.")
        if not Path(argv[0]).is_file():
            raise ValueError(f"{argv[0]} doesn't exist.")
    config.save({"training_python": argv, "training_wsl": bool(wsl)})
    return env()


# ---------- the GPU lease ----------

_lease_lock = threading.Lock()
_holder: str | None = None


def _rendering() -> str | None:
    """The kind of a running pipeline job that holds the GPU (a render, tracking or an analysis), else None."""
    return next((j["kind"] for j in store.find("job", status="running") if j["kind"] in GPU_KINDS), None)


def block_reason() -> str | None:
    """Why the GPU is not ours right now (None = free)."""
    live = [c for c in store.find("obs") if c.get("enabled") and c.get("streaming")]  # a streaming flag, when OBS reports it
    rec = obs.recording()
    if rec or live:
        names = ", ".join(c.get("name") or "OBS" for c in rec + live)
        return f"OBS is {'recording' if rec else 'streaming'} ({names}): training waits so the stream never drops frames."
    if kind := _rendering():
        return ("A video is being analysed (Whisper runs on the GPU)" if kind == "analyze" else "A render is running") +             ": training waits for it to finish."
    return None


def lease(job_id: str) -> None:
    global _holder
    with _lease_lock:
        if _holder and _holder != job_id:
            raise RuntimeError("A training job is already running: one at a time.")
        if why := block_reason():
            raise RuntimeError(why)
        _holder = job_id


def release_lease(job_id: str) -> None:
    global _holder
    with _lease_lock:
        if _holder == job_id:
            _holder = None


def _watch(cur: dict) -> None:
    """watchdog() applied inside store.update, on the job as it is now (a cancel or a manual pause in between wins)."""
    if cur["status"] in ("running", "paused"):
        w = watchdog(cur)
        cur.update({k: w.get(k) for k in ("status", "paused_by", "message")})


def watchdog(job: dict) -> dict:
    """Pause a running job while the GPU is needed elsewhere, resume it after; a manual pause only the user lifts."""
    ctl = Path(job["dir"]) / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    why = block_reason()
    if job["status"] == "running" and why:
        (ctl / "pause").touch()
        return {**job, "status": "paused", "paused_by": "auto", "message": why}
    if job["status"] == "paused" and job.get("paused_by") == "auto" and not why:
        (ctl / "pause").unlink(missing_ok=True)
        return {**job, "status": "running", "paused_by": None, "message": "Resumed"}
    return job


# ---------- worker progress ----------

def parse_progress(line: str) -> dict:
    """One stdout line of worker.py: a JSON event, or anything else (library chatter) as a log line."""
    line = ANSI.sub("", line).strip()  # ollama create draws progress bars with terminal escapes
    try:
        ev = json.loads(line)
    except ValueError:
        ev = None
    return ev if isinstance(ev, dict) and isinstance(ev.get("event"), str) else {"event": "log", "text": line}


def apply_progress(job: dict, ev: dict) -> None:
    kind = ev["event"]
    if kind == "step":
        job.setdefault("metrics", []).append({k: ev.get(k) for k in ("step", "loss", "tok_s")})
        del job["metrics"][:-2000]
        job["last"] = ev
    elif kind == "log":
        if ev.get("text"):
            job.setdefault("log", []).append(ev["text"][:500])
            del job["log"][:-200]
    elif kind == "done":
        job["result"] = {**(job.get("result") or {}), **{k: v for k, v in ev.items() if k != "event"}}
    elif kind == "error":
        job["error"] = str(ev.get("message") or "")[:2000]
    else:
        job.setdefault("events", {})[kind] = ev


PROGRESS_EVERY = 3.0  # seconds between batched progress writes of a running job

# ---------- export ----------

def export_commands(e: dict, job_dir: Path, tag: str, lora: bool) -> list[tuple[str, list[str]]]:
    """merge (LoRA) → convert_hf_to_gguf.py f16 → llama-quantize Q4_K_M → ollama create. The worker runs the converter
    (it maps a custom byte-level BPE tokenizer to llama.cpp's gpt-2 pre-tokenizer)."""
    job_dir, root = Path(job_dir), Path(e["root"])
    conv = wsl_path if e.get("wsl") else str
    cmds = []
    if lora:
        cmds.append(("merge", worker_argv(e, "merge", "--config", job_dir / "config.json", "--out", job_dir / "merged")))
    cmds.append(("convert", worker_argv(e, "convert", "--script", conv(root / "llama.cpp" / "convert_hf_to_gguf.py"),
                                        "--model", conv(job_dir / "merged"), "--outfile", conv(job_dir / "model-f16.gguf"),
                                        "--outtype", "f16")))
    cmds.append(("quantize", [str(root / "llama-bin" / "llama-quantize.exe"), str(job_dir / "model-f16.gguf"),
                              str(job_dir / "model-q4_k_m.gguf"), QUANT]))
    cmds.append(("create", ["ollama", "create", tag, "-f", str(job_dir / "Modelfile")]))
    return cmds


CHAT = ('TEMPLATE """{{- if .System }}<|system|>\n{{ .System }}\n{{ end }}{{- range .Messages }}<|{{ .Role }}|>\n'
        '{{ .Content }}\n{{ end }}<|assistant|>\n"""\nPARAMETER stop "<|end|>"\n')


def modelfile(gguf_name: str, scratch: bool) -> str:
    """A from-scratch model carries Reelwright's own chat format; a tuned HF base keeps the template inside its GGUF."""
    return f"FROM ./{gguf_name}\nPARAMETER temperature 0.2\nPARAMETER num_ctx 8192\n" + (CHAT if scratch else "")


# ---------- the job queue ----------

_q: queue.Queue = queue.Queue()
_procs: dict[str, subprocess.Popen] = {}
_started = False


def jobs_dir() -> Path:
    return config.DATA / "training" / "jobs"


def submit(kind: str, steps_spec: dict, gpu: bool = True) -> dict:
    """Queue a job. A GPU job is refused up front while the GPU is busy (OBS, a render): starting it would only pause."""
    if gpu and (why := block_reason()):
        raise RuntimeError(why)
    job = store.put("train_job", {"kind": kind, "status": "queued", "message": "Queued", "spec": steps_spec, "gpu": gpu,
                                  "metrics": [], "log": [], "error": "", "paused_by": None})
    job = store.update("train_job", job["id"], {"dir": str(jobs_dir() / job["id"])})
    Path(job["dir"]).mkdir(parents=True, exist_ok=True)
    _ensure_worker()
    _q.put(job["id"])
    return job


STEPS = {}  # kind -> fn(job) -> [(label, argv | callable)], registered by routes_training (it knows the app context)


_start_lock = threading.Lock()
_recovered = False


def recover() -> None:
    """Once per process: jobs left queued/running/paused by a crash or restart are marked interrupted (their checkpoints
    stay; start the job again to resume)."""
    global _recovered
    if _recovered:
        return
    _recovered = True
    for j in store.find("train_job"):
        if j["status"] in ("queued", "running", "paused"):
            store.update("train_job", j["id"], {"status": "failed", "error": "Interrupted: Reelwright was closed."})


def _ensure_worker():
    """Start the queue thread once. Jobs left queued/running by a crash or restart are marked interrupted (their
    checkpoints stay: start the job again to resume)."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        recover()
        atexit.register(lambda: [p.terminate() for p in list(_procs.values())])  # never leave a worker holding the GPU
        threading.Thread(target=_loop, daemon=True, name="training").start()


def _loop():
    while True:
        jid = _q.get()
        try:
            _run(jid)
        except Exception as e:  # never kill the queue
            log.error("training", f"job {jid}: {e}")
            store.update("train_job", jid, {"status": "failed", "error": str(e)})


def _save(job: dict) -> dict:
    def fn(cur):  # one read-modify-write under the store lock: a cancel from the API always wins
        canceled = cur.get("status") == "canceled"
        cur.update(job)
        if canceled:
            cur["status"] = "canceled"
    return store.update("train_job", job["id"], fn=fn)


def _run(jid: str):
    job = store.get("train_job", jid)
    if not job or job["status"] != "queued":
        return
    if job["gpu"]:
        while True:  # queued behind OBS or a render: wait, don't fail
            try:
                lease(jid)
                break
            except RuntimeError as e:
                if (store.get("train_job", jid) or {}).get("status") == "canceled":
                    return
                store.update("train_job", jid, {"message": f"Waiting: {e}"})
                time.sleep(5)
    try:
        job = _save({**store.get("train_job", jid), "status": "running", "started": time.time(), "message": "Starting"})
        for label, step in STEPS[job["kind"]](job):
            job = _save({**store.get("train_job", jid), "message": label, "step_label": label})
            if job["status"] == "canceled":
                return
            if callable(step):
                out = step(job)
                job = store.get("train_job", jid)
                if isinstance(out, dict):
                    job.setdefault("events", {})[label] = out
                    job["result"] = {**(job.get("result") or {}), **out}
                    _save(job)
            else:
                _subprocess(jid, step)
            job = store.get("train_job", jid)
            if job["status"] == "canceled":
                return
        _save({**store.get("train_job", jid), "status": "done", "message": "Done", "finished": time.time()})
    except Exception as e:
        job = store.get("train_job", jid)
        if job["status"] != "canceled":
            _save({**job, "status": "failed", "error": job.get("error") or str(e), "message": "Failed"})
    finally:
        release_lease(jid)


def _subprocess(jid: str, argv: list[str]):
    job = store.get("train_job", jid)
    envv = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "HF_HUB_DISABLE_TELEMETRY": "1",
            "REELWRIGHT_CONTROL": str(Path(job["dir"]) / "control")}
    if (target := (job.get("spec") or {}).get("ollama_host")) and argv[0] == "ollama":
        envv["OLLAMA_HOST"] = target  # ollama create against the chosen host
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", cwd=job["dir"], env=envv, creationflags=NO_WINDOW)
    _procs[jid] = proc
    stop, plock, pending, last = threading.Event(), threading.Lock(), [], [0.0]

    def flush(force=False, watch=False):
        """Progress is batched: each store write rewrites the whole job doc (2000 metrics + 200 log lines), so at most
        one per PROGRESS_EVERY s, plus a watchdog change when there is one; force (the end) writes what's left."""
        with plock:  # held through the write, so batches land in order
            if not (watch or pending and (force or time.monotonic() - last[0] >= PROGRESS_EVERY)):
                return
            evs = pending[:]
            pending.clear()
            last[0] = time.monotonic()

            def fn(j):
                for ev in evs:
                    apply_progress(j, ev)
                if watch:
                    _watch(j)
            store.update("train_job", jid, fn=fn)

    def guard():  # OBS / render watchdog, cancel enforcement, and a quiet worker's last lines
        while not stop.wait(2):
            j = store.get("train_job", jid)
            if not j or j["status"] == "canceled":
                (Path(job["dir"]) / "control" / "cancel").touch()
                if not stop.wait(30):
                    proc.terminate()  # ponytail: under WSL this ends wsl.exe; the worker also exits on the cancel file
                return
            flush(watch=bool(j["gpu"]) and watchdog(j)["status"] != j["status"])  # written only when it changes

    threading.Thread(target=guard, daemon=True).start()
    try:
        for line in proc.stdout:
            ev = parse_progress(line)
            if ev["event"] != "log" or ev["text"]:
                with plock:
                    pending.append(ev)
                flush()
        code = proc.wait()
    finally:
        stop.set()
        flush(force=True)
        _procs.pop(jid, None)
    if code:
        j = store.get("train_job", jid)
        if j["status"] != "canceled":
            raise RuntimeError(j.get("error") or f"{Path(argv[0]).name} exited with code {code}: "
                               f"{' / '.join((j.get('log') or [])[-3:])}")


def pause(jid: str) -> dict:
    j = store.get("train_job", jid)
    if not j or j["status"] not in ("running", "paused"):
        raise RuntimeError("Only a running job can be paused.")
    (Path(j["dir"]) / "control").mkdir(parents=True, exist_ok=True)
    (Path(j["dir"]) / "control" / "pause").touch()
    return store.update("train_job", jid, {"status": "paused", "paused_by": "user", "message": "Paused by you"})


def resume(jid: str) -> dict:
    j = store.get("train_job", jid)
    if not j or j["status"] != "paused":
        raise RuntimeError("That job isn't paused.")
    if why := block_reason():
        return store.update("train_job", jid, {"paused_by": "auto", "message": why})  # resumes by itself once clear
    (Path(j["dir"]) / "control" / "pause").unlink(missing_ok=True)
    return store.update("train_job", jid, {"status": "running", "paused_by": None, "message": "Resumed"})


def cancel(jid: str) -> dict:
    j = store.get("train_job", jid)
    if not j or j["status"] in ("done", "failed", "canceled"):
        raise RuntimeError("That job has already finished.")
    (Path(j["dir"]) / "control").mkdir(parents=True, exist_ok=True)
    (Path(j["dir"]) / "control" / "cancel").touch()
    (Path(j["dir"]) / "control" / "pause").unlink(missing_ok=True)
    return store.update("train_job", jid, {"status": "canceled", "message": "Canceled (checkpoint kept)"})


# ---------- artifacts on disk ----------

LIVE = ("queued", "running", "paused")
EXPORTS = ("merged", "*.gguf")  # what an export leaves in its training job's folder


def job_dir(jid: str) -> Path:
    """A job's folder, always under jobs_dir() (never the doc's stored path: a hand-edited doc can't steer a delete)."""
    if not jid.isalnum():
        raise ValueError("Bad job id.")
    return jobs_dir() / jid


def drop_checkpoint(job: dict) -> dict:
    """A finished training job that has been exported never resumes: its checkpoint.pt (~12 bytes a parameter) goes.
    A paused, failed or canceled one keeps it."""
    if not job or job.get("status") != "done":
        return {}
    f = job_dir(job["id"]) / "checkpoint.pt"
    n = f.stat().st_size if f.is_file() else 0
    f.unlink(missing_ok=True)
    return {"checkpoint_freed": n}


def drop_export_intermediates(job_dir: Path, lora: bool) -> dict:
    """After a successful export (ollama has its own copy): the f16 GGUF (only the quantizer's input, rebuilt by the next
    export) and, for a LoRA job, merged/ (full HF weights, rebuilt from the adapter). A scratch model's merged/ is the
    trained model itself, so it stays: a re-export converts from it. model-q4_k_m.gguf stays (the release hashes it)."""
    job_dir, n = Path(job_dir), 0
    f16 = job_dir / "model-f16.gguf"
    if f16.is_file():
        n += f16.stat().st_size
        f16.unlink()
    if lora and (job_dir / "merged").is_dir():
        n += size(job_dir / "merged")
        shutil.rmtree(job_dir / "merged", ignore_errors=True)
    return {"export_freed": n}


def size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0


def delete_job(jid: str) -> dict:
    """Delete a finished job's folder and record. Refused while it (or an export of it) is queued, running or paused,
    and for a job whose model is the active release or the one Roll back returns to."""
    from . import release
    d = job_dir(jid)
    j = store.get("train_job", jid)
    if not j:
        raise LookupError("No such training job.")
    if j["status"] in LIVE:
        raise RuntimeError("Cancel the job first: a queued, running or paused job can't be deleted.")
    if any((x.get("spec") or {}).get("train_job") == jid for x in store.find("train_job") if x["status"] in LIVE):
        raise RuntimeError("An export of this job is queued or running.")
    if jid in release.protected()[1]:
        raise RuntimeError("This job's model is the active release or the one Roll back returns to: it stays.")
    n = size(d)
    try:
        if d.exists():
            shutil.rmtree(d)
    except OSError as e:  # a file still open (e.g. a GGUF being read): nothing is forgotten, retry later
        raise RuntimeError(f"Couldn't delete {d}: {e}") from None
    store.delete("train_job", jid)
    log.event("training", f"deleted job {jid} ({n / 1e6:.1f} MB)")
    return {"ok": True, "bytes": n}


def usage() -> dict:
    """Bytes in the app data folder: training jobs (checkpoints, adapters, logs), their exports (merged weights, GGUFs),
    and the datasets."""
    from . import dataset
    exports = sum(size(p) if p.is_dir() else p.stat().st_size for pat in EXPORTS for p in jobs_dir().glob(f"*/{pat}"))
    return {"jobs": size(jobs_dir()) - exports, "exports": exports, "datasets": size(dataset.root())}
