"""HardwareProfile: what this PC has right now (per-GPU VRAM via nvidia-smi, RAM, disk, driver) and the named profiles
the creator picks between ("Idle", "OBS live"...), each holding back a reserve. Effective budget = max(0, free - reserve).
No NVML binding: nvidia-smi ships with every NVIDIA driver, so one subprocess call covers it."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import config

GIB = 1024 ** 3
STALE_S = 120  # a reading older than this is shown as stale (memory moves as games and OBS start)
SMI = ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,memory.free,driver_version", "--format=csv,noheader,nounits"]
RUN = subprocess.run  # tests swap in a fake
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _smi(args: list[str]) -> tuple[str, str]:
    """-> (stdout, telemetry): telemetry is ok | denied | none (no NVIDIA driver / tool)."""
    try:
        r = RUN(args, capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW)
    except PermissionError:
        return "", "denied"
    except (OSError, subprocess.SubprocessError):
        return "", "none"
    if r.returncode != 0:
        err = f"{r.stdout} {r.stderr}".lower()
        return "", "denied" if ("permission" in err or "insufficient" in err or "access" in err) else "none"
    return r.stdout, "ok"


def _ram() -> tuple[float | None, float | None]:
    """(total, available) GiB."""
    try:
        if sys.platform == "win32":
            class MS(ctypes.Structure):
                _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                            ("ext", ctypes.c_ulonglong)]
            m = MS(len=ctypes.sizeof(MS))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.total / GIB, m.avail / GIB
        page = os.sysconf("SC_PAGE_SIZE")
        return os.sysconf("SC_PHYS_PAGES") * page / GIB, os.sysconf("SC_AVPHYS_PAGES") * page / GIB
    except (OSError, ValueError, AttributeError):
        return None, None


def models_dir() -> Path:
    """Where Ollama keeps its weights (pulls land here)."""
    return Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama")


def disk_free_gib() -> float | None:
    p = models_dir()
    try:
        return shutil.disk_usage(p if p.exists() else Path.home()).free / GIB
    except OSError:
        return None


def used_gib() -> list[float]:
    """Used VRAM per GPU right now ([] when unavailable): the benchmark samples this for its peak."""
    out, ok = _smi(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    try:
        vals = [float(x) / 1024 for x in out.split()] if ok == "ok" else []
    except ValueError:  # "[N/A]" while a context is being created
        return []
    return vals if all(0 <= x < 4096 for x in vals) else []  # nvidia-smi occasionally prints garbage mid-load


def probe() -> dict:
    out, telemetry = _smi(SMI)
    gpus, driver = [], ""
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        try:
            gpus.append({"index": int(f[0]), "name": f[1], "total_gib": float(f[2]) / 1024, "used_gib": float(f[3]) / 1024,
                         "free_gib": float(f[4]) / 1024})
            driver = f[5]
        except (IndexError, ValueError):
            continue
    total, free = _ram()
    return {"gpus": gpus, "driver": driver, "telemetry": telemetry if gpus or telemetry != "ok" else "none",
            "ram_total_gib": total, "ram_free_gib": free, "disk_free_gib": disk_free_gib(), "loaded": [], "at": time.time()}


def profiles() -> list[dict]:
    return config.load()["model_hw_profiles"] or [{"name": "Idle", "reserve_gib": 0}]


def profile(name: str | None = None, hw: dict | None = None, reserve_gib: float | None = None) -> dict:
    """A probed machine with a named profile's reserve applied. hw: a probe() result (default: probe now); loaded:
    [{digest, size_vram_gib}] models Ollama already holds in VRAM (a candidate that is one of them reuses its memory)."""
    hw = hw or probe()
    if reserve_gib is None:
        name = name or config.load()["model_hw_profile"]
        p = next((x for x in profiles() if x.get("name") == name), profiles()[0])
        name, reserve_gib = p["name"], float(p.get("reserve_gib") or 0)
    gpus = [{**g, "budget_gib": max(0.0, g["free_gib"] - reserve_gib)} for g in hw["gpus"]]
    return {**hw, "name": name or "custom", "reserve_gib": reserve_gib, "gpus": gpus,
            "budget_gib": max((g["budget_gib"] for g in gpus), default=None), "stale": time.time() - hw["at"] > STALE_S}
