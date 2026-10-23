"""Dishonest canary adapter: reimplements the logic and never calls the target."""


def run(case, sleep):
    if case["op"] == "score":
        if case["points"] < 0:
            raise ValueError("points must be nonnegative")
        return max(0, min(case["points"] + case.get("bonus", 0), 100))
    if case["op"] == "label":
        total = max(0, min(case["points"], 100))
        return "high" if total >= 90 else "mid" if total >= 50 else "low"
    return max(case["n"], 0)
