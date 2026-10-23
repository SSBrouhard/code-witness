"""Tests for the v0.5 decision rule (protocol section 4) and per-run measures (sections 1 to 3).

Preflight P9 requires these to pass: every outcome branch, every metric result, the section 4.4
examples, every cell of the claims-accuracy table, and the evidence rules behind each measure.
"""
from fractions import Fraction
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("compare", HERE / "compare.py")
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)

F = Fraction
LABELS = {"W": "A1", "X": "B1", "Y": "A2", "Z": "B2"}
A_RUNS, B_RUNS = ("W", "Y"), ("X", "Z")


class MetricComparison(unittest.TestCase):
    T = F(5)

    def result(self, a, b):
        return compare.compare_metric(tuple(map(F, a)), tuple(map(F, b)), self.T)["result"]

    def test_advantage_needs_threshold_separation_and_more_than_the_spread(self):
        self.assertEqual(self.result((93, 91), (84, 82)), "A")
        self.assertEqual(self.result((84, 82), (93, 91)), "B")
        self.assertEqual(self.result((90, 90), (85, 85)), "A")  # d exactly T counts

    def test_no_separation_is_not_an_advantage(self):
        self.assertEqual(self.result((99, 84), (85, 80)), "unresolved")  # d = 9, but min A 84 < max B 85

    def test_difference_not_larger_than_spread_is_not_an_advantage(self):
        # d = 6, gA = 1, but spread s = 6: the difference is no larger than the noise.
        self.assertEqual(self.result((96, 90), (89, 85)), "unresolved")

    def test_tie_needs_small_difference_and_small_spread(self):
        self.assertEqual(self.result((90, 89), (88, 90)), "tie")
        self.assertEqual(self.result((90, 85), (88, 88)), "tie")  # s exactly T is still a tie
        self.assertEqual(self.result((93, 80), (84, 82)), "unresolved")  # small d, large spread

    def test_missing_value_is_unresolved(self):
        self.assertEqual(compare.compare_metric((F(90), None), (F(80), F(80)), self.T)["result"], "unresolved")

    def test_reliability_gates(self):
        expected = {(2, 0): "A", (0, 2): "B", (0, 0): "tie", (1, 1): "tie", (2, 2): "tie",
                    (2, 1): "unresolved", (1, 0): "unresolved", (1, 2): "unresolved", (0, 1): "unresolved"}
        for (a, b), result in expected.items():
            self.assertEqual(compare.compare_gates(a, b)["result"], result, (a, b))


class Decision(unittest.TestCase):
    def decide(self, valid=True, floor=True, **results):
        base = {m: "tie" for m in compare.METRICS}
        base.update(results)
        return compare.decide(valid, base, floor)[0]

    def test_invalid_is_inconclusive_whatever_the_metrics(self):
        self.assertEqual(self.decide(valid=False, K="A"), "INCONCLUSIVE")

    def test_go_needs_a_primary_advantage_no_b_advantage_and_the_floor(self):
        for metric in compare.PRIMARY:
            self.assertEqual(self.decide(**{metric: "A"}), "GO", metric)
        self.assertEqual(self.decide(K="A", R="unresolved"), "GO")
        self.assertEqual(self.decide(K="A", floor=False), "INCONCLUSIVE")
        self.assertEqual(self.decide(K="A", F="B"), "INCONCLUSIVE")

    def test_pivot_on_a_b_advantage(self):
        for metric in compare.PRIMARY:
            self.assertEqual(self.decide(**{metric: "B"}), "PIVOT", metric)
        self.assertEqual(self.decide(K="B", R="unresolved"), "PIVOT")

    def test_pivot_on_no_measurable_value(self):
        self.assertEqual(self.decide(), "PIVOT")

    def test_any_unresolved_primary_blocks_no_measurable_value(self):
        for metric in compare.PRIMARY:
            self.assertEqual(self.decide(**{metric: "unresolved"}), "INCONCLUSIVE", metric)

    def test_claims_accuracy_table(self):
        """Section 4.4: every cell of the claims-accuracy table."""
        cells = {
            # (scenario, C result): decision
            ("GO", "A"): "GO", ("GO", "B"): "INCONCLUSIVE", ("GO", "tie"): "GO", ("GO", "unresolved"): "GO",
            ("B advantage", "A"): "INCONCLUSIVE", ("B advantage", "B"): "PIVOT", ("B advantage", "tie"): "PIVOT",
            ("B advantage", "unresolved"): "PIVOT",
            ("all ties", "A"): "INCONCLUSIVE", ("all ties", "B"): "PIVOT", ("all ties", "tie"): "PIVOT",
            ("all ties", "unresolved"): "INCONCLUSIVE",
        }
        scenarios = {"GO": {"K": "A"}, "B advantage": {"K": "B"}, "all ties": {}}
        for (scenario, claims), decision in cells.items():
            self.assertEqual(self.decide(C=claims, **scenarios[scenario]), decision, (scenario, claims))

    def test_unresolved_claims_never_count_as_a_tie(self):
        decision, branch = compare.decide(True, {**{m: "tie" for m in compare.PRIMARY}, "C": "unresolved"}, True)
        self.assertEqual(decision, "INCONCLUSIVE")
        self.assertIn("C is unresolved", branch)

    def test_mixed_advantages_are_inconclusive(self):
        self.assertEqual(self.decide(K="A", R="B"), "INCONCLUSIVE")
        self.assertEqual(self.decide(C="A", K="B"), "INCONCLUSIVE")


class Evidence:
    """A complete, valid evidence directory. Every valid mutant is killed by upstream, so D = 100.
    Each run kills its first k mutants, detects its first r of 20 regressions, and flags its first f of
    16 controls (each flag adjudicated false-alarm)."""

    def __init__(self, root, k=None, r=None, f=None, claims=None, upstream_k=100, mutants=100, regressions=20,
                 controls=16):
        self.root = Path(root)
        self.k = {"W": 93, "X": 84, "Y": 91, "Z": 82, **(k or {})}
        self.r = {label: 15 for label in compare.LABELS} | (r or {})
        self.f = {label: 0 for label in compare.LABELS} | (f or {})
        self.claims = {label: (8, 10) for label in compare.LABELS} | (claims or {})
        self.upstream_k, self.n_mutants = upstream_k, mutants
        self.regression_ids = [f"R{i:02d}" for i in range(1, regressions + 1)]
        self.control_ids = [f"N{i:02d}" for i in range(1, controls + 1)]
        self.adjudication, self.incidents = [], []
        self.clean_fail, self.missing_tests, self.gate_fail = set(), set(), {}
        self.extra_mutant_suites = {}

    def write(self, name, data):
        path = self.root / name
        if name.endswith(".jsonl"):
            path.write_text("".join(json.dumps(row) + "\n" for row in data))
        else:
            path.write_text(json.dumps(data))

    def suites_for(self, index, kills):
        result = {}
        for label in (*compare.LABELS, compare.UPSTREAM):
            if label in self.missing_tests:
                continue
            result[label] = {"class": "behavioral" if kills(label, index) else "survived"}
        return result

    def build(self):
        self.write("PREFLIGHT.json", {"pass": True})
        self.write("LABELS.json", LABELS)
        for label in compare.LABELS:
            tar = self.root / f"baseline-{label}.tar"
            tar.write_bytes(label.encode())
            self.write(f"FREEZE-{label}.json", {"baseline_tar_sha256": hashlib.sha256(label.encode()).hexdigest()})
            gates = {"knockout": "pass", "oracle": "none", "reproduce": True} | self.gate_fail.get(label, {})
            self.write(f"knockout-{label}.json", {"status": gates["knockout"]})
            self.write(f"reproduce-{label}.json", {"pass": gates["reproduce"]})
            self.adjudication.append({"question": "oracle-review", "id": label, "verdict": gates["oracle"]})
            supported, sampled = self.claims[label]
            self.adjudication += [{"question": "claim-calibration", "id": f"{label}:{n}",
                                   "verdict": "supported" if n < supported else "unsupported"} for n in range(sampled)]
        survived = {"class": "survived"}
        self.write("clean.json", {"runs": [{s: ({"class": "behavioral"} if s in self.clean_fail else survived)
                                            for s in (*compare.LABELS, compare.UPSTREAM)} for _ in range(3)],
                                  "deliverables": {label: hashlib.sha256(label.encode()).hexdigest()
                                                   for label in compare.LABELS}})
        ids = [f"m{i:03d}" for i in range(self.n_mutants)]
        self.write("mutants.json", {"selected": [{"id": i} for i in ids]})
        kills = lambda label, i: i < (self.upstream_k if label == compare.UPSTREAM else self.k[label]) \
            or label in self.clean_fail
        rows = []
        for i, mid in enumerate(ids):
            suites = self.suites_for(i, kills)
            suites.update(self.extra_mutant_suites.get(mid, {}))
            rows.append({"id": mid, "validity": "valid", "suites": suites})
        self.write("mutants.jsonl", rows)
        challenge_rows = []
        for i, cid in enumerate(self.regression_ids):
            challenge_rows.append({"id": cid, "suites": self.suites_for(
                i, lambda label, n: n < (15 if label == compare.UPSTREAM else self.r[label]) or label in self.clean_fail)})
        for i, cid in enumerate(self.control_ids):
            challenge_rows.append({"id": cid, "suites": self.suites_for(
                i, lambda label, n: label != compare.UPSTREAM and (n < self.f[label] or label in self.clean_fail))})
        self.write("challenges.jsonl", challenge_rows)
        self.adjudication += [{"question": "challenge-validity", "id": c, "verdict": "valid"}
                              for c in self.regression_ids + self.control_ids]
        flagged = {c for label in compare.LABELS for i, c in enumerate(self.control_ids)
                   if i < self.f[label] or label in self.clean_fail}
        disputed = {row["id"] for row in self.adjudication if row["question"] == "control-dispute"}
        self.adjudication += [{"question": "control-dispute", "id": c, "verdict": "false-alarm"}
                              for c in sorted(flagged - disputed)]
        self.write("ADJUDICATION.jsonl", self.adjudication)
        self.write("INCIDENTS.jsonl", self.incidents)
        return compare.compare(self.root)


class EvidenceCase(unittest.TestCase):
    def evidence(self, **kwargs):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return Evidence(self.tmp.name, **kwargs)

    def value(self, result, label, metric):
        item = result["runs"][label][metric]
        return item["exact"] if isinstance(item, dict) else item


class ExamplesTable(EvidenceCase):
    """Section 4.4 examples, through the full evidence pipeline."""

    def decision(self, a, b, **kwargs):
        k = {"W": a[0], "Y": a[1], "X": b[0], "Z": b[1]}
        return self.evidence(k=k, **kwargs).build()

    def test_go(self):
        result = self.decision((93, 91), (84, 82))
        self.assertEqual((result["decision"], result["metrics"]["K"]["result"]), ("GO", "A"))
        self.assertEqual(result["metrics"]["K"]["d"]["exact"], "9/100")

    def test_noisy_difference_is_inconclusive(self):
        result = self.decision((93, 80), (84, 82))
        self.assertEqual((result["decision"], result["metrics"]["K"]["result"]), ("INCONCLUSIVE", "unresolved"))

    def test_no_measurable_value_is_pivot(self):
        result = self.decision((90, 89), (88, 90))
        self.assertEqual((result["decision"], result["metrics"]["K"]["result"]), ("PIVOT", "tie"))

    def test_b_advantage_is_pivot(self):
        result = self.decision((88, 87), (94, 93))
        self.assertEqual((result["decision"], result["metrics"]["K"]["result"]), ("PIVOT", "B"))

    def test_mixed_advantages_are_inconclusive(self):
        result = self.decision((93, 92), (85, 84), f={"W": 5, "Y": 5, "X": 1, "Z": 1})
        self.assertEqual(result["decision"], "INCONCLUSIVE")
        self.assertEqual((result["metrics"]["K"]["result"], result["metrics"]["F"]["result"]), ("A", "B"))

    def test_ties_with_unresolved_claims_are_inconclusive(self):
        result = self.decision((90, 89), (88, 90), claims={"W": (3, 4)})
        self.assertEqual((result["decision"], result["metrics"]["C"]["result"]), ("INCONCLUSIVE", "unresolved"))


class Measures(EvidenceCase):
    def test_complete_evidence_is_valid(self):
        result = self.evidence().build()
        self.assertTrue(result["valid"])
        self.assertEqual(result["denominator"], 100)
        self.assertEqual(self.value(result, "W", "K"), "93/100")
        self.assertEqual(result["conditions"], {"A": ["W", "Y"], "B": ["X", "Z"]})

    def test_run_without_usable_baseline_scores_nothing_and_flags_everything(self):
        evidence = self.evidence()
        evidence.clean_fail = {"X"}
        result = evidence.build()
        run = result["runs"]["X"]
        self.assertEqual((self.value(result, "X", "K"), run["R"], run["F"], run["gates"]), ("0", 0, 16, False))
        self.assertTrue(result["valid"])

    def test_kills_from_a_suite_failing_clean_replay_do_not_enter_d(self):
        evidence = self.evidence(mutants=105, upstream_k=100, k={"W": 90, "Y": 90, "X": 90, "Z": 90})
        evidence.clean_fail = {"X"}  # X "kills" everything, including m100..m104
        for i in range(100, 105):
            evidence.adjudication.append({"question": "equivalence", "id": f"m{i:03d}", "verdict": "equivalent"})
        result = evidence.build()
        self.assertEqual(result["denominator"], 100)
        self.assertTrue(result["valid"])

    def test_survivors_need_equivalence_verdicts(self):
        result = self.evidence(upstream_k=95, k={"W": 90, "Y": 90, "X": 90, "Z": 90}).build()
        self.assertFalse(result["validity"]["V4"])
        self.assertEqual(result["decision"], "INCONCLUSIVE")

    def test_not_equivalent_enters_d_only_when_confirmed(self):
        evidence = self.evidence(upstream_k=98, k={"W": 90, "Y": 90, "X": 90, "Z": 90})
        evidence.adjudication += [
            {"question": "equivalence", "id": "m098", "verdict": "not-equivalent", "confirmed": True},
            {"question": "equivalence", "id": "m099", "verdict": "not-equivalent"}]
        result = evidence.build()
        self.assertEqual(result["denominator"], 99)
        self.assertEqual(result["mutation"]["not_equivalent_unconfirmed"], ["m099"])

    def test_mutation_results_must_match_selected_ids_exactly(self):
        evidence = self.evidence()
        result = evidence.build()
        rows = (evidence.root / "mutants.jsonl").read_text().splitlines()
        (evidence.root / "mutants.jsonl").write_text("\n".join(rows[:-1] + [rows[0]]) + "\n")
        result = compare.compare(evidence.root)
        self.assertFalse(result["validity"]["V4"])
        self.assertEqual(result["mutation"]["result_identity_gaps"]["missing"], ["m099"])
        self.assertEqual(result["mutation"]["result_identity_gaps"]["duplicated"], ["m000"])

    def test_every_usable_suite_needs_a_result_for_every_valid_mutant(self):
        evidence = self.evidence()
        evidence.missing_tests = {"Z"}  # Z passes clean replay but has no mutant or challenge results
        result = evidence.build()
        self.assertFalse(result["validity"]["V4"])
        self.assertIn("m000:Z", result["mutation"]["missing_suite_results"])

    def test_persistent_errors_above_five_percent_invalidate(self):
        evidence = self.evidence()
        evidence.extra_mutant_suites = {f"m{i:03d}": {"W": {"class": "error"}} for i in range(94, 100)}
        result = evidence.build()
        self.assertEqual(result["mutation"]["persistent_error_stop"], ["W"])
        self.assertFalse(result["valid"])

    def test_unresolved_failure_classification_makes_that_k_unresolved(self):
        evidence = self.evidence()
        evidence.extra_mutant_suites = {"m000": {"X": {"class": "adjudicate"}}}
        result = evidence.build()
        self.assertIsNone(result["runs"]["X"]["K"])
        self.assertEqual(result["metrics"]["K"]["result"], "unresolved")
        evidence.adjudication.append({"question": "failure-classification", "id": "m000", "suite": "X",
                                      "verdict": "behavioral"})
        evidence.write("ADJUDICATION.jsonl", evidence.adjudication)
        self.assertEqual(compare.compare(evidence.root)["metrics"]["K"]["result"], "A")

    def test_small_denominator_invalidates(self):
        self.assertFalse(self.evidence(mutants=99, upstream_k=99).build()["validity"]["V5"])

    def test_invalidating_incident_and_hash_mismatch(self):
        evidence = self.evidence()
        evidence.incidents = [{"condition": "S4"}]
        self.assertFalse(evidence.build()["validity"]["V2"])
        evidence = self.evidence()
        evidence.build()
        (evidence.root / "baseline-W.tar").write_bytes(b"changed")
        self.assertFalse(compare.compare(evidence.root)["validity"]["V1"])

    def test_scored_archives_must_be_the_frozen_ones(self):
        evidence = self.evidence()
        evidence.build()
        clean = json.loads((evidence.root / "clean.json").read_text())
        clean["deliverables"]["X"] = "0" * 64  # scored against some other archive
        (evidence.root / "clean.json").write_text(json.dumps(clean))
        result = compare.compare(evidence.root)
        self.assertFalse(result["validity"]["V1"])
        self.assertFalse(result["validity"]["details"]["scored_archives_match_freeze"]["X"])
        self.assertEqual(result["decision"], "INCONCLUSIVE")

    def test_labels_must_map_two_runs_per_condition(self):
        evidence = self.evidence()
        evidence.build()
        (evidence.root / "LABELS.json").write_text(json.dumps({"W": "A1", "X": "A2", "Y": "A1", "Z": "B2"}))
        self.assertFalse(compare.compare(evidence.root)["validity"]["V3"])

    def test_confirmed_reclassified_control_leaves_every_run(self):
        evidence = self.evidence(f={"W": 1, "Y": 1, "X": 1, "Z": 1}, controls=17)
        evidence.adjudication.append({"question": "control-dispute", "id": "N01", "verdict": "behavior-changed",
                                      "confirmed": True})
        result = evidence.build()
        self.assertEqual(result["challenges"]["reclassified_controls"], ["N01"])
        self.assertEqual(result["challenges"]["valid_controls"], 16)
        self.assertEqual([result["runs"][l]["F"] for l in compare.LABELS], [0, 0, 0, 0])

    def test_unconfirmed_behavior_changed_and_indeterminate_count_as_false_alarms(self):
        evidence = self.evidence(f={"W": 2})
        evidence.adjudication += [
            {"question": "control-dispute", "id": "N01", "verdict": "behavior-changed"},
            {"question": "control-dispute", "id": "N02", "verdict": "indeterminate"}]
        result = evidence.build()
        self.assertEqual(result["runs"]["W"]["F"], 2)
        self.assertEqual(result["challenges"]["reclassified_controls"], [])

    def test_untested_control_makes_that_runs_false_alarms_unresolved(self):
        evidence = self.evidence()
        evidence.build()
        rows = [json.loads(line) for line in (evidence.root / "challenges.jsonl").read_text().splitlines()]
        rows[-1]["suites"]["W"] = {"class": "timeout"}
        (evidence.root / "challenges.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        result = compare.compare(evidence.root)
        self.assertIsNone(result["runs"]["W"]["F"])
        self.assertEqual(result["metrics"]["F"]["result"], "unresolved")

    def test_too_few_valid_regressions_or_controls_are_unresolved(self):
        result = self.evidence(regressions=19).build()
        self.assertEqual(result["metrics"]["R"]["result"], "unresolved")
        result = self.evidence(controls=15).build()
        self.assertEqual(result["metrics"]["F"]["result"], "unresolved")

    def test_reliability_gates_each_count(self):
        for gate in ({"knockout": "insufficient-evidence"}, {"oracle": "self-referential"}, {"reproduce": False}):
            evidence = self.evidence()
            evidence.gate_fail = {"X": gate, "Z": gate}
            result = evidence.build()
            self.assertEqual(result["metrics"]["gates"]["result"], "A", gate)

    def test_claims_need_five_sampled(self):
        result = self.evidence(claims={"W": (4, 4)}).build()
        self.assertIsNone(result["runs"]["W"]["C"])
        self.assertEqual(result["metrics"]["C"]["result"], "unresolved")

    def test_duplicate_verdicts_are_refused_identical_or_conflicting(self):
        for extra, kind in (({"question": "oracle-review", "id": "W", "verdict": "none"}, "identical"),
                            ({"question": "oracle-review", "id": "W", "verdict": "reimplements"}, "conflicting")):
            evidence = self.evidence()
            evidence.adjudication.append(extra)
            with self.assertRaises(SystemExit) as refused:
                evidence.build()
            self.assertIn("duplicate verdict records refused: oracle-review W", str(refused.exception))
            self.assertIn(kind, str(refused.exception))

    def test_a_later_conflicting_verdict_cannot_override_an_earlier_one(self):
        evidence = self.evidence(upstream_k=99)
        evidence.adjudication += [{"question": "equivalence", "id": "m099", "verdict": "equivalent"},
                                  {"question": "equivalence", "id": "m099", "verdict": "not-equivalent",
                                   "confirmed": True}]
        with self.assertRaises(SystemExit) as refused:
            evidence.build()
        self.assertIn("equivalence m099", str(refused.exception))

    def test_same_id_under_another_suite_or_question_is_not_a_duplicate(self):
        evidence = self.evidence()
        evidence.extra_mutant_suites = {"m000": {"X": {"class": "adjudicate"}, "Z": {"class": "adjudicate"}}}
        evidence.adjudication += [
            {"question": "failure-classification", "id": "m000", "suite": "X", "verdict": "behavioral"},
            {"question": "failure-classification", "id": "m000", "suite": "Z", "verdict": "behavioral"},
            {"question": "equivalence", "id": "m000", "verdict": "equivalent"}]
        self.assertTrue(evidence.build()["valid"])

    def test_floor_blocks_go(self):
        result = self.evidence(r={"W": 13, "Y": 13, "X": 13, "Z": 13}).build()
        self.assertEqual(result["metrics"]["K"]["result"], "A")
        self.assertFalse(result["floor"]["met"])
        self.assertEqual(result["decision"], "INCONCLUSIVE")
        result = self.evidence(upstream_k=100, k={"W": 89, "Y": 89, "X": 80, "Z": 80}).build()
        self.assertFalse(result["floor"]["per_run"]["W"]["K"])  # 89 is below upstream 100 minus 10
        self.assertEqual(result["decision"], "INCONCLUSIVE")


if __name__ == "__main__":
    unittest.main()
