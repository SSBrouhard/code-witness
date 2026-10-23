import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from debt_compiler import extract
from debt_compiler.cli import main
from debt_compiler.diff import compare

ROOT = Path(__file__).resolve().parents[1]


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        for target in ("legacy", "modern", "fixtures"):
            (self.source / target).mkdir(parents=True)
        self.fixture = self.source / "fixtures/cases.json"
        self.fixture.write_text('[{"name":"case","input":{}}]')
        self.modules('return "A"')

    def modules(self, body):
        for target in ("legacy", "modern"):
            (self.source / target / "batch.py").write_text("def run(case, sleep):\n    " + body + "\n")

    def pack(self, name="pack", **kwargs):
        out = self.root / name
        summary = extract(self.source / "legacy", out, **kwargs)
        return out, summary

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(list(args))
        return code, json.loads(output.getvalue())

    def test_matched_behavior_changes_are_detected_end_to_end(self):
        stamps = {}
        for target in ("legacy", "modern"):
            module = self.source / target / "batch.py"
            py_compile.compile(str(module), doraise=True)
            stamps[target] = module.stat().st_mtime_ns
        old, _ = self.pack("old")
        self.modules('return "B"')
        for target, stamp in stamps.items():
            os.utime(self.source / target / "batch.py", ns=(stamp, stamp))
        new, _ = self.pack("new")
        for pack in (old, new):
            data = json.loads((pack / "tests/observations.json").read_text())
            self.assertTrue(data["rows"][0]["equal"])
        changed, report = compare(old, new)
        self.assertTrue(changed)
        self.assertIn("dual-run cases: +0 -0 changed 1", report)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["diff", str(old), str(new)]), 1)

    def test_every_observable_and_fixture_input_is_compared(self):
        old, _ = self.pack("old")
        new = self.root / "new"
        shutil.copytree(old, new)
        observation = new / "tests/observations.json"
        original = observation.read_text()
        changes = {"result": "B", "exception": {"type": "ValueError", "message": "changed"},
                   "stdout": "out", "stderr": "err", "sleeps": [2]}
        for target in ("legacy", "modern"):
            for field, value in changes.items():
                with self.subTest(target=target, field=field):
                    data = json.loads(original)
                    data["rows"][0][target][field] = value
                    observation.write_text(json.dumps(data))
                    self.assertTrue(compare(old, new)[0])
        data = json.loads(original)
        data["rows"][0]["input"] = {"different": True}
        observation.write_text(json.dumps(data))
        self.assertTrue(compare(old, new)[0])

    def test_json_boolean_and_number_are_distinct_behavior(self):
        self.modules("return 1")
        old, _ = self.pack("old")
        self.modules("return True")
        new, _ = self.pack("new")
        self.assertTrue(compare(old, new)[0])
        (self.source / "legacy/batch.py").write_text("def run(case, sleep):\n    return 1\n")
        _, summary = self.pack("mixed")
        self.assertEqual(summary["verification"]["status"], "FAIL")
        replay = subprocess.run([sys.executable, str(old / "tests/test_characterization.py")], capture_output=True, text=True)
        self.assertNotEqual(replay.returncode, 0)

    def test_statuses_and_required_runtime_cli_and_library(self):
        pack, summary = self.pack(require_runtime=True)
        self.assertEqual(summary["verification"]["status"], "PASS")
        self.assertEqual(summary["exit_code"], 0)
        manifest = json.loads((pack / "manifest.json").read_text())
        self.assertEqual(manifest["verification"], summary["verification"])
        self.assertIn("Verification: PASS", (pack / "dual_run.md").read_text())
        (self.source / "modern/batch.py").unlink()
        _, summary = self.pack("legacy-only")
        self.assertEqual(summary["verification"]["status"], "INCONCLUSIVE")
        self.assertEqual(summary["exit_code"], 0)
        code, summary = self.cli("extract", str(self.source / "legacy"), "--out", str(self.root / "required"), "--require-runtime")
        self.assertEqual(code, 2)
        self.assertEqual(summary["verification"]["status"], "BLOCKED")
        replay = subprocess.run([sys.executable, str(self.root / "required/tests/test_characterization.py")], capture_output=True, text=True)
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("required runtime", replay.stderr.lower())

    def test_static_only_and_generated_coverage_reporting(self):
        self.fixture.unlink()
        pack, summary = self.pack()
        self.assertEqual(summary["verification"]["status"], "INCONCLUSIVE")
        self.assertEqual(summary["exit_code"], 0)
        replay = subprocess.run([sys.executable, str(pack / "tests/test_characterization.py"), "-v"], capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertIn("NOT RUN", replay.stderr)
        self.assertIn("skipped=1", replay.stderr)
        required = subprocess.run([sys.executable, str(pack / "tests/test_characterization.py"), "--require-runtime"], capture_output=True, text=True)
        self.assertNotEqual(required.returncode, 0)
        code, summary = self.cli("extract", str(self.source / "legacy"), "--out", str(self.root / "required"), "--require-runtime")
        self.assertEqual(code, 2)
        self.assertEqual(summary["verification"]["status"], "BLOCKED")

    def test_unexercised_candidate_checks_are_inconclusive(self):
        shutil.rmtree(self.source)
        shutil.copytree(ROOT / "sample", self.source)
        cases = json.loads(self.fixture.read_text())
        self.fixture.write_text(json.dumps([cases[-1]]))
        _, summary = self.pack(require_runtime=True)
        self.assertEqual(summary["verification"]["status"], "INCONCLUSIVE")
        self.assertEqual(summary["exit_code"], 2)
        pack, _ = self.pack("optional")
        replay = subprocess.run([sys.executable, str(pack / "tests/test_characterization.py"), "-v"], capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertIn("INCONCLUSIVE", replay.stderr)
        self.assertIn("skipped=1", replay.stderr)

    def test_required_replay_rechecks_stored_coverage(self):
        pack, _ = self.pack(require_runtime=True)
        path = pack / "tests/observations.json"
        data = json.loads(path.read_text())
        data["rows"] = []
        path.write_text(json.dumps(data))
        replay = subprocess.run([sys.executable, str(pack / "tests/test_characterization.py")], capture_output=True, text=True)
        self.assertNotEqual(replay.returncode, 0)
        self.assertNotIn("skipped=", replay.stderr)

    def test_mismatch_and_harness_failure_are_distinct(self):
        (self.source / "modern/batch.py").write_text('def run(case, sleep):\n    return "B"\n')
        _, summary = self.pack("mismatch", require_runtime=True)
        self.assertEqual(summary["verification"]["status"], "FAIL")
        self.assertTrue(summary["failed"])
        self.assertEqual(summary["exit_code"], 1)
        (self.source / "modern/batch.py").write_text('raise RuntimeError("cannot import")\n')
        pack, summary = self.pack("blocked")
        self.assertEqual(summary["verification"]["status"], "BLOCKED")
        self.assertEqual(summary["exit_code"], 2)
        self.assertFalse(summary["failed"])
        self.assertTrue((pack / "graph.md").exists())
        self.assertIn("BLOCKED", (pack / "dual_run.md").read_text())
        for name in ("dual_run.md", "manifest.json", "tests/observations.json", "risk.md"):
            self.assertNotIn(str(self.root), (pack / name).read_text())

    def test_replay_keeps_completed_mismatch_when_later_case_is_blocked(self):
        (self.source / "legacy/batch.py").write_text('def run(case, sleep):\n    return case["value"]\n')
        (self.source / "modern/batch.py").write_text('def run(case, sleep):\n    if case.get("blocked"):\n        raise SystemExit("blocked")\n    return case["value"] + 1\n')
        self.fixture.write_text('[{"name":"first","input":{"value":1}}, {"name":"blocked","input":{"value":2,"blocked":true}}]')
        pack, summary = self.pack()
        self.assertEqual(summary["verification"]["status"], "FAIL")
        self.assertEqual(summary["exit_code"], 1)
        replay = subprocess.run([sys.executable, str(pack / "tests/test_characterization.py"), "-v"], capture_output=True, text=True)
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("first", replay.stderr)

    def test_fingerprints_include_modern_fixtures_and_dirty_sources(self):
        subprocess.run(["git", "init", str(self.source)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline"], check=True, capture_output=True)
        old, _ = self.pack("old")
        before = json.loads((old / "manifest.json").read_text())
        self.assertEqual(before["format_version"], 2)
        self.assertFalse(before["source_roots"][0]["git_dirty"])
        self.assertEqual(before["fingerprints"]["sources"]["legacy/batch.py"], hashlib.sha256((self.source / "legacy/batch.py").read_bytes()).hexdigest())
        self.assertIn("modern/batch.py", before["fingerprints"]["sources"])
        self.assertIn("fixtures/cases.json", before["fingerprints"]["fixtures"])
        self.assertIn("python_version", before["environment"])
        self.assertIn("tree-sitter", before["environment"]["dependencies"])
        for target in ("legacy", "modern"):
            with (self.source / target / "batch.py").open("a") as handle:
                handle.write("# uncommitted change\n")
        changed_source, _ = self.pack("changed-source")
        middle = json.loads((changed_source / "manifest.json").read_text())
        self.assertNotEqual(before["fingerprints"]["sources"], middle["fingerprints"]["sources"])
        self.assertEqual(before["fingerprints"]["fixtures"], middle["fingerprints"]["fixtures"])
        self.fixture.write_text('[{"name":"case","input":{"extra":true}}]')
        new, _ = self.pack("new")
        after = json.loads((new / "manifest.json").read_text())
        self.assertEqual(before["source_roots"][0]["git_head"], after["source_roots"][0]["git_head"])
        self.assertTrue(after["source_roots"][0]["git_dirty"])
        for category in ("sources", "fixtures"):
            self.assertNotEqual(before["fingerprints"][category], after["fingerprints"][category])
        self.assertEqual(middle["fingerprints"]["sources"], after["fingerprints"]["sources"])
        self.assertNotIn(str(self.root), json.dumps(after))

    def test_changing_evidence_during_execution_cannot_pass(self):
        from debt_compiler.harness import run_case
        def mutating_run(*args, **kwargs):
            result = run_case(*args, **kwargs)
            self.fixture.write_text('[{"name":"changed","input":{}}]')
            return result
        with patch("debt_compiler.harness.run_case", side_effect=mutating_run):
            _, summary = self.pack(require_runtime=True)
        self.assertEqual(summary["verification"]["status"], "BLOCKED")
        self.assertEqual(summary["exit_code"], 2)

    def test_static_evidence_mutation_writes_blocked_manifest(self):
        from debt_compiler.harness import execute
        self.fixture.unlink()
        def mutating_execute(*args):
            result = execute(*args)
            with (self.source / "legacy/batch.py").open("a") as handle:
                handle.write("# changed during static extraction\n")
            return result
        with patch("debt_compiler.cli.execute", side_effect=mutating_execute):
            pack, summary = self.pack()
        self.assertEqual(summary["verification"]["status"], "BLOCKED")
        self.assertEqual(json.loads((pack / "manifest.json").read_text())["exit_code"], 2)

    def test_v1_compatibility_and_unsupported_format(self):
        old, _ = self.pack("old")
        new = self.root / "new"
        shutil.copytree(old, new)
        manifest_file = new / "manifest.json"
        manifest = json.loads(manifest_file.read_text())
        manifest["format_version"] = 1
        manifest_file.write_text(json.dumps(manifest))
        self.assertFalse(compare(old, new)[0])
        manifest["format_version"] = 999
        manifest_file.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Unsupported pack format"):
            compare(old, new)

    def test_foreign_target_stays_static_and_compares_with_v1(self):
        out = self.root / "foreign"
        with patch("debt_compiler.harness.run_case", side_effect=AssertionError("foreign code must not execute")):
            summary = extract(ROOT / "fixtures/foreign/http.py", out)
        self.assertEqual(summary["verification"]["status"], "INCONCLUSIVE")
        self.assertEqual(summary["fixtures"], 0)
        self.assertFalse(compare(ROOT / "packs/foreign-1", out)[0])


if __name__ == "__main__":
    unittest.main()
