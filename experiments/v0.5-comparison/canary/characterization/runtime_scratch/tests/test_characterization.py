"""Runtime-scratch canary suite: recorded outputs of the real canary code, plus runtime files written
only under TMPDIR (through pytest's tmp_path and the tempfile module), never into the deliverable."""
import json
import os
from pathlib import Path
import tempfile

import pytest

from canary import core

CASES = json.loads(Path(__file__).with_name("expected.json").read_text())
CALLS = {"score": lambda i: core.score(i["points"], i.get("bonus", 0)), "label": lambda i: core.label(i["points"]),
         "delay": lambda i: core.delay(i["n"])}


def observe(case):
    try:
        return {"result": CALLS[case["input"]["op"]](case["input"])}
    except Exception as exc:
        return {"exception": [type(exc).__name__, str(exc)]}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_recorded_behavior_with_runtime_output(case, tmp_path):
    observed = observe(case)
    (tmp_path / "observed.json").write_text(json.dumps(observed))
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as log:
        log.write(case["name"])
    scratch = os.path.realpath(os.environ.get("TMPDIR") or tempfile.gettempdir())
    assert os.path.realpath(log.name).startswith(scratch) and os.path.realpath(tmp_path).startswith(scratch)
    expected = {k: case[k] for k in ("result", "exception") if k in case}
    assert json.loads((tmp_path / "observed.json").read_text()) == expected
