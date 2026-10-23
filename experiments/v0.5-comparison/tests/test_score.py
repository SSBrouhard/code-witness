"""Tests for the v0.5 scorer (not engine tests). The full canary flow is preflight_canary.py."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
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


def sealed(root, entries):
    """Seal each deliverable into an archive, as the coordinator's freeze does, and declare it."""
    (root / "sealed").mkdir(exist_ok=True)
    for entry in entries:
        name = entry.get("deliverable") or ("_witness" if entry["kind"] == "witness" else None)
        if name is None:
            continue
        source, archive = root / entry["tree"] / name, root / "sealed" / f"{entry['name']}.tar"
        with tarfile.open(archive, "w") as tar:
            for path in sorted(source.rglob("*")):
                if "__pycache__" not in path.parts:
                    tar.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
        entry.update(archive=f"sealed/{entry['name']}.tar", archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    return list(entries)


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


class CollectionClassification(unittest.TestCase):
    """Amendment B2: a collection failure is a detection only when mutated target code demonstrably caused it."""

    def junit(self, last_line, module="test.test_core", message="collection failure"):
        root = ET.fromstring(f'<testsuites><testsuite><testcase classname="" name="{module}">'
                             f'<error message="{message}">{failure(last_line)}</error></testcase></testsuite></testsuites>')
        return score.junit_cases(root, SCOPE)

    def classify(self, last_line, clean=frozenset({"test.test_core"}), rerun_line=None, module="test.test_core"):
        cases = self.junit(last_line, module)
        again = self.junit(rerun_line or last_line, module)
        calls = []
        score.classify_collection(cases, set(clean), lambda: calls.append(1) or again)
        return next(iter(cases.values()))["class"], len(calls)

    def test_reproducible_target_scope_exception_after_clean_collection_is_a_detection(self):
        self.assertEqual(self.classify("canary/core.py:5: ValueError"), ("collection-target", 1))
        self.assertIn("collection-target", score.KILL_CLASSES)

    def test_dependency_failures_stay_errors_even_in_target_scope(self):
        self.assertEqual(self.classify("canary/core.py:1: ModuleNotFoundError"), ("dependency", 0))
        self.assertEqual(self.classify("vendor/x.py:3: ImportError"), ("dependency", 0))

    def test_module_that_did_not_collect_cleanly_on_unchanged_source_stays_an_error(self):
        self.assertEqual(self.classify("canary/core.py:5: ValueError", clean=set()), ("collection", 0))
        self.assertEqual(self.classify("canary/core.py:5: ValueError", clean={"test.test_other"}), ("collection", 0))

    def test_unparseable_traceback_stays_an_error(self):
        cases = score.junit_cases(ET.fromstring(
            '<testsuites><testsuite><testcase classname="" name="test.test_core">'
            '<error message="collection failure">ERROR: file or directory not found: tests</error>'
            '</testcase></testsuite></testsuites>'), SCOPE)
        score.classify_collection(cases, {"test.test_core"}, lambda: self.fail("no rerun for an unparseable error"))
        self.assertEqual(next(iter(cases.values()))["class"], "collection")

    def test_exception_raised_outside_target_scope_goes_to_adjudication(self):
        for location in ("tests/test_core.py:7: RuntimeError", "/usr/lib/python3.13/json/decoder.py:9: JSONDecodeError",
                         "/venv/lib/python3.13/site-packages/_pytest/python.py:5: TypeError"):
            self.assertEqual(self.classify(location), ("unclassified", 0), location)

    def test_target_exception_that_does_not_reproduce_goes_to_adjudication(self):
        self.assertEqual(self.classify("canary/core.py:5: ValueError", rerun_line="canary/core.py:9: ValueError"),
                         ("unclassified", 1))
        self.assertEqual(self.classify("canary/core.py:5: ValueError", rerun_line="tests/test_core.py:2: KeyError"),
                         ("unclassified", 1))

    def test_setup_errors_and_failures_are_unchanged(self):
        root = ET.fromstring('<testsuites><testsuite><testcase classname="t" name="a">'
                             '<error message="failed on setup">canary/core.py:5: ValueError</error></testcase>'
                             '</testsuite></testsuites>')
        self.assertEqual(score.junit_cases(root, SCOPE)["t::a"]["class"], "setup")

    def test_collection_target_counts_as_behavioral_in_a_suite_result(self):
        cases = self.junit("canary/core.py:5: ValueError")
        score.classify_collection(cases, {"test.test_core"}, lambda: self.junit("canary/core.py:5: ValueError"))
        tests = {t: c["class"] for t, c in cases.items()}
        self.assertEqual(score.classify(sorted(t for t, c in tests.items() if c in score.KILL_CLASSES), [], []),
                         "behavioral")

    def test_knockout_maps_a_module_collection_failure_to_its_units(self):
        clean = {"test.test_core::test_a": {"class": "passed"}, "test.test_core.TestX::test_b": {"class": "passed"},
                 "test.test_other::test_c": {"class": "passed"}}
        knocked = {"::test.test_core": {"class": "collection-target", "module": "test.test_core", "knockout": True},
                   "test.test_other::test_c": {"class": "passed"}}
        _, cases, reached = score.pytest_knockout_cases(clean, knocked, False)
        self.assertEqual(cases["changed"], ["test.test_core.TestX::test_b", "test.test_core::test_a"])
        self.assertEqual(cases["unchanged"], ["test.test_other::test_c"])
        self.assertEqual(len(reached), 2)
        knocked["::test.test_core"]["class"] = "unclassified"
        _, cases, _ = score.pytest_knockout_cases(clean, knocked, False)
        self.assertEqual(len(cases["errored"]), 2)


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
        path = self.write([{"name": "W", "kind": "witness", "tree": "trees/W", "archive": "sealed/W.tar",
                            "archive_sha256": "0" * 64},
                           {"name": "X", "kind": "pytest", "tree": "trees/X", "argv": CHAR_ARGV}])
        w, x = score.load_suites(path)
        root = path.resolve().parent
        self.assertEqual(w["pack"], root / "trees/W/_witness/pack")
        self.assertEqual(w["archive"], root / "sealed/W.tar")
        self.assertIsNone(x["deliverable"])
        self.assertEqual((x["tree"], x["timeout"], x["env"]), (root / "trees/X", 300.0, {}))

    def test_invalid_declarations_are_refused(self):
        bad = ([{"name": "W", "kind": "witness", "tree": "t"}, {"name": "W", "kind": "witness", "tree": "u"}],
               [{"name": "W", "kind": "witness", "tree": "t"}, {"name": "X", "kind": "witness", "tree": "t"}],
               [{"name": "X", "kind": "pytest", "tree": "t", "argv": ["python", "tests"]}],
               [{"name": "X", "kind": "shell", "tree": "t"}], [],
               [{"name": "W", "kind": "witness", "tree": "t"}],  # a deliverable without its sealed archive
               [{"name": "X", "kind": "pytest", "tree": "t", "argv": CHAR_ARGV, "deliverable": "_characterization",
                 "archive": "sealed/X.tar"}])
        for suites in bad:
            with self.assertRaises(SystemExit, msg=suites):
                score.load_suites(self.write(suites))


@unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest is not installed")
class PytestSuites(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def suites(self, *entries, name="suites.json"):
        path = self.tmp / name
        path.write_text(json.dumps(sealed(self.tmp, entries)))
        return path

    def char(self, name, suite, decoys=True):
        git_tree(self.tmp / name, suite, decoys)
        return {"name": name, "kind": "pytest", "tree": name, "argv": CHAR_ARGV, "env": {"PYTHONPATH": "."},
                "deliverable": "_characterization", "timeout": 30}

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

    def tampering(self, name):
        entry = self.char(name, "honest")
        (self.tmp / name / "_characterization/tests/test_tamper.py").write_text(
            "from pathlib import Path\n\n\ndef test_writes_into_its_own_deliverable():\n"
            "    Path(__file__).parent.with_name('written.txt').write_text('changed')\n")
        return entry

    def test_a_suite_changing_its_frozen_deliverable_stops_scoring(self):
        suites = self.suites(self.tampering("T"))
        with self.assertRaises(SystemExit) as stopped:
            call("check", "--suites", suites, "--scope", *SCOPE)
        self.assertIn("S5: frozen deliverable changed during scoring (check run 1)", str(stopped.exception))
        self.assertIn("written.txt", str(stopped.exception))

    def test_a_changed_deliverable_records_no_challenge_result(self):
        suites = self.suites(self.tampering("T"))
        patches = self.tmp / "patches"
        patches.mkdir()
        util = self.tmp / "T/canary/util.py"
        util.write_text(util.read_text().replace("min(value, high))", "min(value, high))  # same behavior"))
        (patches / "N01.patch").write_text(subprocess.run(["git", "-C", str(self.tmp / "T"), "diff"],
                                                          capture_output=True, text=True, check=True).stdout)
        subprocess.run(["git", "-C", str(self.tmp / "T"), "checkout", "-q", "--", "."], check=True)
        out = self.tmp / "challenges.jsonl"
        with self.assertRaises(SystemExit) as stopped:
            call("challenges", "--suites", suites, "--scope", *SCOPE, "--dir", patches, "--ids", "N01", "--out", out)
        self.assertIn("(N01)", str(stopped.exception))
        self.assertEqual(out.read_text(), "")

    def test_bytecode_written_into_a_deliverable_does_not_stop_scoring(self):
        entry = self.char("P", "honest")
        (self.tmp / "P/_characterization/tests/test_cache.py").write_text(
            "from pathlib import Path\n\n\ndef test_writes_a_cache_file():\n"
            "    cache = Path(__file__).parent / '__pycache__'\n    cache.mkdir(exist_ok=True)\n"
            "    (cache / 'x.cpython-313.pyc').write_bytes(b'cache')\n")
        result = call("check", "--suites", self.suites(entry), "--scope", *SCOPE)
        self.assertEqual(result["runs"][0]["P"]["class"], "survived")
        self.assertIn("P", result["deliverables"])

    def test_tampering_between_commands_and_in_a_shard_copy_is_refused(self):
        suites = self.suites(self.char("B", "honest"))
        self.assertEqual(call("check", "--suites", suites, "--scope", *SCOPE)["deliverables"],
                         {"B": json.loads(suites.read_text())[0]["archive_sha256"]})
        # A parallel shard: a copy of the tree with an edited expectation, declared against the same archive.
        shutil.copytree(self.tmp / "B", self.tmp / "B-shard")
        expected = self.tmp / "B-shard/_characterization/tests/expected.json"
        expected.write_text(expected.read_text().replace('"high"', '"mid"', 1))
        shard = self.tmp / "shard.json"
        shard.write_text(json.dumps([{**json.loads(suites.read_text())[0], "tree": "B-shard"}]))
        for declared in (shard, suites):
            if declared is suites:  # The same edit to the original tree after a completed command.
                shutil.copy(expected, self.tmp / "B/_characterization/tests/expected.json")
            with self.assertRaises(SystemExit) as stopped:
                call("check", "--suites", declared, "--scope", *SCOPE)
            self.assertIn("working copy differs from the sealed archive", str(stopped.exception))
            self.assertIn("tests/expected.json", str(stopped.exception))

    def test_collection_failures_are_classified_by_where_they_were_raised(self):
        suites = self.suites(self.char("T", "collection_time"), self.char("O", "collection_outside"))
        patches = self.tmp / "patches"
        patches.mkdir()
        core = self.tmp / "T/canary/core.py"
        original = core.read_text()
        # R01: score() raises for positive points, so code under test raises while pytest collects.
        # R02: label(90) only returns a different value, so the collection-outside test file raises itself.
        for cid, edit in (("R01", ("points < 0", "points > 0")), ("R02", ("total >= 90", "total >= 95"))):
            core.write_text(original.replace(*edit))
            (patches / f"{cid}.patch").write_text(subprocess.run(["git", "-C", str(self.tmp / "T"), "diff"],
                                                                 capture_output=True, text=True, check=True).stdout)
            core.write_text(original)
        out = self.tmp / "challenges.jsonl"
        call("challenges", "--suites", suites, "--scope", *SCOPE, "--dir", patches, "--ids", "R01", "R02", "--out", out)
        r01, r02 = [json.loads(line)["suites"] for line in out.read_text().splitlines()]
        for name in ("T", "O"):  # raised by target code during collection: a detection in both suites
            self.assertEqual((r01[name]["class"], set(r01[name]["tests"].values())), ("behavioral", {"collection-target"}))
        self.assertEqual((r02["T"]["class"], set(r02["T"]["tests"].values())), ("behavioral", {"assertion"}))
        # raised by the test file itself: adjudication, never a detection on its own
        self.assertEqual((r02["O"]["class"], set(r02["O"]["tests"].values())), ("adjudicate", {"unclassified"}))

    def test_collection_failure_on_unchanged_source_is_never_a_detection(self):
        entry = self.char("U", "collection_time")
        core = self.tmp / "U/canary/core.py"
        core.write_text(core.read_text().replace("points < 0", "points > 0"))
        subprocess.run(["git", "-C", str(self.tmp / "U"), "-c", "user.name=t", "-c", "user.email=t@localhost",
                        "commit", "-qam", "broken at base"], check=True, capture_output=True)
        result = call("check", "--suites", self.suites(entry), "--scope", *SCOPE)
        self.assertEqual(result["collected_modules"], {"U": []})
        self.assertEqual(result["runs"][0]["U"]["class"], "error")

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


class DeliverableFingerprint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.deliverable = self.tmp / "tree/_witness"
        (self.deliverable / "pack/tests").mkdir(parents=True)
        (self.deliverable / "pack/tests/observations.json").write_text("{}")
        (self.deliverable / "BASELINE.md").write_text("baseline")
        (self.tmp / "suites.json").write_text(json.dumps(sealed(self.tmp, [{"name": "W", "kind": "witness",
                                                                             "tree": "tree"}])))
        self.suites = score.load_suites(self.tmp / "suites.json")
        self.frozen = score.snapshot(self.suites)

    def test_witness_deliverable_defaults_to_witness_directory(self):
        self.assertEqual(self.suites[0]["deliverable"], self.deliverable.resolve())
        self.assertEqual(set(self.frozen["W"]), {"BASELINE.md", "pack/tests/observations.json"})

    def test_python_caches_are_incidental(self):
        for cache in ("pack/__pycache__/x.cpython-313.pyc", "slice/legacy/batch.pyc", ".pytest_cache/v/cache/x"):
            (self.deliverable / cache).parent.mkdir(parents=True, exist_ok=True)
            (self.deliverable / cache).write_text("cache")
        score.require_frozen(self.suites, self.frozen, "caches")

    def test_working_copy_must_match_the_sealed_archive_at_every_start(self):
        """Tampering between commands is caught when the next command starts, not only during one."""
        (self.deliverable / "pack/tests/observations.json").write_text('{"rows": []}')
        with self.assertRaises(SystemExit) as stopped:
            score.snapshot(score.load_suites(self.tmp / "suites.json"))
        self.assertIn("W: working copy differs from the sealed archive", str(stopped.exception))
        self.assertIn("pack/tests/observations.json", str(stopped.exception))

    def test_archive_must_match_its_sealed_hash(self):
        with tarfile.open(self.tmp / "sealed/W.tar", "a") as tar:
            info = tarfile.TarInfo("EXTRA.md")
            tar.addfile(info, io.BytesIO(b""))
        with self.assertRaises(SystemExit) as stopped:
            score.snapshot(score.load_suites(self.tmp / "suites.json"))
        self.assertIn("archive does not match its sealed hash", str(stopped.exception))

    def test_changed_added_and_removed_files_stop_scoring(self):
        edits = (lambda: (self.deliverable / "pack/tests/observations.json").write_text('{"rows": []}'),
                 lambda: (self.deliverable / "NEW.md").write_text("added"),
                 lambda: (self.deliverable / "BASELINE.md").unlink(),
                 lambda: shutil.rmtree(self.deliverable))
        names = ("pack/tests/observations.json", "NEW.md", "BASELINE.md", "BASELINE.md")
        for edit, name in zip(edits, names):
            self.setUp()
            edit()
            with self.assertRaises(SystemExit) as stopped:
                score.require_frozen(self.suites, self.frozen, "edit")
            self.assertIn("S5: frozen deliverable changed", str(stopped.exception))
            self.assertIn(name, str(stopped.exception))


@unittest.skipUnless(HAS_COSMIC and importlib.util.find_spec("pytest"), "cosmic-ray is not installed")
class MutantsAcrossTrees(unittest.TestCase):
    def test_each_mutant_reaches_every_tree_identically_and_trees_are_restored(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for name in ("B", "B2"):
            git_tree(tmp / name, "honest", decoys=True)
        git_tree(tmp / "upstream")
        (tmp / "suites.json").write_text(json.dumps(sealed(tmp, [
            {"name": n, "kind": "pytest", "tree": n, "argv": CHAR_ARGV, "env": {"PYTHONPATH": "."}, "timeout": 10,
             "deliverable": "_characterization"}
            for n in ("B", "B2")] + [{"name": "upstream", "kind": "pytest", "tree": "upstream", "timeout": 10,
                                      "argv": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]}])))
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
