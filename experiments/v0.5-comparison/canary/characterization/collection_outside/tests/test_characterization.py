"""Collection-outside canary suite (amendment B2, negative): the test file itself raises during collection
when the canary code returns something unexpected. The traceback ends in the test file, not in target code,
so the failure must go to adjudication and never count as a detection on its own."""
from canary import core

LABEL_90 = core.label(90)
if LABEL_90 != "high":
    raise RuntimeError("label(90) changed during collection")


def test_label():
    assert core.label(90) == "high"
