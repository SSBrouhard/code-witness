"""Regression tests for the v0.4 scorer and assessment (not engine tests)."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


score, assess = module("score"), module("assess")
SCOPE = {"canary/core.py", "canary/util.py"}


def failure(last_line):
    return f"def test():\n>       assert thing()\n\nE   something\n\n{last_line}"


class FailureClassification(unittest.TestCase):
    def test_assertion_and_pytest_outcomes_are_assertions(self):
        self.assertEqual(score.classify_failure(failure("tests/test_core.py:7: AssertionError"), SCOPE), "assertion")
        self.assertEqual(score.classify_failure(failure("tests/test_core.py:9: Failed"), SCOPE), "assertion")

    def test_exception_raised_in_target_scope_is_behavioral(self):
        self.assertEqual(score.classify_failure(failure("canary/util.py:2: TypeError"), SCOPE), "target-exception")

    def test_dependency_failures_are_not_kills(self):
        self.assertEqual(score.classify_failure(failure("canary/core.py:3: ModuleNotFoundError"), SCOPE), "dependency")
        self.assertEqual(score.classify_failure(failure("vendor/lib.py:3: ImportError"), SCOPE), "dependency")

    def test_exception_outside_target_scope_or_unparseable_needs_adjudication(self):
        self.assertEqual(score.classify_failure(failure("tests/conftest.py:12: OSError"), SCOPE), "unclassified")
        self.assertEqual(score.classify_failure(failure("/usr/lib/python3/socket.py:9: TimeoutError"), SCOPE),
                         "unclassified")
        self.assertEqual(score.classify_failure("", SCOPE), "unclassified")
        self.assertEqual(score.classify_failure("E   no location line", SCOPE), "unclassified")

    def test_junit_errors_are_collection_or_setup(self):
        root = ET.fromstring(
            '<testsuites><testsuite>'
            '<testcase classname="tests.test_core" name=""><error message="collection failure">x</error></testcase>'
            '<testcase classname="tests.test_io" name="test_a"><error message="failed on setup">y</error></testcase>'
            f'<testcase classname="tests.test_core" name="test_b"><failure message="m">{failure("canary/core.py:5: KeyError")}</failure></testcase>'
            '<testcase classname="tests.test_core" name="test_c"/>'
            '</testsuite></testsuites>')
        self.assertEqual(score.classify_junit(root, SCOPE), {"tests.test_core::": "collection",
                                                             "tests.test_io::test_a": "setup",
                                                             "tests.test_core::test_b": "target-exception"})

    def test_suite_class_precedence(self):
        self.assertEqual(score.classify(["t"], ["x"], ["e"], ["u"]), "behavioral")
        self.assertEqual(score.classify([], ["x"], ["e"], ["u"]), "adjudicate")
        self.assertEqual(score.classify([], ["x"], ["e"]), "timeout")
        self.assertEqual(score.classify([], [], ["e"]), "error")
        self.assertEqual(score.classify([], [], []), "survived")


@unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is not installed")
class UpstreamSuite(unittest.TestCase):
    def run_suite(self, edit=None, extra_test=None):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp, "tree")
            shutil.copytree(HERE / "canary" / "repo", tree)
            if edit:
                path, old, new = edit
                (tree / path).write_text((tree / path).read_text().replace(old, new))
            if extra_test:
                (tree / "tests" / "test_extra.py").write_text(extra_test)
            argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
            return score.upstream_suite(tree, argv, 120, SCOPE)

    def test_clean_suite_survives(self):
        self.assertEqual(self.run_suite()["class"], "survived")

    def test_assertion_and_target_exception_are_kills(self):
        self.assertEqual(self.run_suite(("canary/core.py", "points + bonus", "points - bonus"))["class"], "behavioral")
        result = self.run_suite(("canary/util.py", "min(value, high)", "min(value, None)"))
        self.assertEqual(result["class"], "behavioral")
        self.assertIn("target-exception", result["tests"].values())

    def test_dependency_failure_is_an_error_not_a_kill(self):
        result = self.run_suite(("canary/core.py", "from .util import", "import not_a_real_dependency\nfrom .util import"))
        self.assertEqual(result["class"], "error")
        self.assertEqual(result["behavioral"], [])

    def test_unrelated_exception_goes_to_adjudication(self):
        result = self.run_suite(extra_test="def test_env():\n    raise OSError('disk unavailable')\n")
        self.assertEqual(result["class"], "adjudicate")
        self.assertEqual(result["behavioral"], [])


class KnockoutStatus(unittest.TestCase):
    def test_only_observed_changes_can_pass(self):
        self.assertEqual(score.knockout_status(10, 8, 0, 0), "pass")
        self.assertEqual(score.knockout_status(10, 8, 1, 1), "pass")

    def test_failures_never_count_as_changes(self):
        self.assertEqual(score.knockout_status(10, 0, 0, 10), "insufficient-evidence")
        self.assertEqual(score.knockout_status(10, 7, 1, 0), "insufficient-evidence")
        self.assertEqual(score.knockout_status(10, 5, 1, 1), "fail")
        self.assertEqual(score.knockout_status(10, 0, 0, 0), "fail")
        self.assertEqual(score.knockout_status(0, 0, 0, 0), "insufficient-evidence")


def mutant(identifier, witness, upstream, validity="valid"):
    record = {"id": identifier, "validity": validity}
    if validity == "valid":
        record["suites"] = {"witness": {"class": witness}, "upstream": {"class": upstream}}
    return record


def suites(witness, upstream="survived"):
    return {"witness": {"class": witness}, "upstream": {"class": upstream}}


class Evidence:
    """A complete synthetic evidence directory whose outcome is Useful unless a test changes it."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.files = {}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            data = json.dumps({"verification": {"status": "INCONCLUSIVE"}}).encode()
            info = tarfile.TarInfo("pack/manifest.json")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        self.tar = buffer.getvalue()
        records = [mutant(f"m{i}", "behavioral", "behavioral") for i in range(6)]
        records += [mutant("m6", "survived", "behavioral"), mutant("m7", "behavioral", "survived"),
                    mutant("eq", "survived", "survived"), mutant("bad", None, None, "non-compiling")]
        regressions = [{"id": f"R{i:02d}", "suites": suites("behavioral" if i <= 8 else "survived")} for i in range(1, 11)]
        controls = [{"id": f"N{i:02d}", "suites": suites("survived")} for i in range(1, 11)]
        adjudication = [{"id": "eq", "question": "equivalence", "verdict": "equivalent"},
                        {"id": "adapter", "question": "adapter-reimplementation", "verdict": "none"}]
        adjudication += [{"id": c["id"], "question": "challenge-validity", "verdict": "valid"} for c in regressions + controls]
        self.files = {
            "PREFLIGHT.json": {"pass": True},
            "FREEZE.json": {"baseline_tar_sha256": hashlib.sha256(self.tar).hexdigest()},
            "PREREG.json": {"registered_at": "2026-11-01T09:00:00+00:00"},
            "clean.json": {"runs": [suites("survived")] * 3},
            "knockout.json": {"status": "pass", "counts": {"changed": 10, "unchanged": 0, "blocked": 0, "errored": 0}},
            "mutants.json": {"selected": [{"id": r["id"]} for r in records]},
            "mutants.jsonl": records, "challenges.jsonl": regressions + controls, "ADJUDICATION.jsonl": adjudication,
            "INTERVENTIONS.jsonl": [{"category": "login", "minutes": 5}, {"category": "approval", "minutes": 2}],
            "INCIDENTS.jsonl": [],
            "USAGE.jsonl": [{"role": "investigator", "start": "2026-11-01T10:00:00+00:00", "end": "2026-11-01T13:30:00+00:00",
                             "tokens": 4_000_000, "model_invocations": 40},
                            {"role": "challenger", "start": "2026-11-01T14:00:00+00:00", "end": "2026-11-01T15:00:00+00:00",
                             "tokens": 1_000_000, "model_invocations": 10}],
        }

    def assess(self):
        for name, value in self.files.items():
            path = self.dir / name
            if name.endswith(".jsonl"):
                path.write_text("".join(json.dumps(row) + "\n" for row in value))
            else:
                path.write_text(json.dumps(value))
        (self.dir / "baseline.tar").write_bytes(self.tar)
        out = self.dir / "assessment.json"
        assess.main(["assess", "--evidence", str(self.dir), "--out", str(out)])
        return json.loads(out.read_text())


class Assessment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Evidence(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def run_assess(self):
        with unittest.mock.patch("sys.stdout", io.StringIO()):
            return self.evidence.assess()

    def test_complete_evidence_is_useful(self):
        result = self.run_assess()
        self.assertEqual({k: v["status"] for k, v in result["criteria"].items()}, dict.fromkeys(
            ("C1", "C2", "C3", "C4", "C5", "C6", "C7"), "met"))
        self.assertEqual(result["outcome"], "Useful")
        c3 = result["criteria"]["C3"]
        self.assertEqual((c3["primary_denominator"], c3["kill_rate"]), (8, {"witness": "7/8", "upstream": "7/8"}))
        self.assertEqual((c3["equivalent"], c3["validity"]), (1, {"valid": 9, "non-compiling": 1}))

    def test_mutation_margin_is_ten_points_on_the_same_denominator(self):
        records = self.evidence.files["mutants.jsonl"]
        records[0]["suites"]["witness"]["class"] = "survived"  # witness 6/8 vs upstream 7/8: within 10 points? no, 12.5
        self.assertEqual(self.run_assess()["criteria"]["C3"]["status"], "not met")

    def test_uncertain_failures_are_not_kills_and_block_evaluation(self):
        records = self.evidence.files["mutants.jsonl"]
        records[6]["suites"]["upstream"]["class"] = "adjudicate"
        result = self.run_assess()["criteria"]["C3"]
        self.assertEqual(result["status"], "not evaluable")
        self.assertEqual(result["unresolved_failure_classifications"], ["m6:upstream"])
        self.evidence.files["ADJUDICATION.jsonl"] += [
            {"id": "m6", "question": "failure-classification", "suite": "upstream", "verdict": "not-behavioral"},
            {"id": "m6", "question": "equivalence", "verdict": "not-equivalent"}]
        result = self.run_assess()["criteria"]["C3"]
        self.assertEqual(result["kill_rate"], {"witness": "7/8", "upstream": "6/8"})
        self.assertTrue(result["exceeds_upstream"])

    def test_timeouts_and_errors_are_not_kills(self):
        records = self.evidence.files["mutants.jsonl"]
        records[7]["suites"]["witness"]["class"] = "timeout"
        self.evidence.files["ADJUDICATION.jsonl"].append({"id": "m7", "question": "equivalence", "verdict": "indeterminate"})
        result = self.run_assess()["criteria"]["C3"]
        self.assertEqual(result["primary_denominator"], 7)
        self.assertEqual(result["kill_rate"], {"witness": "6/7", "upstream": "7/7"})
        self.assertEqual(result["with_timeouts"]["witness"], "6/7")
        self.assertEqual(result["indeterminate"], 1)

    def test_unadjudicated_survivors_make_mutation_not_evaluable(self):
        self.evidence.files["ADJUDICATION.jsonl"] = [r for r in self.evidence.files["ADJUDICATION.jsonl"]
                                                     if r["question"] != "equivalence"]
        result = self.run_assess()
        self.assertEqual(result["criteria"]["C3"]["unadjudicated"], ["eq"])
        self.assertEqual(result["outcome"], "Partially useful")

    def test_mutation_results_must_match_selected_ids_exactly(self):
        records = self.evidence.files["mutants.jsonl"]
        records[1] = dict(records[0])  # same count as selected: m0 twice, m1 missing
        result = self.run_assess()["criteria"]["C3"]
        self.assertEqual(result["status"], "not evaluable")
        self.assertEqual(result["mutants_scored"], "10/10")
        self.assertEqual(result["result_identity_gaps"], {"missing": ["m1"], "duplicated": ["m0"], "unexpected": []})
        records[1] = mutant("stray", "behavioral", "behavioral")
        result = self.run_assess()["criteria"]["C3"]
        self.assertEqual((result["status"], result["result_identity_gaps"]["unexpected"]), ("not evaluable", ["stray"]))

    def test_resumed_scoring_with_exact_ids_still_evaluates(self):
        records = self.evidence.files["mutants.jsonl"]
        self.evidence.files["mutants.jsonl"] = records[5:] + records[:5]  # appended out of order after a resume
        self.assertEqual(self.run_assess()["criteria"]["C3"]["status"], "met")

    def test_duplicate_or_missing_challenge_results_are_not_evaluable(self):
        challenges = self.evidence.files["challenges.jsonl"]
        challenges.append(dict(challenges[0]))
        result = self.run_assess()["criteria"]
        self.assertEqual((result["C4"]["status"], result["C5"]["status"]), ("not evaluable", "not evaluable"))
        self.assertEqual(result["C4"]["result_identity_gaps"]["duplicated"], ["R01"])
        challenges.pop()
        challenges.pop()
        self.assertEqual(self.run_assess()["criteria"]["C5"]["result_identity_gaps"]["missing"], ["N10"])

    def test_controls_that_did_not_run_make_false_alarms_not_evaluable(self):
        challenges = self.evidence.files["challenges.jsonl"]
        for control, outcome in zip(challenges[10:13], ("timeout", "error", "adjudicate"), strict=True):
            control["suites"]["witness"]["class"] = outcome
        result = self.run_assess()["criteria"]["C5"]
        self.assertEqual(result["status"], "not evaluable")
        self.assertEqual(result["false_alarms"], [])
        self.assertEqual(result["untested_controls"], {"N01": "timeout", "N02": "error", "N03": "adjudicate"})

    def test_regressions_need_ten_valid_and_seven_detected(self):
        challenges = self.evidence.files["challenges.jsonl"]
        challenges[7]["suites"]["witness"]["class"] = "survived"
        challenges[6]["suites"]["witness"]["class"] = "error"
        self.assertEqual(self.run_assess()["criteria"]["C4"]["status"], "not met")
        self.evidence.files["ADJUDICATION.jsonl"] = [r for r in self.evidence.files["ADJUDICATION.jsonl"] if r["id"] != "R10"]
        self.assertEqual(self.run_assess()["criteria"]["C4"]["status"], "not evaluable")

    def test_false_alarms_need_adjudication_and_indeterminate_counts_against(self):
        challenges = self.evidence.files["challenges.jsonl"]
        challenges[10]["suites"]["witness"]["class"] = "behavioral"
        challenges[11]["suites"]["witness"]["class"] = "behavioral"
        self.assertEqual(self.run_assess()["criteria"]["C5"]["status"], "not evaluable")
        self.evidence.files["ADJUDICATION.jsonl"] += [{"id": "N01", "question": "control-dispute", "verdict": "false-alarm"},
                                                     {"id": "N02", "question": "control-dispute", "verdict": "indeterminate"}]
        self.assertEqual(self.run_assess()["criteria"]["C5"]["status"], "not met")
        self.evidence.files["ADJUDICATION.jsonl"][-1]["verdict"] = "behavior-changed"
        result = self.run_assess()["criteria"]["C5"]
        self.assertEqual((result["status"], result["valid_controls"]), ("not evaluable", 9))

    def test_knockout_insufficient_evidence_is_not_evaluable(self):
        self.evidence.files["knockout.json"] = {"status": "insufficient-evidence", "counts": {}}
        result = self.run_assess()
        self.assertEqual(result["criteria"]["C2"]["status"], "not evaluable")
        self.assertEqual(result["outcome"], "Not demonstrated")

    def test_integrity_requires_preflight_matching_freeze_and_no_invalidating_stop(self):
        self.evidence.files["FREEZE.json"]["baseline_tar_sha256"] = "0" * 64
        self.assertEqual(self.run_assess()["criteria"]["C1"]["status"], "not met")
        self.evidence = Evidence(self.tmp.name)
        self.evidence.files["INCIDENTS.jsonl"] = [{"condition": "S4"}]
        self.assertEqual(self.run_assess()["outcome"], "Not demonstrated")

    def test_supervision_limits(self):
        self.evidence.files["INTERVENTIONS.jsonl"].append({"category": "code-explanation", "minutes": 1})
        self.assertEqual(self.run_assess()["criteria"]["C6"]["status"], "not met")
        self.evidence.files["INTERVENTIONS.jsonl"] = [{"category": "approval", "minutes": 46}]
        self.assertEqual(self.run_assess()["criteria"]["C6"]["status"], "not met")

    def test_cost_limits_and_unmeasured_tokens(self):
        usage = self.evidence.files["USAGE.jsonl"]
        usage[0]["end"] = "2026-11-01T14:01:00+00:00"
        self.assertEqual(self.run_assess()["criteria"]["C7"]["status"], "not met")
        usage[0]["end"] = "2026-11-01T13:30:00+00:00"
        usage[1]["tokens"] = None
        result = self.run_assess()["criteria"]["C7"]
        self.assertEqual((result["status"], result["tokens"]), ("met", "unmeasured: challenger"))
        usage[1]["incremental_charge_usd"] = 5
        self.assertEqual(self.run_assess()["criteria"]["C7"]["unauthorized_charges"], ["challenger"])


class UsageRows(unittest.TestCase):
    def row(self, runtime, lines):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as log:
            log.write("\n".join(json.dumps(line) for line in lines) + "\n")
        self.addCleanup(Path(log.name).unlink)
        stdout = io.StringIO()
        with unittest.mock.patch("sys.stdout", stdout):
            assess.main(["usage", "--runtime", runtime, "--role", "investigator", "--log", log.name,
                         "--start", "2026-11-01T10:00:00+00:00", "--end", "2026-11-01T11:00:00+00:00"])
        return json.loads(stdout.getvalue())

    def test_codex_turn_usage(self):
        row = self.row("codex", [{"type": "thread.started"},
                                 {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 7}},
                                 {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 3}}])
        self.assertEqual((row["tokens"], row["model_invocations"], row["incremental_charge_usd"]), (160, 2, 0))

    def test_claude_result_usage_estimate_is_not_a_charge(self):
        row = self.row("claude", [{"type": "result", "num_turns": 3, "total_cost_usd": 0.5,
                                   "usage": {"input_tokens": 2, "cache_creation_input_tokens": 10,
                                             "cache_read_input_tokens": 100, "output_tokens": 8}}])
        self.assertEqual((row["tokens"], row["model_invocations"]), (120, 3))
        self.assertEqual((row["estimated_api_equivalent_usd_not_a_charge"], row["incremental_charge_usd"]), (0.5, 0))

    def test_missing_usage_is_unmeasured(self):
        self.assertIsNone(self.row("codex", [{"type": "thread.started"}])["tokens"])


if __name__ == "__main__":
    unittest.main()
