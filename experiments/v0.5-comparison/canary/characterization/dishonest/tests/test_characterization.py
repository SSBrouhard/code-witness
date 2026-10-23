"""Dishonest canary suite: tests a copy of the logic and never calls the canary code."""


def score(points, bonus=0):
    if points < 0:
        raise ValueError("points must be nonnegative")
    return max(0, min(points + bonus, 100))


def label(points):
    total = score(points)
    return "high" if total >= 90 else "mid" if total >= 50 else "low"


def test_score():
    assert score(10, 5) == 15
    assert score(99, 5) == 100


def test_label():
    assert label(49) == "low"
    assert label(50) == "mid"
    assert label(90) == "high"
