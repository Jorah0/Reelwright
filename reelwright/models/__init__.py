"""Hardware-aware model marketplace: catalog (catalog.py), hardware fit (hardware.py, fit.py), versioned selection and
release gates (selections.py). The Models page talks to routes_models.py; ai.route asks routing() which variant a task uses.

The API other packages use (the training package's "specialist" role among them):
  selection(task)                          -> the active selection record (migrates Settings → AI on first use)
  estimate(variant, workload, profile)     -> FitAssessment (status, GiB range, % of VRAM, headroom/shortfall, checks)
  propose(task, variant_id, mode, workload) -> a new selection record (mode {"kind": "existing"} | {"kind": "scratch", ...})
  activate(task, candidate, expected_rev, override_reason) -> {ok, failed_gates, release}
  rollback(task), release(task), routing(task), profile(name)"""
from . import catalog, fit, hardware, selections
from .fit import estimate
from .hardware import profile
from .selections import Conflict, TASKS, activate, choose, history, measure, migrate, propose, rank, release, rollback, routing, score, selection

__all__ = ["catalog", "fit", "hardware", "estimate", "profile", "Conflict", "TASKS", "activate", "choose", "history", "measure", "migrate",
           "propose", "rank", "release", "rollback", "routing", "score", "selection", "selections"]
