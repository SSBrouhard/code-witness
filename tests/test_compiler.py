import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from debt_compiler import extract
from debt_compiler.archaeology import context
from debt_compiler.flags import scan
from debt_compiler.graph import Graph
from debt_compiler.harness import run_case
from debt_compiler.invariants import check
from debt_compiler.parse import discover, parse

ROOT = Path(__file__).resolve().parents[1]


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def sample(self):
        target = self.root / "sample"
        shutil.copytree(ROOT / "sample", target, ignore=shutil.ignore_patterns("__pycache__"))
        return target

    def test_full_cli_and_generated_tests(self):
        out = self.root / "pack"
        completed = subprocess.run([sys.executable, "-c", "from debt_compiler.cli import main; raise SystemExit(main())",
                                    "extract", str(ROOT / "sample/legacy"), "--out", str(out)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for name in ("graph.md", "annotations.json", "invariants.json", "tests", "dual_run.md", "risk.md", "manifest.json"):
            self.assertTrue((out / name).exists(), name)
        observations = json.loads((out / "tests/observations.json").read_text())
        rows = {r["name"]: r for r in observations["rows"]}
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(r["equal"] for r in rows.values()))
        self.assertEqual(rows["round-half-up"]["legacy"]["result"]["balance"], "11.00")
        self.assertEqual(rows["overdraft-enabled"]["legacy"]["result"]["balance"], "-1.00")
        self.assertEqual(rows["retry-recovers"]["legacy"]["sleeps"], [50])
        self.assertEqual(rows["retry-exhausted"]["legacy"]["sleeps"], [50, 50])
        self.assertEqual(rows["unknown-operation"]["legacy"]["exception"]["type"], "ValueError")
        self.assertEqual([e["status"] for e in rows["negative-attempt"]["legacy"]["result"]["events"]], ["negative-rejected"] * 2)
        annotations = json.loads((out / "annotations.json").read_text())
        tags = {a["tag"] for a in annotations}
        self.assertTrue({"sleep", "retry", "bare-except", "magic-number", "manual-rounding", "feature-flag", "rounding"} <= tags)
        self.assertTrue(all(a["risk"] == "do-not-touch" for a in annotations if a["tag"] in {"sleep", "retry"}))
        rules = json.loads((out / "invariants.json").read_text())
        self.assertEqual(len(rules), 4)
        self.assertTrue(all(r["status"] == "candidate-fixture-checked" for r in rules))
        tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(out / "tests")], capture_output=True, text=True)
        self.assertEqual(tests.returncode, 0, tests.stderr)

    def test_modern_mutation_fails_comparison_and_baseline(self):
        sample = self.sample()
        out = self.root / "pack"
        extract(sample / "legacy", out)
        modern = sample / "modern/batch.py"
        modern.write_text(modern.read_text().replace("sleep(50)", "sleep(49)"))
        tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(out / "tests")], capture_output=True, text=True)
        self.assertNotEqual(tests.returncode, 0)
        self.assertIn("retry-recovers", tests.stderr)
        result = extract(sample / "legacy", self.root / "changed-pack")
        self.assertTrue(result["failed"])
        self.assertIn("MISMATCH", (self.root / "changed-pack/dual_run.md").read_text())

    def test_static_file_set_and_missing_history(self):
        first, second = self.root / "one.py", self.root / "two.py"
        first.write_text("def f(x):\n    balance = x + 42\n    sleep(50)\n    return balance\n")
        second.write_text("def g():\n    return f(1)\n")
        sources = parse(discover([first, second]))
        graph = Graph.build(sources)
        self.assertEqual(len([n for n in graph.nodes if n["kind"] == "function"]), 2)
        self.assertTrue(any(e[1] == "syntactic data dependency" and e[2] == "balance" for e in graph.edges))
        flags = scan(sources)
        self.assertTrue(all(a["git"]["status"] == "unavailable" for a in flags))
        result = extract([first, second], self.root / "pack")
        self.assertEqual(result["fixtures"], 0)
        self.assertIn("NOT RUN", (self.root / "pack/dual_run.md").read_text())
        self.assertEqual(json.loads((self.root / "pack/invariants.json").read_text()), [])

    def test_git_blame_subject_and_uncommitted_line(self):
        repo = self.root / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
        git("init")
        module = repo / "batch.py"
        module.write_text("sleep(50)\n")
        git("add", "batch.py")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Preserve settlement delay")
        ctx = context(module, 1)
        self.assertEqual(ctx["status"], "available")
        self.assertEqual(ctx["subject"], "Preserve settlement delay")
        self.assertEqual(len(ctx["commit"]), 40)
        module.write_text("sleep(51)\n")
        self.assertEqual(context(module, 1)["status"], "uncommitted")

    def test_legacy_only(self):
        sample = self.sample()
        shutil.rmtree(sample / "modern")
        result = extract(sample / "legacy", self.root / "pack")
        self.assertFalse(result["failed"])
        self.assertEqual(result["fixtures"], 10)
        self.assertIn("LEGACY ONLY", (self.root / "pack/dual_run.md").read_text())

    def test_bad_input_fails_before_pack_written(self):
        invalid = self.root / "bad.py"
        invalid.write_text("def broken(:\n")
        with self.assertRaisesRegex(ValueError, "parse error"):
            extract(invalid, self.root / "pack")
        self.assertFalse((self.root / "pack").exists())
        with self.assertRaisesRegex(ValueError, "does not exist"):
            discover(self.root / "absent")
        with self.assertRaisesRegex(ValueError, "No Python"):
            discover([])

    def test_missing_entrypoint_is_harness_failure(self):
        module = self.root / "missing.py"
        module.write_text("x = 1\n")
        with self.assertRaisesRegex(RuntimeError, "must define run"):
            run_case(module, {})

    def test_timeout_is_harness_failure(self):
        module = self.root / "slow.py"
        module.write_text("def run(case, sleep):\n    while True: pass\n")
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            run_case(module, {}, timeout=0.2)

    def test_machine_checks_reject_broken_contracts(self):
        def verdict(op, case, output):
            return check({"machine_check": {"op": op}}, case, {"result": output, "exception": None})
        self.assertEqual(verdict("nonnegative-balance", {"balance": "0"}, {"balance": "-1", "events": []}), "fail")
        self.assertEqual(verdict("half-up-cents", {"operations": [{"amount": "1.005"}]}, {"events": [{"index": 0, "status": "applied", "amount": "1.00"}]}), "fail")
        self.assertEqual(verdict("input-event-order", {"operations": [{}, {}]}, {"events": [{"index": 1}, {"index": 0}]}), "fail")
        self.assertEqual(verdict("reject-negative", {"operations": [{"amount": "-0.001"}]}, {"events": [{"index": 0, "status": "applied"}]}), "fail")
        self.assertEqual(verdict("input-event-order", {"operations": []}, {}), "fail")


if __name__ == "__main__":
    unittest.main()
