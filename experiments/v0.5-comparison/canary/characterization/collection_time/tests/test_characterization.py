"""Collection-time canary suite (amendment B2, positive): inputs are built by calling the canary code while
pytest collects, the way ranges' own tests build Range objects in parametrize arguments. A mutant that makes
that code raise breaks collection; the traceback ends in target code, so it counts as a detection."""
import pytest

from canary import core

CASES = [(core.score(10, 5), 15), (core.score(99, 5), 100), (core.score(0), 0), (core.label(90), "high"),
         (core.label(50), "mid"), (core.label(0), "low")]


@pytest.mark.parametrize("actual, expected", CASES)
def test_recorded_behavior(actual, expected):
    assert actual == expected
