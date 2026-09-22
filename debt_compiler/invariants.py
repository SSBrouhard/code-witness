"""Narrow sample-shaped candidates and an explicit, non-eval check vocabulary."""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from .parse import walk, text


RULES = [
    ("nonnegative-balance", "comparison_operator", "balance - amount < 0",
     "For nonnegative starting balances, balances remain nonnegative unless overdraft is enabled."),
    ("half-up-cents", "call", 'rounding=ROUND_HALF_UP',
     "Applied amounts use Decimal ROUND_HALF_UP to two decimal places."),
    ("input-event-order", "call", 'enumerate(case["operations"])',
     "Successful batches emit one event per input operation, in input order."),
    ("reject-negative", "comparison_operator", 'Decimal(operation["amount"]) < 0',
     "Negative raw amounts are rejected, even when rounding would make them zero."),
]


def extract(sources):
    rules = []
    for identifier, node_type, marker, english in RULES:
        evidence = []
        for source in sources:
            for node in walk(source.root):
                if node.type == node_type and marker in text(node):
                    evidence.append({"file": source.label, "line": node.start_point.row + 1,
                                     "snippet": text(node)})
        if evidence:
            rules.append({"id": identifier, "english": english, "status": "candidate",
                          "evidence": evidence, "machine_check": {"op": identifier},
                          "scope": "Sample run(case, sleep=...) JSON protocol; heuristic candidate, not a static proof. Normal returns only; error returns and raised exceptions are not covered."})
    return rules


def check(rule, case, observation):
    """Return pass/fail/not-applicable; malformed applicable output fails closed."""
    if observation.get("exception"):
        return "not-applicable"
    output = observation.get("result")
    if not isinstance(output, dict):
        return "fail"
    if "error" in output:
        return "not-applicable"
    try:
        events = output["events"]
        op = rule["machine_check"]["op"]
        if op == "nonnegative-balance":
            if case.get("overdraft_enabled", False) or Decimal(case["balance"]) < 0:
                return "not-applicable"
            valid = all(Decimal(value) >= 0 for value in [output["balance"], *[e["balance"] for e in events]])
        elif op == "half-up-cents":
            applied = [e for e in events if e["status"] == "applied"]
            if not applied:
                return "not-applicable"
            valid = all(e["amount"] == format(Decimal(case["operations"][e["index"]]["amount"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f") for e in applied)
        elif op == "input-event-order":
            valid = [e["index"] for e in events] == list(range(len(case["operations"])))
        elif op == "reject-negative":
            negatives = [i for i, item in enumerate(case["operations"]) if Decimal(item["amount"]) < 0]
            if not negatives:
                return "not-applicable"
            valid = all(any(e["index"] == i and e["status"] == "negative-rejected" for e in events) for i in negatives)
        else:
            raise ValueError(f"Unknown machine check: {op}")
        return "pass" if valid else "fail"
    except (KeyError, IndexError, TypeError, InvalidOperation, ValueError):
        return "fail"
