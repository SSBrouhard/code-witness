"""Tests for the v0.5 scorer (not engine tests). The full canary flow is preflight_canary.py."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("score", HERE / "score.py")
score = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score)

SCOPE = {"canary/core.py", "canary/util.py"}
CHAR_ARGV = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "_characterization/pytest.ini",
             "--rootdir=_characterization", "--confcutdir=_characterization", "_characterization/tests"]
HAS_COSMIC = Path(score.COSMIC).is_file()


def failure(last_line):
    return f"def test():\n>       assert thing()\n\nE   something\n\n{last_line}"


def commit(path):
    for command in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", "canary"]):
        subprocess.run(["git", "-C", str(path), *command], check=True, capture_output=True)


def git_tree(path, suite=None, decoys=False):
    shutil.copytree(HERE / "canary/repo", path)
    if decoys:
        shutil.copy(HERE / "canary/decoys/conftest.py", path / "conftest.py")
        shutil.copy(HERE / "canary/decoys/pytest.ini", path / "pytest.ini")
    commit(path)
    (path / ".git/info/exclude").write_text("_characterization/\n_witness/\n")
    if suite:
        shutil.copytree(HERE / "canary/characterization" / suite, path / "_characterization")
    return path


def call(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        score.main([str(a) for a in argv])
    return json.loads(out.getvalue()) if out.getvalue().strip() else None


class Classification(unittest.TestCase):
    def test_failure_classes_match_v04(self):
        self.assertEqual(score.classify_failure(failure("tests/test_core.py:7: AssertionError"), SCOPE), "assertion")
        self.assertEqual(score.classify_failure(failure("canary/util.py:2: TypeError"), SCOPE), "target-exception")
        self.assertEqual(score.classify_failure(failure("canary/core.py:3: ModuleNotFoundError"), SCOPE), "dependency")
        self.assertEqual(score.classify_failure(failure("_characterization/tests/t.py:3: KeyError"), SCOPE),
                         "unclassified")

    def test_knockout_exception_is_a_target_exception_no_builtin_catches(self):
        self.assertEqual(score.classify_failure(failure("canary/core.py:5: canary.core.CodeWitnessKnockout"), SCOPE),
                         "target-exception")
        namespace = {}
        with self.assertRaises(Exception) as caught:
            exec(score.KNOCKOUT, namespace)
        self.assertEqual(type(caught.exception).__name__, "CodeWitnessKnockout")
        self.assertNotIsInstance(caught.exception, (RuntimeError, ValueError, TypeError, KeyError, LookupError))

    def test_junit_cases_record_every_item(self):
        root = ET.fromstring(
            '<testsuites><testsuite>'
            '<testcase classname="t" name="passes"/>'
            '<testcase classname="t" name="skips"><skipped/></testcase>'
            f'<testcase classname="t" name="fails"><failure>{failure("canary/core.py:5: KeyError")} code-witness knockout</failure></testcase>'
            '<testcase classname="t" name=""><error message="collection failure">x</error></testcase>'
            '</testsuite></testsuites>')
        cases = score.junit_cases(root, SCOPE)
        self.assertEqual({k: v["class"] for k, v in cases.items()},
                         {"t::passes": "passed", "t::skips": "skipped", "t::fails": "unclassified", "t::": "collection"})


class KnockoutUnits(unittest.TestCase):
    clean = {"t::a": {"class": "passed"}, "t::b": {"class": "passed"}, "t::c": {"class": "passed"},
             "t::d": {"class": "passed"}, "t::skip": {"class": "skipped"}}

    def test_units_are_items_that_passed_on_the_unchanged_tree(self):
        knocked = {"t::a": {"class": "target-exception", "knockout": True}, "t::b": {"class": "assertion"},
                   "t::c": {"class": "passed"}, "t::skip": {"class": "skipped"}}
        units, cases, reached = score.pytest_knockout_cases(self.clean, knocked, False)
        self.assertEqual(units, ["t::a", "t::b", "t::c", "t::d"])
        self.assertEqual(cases, {"changed": ["t::a", "t::b"], "unchanged": ["t::c"], "blocked": [],
                                 "errored": ["t::d"]})  # missing under knockout: errored, never changed
        self.assertEqual(reached, ["t::a"])

    def test_collection_and_unclassified_failures_are_errored(self):
        knocked = {name: {"class": kind} for name, kind in zip(self.clean, ("collection", "setup", "unclassified",
                                                                           "dependency"))}
        _, cases, _ = score.pytest_knockout_cases(self.clean, knocked, False)
        self.assertEqual(len(cases["errored"]), 4)
        self.assertEqual(score.knockout_status(4, 0, 0, 4), "insufficient-evidence")

    def test_timeout_blocks_every_unit(self):
        _, cases, _ = score.pytest_knockout_cases(self.clean, {}, True)
        self.assertEqual(len(cases["blocked"]), 4)

    def test_status_thresholds(self):
        self.assertEqual(score.knockout_status(10, 8, 0, 0), "pass")
        self.assertEqual(score.knockout_status(10, 7, 1, 0), "insufficient-evidence")
        self.assertEqual(score.knockout_status(10, 7, 0, 0), "fail")
        self.assertEqual(score.knockout_status(0, 0, 0, 0), "insufficient-evidence")


class Suites(unittest.TestCase):
    def write(self, suites):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name, "suites.json")
        path.write_text(json.dumps(suites))
        return path

    def test_declarations_resolve_relative_to_the_file(self):
        path = self.write([{"name": "W", "kind": "witness", "tree": "trees/W"},
                           {"name": "X", "kind": "pytest", "tree": "trees/X", "argv": CHAR_ARGV}])
        w, x = score.load_suites(path)
        root = path.resolve().parent
        self.assertEqual(w["pack"], root / "trees/W/_witness/pack")
        self.assertEqual((x["tree"], x["timeout"], x["env"]), (root / "trees/X", 300.0, {}))

    def test_invalid_declarations_are_refused(self):
        bad = ([{"name": "W", "kind": "witness", "tree": "t"}, {"name": "W", "kind": "witness", "tree": "u"}],
               [{"name": "W", "kind": "witness", "tree": "t"}, {"name": "X", "kind": "witness", "tree": "t"}],
               [{"name": "X", "kind": "pytest", "tree": "t", "argv": ["python", "tests"]}],
               [{"name": "X", "kind": "shell", "tree": "t"}], [])
        for suites in bad:
            with self.assertRaises(SystemExit, msg=suites):
                score.load_suites(self.write(suites))


@unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is not installed")
class PytestSuites(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def suites(self, *entries):
        path = self.tmp / "suites.json"
        path.write_text(json.dumps(list(entries)))
        return path

    def char(self, name, suite, decoys=True):
        git_tree(self.tmp / name, suite, decoys)
        return {"name": name, "kind": "pytest", "tree": name, "argv": CHAR_ARGV, "env": {"PYTHONPATH": "."},
                "timeout": 30}

    def test_fixed_command_ignores_upstream_configuration(self):
        suites = self.suites(self.char("B", "honest"))
        result = call("check", "--suites", suites, "--scope", *SCOPE)["runs"][0]["B"]
        self.assertEqual((result["class"], result["items"]), ("survived", 12))

    def test_mutated_source_is_a_kill_and_trees_must_match(self):
        suites = self.suites(self.char("B", "honest"), self.char("B2", "honest"))
        for name in ("B", "B2"):
            core = self.tmp / name / "canary/core.py"
            core.write_text(core.read_text().replace("total >= 90", "total >= 80"))
        result = call("check", "--suites", suites, "--scope", *SCOPE)["runs"][0]
        self.assertEqual((result["B"]["class"], result["B2"]["class"]), ("behavioral", "behavioral"))
        subprocess.run(["git", "-C", str(self.tmp / "B2"), "checkout", "-q", "--", "."], check=True)
        with self.assertRaises(SystemExit) as stopped:
            call("check", "--suites", suites, "--scope", *SCOPE)
        self.assertIn("S5: suite trees differ", str(stopped.exception))

    def test_pytest_knockout_detects_a_dishonest_suite(self):
        suites = self.suites(self.char("B", "honest"), self.char("D", "dishonest"))
        self.assertEqual(call("knockout", "--suites", suites, "--scope", *SCOPE, "--suite", "B")["status"], "pass")
        dishonest = call("knockout", "--suites", suites, "--scope", *SCOPE, "--suite", "D")
        self.assertEqual((dishonest["status"], dishonest["counts"]["unchanged"]), ("fail", 2))
        status = subprocess.run(["git", "-C", str(self.tmp / "B"), "status", "--porcelain"], capture_output=True,
                                text=True).stdout
        self.assertEqual(status, "")

    def test_reproduce_runs_in_reverse_order_and_hash_seeds(self):
        suites = self.suites(self.char("O", "order_dependent"))
        stripped = self.tmp / "stripped"
        shutil.copytree(HERE / "canary/repo", stripped, ignore=shutil.ignore_patterns("tests"))
        commit(stripped)
        result = call("reproduce", "--suites", suites, "--scope", *SCOPE, "--suite", "O", "--repo", stripped,
                      "--deliverable", self.tmp / "O/_characterization", "--python", sys.executable,
                      "--workdir", self.tmp / "replay")
        self.assertEqual({k: v["pass"] for k, v in result["variants"].items()},
                         {"hashseed-0": True, "hashseed-1": True, "reverse-order": False})
        self.assertFalse(result["pass"])
        self.assertNotEqual(Path(result["path"]).resolve(), (self.tmp / "O").resolve())


@unittest.skipUnless(HAS_COSMIC and importlib.util.find_spec("pytest"), "cosmic-ray is not installed")
class MutantsAcrossTrees(unittest.TestCase):
    def test_each_mutant_reaches_every_tree_identically_and_trees_are_restored(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for name in ("B", "B2"):
            git_tree(tmp / name, "honest", decoys=True)
        git_tree(tmp / "upstream")
        (tmp / "suites.json").write_text(json.dumps([
            {"name": n, "kind": "pytest", "tree": n, "argv": CHAR_ARGV, "env": {"PYTHONPATH": "."}, "timeout": 10}
            for n in ("B", "B2")] + [{"name": "upstream", "kind": "pytest", "tree": "upstream", "timeout": 10,
                                      "argv": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]}]))
        call("mutants", "--tree", tmp / "upstream", "--scope", *SCOPE, "--seed", "1", "--cap", "6",
             "--out", tmp / "mutants.json")
        call("run", "--suites", tmp / "suites.json", "--scope", *SCOPE, "--mutants", tmp / "mutants.json",
             "--import-module", "canary.core", "--out", tmp / "results.jsonl")
        rows = [json.loads(line) for line in (tmp / "results.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 6)
        for row in rows:
            if row["validity"] == "valid":
                self.assertEqual(set(row["suites"]), {"B", "B2", "upstream"})
                self.assertEqual(row["suites"]["B"]["class"], row["suites"]["B2"]["class"])
        for name in ("B", "B2", "upstream"):
            self.assertEqual(subprocess.run(["git", "-C", str(tmp / name), "status", "--porcelain"],
                                            capture_output=True, text=True).stdout, "")


if __name__ == "__main__":
    unittest.main()
