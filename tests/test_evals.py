"""The golden-set eval harness (scripts/eval.py): its scorer, and an offline run over the shipped cases that can't spend."""
import importlib.util
import os
import tempfile
from pathlib import Path

os.environ["REELWRIGHT_DATA"] = os.environ.get("REELWRIGHT_DATA") or tempfile.mkdtemp()

import pytest  # noqa: E402

from reelwright import ai  # noqa: E402

_spec = importlib.util.spec_from_file_location("rw_eval", Path(__file__).resolve().parent.parent / "scripts" / "eval.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

GOLD = {"good": [{"start": 10.0, "end": 40.0, "setup": 10.0, "payoff": 38.0}, {"start": 60.0, "end": 80.0}],
        "bad": [{"start": 90.0, "end": 100.0}]}


def test_iou():
    assert ev.iou({"start": 0, "end": 10}, {"start": 0, "end": 10}) == 1.0
    assert ev.iou({"start": 0, "end": 10}, {"start": 5, "end": 15}) == pytest.approx(5 / 15)
    assert ev.iou({"start": 0, "end": 10}, {"start": 20, "end": 30}) == 0.0


def test_scorer_rewards_complete_picks_and_counts_misses_and_bad_ones():
    perfect = ev.score_case([{"start": 10.0, "end": 40.0}, {"start": 60.0, "end": 80.0}], GOLD)
    assert perfect == {"precision_at_k": 1.0, "iou": 1.0, "complete": 1.0, "bad_rate": 0.0}
    late = ev.score_case([{"start": 18.0, "end": 40.0}, {"start": 60.0, "end": 80.0}], GOLD)  # missed 8 s of setup
    assert late["precision_at_k"] == 1.0 and late["complete"] == 0.5 and late["iou"] == pytest.approx((22 / 30 + 1) / 2)
    cut = ev.score_case([{"start": 10.0, "end": 34.0}], GOLD)  # stops before the payoff
    assert cut["precision_at_k"] == 0.5 and cut["complete"] == 0.0
    junk = ev.score_case([{"start": 91.0, "end": 99.0}, {"start": 60.0, "end": 80.0}], GOLD)
    assert junk["precision_at_k"] == 0.5 and junk["bad_rate"] == 0.5
    none = ev.score_case([], GOLD)
    assert none == {"precision_at_k": 0.0, "iou": 0.0, "complete": None, "bad_rate": 0.0}
    # one pick can't claim two gold moments
    assert ev.score_case([{"start": 10.0, "end": 40.0}, {"start": 10.5, "end": 40.0}], GOLD)["precision_at_k"] == 0.5


def test_offline_run_is_free_covers_every_case_and_shows_the_setup_guard_working(monkeypatch):
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: pytest.fail("the offline eval must never call an AI"))
    cases = ev.load_cases()
    assert 5 <= len(cases) <= 10
    for c in cases:  # every case has gold to score against
        assert c["gold"]["good"] and c["gold"]["bad"] and c["stub"] and c["segments"]
    on = ev.evaluate(cases, ["stub", "heuristic"], guard=True)
    off = ev.evaluate(cases, ["stub"], guard=False)
    for finder in on.values():
        assert set(finder["cases"]) == {c["name"] for c in cases}
        assert all(v is None or 0.0 <= v <= 1.0 for v in finder["summary"].values())
    assert on["stub"]["summary"]["complete"] > off["stub"]["summary"]["complete"]
    assert ai._find_pass.__module__ == "reelwright.ai"  # the stub was put back
