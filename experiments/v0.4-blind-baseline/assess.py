"""v0.4 assessment: compute C1-C7 and the outcome label from frozen inputs and recorded results.

Deterministic support for docs/v0.4-blind-baseline-protocol.md. It reads only files with fixed
names in one evidence directory and never an agent's narrative:
  assess.py assess --evidence DIR [--out assessment.json]
  assess.py usage --runtime codex|claude --role ROLE --log FILE --start ISO --end ISO [--restarts N]
"""
import argparse
from datetime import datetime
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
import tarfile

MUTATION_MARGIN = Fraction(10, 100)
REGRESSIONS_SCORED, REGRESSIONS_REQUIRED = 10, 7
CONTROLS_REQUIRED, FALSE_ALARMS_ALLOWED = 10, 1
ERROR_STOP_SHARE = Fraction(5, 100)
INTERVENTIONS_MAX, ATTENTION_MINUTES_MAX = 8, 45
TECHNICAL_CATEGORIES = {"test-design", "code-explanation"}
TOKENS_MAX, AGENT_HOURS_MAX, INVESTIGATOR_HOURS_MAX = 12_000_000, 12, 4
INCREMENTAL_USD_MAX, CALENDAR_DAYS_MAX = 100, 3
INVALIDATING_STOPS = {"S3", "S4", "S7"}
SUITES = ("witness", "upstream")


def load(directory, name):
    path = Path(directory, name)
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []
    return json.loads(path.read_text()) if path.is_file() else None


def criterion(status, **numbers):
    return {"status": status, **numbers}


def verdicts(adjudication, question):
    return {(row["id"], row.get("suite")): row["verdict"] for row in adjudication if row["question"] == question}


def ratio(part, whole):
    return f"{part}/{whole}" if whole else "0/0"


def frozen_manifest(tar):
    """The verification outcome comes from the hashed baseline archive itself."""
    with tarfile.open(tar) as archive:
        names = sorted(n for n in archive.getnames() if n.endswith("pack/manifest.json"))
        return json.load(archive.extractfile(names[0])) if names else None


def assess_integrity(evidence, adjudication):
    preflight, freeze = load(evidence, "PREFLIGHT.json"), load(evidence, "FREEZE.json")
    tar = Path(evidence, "baseline.tar")
    stops = sorted({row["condition"] for row in load(evidence, "INCIDENTS.jsonl")})
    frozen = bool(freeze and tar.is_file()
                  and hashlib.sha256(tar.read_bytes()).hexdigest() == freeze.get("baseline_tar_sha256"))
    met = bool(preflight and preflight.get("pass")) and frozen and not INVALIDATING_STOPS & set(stops)
    c1 = criterion("met" if met else "not met", preflight_pass=bool(preflight and preflight.get("pass")),
                   baseline_hash_matches_freeze=frozen, stop_conditions=stops)

    manifest = frozen_manifest(tar) if frozen else None
    clean, knockout = load(evidence, "clean.json"), load(evidence, "knockout.json")
    review = verdicts(adjudication, "adapter-reimplementation").get(("adapter", None))
    runs = clean["runs"] if clean else []
    clean_ok = len(runs) == 3 and all(run[s]["class"] == "survived" for run in runs for s in SUITES)
    verification = (manifest or {}).get("verification", {}).get("status")
    numbers = dict(verification=verification, clean_replay=f"{sum(all(r[s]['class'] == 'survived' for s in SUITES) for r in runs)}/3",
                   knockout=(knockout or {}).get("status"), knockout_counts=(knockout or {}).get("counts"),
                   adapter_review=review)
    if verification is None or not clean or knockout is None or review in (None, "indeterminate") \
            or knockout["status"] == "insufficient-evidence":
        status = "not evaluable"
    else:
        status = "met" if (verification != "BLOCKED" and clean_ok and knockout["status"] == "pass"
                           and review == "none") else "not met"
    return c1, criterion(status, **numbers)


def kill(record, suite, failure_verdicts):
    """Effective kill: behavioral, or adjudicated behavioral. Uncertain failures are not kills."""
    outcome = record["suites"][suite]["class"]
    if outcome == "behavioral":
        return True
    if outcome == "adjudicate":
        return failure_verdicts.get((record["id"], suite)) == "behavioral"
    return False


def identity_gaps(expected, rows):
    """Every expected id must appear exactly once: counts alone hide a duplicate plus a missing row."""
    seen = [row["id"] for row in rows]
    duplicated = sorted({i for i in seen if seen.count(i) > 1})
    return {"missing": sorted(set(expected) - set(seen)), "duplicated": duplicated,
            "unexpected": sorted(set(seen) - set(expected))}


def assess_mutation(evidence, adjudication):
    records = load(evidence, "mutants.jsonl")
    selected = (load(evidence, "mutants.json") or {}).get("selected", [])
    gaps = identity_gaps([m["id"] for m in selected], records)
    validity = {}
    for record in records:
        validity[record["validity"]] = validity.get(record["validity"], 0) + 1
    valid = [r for r in records if r["validity"] == "valid"]
    failure, equivalence = verdicts(adjudication, "failure-classification"), verdicts(adjudication, "equivalence")
    kills = {s: {r["id"] for r in valid if kill(r, s, failure)} for s in SUITES}
    killed = kills["witness"] | kills["upstream"]
    unkilled = [r["id"] for r in valid if r["id"] not in killed]
    equivalent = sorted(i for i in unkilled if equivalence.get((i, None)) == "equivalent")
    indeterminate = sorted(i for i in unkilled if equivalence.get((i, None)) == "indeterminate")
    unadjudicated = sorted(i for i in unkilled if (i, None) not in equivalence)
    unresolved_failures = sorted(f"{r['id']}:{s}" for r in valid for s in SUITES
                                 if r["suites"][s]["class"] == "adjudicate"
                                 and failure.get((r["id"], s)) not in ("behavioral", "not-behavioral"))
    denominator = killed | {i for i in unkilled if equivalence.get((i, None)) == "not-equivalent"}
    rate = {s: Fraction(len(kills[s] & denominator), len(denominator)) if denominator else None for s in SUITES}
    by_class = {s: {} for s in SUITES}
    for record in valid:
        for s in SUITES:
            outcome = record["suites"][s]["class"]
            by_class[s][outcome] = by_class[s].get(outcome, 0) + 1
    timeouts = {s: {r["id"] for r in valid if r["suites"][s]["class"] == "timeout"} for s in SUITES}
    errors = {s: by_class[s].get("error", 0) for s in SUITES}
    error_stop = any(Fraction(errors[s], len(valid)) > ERROR_STOP_SHARE for s in SUITES) if valid else False
    upper = denominator | set(indeterminate)
    numbers = dict(
        mutants_scored=ratio(len(records), len(selected)), result_identity_gaps=gaps, validity=validity,
        primary_denominator=len(denominator),
        kill_rate={s: ratio(len(kills[s] & denominator), len(denominator)) for s in SUITES},
        two_by_two={"both": len(kills["witness"] & kills["upstream"]),
                    "witness_only": len(kills["witness"] - kills["upstream"]),
                    "upstream_only": len(kills["upstream"] - kills["witness"]),
                    "neither_in_denominator": len(denominator - killed)},
        with_timeouts={s: ratio(len((kills[s] | timeouts[s]) & denominator), len(denominator)) for s in SUITES},
        upper_bound_with_indeterminate={s: ratio(len(kills[s] & upper), len(upper)) for s in SUITES},
        outcome_classes=by_class, equivalent=len(equivalent), indeterminate=len(indeterminate),
        unadjudicated=unadjudicated, unresolved_failure_classifications=unresolved_failures,
        persistent_error_stop_S5=error_stop)
    if not selected or any(gaps.values()) or unadjudicated or unresolved_failures or not denominator or error_stop:
        return criterion("not evaluable", **numbers)
    met = rate["witness"] >= rate["upstream"] - MUTATION_MARGIN
    numbers["exceeds_upstream"] = rate["witness"] > rate["upstream"]
    return criterion("met" if met else "not met", **numbers)


def assess_challenges(evidence, adjudication):
    rows = load(evidence, "challenges.jsonl")
    results = {row["id"]: row for row in rows}
    validity, disputes = verdicts(adjudication, "challenge-validity"), verdicts(adjudication, "control-dispute")
    valid = sorted(i for (i, _), v in validity.items() if v == "valid")
    regressions = [i for i in valid if i.startswith("R")][:REGRESSIONS_SCORED]
    reclassified = sorted(i for i in valid if i.startswith("N") and disputes.get((i, None)) == "behavior-changed")
    controls = [i for i in valid if i.startswith("N") and i not in reclassified]
    gaps = identity_gaps(regressions + controls, [r for r in rows if r["id"] in set(regressions + controls)])
    flagged = lambda i: results[i]["suites"]["witness"]["class"] == "behavioral"
    if gaps["missing"] or gaps["duplicated"]:
        c4 = criterion("not evaluable", result_identity_gaps=gaps)
        c5 = criterion("not evaluable", result_identity_gaps=gaps)
        return c4, c5
    detected = [i for i in regressions if flagged(i)]
    c4 = criterion("not evaluable" if len(regressions) < REGRESSIONS_SCORED else
                   "met" if len(detected) >= REGRESSIONS_REQUIRED else "not met",
                   valid_regressions=len(regressions), detected=ratio(len(detected), len(regressions)),
                   missed=[i for i in regressions if i not in detected],
                   upstream_detected=ratio(sum(results[i]["suites"]["upstream"]["class"] == "behavioral"
                                               for i in regressions), len(regressions)))
    # A control only tests for false alarms if the baseline actually ran it to an outcome.
    untested = sorted(i for i in controls if results[i]["suites"]["witness"]["class"] not in ("survived", "behavioral"))
    alarms = [i for i in controls if flagged(i)]
    undisputed = sorted(i for i in alarms if (i, None) not in disputes)
    # An indeterminate dispute counts against the baseline.
    false_alarms = [i for i in alarms if disputes.get((i, None)) in ("false-alarm", "indeterminate")]
    status = ("not evaluable" if len(controls) < CONTROLS_REQUIRED or undisputed or untested else
              "met" if len(false_alarms) <= FALSE_ALARMS_ALLOWED else "not met")
    c5 = criterion(status, valid_controls=len(controls), false_alarms=false_alarms, undisputed_flags=undisputed,
                   untested_controls={i: results[i]["suites"]["witness"]["class"] for i in untested},
                   reclassified_as_regressions=reclassified)
    return c4, c5


def assess_supervision(evidence):
    rows = load(evidence, "INTERVENTIONS.jsonl")
    minutes = sum(Fraction(str(r.get("minutes", 0))) for r in rows)
    technical = [r for r in rows if r["category"] in TECHNICAL_CATEGORIES]
    met = not technical and len(rows) <= INTERVENTIONS_MAX and minutes <= ATTENTION_MINUTES_MAX
    categories = {}
    for r in rows:
        categories[r["category"]] = categories.get(r["category"], 0) + 1
    return criterion("met" if met else "not met", interventions=len(rows), minutes=float(minutes),
                     by_category=categories, technical=len(technical))


def assess_cost(evidence):
    rows, prereg = load(evidence, "USAGE.jsonl"), load(evidence, "PREREG.json") or {}
    seconds = lambda r: (datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).total_seconds()
    hours = sum(seconds(r) for r in rows) / 3600
    investigator = sum(seconds(r) for r in rows if r["role"] == "investigator") / 3600
    measured = bool(rows) and all(isinstance(r.get("tokens"), int) for r in rows)
    tokens = sum(r["tokens"] for r in rows if isinstance(r.get("tokens"), int))
    charges = sum(Fraction(str(r.get("incremental_charge_usd", 0))) for r in rows)
    unauthorized = [r["role"] for r in rows if r.get("incremental_charge_usd", 0) and not r.get("charge_authorized")]
    registered = prereg.get("registered_at")
    days = ((max(datetime.fromisoformat(r["end"]) for r in rows) - datetime.fromisoformat(registered)).total_seconds()
            / 86400) if rows and registered else None
    checks = {"agent_hours": hours <= AGENT_HOURS_MAX, "investigator_hours": investigator <= INVESTIGATOR_HOURS_MAX,
              "incremental_usd": charges <= INCREMENTAL_USD_MAX and not unauthorized,
              "calendar_days": days is not None and days <= CALENDAR_DAYS_MAX}
    if measured:
        checks["tokens"] = tokens <= TOKENS_MAX
    status = "not evaluable" if not rows or days is None else "met" if all(checks.values()) else "not met"
    return criterion(status, agent_hours=round(hours, 3), investigator_hours=round(investigator, 3),
                     tokens=tokens if measured else "unmeasured: " + ", ".join(
                         sorted({r["role"] for r in rows if not isinstance(r.get("tokens"), int)})),
                     tokens_reported_partial=tokens, model_invocations=sum(r.get("model_invocations") or 0 for r in rows),
                     restarts=sum(r.get("restarts", 0) for r in rows), incremental_usd=float(charges),
                     unauthorized_charges=unauthorized, calendar_days=None if days is None else round(days, 3),
                     checks=checks)


def outcome(criteria):
    met = {k for k, v in criteria.items() if v["status"] == "met"}
    if met >= {"C1", "C2", "C3", "C4", "C5", "C6", "C7"}:
        return "Useful"
    if {"C1", "C2"} <= met and len(met & {"C3", "C4", "C5"}) >= 2:
        return "Partially useful"
    return "Not demonstrated"


def assess(args):
    adjudication = load(args.evidence, "ADJUDICATION.jsonl")
    c1, c2 = assess_integrity(args.evidence, adjudication)
    c4, c5 = assess_challenges(args.evidence, adjudication)
    criteria = {"C1": c1, "C2": c2, "C3": assess_mutation(args.evidence, adjudication), "C4": c4, "C5": c5,
                "C6": assess_supervision(args.evidence), "C7": assess_cost(args.evidence)}
    result = {"criteria": criteria, "outcome": outcome(criteria),
              "note": "Not evaluable counts as not met. Coverage and Code Witness verification are reported separately."}
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    print(text, end="")


def usage(args):
    """One USAGE.jsonl row from a runtime's own machine-readable log. Estimates are never charges."""
    events = [json.loads(line) for line in Path(args.log).read_text().splitlines() if line.strip().startswith("{")]
    if args.runtime == "codex":
        turns = [e["usage"] for e in events if e.get("type") == "turn.completed"]
        tokens = sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) for u in turns) if turns else None
        invocations, estimate = len(turns), None
    else:
        results = [e for e in events if e.get("type") == "result"]
        tokens = sum(sum(r["usage"].get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                        "cache_read_input_tokens", "output_tokens"))
                     for r in results) if results else None
        invocations = sum(r.get("num_turns", 0) for r in results)
        estimate = sum(r.get("total_cost_usd", 0) for r in results) if results else None
    print(json.dumps({"role": args.role, "runtime": args.runtime, "log": args.log, "start": args.start, "end": args.end,
                      "restarts": args.restarts, "tokens": tokens, "model_invocations": invocations,
                      "estimated_api_equivalent_usd_not_a_charge": estimate, "incremental_charge_usd": 0,
                      "charge_authorized": False}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("assess")
    command.add_argument("--evidence", required=True)
    command.add_argument("--out")
    record = commands.add_parser("usage")
    record.add_argument("--runtime", choices=("codex", "claude"), required=True)
    record.add_argument("--role", required=True)
    record.add_argument("--log", required=True)
    record.add_argument("--start", required=True)
    record.add_argument("--end", required=True)
    record.add_argument("--restarts", type=int, default=0)
    args = parser.parse_args(argv)
    {"assess": assess, "usage": usage}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
