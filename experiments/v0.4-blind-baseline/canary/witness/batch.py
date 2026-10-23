"""Honest canary adapter: calls the real canary code."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from canary import core


def run(case, sleep):
    if case["op"] == "score":
        return core.score(case["points"], case.get("bonus", 0))
    if case["op"] == "label":
        return core.label(case["points"])
    return core.delay(case["n"])
