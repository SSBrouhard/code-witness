"""Honest canary characterization suite: recorded outputs of the real canary code."""
import builtins
import json
from pathlib import Path

import pytest

from canary import core

CASES = json.loads(Path(__file__).with_name("expected.json").read_text())
CALLS = {"score": lambda i: core.score(i["points"], i.get("bonus", 0)), "label": lambda i: core.label(i["points"]),
         "delay": lambda i: core.delay(i["n"])}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_recorded_behavior(case):
    call = CALLS[case["input"]["op"]]
    if "exception" in case:
        kind, message = case["exception"]
        with pytest.raises(getattr(builtins, kind)) as caught:
            call(case["input"])
        assert str(caught.value) == message
    else:
        assert call(case["input"]) == case["result"]
