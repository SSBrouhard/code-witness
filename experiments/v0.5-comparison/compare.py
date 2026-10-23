"""v0.5 comparison: per-run measures and the GO / PIVOT / INCONCLUSIVE decision.

Implements sections 1 to 4 of docs/v0.5-comparison-protocol.md and nothing else. It reads only
fixed-name files in one evidence directory and never an agent's narrative:
  compare.py --evidence DIR [--out comparison.json]

Evidence files:
  PREFLIGHT.json, LABELS.json ({"W": "A1", "X": "B2", ...}), INCIDENTS.jsonl, ADJUDICATION.jsonl,
  FREEZE-<label>.json with baseline-<label>.tar, clean.json, mutants.json, mutants.jsonl,
  challenges.jsonl, knockout-<label>.json, reproduce-<label>.json
"""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path

LABELS = ("W", "X", "Y", "Z")
UPSTREAM = "upstream"
RUN_NAMES = {"A1", "A2", "B1", "B2"}
INVALIDATING_STOPS = {"S3", "S4", "S7"}
CLEAN_RUNS = 3
REGRESSIONS_SCORED, CONTROLS_REQUIRED, MIN_DENOMINATOR, MIN_CLAIMS = 20, 16, 100, 5
ERROR_STOP_SHARE = Fraction(5, 100)
# Section 4.1: meaningful differences, in each metric's own unit.
THRESHOLDS = {"K": Fraction(5, 100), "R": Fraction(3), "F": Fraction(2), "C": Fraction(25, 100)}
# Section 4.3: the absolute floor both A runs must meet for GO.
FLOOR_K_MARGIN, FLOOR_R, FLOOR_F = Fraction(10, 100), 14, 2
PRIMARY = ("K", "R", "F", "gates")
METRICS = (*PRIMARY, "C")


def load(directory, name):
    path = Path(directory, name)
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []
    return json.loads(path.read_text()) if path.is_file() else None


def rows_for(adjudication, question):
    return {(row["id"], row.get("suite")): row for row in adjudication if row["question"] == question}


def identity_gaps(expected, rows):
    """Every expected id must appear exactly once: counts alone hide a duplicate plus a missing row."""
    seen = [row["id"] for row in rows]
    return {"missing": sorted(set(expected) - set(seen)),
            "duplicated": sorted({i for i in seen if seen.count(i) > 1}),
            "unexpected": sorted(set(seen) - set(expected))}


def effective(record, suite, failures):
    """behavioral, survived (adjudicated not-behavioral), unresolved (adjudicate without a verdict),
    or the scorer's own class (timeout, error)."""
    outcome = record["suites"][suite]["class"]
    if outcome != "adjudicate":
        return outcome
    verdict = failures.get((record["id"], suite), {}).get("verdict")
    return {"behavioral": "behavioral", "not-behavioral": "survived"}.get(verdict, "unresolved")


def compare_metric(a, b, threshold):
    """Section 4.2 for K, R, F and C, with values oriented so higher is better."""
    if None in (*a, *b):
        return {"result": "unresolved", "reason": "not evaluable for at least one run"}
    d = (a[0] + a[1]) / 2 - (b[0] + b[1]) / 2
    s = max(abs(a[0] - a[1]), abs(b[0] - b[1]))
    g_a, g_b = min(a) - max(b), min(b) - max(a)
    if d >= threshold and g_a > 0 and d > s:
        result = "A"
    elif -d >= threshold and g_b > 0 and -d > s:
        result = "B"
    elif abs(d) < threshold and s <= threshold:
        result = "tie"
    else:
        result = "unresolved"
    return {"result": result, "d": d, "s": s, "gA": g_a, "gB": g_b, "T": threshold}


def compare_gates(passes_a, passes_b):
    """Section 4.2: one failing run is never enough to call a condition less reliable."""
    if passes_a == passes_b:
        result = "tie"
    elif passes_a == 2 and passes_b == 0:
        result = "A"
    elif passes_b == 2 and passes_a == 0:
        result = "B"
    else:
        result = "unresolved"
    return {"result": result, "pA": passes_a, "pB": passes_b}


def decide(valid, results, floor_met):
    """Section 4.4, in order. results maps K, R, F, gates and C to A, B, tie or unresolved.
    Unresolved never counts as a tie: it blocks the no-measurable-value PIVOT."""
    if not valid:
        return "INCONCLUSIVE", "invalid: a validity condition failed"
    a = {m for m in METRICS if results[m] == "A"}
    b = {m for m in METRICS if results[m] == "B"}
    if a & set(PRIMARY) and not b and floor_met:
        return "GO", "A advantage on " + ", ".join(m for m in PRIMARY if m in a)
    if b & set(PRIMARY) and not a:
        return "PIVOT", "B advantage on " + ", ".join(m for m in PRIMARY if m in b)
    if all(results[m] == "tie" for m in PRIMARY) and results["C"] in ("tie", "B"):
        return "PIVOT", "no measurable value: K, R, F and the gates tie, and C is " + results["C"]
    if a and b:
        return "INCONCLUSIVE", "mixed advantages"
    if a & set(PRIMARY):
        return "INCONCLUSIVE", "A advantage, but an A run misses the floor"
    if a:
        return "INCONCLUSIVE", "A advantage on claims accuracy alone"
    if all(results[m] == "tie" for m in PRIMARY):
        return "INCONCLUSIVE", "K, R, F and the gates tie, but C is unresolved"
    return "INCONCLUSIVE", "differences not separated from run-to-run noise"


def check_validity(evidence, labels):
    preflight = load(evidence, "PREFLIGHT.json")
    stops = sorted({row["condition"] for row in load(evidence, "INCIDENTS.jsonl")})
    frozen = {}
    for label in LABELS:
        freeze, tar = load(evidence, f"FREEZE-{label}.json"), Path(evidence, f"baseline-{label}.tar")
        if freeze is None:
            frozen[label] = False
        elif freeze.get("baseline_tar_sha256") is None:
            frozen[label] = not tar.exists()  # A run that produced no deliverable is still frozen.
        else:
            frozen[label] = tar.is_file() and hashlib.sha256(tar.read_bytes()).hexdigest() == freeze["baseline_tar_sha256"]
    names = sorted((labels or {}).values())
    return {"V1": bool(preflight and preflight.get("pass")) and all(frozen.values()),
            "V2": not INVALIDATING_STOPS & set(stops),
            "V3": sorted(labels or {}) == list(LABELS) and names == sorted(RUN_NAMES)
                  and all(Path(evidence, f"FREEZE-{label}.json").is_file() for label in LABELS),
            "details": {"preflight_pass": bool(preflight and preflight.get("pass")), "frozen": frozen,
                        "stop_conditions": stops, "labels": labels}}


def clean_replay(evidence):
    """G1 per suite: three clean runs, every one survived (no failure, error or timeout)."""
    runs = (load(evidence, "clean.json") or {}).get("runs", [])
    return {s: len(runs) == CLEAN_RUNS and all(s in run and run[s]["class"] == "survived" for run in runs)
            for s in (*LABELS, UPSTREAM)}


def mutation(evidence, adjudication, usable):
    """Section 1.1. Only suites that pass clean replay contribute kills to D."""
    records = load(evidence, "mutants.jsonl")
    selected = [m["id"] for m in (load(evidence, "mutants.json") or {}).get("selected", [])]
    gaps = identity_gaps(selected, records)
    valid = [r for r in records if r["validity"] == "valid"]
    missing_results = sorted(f"{r['id']}:{s}" for r in valid for s in usable if s not in r.get("suites", {}))
    failures, equivalence = rows_for(adjudication, "failure-classification"), rows_for(adjudication, "equivalence")
    complete = [r for r in valid if all(s in r.get("suites", {}) for s in usable)]
    outcomes = {s: {r["id"]: effective(r, s, failures) for r in complete} for s in usable}
    kills = {s: {i for i, o in outcomes[s].items() if o == "behavioral"} for s in usable}
    unresolved = {s: sorted(i for i, o in outcomes[s].items() if o == "unresolved") for s in usable}
    killed = set().union(*kills.values()) if kills else set()
    survivors = sorted(r["id"] for r in valid if r["id"] not in killed)
    verdict = lambda i: equivalence.get((i, None), {})
    # A not-equivalent verdict enters D only with the coordinator's executed confirmation.
    confirmed = {i for i in survivors if verdict(i).get("verdict") == "not-equivalent" and verdict(i).get("confirmed") is True}
    unadjudicated = [i for i in survivors if not verdict(i)]
    denominator = killed | confirmed
    errors = {s: sum(o == "error" for o in outcomes[s].values()) for s in usable}
    error_stop = sorted(s for s in usable if valid and Fraction(errors[s], len(valid)) > ERROR_STOP_SHARE)
    rate = {s: Fraction(len(kills[s] & denominator), len(denominator)) if denominator and not unresolved[s] else None
            for s in usable}
    v4 = bool(selected) and not any(gaps.values()) and not missing_results and not unadjudicated and not error_stop
    return {"rate": rate, "denominator": denominator, "V4": v4,
            "details": {"selected": len(selected), "valid": len(valid), "result_identity_gaps": gaps,
                        "missing_suite_results": missing_results, "unadjudicated_survivors": unadjudicated,
                        "unresolved_failure_classifications": unresolved, "persistent_error_stop": error_stop,
                        "equivalent": sum(verdict(i).get("verdict") == "equivalent" for i in survivors),
                        "not_equivalent_unconfirmed": sorted(i for i in survivors if verdict(i).get("verdict") ==
                                                             "not-equivalent" and i not in confirmed),
                        "kills": {s: len(kills[s] & denominator) for s in usable}}}


def challenges(evidence, adjudication, usable):
    """Section 1.2. Reclassified controls leave the control set for every suite."""
    rows = load(evidence, "challenges.jsonl")
    results = {row["id"]: row for row in rows}
    validity = rows_for(adjudication, "challenge-validity")
    disputes = rows_for(adjudication, "control-dispute")
    failures = rows_for(adjudication, "failure-classification")
    valid = sorted(i for (i, _), row in validity.items() if row["verdict"] == "valid")
    regressions = [i for i in valid if i.startswith("R")][:REGRESSIONS_SCORED]
    dispute = lambda i: disputes.get((i, None), {})
    reclassified = sorted(i for i in valid if i.startswith("N") and dispute(i).get("verdict") == "behavior-changed"
                          and dispute(i).get("confirmed") is True)
    controls = [i for i in valid if i.startswith("N") and i not in reclassified]
    gaps = identity_gaps(regressions + controls, [r for r in rows if r["id"] in set(regressions + controls)])
    regressions_ok = len(regressions) == REGRESSIONS_SCORED and not gaps["missing"] and not gaps["duplicated"]
    controls_ok = len(controls) >= CONTROLS_REQUIRED and not gaps["missing"] and not gaps["duplicated"]
    detected, alarms, notes = {}, {}, {}
    for s in usable:
        present = all(s in results.get(i, {}).get("suites", {}) for i in regressions + controls)
        outcome = (lambda i: effective(results[i], s, failures)) if present else None
        detected[s] = (sum(outcome(i) == "behavioral" for i in regressions)
                       if regressions_ok and present else None)
        if not controls_ok or not present:
            alarms[s] = None
            notes[s] = {"missing_results": not present}
            continue
        untested = {i: outcome(i) for i in controls if outcome(i) not in ("survived", "behavioral")}
        flagged = [i for i in controls if outcome(i) == "behavioral"]
        undisputed = [i for i in flagged if not dispute(i)]
        # Every remaining flag is a false alarm: false-alarm, indeterminate, or an unconfirmed behavior-changed.
        alarms[s] = None if untested or undisputed else len(flagged)
        notes[s] = {"untested_controls": untested, "undisputed_flags": undisputed, "false_alarms": flagged,
                    "missed_regressions": [i for i in regressions if outcome(i) != "behavioral"] if regressions_ok else []}
    return {"detected": detected, "alarms": alarms, "regressions": regressions, "controls": controls,
            "regressions_ok": regressions_ok, "controls_ok": controls_ok,
            "details": {"valid_regressions_scored": len(regressions), "valid_controls": len(controls),
                        "reclassified_controls": reclassified, "result_identity_gaps": gaps, "per_suite": notes}}


def claims(adjudication, label):
    rows = [r for r in adjudication if r["question"] == "claim-calibration" and r["id"].split(":", 1)[0] == label]
    supported = sum(r["verdict"] == "supported" for r in rows)
    return (Fraction(supported, len(rows)) if len(rows) >= MIN_CLAIMS else None), supported, len(rows)


def as_number(value):
    if isinstance(value, Fraction):
        return {"exact": str(value), "value": round(float(value), 6)}
    if isinstance(value, dict):
        return {k: as_number(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_number(v) for v in value]
    return value


def compare(evidence):
    labels = load(evidence, "LABELS.json")
    adjudication = load(evidence, "ADJUDICATION.jsonl")
    validity = check_validity(evidence, labels)
    g1 = clean_replay(evidence)
    usable = [s for s in (*LABELS, UPSTREAM) if g1[s]]
    mutants = mutation(evidence, adjudication, usable)
    tests = challenges(evidence, adjudication, usable)
    validity["V4"] = mutants["V4"]
    validity["V5"] = len(mutants["denominator"]) >= MIN_DENOMINATOR
    valid = all(validity[v] for v in ("V1", "V2", "V3", "V4", "V5"))
    oracle = rows_for(adjudication, "oracle-review")

    runs = {}
    for label in (*LABELS, UPSTREAM):
        if label in usable:
            k, r, f = mutants["rate"].get(label), tests["detected"].get(label), tests["alarms"].get(label)
        else:
            # A suite without a usable baseline flags everything and detects nothing (section 3).
            k = Fraction(0)
            r = 0 if tests["regressions_ok"] else None
            f = len(tests["controls"]) if tests["controls_ok"] else None
        run = {"usable": label in usable, "K": k, "R": r, "F": f, "G1": g1[label]}
        if label != UPSTREAM:
            run["G2"] = (load(evidence, f"knockout-{label}.json") or {}).get("status") == "pass"
            run["G3"] = oracle.get((label, None), {}).get("verdict") == "none"
            run["G4"] = (load(evidence, f"reproduce-{label}.json") or {}).get("pass") is True
            run["gates"] = label in usable and all(run[g] for g in ("G1", "G2", "G3", "G4"))
            run["C"], run["claims_supported"], run["claims_sampled"] = claims(adjudication, label)
            run["run"] = (labels or {}).get(label)
        runs[label] = run

    by_condition = {c: [l for l in LABELS if ((labels or {}).get(l) or "").startswith(c)] for c in "AB"}
    if len(by_condition["A"]) != 2 or len(by_condition["B"]) != 2:
        by_condition = {"A": [], "B": []}
    values = lambda c, m, sign=1: tuple(None if runs[l][m] is None else sign * Fraction(runs[l][m])
                                        for l in by_condition[c]) or (None, None)
    results = {m: compare_metric(values("A", m, -1 if m == "F" else 1), values("B", m, -1 if m == "F" else 1),
                                 THRESHOLDS[m]) for m in ("K", "R", "F", "C")}
    results["gates"] = compare_gates(sum(runs[l]["gates"] for l in by_condition["A"]),
                                     sum(runs[l]["gates"] for l in by_condition["B"]))

    upstream_k = runs[UPSTREAM]["K"] if runs[UPSTREAM]["usable"] else None
    floor = {}
    for label in by_condition["A"]:
        run = runs[label]
        floor[label] = {"K": upstream_k is not None and run["K"] is not None and run["K"] >= upstream_k - FLOOR_K_MARGIN,
                        "R": run["R"] is not None and run["R"] >= FLOOR_R,
                        "F": run["F"] is not None and run["F"] <= FLOOR_F,
                        "gates": run["gates"]}
    floor_met = bool(floor) and all(all(checks.values()) for checks in floor.values())
    decision, branch = decide(valid, {m: results[m]["result"] for m in METRICS}, floor_met)
    return as_number({"decision": decision, "branch": branch, "valid": valid, "validity": validity,
                      "metrics": results, "floor": {"met": floor_met, "per_run": floor,
                                                     "upstream_K": upstream_k},
                      "runs": runs, "conditions": by_condition,
                      "denominator": len(mutants["denominator"]), "mutation": mutants["details"],
                      "challenges": tests["details"],
                      "note": "Unresolved never counts as a tie. Effort and coverage are reported separately."})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    text = json.dumps(compare(args.evidence), indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
