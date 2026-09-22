import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from debt_compiler import extract
from debt_compiler.cli import main
from debt_compiler.diff import compare
from debt_compiler.jev import review

ROOT = Path(__file__).resolve().parents[1]


class PackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def checkout(self):
        repo = self.root / "original-repo"
        shutil.copytree(ROOT / "sample", repo / "sample", ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        return repo

    def test_portable_pack_and_relocated_sources(self):
        repo = self.checkout()
        pack = self.root / "original-pack"
        # Record the actual invocation directory even when it isn't the repo root.
        previous = Path.cwd()
        try:
            os.chdir(repo / "sample")
            extract("legacy", pack)
        finally:
            os.chdir(previous)
        manifest = json.loads((pack / "manifest.json").read_text())
        self.assertEqual(manifest["source_files"], ["sample/legacy/batch.py"])
        self.assertEqual(manifest["source_roots"][0]["path"], "..")
        self.assertEqual((pack / manifest["default_source_root"]).resolve(), repo / "sample")
        self.assertEqual(manifest["exit_classification"], "match")
        self.assertIsNone(manifest["source_roots"][0]["git_head"])
        for name in ("graph.md", "annotations.json", "invariants.json", "manifest.json", "dual_run.md", "risk.md", "tests/observations.json", "tests/test_characterization.py"):
            self.assertNotIn(str(self.root), (pack / name).read_text(), name)
        # Defaults work from an unrelated current directory before relocation.
        default_run = subprocess.run([sys.executable, str(pack / "tests/test_characterization.py")], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(default_run.returncode, 0, default_run.stderr)
        moved_repo = self.root / "relocated" / "repo"
        moved_repo.parent.mkdir()
        shutil.move(repo, moved_repo)
        moved_pack = self.root / "relocated" / "pack"
        shutil.move(pack, moved_pack)
        replay = subprocess.run([sys.executable, str(moved_pack / "tests/test_characterization.py"), "--source-root", str(moved_repo), "-v"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertFalse(repo.exists())
        fresh = self.root / "fresh-pack"
        extract(moved_repo / "sample/legacy", fresh)
        self.assertFalse(compare(moved_pack, fresh)[0])

    def test_diff_same_extract_and_each_contract_category(self):
        old, new = self.root / "old", self.root / "new"
        extract(ROOT / "sample/legacy", old)
        extract(ROOT / "sample/legacy", new)
        with patch("builtins.print") as printed:
            self.assertEqual(main(["diff", str(old), str(new)]), 0)
        self.assertIn("Identical-enough", printed.call_args.args[0])
        # Locator/provenance metadata, including old absolute defaults, is ignored.
        manifest = json.loads((new / "manifest.json").read_text())
        manifest.update(extract_time="tomorrow", default_source_root="/a/different/machine")
        (new / "manifest.json").write_text(json.dumps(manifest))
        self.assertFalse(compare(old, new)[0])
        edits = [
            ("annotations.json", lambda data: data[0].update(line=data[0]["line"] + 1), "annotations"),
            ("invariants.json", lambda data: data[0].update(english="A different contract"), "invariants"),
            ("tests/observations.json", lambda data: data["rows"][0].update(equal=False), "dual-run cases"),
            ("tests/observations.json", lambda data: data["rows"][0].update(name="new-case-name"), "dual-run cases"),
        ]
        for filename, edit, category in edits:
            with self.subTest(category=category, filename=filename):
                original = (new / filename).read_text()
                data = json.loads(original)
                edit(data)
                (new / filename).write_text(json.dumps(data))
                changed, report = compare(old, new)
                self.assertTrue(changed)
                self.assertIn(category + ": +", report)
                with patch("builtins.print"):
                    self.assertEqual(main(["diff", str(old), str(new)]), 1)
                (new / filename).write_text(original)
        with (new / "risk.md").open("a") as handle:
            handle.write("\n- A newly discovered operational risk.\n")
        self.assertTrue(compare(old, new)[0])
        self.assertIn("risk buckets: +0 -0 changed 1", compare(old, new)[1])
        with patch("builtins.print"):
            self.assertEqual(main(["diff", str(old), str(self.root / "missing")]), 2)

    def test_no_convention_is_not_run_without_import(self):
        repo = self.checkout()
        module = repo / "sample/legacy/batch.py"
        module.write_text('raise RuntimeError("must never import this module")\ndef unrelated():\n    return 42\n')
        pack = self.root / "static-pack"
        result = extract(module, pack)
        self.assertEqual(result["fixtures"], 0)
        self.assertFalse(result["failed"])
        self.assertIn("NOT RUN", (pack / "dual_run.md").read_text())
        manifest = json.loads((pack / "manifest.json").read_text())
        self.assertEqual(manifest["exit_classification"], "runtime-skipped")
        self.assertEqual(manifest["exit_code"], 0)
        tests = subprocess.run([sys.executable, str(pack / "tests/test_characterization.py"), "-v"], capture_output=True, text=True)
        self.assertEqual(tests.returncode, 0, tests.stderr)
        self.assertIn("NOT RUN", tests.stderr)
        self.assertIn("skipped=1", tests.stderr)
        for name in ("graph.md", "annotations.json", "risk.md"):
            self.assertTrue((pack / name).is_file())

    def test_jev_review_is_advisory_and_leaves_the_contract_pack_unchanged(self):
        pack = self.root / "pack"
        extract(ROOT / "sample/legacy", pack)
        original_risk = (pack / "risk.md").read_text()
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({
                    "model": "jev-1.13.0",
                    "answers": {
                        "claim_0": {
                            "type": "choice",
                            "choice": "supported",
                            "confidence": 0.91,
                            "probabilities": {"supported": 0.91, "insufficient": 0.08, "contradicted": 0.01},
                        }
                    },
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                }).encode()

        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            return Response()

        with patch("debt_compiler.jev.urlopen", fake_urlopen):
            result = review(pack, api_key="test-key", timeout=3)
        self.assertEqual(result["model"], "jev-1.13.0")
        self.assertEqual(result["claims"][0]["review"]["verdict"], "supported")
        self.assertEqual(result["claims"][1]["review"]["verdict"], "not-returned")
        self.assertIn("advisory", result["limitations"][0])
        self.assertEqual((pack / "risk.md").read_text(), original_risk)
        self.assertEqual(calls[0][1], 3)
        payload = json.loads(calls[0][0].data)
        self.assertEqual(payload["model"], "jev-latest")
        self.assertIn("claim_0", payload["questions"])
        self.assertNotIn("claims", payload["state"])
        self.assertNotIn("test-key", calls[0][0].full_url)

    def test_jev_review_cli_writes_a_separate_artifact(self):
        pack = self.root / "pack"
        extract(ROOT / "sample/legacy", pack)
        reviewed = {
            "format_version": 1,
            "model": "jev-1.13.0",
            "claims": [],
            "limitations": ["Jev output is advisory."],
        }
        with patch("debt_compiler.cli.jev_review", return_value=reviewed):
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
                with patch("builtins.print"):
                    self.assertEqual(main(["jev-review", str(pack)]), 0)
        artifact = json.loads((pack / "jev_review.json").read_text())
        self.assertEqual(artifact["model"], "jev-1.13.0")

    def test_jev_review_cli_replaces_an_output_symlink_without_touching_its_target(self):
        pack = self.root / "pack"
        extract(ROOT / "sample/legacy", pack)
        target = self.root / "target.json"
        target.write_text('{"model": "do-not-overwrite"}\n')
        (pack / "jev_review.json").symlink_to(target)
        reviewed = {"format_version": 1, "model": "jev-1.13.0", "claims": [], "limitations": []}
        with patch("debt_compiler.cli.jev_review", return_value=reviewed):
            with patch("builtins.print"):
                self.assertEqual(main(["jev-review", str(pack)]), 0)
        self.assertEqual(target.read_text(), '{"model": "do-not-overwrite"}\n')
        self.assertFalse((pack / "jev_review.json").is_symlink())

    def test_jev_review_rejects_an_empty_claim_answer(self):
        pack = self.root / "pack"
        extract(ROOT / "sample/legacy", pack)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"answers": {"claim_0": {}}}'

        with patch("debt_compiler.jev.urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "unknown review verdict"):
                review(pack, api_key="test-key")

    def test_extract_removes_a_stale_jev_review(self):
        pack = self.root / "pack"
        extract(ROOT / "sample/legacy", pack)
        (pack / "jev_review.json").write_text('{"model": "stale"}\n')
        extract(ROOT / "sample/legacy", pack)
        self.assertFalse((pack / "jev_review.json").exists())

    def test_jev_review_rejects_an_invalid_response(self):
        pack = self.root / "pack"
        extract(ROOT / "sample/legacy", pack)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"answers": {"claim_0": {"choice": "unknown"}}}'

        with patch("debt_compiler.jev.urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "unknown review verdict"):
                review(pack, api_key="test-key")

    def test_rounding_mutant_only_fails_new_boundary_case(self):
        repo = self.checkout()
        modern = repo / "sample/modern/batch.py"
        modern.write_text(modern.read_text().replace("import Decimal, ROUND_HALF_UP", "import Decimal, ROUND_HALF_UP, ROUND_DOWN").replace("rounding=ROUND_HALF_UP", 'rounding=(ROUND_DOWN if raw == Decimal("2.675") else ROUND_HALF_UP)'))
        first = self.root / "ten-cases"
        result = extract(repo / "sample/legacy", first)
        self.assertEqual(result["fixtures"], 10)
        self.assertFalse(result["failed"], "Old ten must not expose this targeted mutant")
        fixture_file = repo / "sample/fixtures/cases.json"
        cases = json.loads(fixture_file.read_text())
        cases.append({"name": "new-rounding-boundary", "input": {"balance": "0", "operations": [{"kind": "credit", "amount": "2.675"}]}})
        fixture_file.write_text(json.dumps(cases))
        second = self.root / "eleven-cases"
        self.assertTrue(extract(repo / "sample/legacy", second)["failed"])
        observations = json.loads((second / "tests/observations.json").read_text())
        self.assertTrue(all(row["equal"] for row in observations["rows"][:10]))
        boundary = observations["rows"][-1]
        self.assertFalse(boundary["equal"])
        self.assertEqual(boundary["checks"]["half-up-cents"], {"legacy": "pass", "modern": "fail"})
        self.assertEqual(json.loads((second / "manifest.json").read_text())["exit_classification"], "mismatch")
        tests = subprocess.run([sys.executable, str(second / "tests/test_characterization.py")], capture_output=True, text=True)
        self.assertNotEqual(tests.returncode, 0)
        self.assertIn("new-rounding-boundary", tests.stderr)


if __name__ == "__main__":
    unittest.main()
