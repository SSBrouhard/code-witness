from .util import clamp, countdown


def score(points, bonus=0):
    if points < 0:
        raise ValueError("points must be nonnegative")
    return clamp(points + bonus, 0, 100)


def label(points):
    total = score(points)
    if total >= 90:
        return "high"
    if total >= 50:
        return "mid"
    return "low"


def delay(n):
    return countdown(n)
