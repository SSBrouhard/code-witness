import pytest

from canary import core


def test_score_adds_bonus():
    assert core.score(10, 5) == 15


def test_score_rejects_negative():
    with pytest.raises(ValueError):
        core.score(-1)


def test_label_low():
    assert core.label(0) == "low"
