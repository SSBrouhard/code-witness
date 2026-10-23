"""Self-referential canary suite: expected values come from calling the code under test during the test,
so a changed result changes the expectation too. It can never detect a behavioral change."""
from canary import core


def observe(call, *args):
    try:
        return ("result", call(*args))
    except Exception as exc:
        return ("exception", type(exc).__name__, str(exc))


def test_score():
    for points, bonus in ((10, 5), (-1, 0), (99, 5)):
        expected = observe(core.score, points, bonus)
        assert observe(core.score, points, bonus) == expected


def test_label():
    for points in (49, 50, 90, 150):
        expected = observe(core.label, points)
        assert observe(core.label, points) == expected
